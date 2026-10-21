"""What batteries actually did: energy, cycles, captured prices, FCAS, and the gap to perfect-foresight arbitrage.

Conventions
-----------
* Time is NEM market time (AEST, UTC+10, no daylight saving). Intervals are indexed by their START; AEMO labels them
  by their end (``SETTLEMENTDATE``), so every AEMO timestamp is shifted back by 5 minutes.
* ``DISPATCH_UNIT_SCADA.SCADAVALUE`` is an instantaneous MW reading taken at the start of the dispatch interval
  (it equals ``DISPATCHLOAD.INITIALMW``). Energy in an interval is the integral of the straight line between the
  readings at its start and end, split exactly into its positive (discharge) and negative (charge) parts.
* Bidirectional units report positive MW when discharging and negative MW when charging.
* Energy revenue = regional price (RRP) x net energy; FCAS revenue = enablement MW x regional FCAS price x 5/60 h.
  Marginal loss factors, fees and contracts are ignored.
* Days are calendar days in market time (288 intervals).

Usage:
    from nem_battery.analysis import battery_table, unit_intervals, summarize_unit, benchmark_unit
    units = battery_table(registry, stations)
    iv = unit_intervals(scada, dispatch, prices, "HPR1", "SA1")
    summary = summarize_unit(iv, units.loc["HPR1"])
"""
from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from nem_battery.data import FCAS_SERVICES
from nem_battery.optimizer import DT_5MIN, BatteryParams, optimize_day

STEP = pd.Timedelta("5min")
INTERVALS_PER_DAY = 288
log = logging.getLogger(__name__)


def battery_table(registry: pd.DataFrame, stations: pd.DataFrame | None = None) -> pd.DataFrame:
    """Latest registration of every battery: name, region, discharge/charge MW, storage MWh (indexed by DUID)."""
    last = registry.sort_values(["DUID", "EFFECTIVEDATE", "VERSIONNO"]).groupby("DUID").tail(1).set_index("DUID")
    out = pd.DataFrame({
        "region": last["REGIONID"],
        "discharge_mw": last["MAXCAPACITY"],
        "charge_mw": last["MINCAPACITY"].where(last["MINCAPACITY"] > 0, last["MAXCAPACITY"]),
        "energy_mwh": last["MAXSTORAGECAPACITY"],
    })
    out["name"] = out.index
    if stations is not None:
        names = stations.set_index("STATIONID")["STATIONNAME"].str.strip()
        out["name"] = last["STATIONID"].map(names).fillna(pd.Series(out.index, index=out.index))
    out["duration_h"] = out["energy_mwh"] / out["discharge_mw"]
    return out[["name", "region", "discharge_mw", "charge_mw", "energy_mwh", "duration_h"]]


def params_by_day(registry: pd.DataFrame, duid: str, days: pd.DatetimeIndex) -> pd.DataFrame:
    """Registered MW/MWh valid on each day (registrations change, e.g. when a battery is extended)."""
    reg = registry[registry["DUID"] == duid].sort_values(["EFFECTIVEDATE", "VERSIONNO"])
    reg = reg.groupby("EFFECTIVEDATE").tail(1)
    frame = pd.DataFrame({"day": days})
    m = pd.merge_asof(frame, reg, left_on="day", right_on="EFFECTIVEDATE", direction="backward")
    m = m.bfill()                  # days before the first registration record: use the earliest one
    return pd.DataFrame({
        "discharge_mw": m["MAXCAPACITY"].to_numpy(float),
        "charge_mw": m["MINCAPACITY"].where(m["MINCAPACITY"] > 0, m["MAXCAPACITY"]).to_numpy(float),
        "energy_mwh": m["MAXSTORAGECAPACITY"].to_numpy(float),
    }, index=days)


def split_energy(a: np.ndarray, b: np.ndarray, dt: float = DT_5MIN) -> tuple[np.ndarray, np.ndarray]:
    """Discharged and charged MWh of a straight-line MW trajectory from ``a`` to ``b`` over ``dt`` hours."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    hi, lo = np.maximum(a, b), np.minimum(a, b)
    width = np.where(hi > lo, hi - lo, 1.0)
    pos = np.where(lo >= 0, (a + b) / 2, np.where(hi <= 0, 0.0, hi ** 2 / (2 * width)))
    neg = np.where(hi <= 0, -(a + b) / 2, np.where(lo >= 0, 0.0, lo ** 2 / (2 * width)))
    return pos * dt, neg * dt


def unit_intervals(scada: pd.DataFrame, dispatch: pd.DataFrame | None, prices: pd.DataFrame,
                   duid: str, region: str) -> pd.DataFrame:
    """5-minute frame for one battery, indexed by interval start (market time).

    Columns: mw (reading at start), discharge_mwh, charge_mwh, net_mwh, rrp, energy_aud, soc_mwh (stored energy at
    start, if reported), availability (MW offered to the market; caps the dispatch target), fcas_aud,
    fcas_<service>_aud, enable_<service> (MW), and ``valid`` (both SCADA readings and the price exist).
    """
    pr = prices[prices["REGIONID"] == region].drop_duplicates("SETTLEMENTDATE")
    pr = pr.set_index(pr["SETTLEMENTDATE"] - STEP).sort_index()
    idx = pd.DatetimeIndex(pr.index, name="interval_start")
    sc = scada[scada["DUID"] == duid]
    inst = pd.Series(sc["SCADAVALUE"].to_numpy(float), index=sc["SETTLEMENTDATE"] - STEP).sort_index()
    inst = inst[~inst.index.duplicated()]
    a = inst.reindex(idx).to_numpy()
    b = inst.reindex(idx + STEP).to_numpy()
    b = np.where(np.isnan(b), a, b)            # last reading of the series: hold the value
    dis, chg = split_energy(np.nan_to_num(a), np.nan_to_num(b))
    out = pd.DataFrame({"mw": a, "discharge_mwh": dis, "charge_mwh": chg}, index=idx)
    out["net_mwh"] = out["discharge_mwh"] - out["charge_mwh"]
    out["rrp"] = pr["RRP"].to_numpy(float)
    out["valid"] = ~np.isnan(a) & ~np.isnan(out["rrp"].to_numpy())
    out.loc[~out["valid"], ["discharge_mwh", "charge_mwh", "net_mwh"]] = 0.0
    out["energy_aud"] = out["rrp"].fillna(0) * out["net_mwh"]

    out["soc_mwh"] = np.nan
    out["availability"] = np.nan
    out["fcas_aud"] = 0.0
    if dispatch is not None:
        d = dispatch[dispatch["DUID"] == duid].sort_values("INTERVENTION").drop_duplicates("SETTLEMENTDATE", keep="last")
        d = d.set_index(d["SETTLEMENTDATE"] - STEP).reindex(idx)
        if "INITIAL_ENERGY_STORAGE" in d:
            out["soc_mwh"] = d["INITIAL_ENERGY_STORAGE"].to_numpy(float)
        if "AVAILABILITY" in d:
            out["availability"] = d["AVAILABILITY"].to_numpy(float)
        for s in FCAS_SERVICES:
            if s not in d or f"{s}RRP" not in pr:
                continue
            enable = d[s].fillna(0).to_numpy(float)
            out[f"enable_{s}"] = enable
            out[f"fcas_{s}_aud"] = enable * pr[f"{s}RRP"].fillna(0).to_numpy(float) * DT_5MIN
            out["fcas_aud"] += out[f"fcas_{s}_aud"]
    return out


def price_percentile_by_day(iv: pd.DataFrame) -> pd.Series:
    """Rank of each interval's price within its day, 0 (cheapest) .. 1 (most expensive)."""
    return iv["rrp"].groupby(iv.index.normalize()).rank(pct=True)


def summarize_unit(iv: pd.DataFrame, unit: pd.Series, params: pd.DataFrame | None = None, top_days: int = 10) -> dict:
    """Headline numbers for one battery over the whole frame.

    ``params`` (from :func:`params_by_day`) makes cycles and per-MW figures use the registration valid on each day;
    without it the latest registration in ``unit`` is used throughout.
    """
    day = iv.index.normalize()
    days = day.nunique()
    valid_share = float(iv["valid"].mean())
    dis, chg = iv["discharge_mwh"].sum(), iv["charge_mwh"].sum()
    energy = iv["energy_aud"].sum()
    fcas = iv["fcas_aud"].sum()
    pct = price_percentile_by_day(iv)
    daily_energy = iv["energy_aud"].groupby(day).sum()
    if params is None:
        mw, cycles = unit["discharge_mw"], dis / unit["energy_mwh"] / days
    else:
        p = params.reindex(daily_energy.index)
        mw = p["discharge_mw"].mean()
        cycles = (iv["discharge_mwh"].groupby(day).sum() / p["energy_mwh"]).mean()
    year = 365 / days
    out = {
        "duid": unit.name, "name": unit["name"], "region": unit["region"],
        "discharge_mw": unit["discharge_mw"], "energy_mwh": unit["energy_mwh"], "duration_h": unit["duration_h"],
        "days": days, "valid_share": valid_share,
        "discharged_mwh": dis, "charged_mwh": chg, "observed_rte": dis / chg if chg else np.nan,
        "cycles_per_day": cycles,
        "energy_revenue_aud": energy, "fcas_revenue_aud": fcas,
        "energy_k_aud_per_mw_yr": energy / mw * year / 1e3,
        "fcas_k_aud_per_mw_yr": fcas / mw * year / 1e3,
        "fcas_share": fcas / (energy + fcas) if energy + fcas > 0 else np.nan,
        "avg_sell_price": (iv["rrp"] * iv["discharge_mwh"]).sum() / dis if dis else np.nan,
        "avg_buy_price": (iv["rrp"] * iv["charge_mwh"]).sum() / chg if chg else np.nan,
        "avg_region_price": iv["rrp"].mean(),
        "sell_price_pct": (pct * iv["discharge_mwh"]).sum() / dis if dis else np.nan,
        "buy_price_pct": (pct * iv["charge_mwh"]).sum() / chg if chg else np.nan,
        f"top{top_days}_days_share": daily_energy.nlargest(top_days).sum() / energy if energy > 0 else np.nan,
    }
    out["captured_spread"] = out["avg_sell_price"] - out["avg_buy_price"]
    for s in FCAS_SERVICES:
        if f"enable_{s}" in iv:
            out[f"avg_enable_{s}"] = iv[f"enable_{s}"].mean()
    return out


def daily_frame(iv: pd.DataFrame) -> pd.DataFrame:
    """Per-day actuals, including the reported stored energy at the start of the day and the largest MW offered."""
    g = iv.groupby(iv.index.normalize())
    daily = pd.DataFrame({
        "discharged_mwh": g["discharge_mwh"].sum(),
        "charged_mwh": g["charge_mwh"].sum(),
        "energy_aud": g["energy_aud"].sum(),
        "fcas_aud": g["fcas_aud"].sum(),
        "valid_share": g["valid"].mean(),
        "n_intervals": g.size(),
        "soc_start": g["soc_mwh"].first(),
        "max_availability": g["availability"].max(),
    })
    daily.index.name = "day"
    return daily


def benchmark_unit(iv: pd.DataFrame, params: pd.DataFrame, rte: float = 0.85, degradation_cost: float = 10.0,
                   max_cycles: float | None = None, min_valid: float = 0.95) -> pd.DataFrame:
    """Perfect-foresight benchmarks for every complete day, next to the actuals.

    * ``opt_registered`` - registered MW and MWh, up to ``max_cycles`` full cycles per day (None = unlimited); every
      MWh discharged costs ``degradation_cost``.
    * ``opt`` - the same battery shrunk to the capacity the operator offered to the market that day: MW and MWh are
      scaled by (largest AVAILABILITY of the day / registered MW), capped at 1. Capacity held back for network
      contracts (SIPS), outages or commissioning is thereby excluded.
    * ``same_volume`` - as ``opt``, but discharging no more energy than the battery really did that day: what
      perfect timing alone would have earned.
    Each day starts at a stored energy the optimiser picks and must end with at least as much, so no energy is
    borrowed from the next day. (The reported state of charge is not used: for several units it is zero or above the
    registered capacity for long stretches.) Days with less than ``min_valid`` of SCADA/price coverage are skipped.
    """
    daily = daily_frame(iv)
    rows = []
    for day, act in daily.iterrows():
        if act["n_intervals"] != INTERVALS_PER_DAY or act["valid_share"] < min_valid:
            continue
        prm = params.loc[day]
        offered = act["max_availability"] / prm["discharge_mw"] if np.isfinite(act["max_availability"]) else 1.0
        offered = float(np.clip(offered, 0.0, 1.0))
        prices = iv.loc[day:day + pd.Timedelta(hours=23, minutes=55), "rrp"].to_numpy()
        row = {"day": day, **act.to_dict(), "offered_share": offered, "same_volume_aud": 0.0, "binaries": 0}
        for key, scale in (("opt_registered", 1.0), ("opt", offered)):
            row[f"{key}_aud"] = row[f"{key}_discharged_mwh"] = 0.0
            if scale <= 0:
                continue
            bp = BatteryParams(discharge_mw=scale * prm["discharge_mw"], charge_mw=scale * prm["charge_mw"],
                               energy_mwh=scale * prm["energy_mwh"], round_trip_efficiency=rte,
                               degradation_cost=degradation_cost)
            cap = max_cycles * bp.energy_mwh if max_cycles else None
            res = optimize_day(prices, bp, max_discharge_mwh=cap)
            row[f"{key}_aud"], row[f"{key}_discharged_mwh"] = res.gross_revenue, res.discharged_mwh
            row["binaries"] += res.binaries
            if key == "opt":
                same = optimize_day(prices, bp, max_discharge_mwh=act["discharged_mwh"])
                row["same_volume_aud"] = same.gross_revenue
                row["binaries"] += same.binaries
        rows.append(row)
    return pd.DataFrame(rows).set_index("day")


def summarize_benchmark(bench: pd.DataFrame, params: pd.DataFrame) -> dict:
    """Actual vs perfect-foresight energy revenue over the benchmarked days, per MW-year and as ratios."""
    mw = params["discharge_mw"].reindex(bench.index).mean()
    year = 365 / len(bench) / mw / 1e3
    actual, opt, same = bench["energy_aud"].sum(), bench["opt_aud"].sum(), bench["same_volume_aud"].sum()
    reg = bench["opt_registered_aud"].sum()
    return {
        "bench_days": len(bench),
        "offered_share": bench["offered_share"].mean(),
        "bench_actual_k_aud_per_mw_yr": actual * year,
        "bench_fcas_k_aud_per_mw_yr": bench["fcas_aud"].sum() * year,
        "same_volume_k_aud_per_mw_yr": same * year,
        "opt_k_aud_per_mw_yr": opt * year,
        "opt_registered_k_aud_per_mw_yr": reg * year,
        "opt_registered_cycles_per_day": (bench["opt_registered_discharged_mwh"]
                                          / params["energy_mwh"].reindex(bench.index)).mean(),
        "capture_ratio": actual / opt if opt > 0 else np.nan,         # vs. capacity offered to the market
        "capture_registered": actual / reg if reg > 0 else np.nan,    # vs. registered capacity
        "timing_ratio": actual / same if same > 0 else np.nan,
        "volume_ratio": same / opt if opt > 0 else np.nan,
        "offered_ratio": opt / reg if reg > 0 else np.nan,
    }


def hourly_profile(iv: pd.DataFrame) -> pd.DataFrame:
    """Average net output (MW, + discharge / - charge) and average price by hour of day."""
    hour = iv.index.hour
    return pd.DataFrame({
        "net_mw": (iv["net_mwh"] / DT_5MIN).groupby(hour).mean(),
        "rrp": iv["rrp"].groupby(hour).mean(),
    }).rename_axis("hour")
