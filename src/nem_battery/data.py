"""Local cache of the NEM data used in this project, built from AEMO's monthly MMSDM archive on NEMWeb.

For every month the raw zips are downloaded, filtered to what the analysis needs and stored as small Parquet files;
the zips are deleted afterwards unless ``keep_raw`` is set. Only bidirectional units (grid-scale batteries registered
as a single "BDU" since the 2024 integrated-storage rule change) are kept.

    data/registry.parquet          battery registration history: region, MW, MWh, efficiencies  (DUDETAIL, DUDETAILSUMMARY)
    data/stations.parquet          station names                                                (STATION)
    data/scada/2025-10.parquet     SCADA MW of every battery, 5-min                             (DISPATCH_UNIT_SCADA)
    data/price/2025-10.parquet     regional energy and FCAS prices, 5-min                       (DISPATCHPRICE)
    data/dispatch/2025-10.parquet  dispatch targets and FCAS enablement of every battery, 5-min  (DISPATCHLOAD)

All timestamps are AEMO ``SETTLEMENTDATE`` values: interval END in NEM market time (AEST, UTC+10, no daylight saving).

Usage:
    from nem_battery.data import fetch, load
    fetch(date(2025, 10, 1), date(2026, 9, 30), Path("data"))
    scada = load("scada", Path("data"))
"""
from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pandas as pd
import requests

from nem_battery.mms import USER_AGENT, download_table, read_mms_zip, to_timestamp

log = logging.getLogger(__name__)

FCAS_SERVICES = ["RAISE1SEC", "RAISE6SEC", "RAISE60SEC", "RAISE5MIN", "RAISEREG",
                 "LOWER1SEC", "LOWER6SEC", "LOWER60SEC", "LOWER5MIN", "LOWERREG"]

# table -> (MMSDM table name, columns to keep, numeric columns)
MONTHLY = {
    "scada": ("DISPATCH_UNIT_SCADA", ["SETTLEMENTDATE", "DUID", "SCADAVALUE"], ["SCADAVALUE"]),
    "price": ("DISPATCHPRICE",
              ["SETTLEMENTDATE", "REGIONID", "INTERVENTION", "RRP"] + [f"{s}RRP" for s in FCAS_SERVICES],
              ["RRP"] + [f"{s}RRP" for s in FCAS_SERVICES]),
    "dispatch": ("DISPATCHLOAD",
                 ["SETTLEMENTDATE", "DUID", "INTERVENTION", "DISPATCHMODE", "INITIALMW", "TOTALCLEARED",
                  "AVAILABILITY", "INITIAL_ENERGY_STORAGE", "ENERGY_STORAGE"] + FCAS_SERVICES,
                 ["INITIALMW", "TOTALCLEARED", "AVAILABILITY", "INITIAL_ENERGY_STORAGE", "ENERGY_STORAGE"]
                 + FCAS_SERVICES),
}
REGISTRY_COLS = ["DUID", "EFFECTIVEDATE", "VERSIONNO", "REGISTEREDCAPACITY", "MAXCAPACITY", "MINCAPACITY",
                 "MAXSTORAGECAPACITY", "STORAGEIMPORTEFFICIENCYFACTOR", "STORAGEEXPORTEFFICIENCYFACTOR"]
SUMMARY_COLS = ["DUID", "START_DATE", "END_DATE", "DISPATCHTYPE", "REGIONID", "STATIONID", "PARTICIPANTID"]


def months(start: date, end: date) -> list[tuple[int, int]]:
    """(year, month) pairs covering start..end inclusive."""
    return [(p.year, p.month) for p in pd.period_range(start, end, freq="M")]


def _read_parts(paths: list[Path], columns: list[str], where=None) -> pd.DataFrame:
    return pd.concat([read_mms_zip(p, columns, where) for p in paths], ignore_index=True)


def _cleanup(paths: list[Path], keep_raw: bool):
    if not keep_raw:
        for p in paths:
            p.unlink(missing_ok=True)


def build_registry(year: int, month: int, data_dir: Path, session: requests.Session, keep_raw: bool = False) -> pd.DataFrame:
    """Registration history of all bidirectional units, as published in the MMSDM archive of ``year-month``."""
    raw = data_dir / "raw"
    sp = download_table("DUDETAILSUMMARY", year, month, raw, session)
    summary = _read_parts(sp, SUMMARY_COLS)
    dp = download_table("DUDETAIL", year, month, raw, session)
    detail = _read_parts(dp, REGISTRY_COLS)
    bdu = set(summary.loc[summary["DISPATCHTYPE"] == "BIDIRECTIONAL", "DUID"])
    latest = (summary[summary["DUID"].isin(bdu)].sort_values("START_DATE").groupby("DUID").tail(1)
              [["DUID", "REGIONID", "STATIONID", "PARTICIPANTID"]])
    reg = detail[detail["DUID"].isin(bdu)].merge(latest, on="DUID", how="left")
    reg["EFFECTIVEDATE"] = to_timestamp(reg["EFFECTIVEDATE"])
    for c in REGISTRY_COLS[2:]:
        reg[c] = pd.to_numeric(reg[c], errors="coerce")
    reg = reg.sort_values(["DUID", "EFFECTIVEDATE", "VERSIONNO"]).reset_index(drop=True)
    reg.to_parquet(data_dir / "registry.parquet", index=False)
    _cleanup(sp + dp, keep_raw)

    try:
        st = download_table("STATION", year, month, raw, session)
        stations = _read_parts(st, ["STATIONID", "STATIONNAME", "STATE", "LASTCHANGED"])
        stations = stations.sort_values("LASTCHANGED").groupby("STATIONID").tail(1).drop(columns="LASTCHANGED")
        stations.to_parquet(data_dir / "stations.parquet", index=False)
        _cleanup(st, keep_raw)
    except FileNotFoundError:
        log.warning("STATION table not found; batteries will be labelled by DUID")
    log.info("registry: %d bidirectional units", reg["DUID"].nunique())
    return reg


def fetch_month(kind: str, year: int, month: int, data_dir: Path, duids: set[str],
                session: requests.Session, refresh: bool = False, keep_raw: bool = False) -> Path:
    """Download, filter and cache one table for one month; returns the Parquet path."""
    table, columns, numeric = MONTHLY[kind]
    out = data_dir / kind / f"{year}-{month:02d}.parquet"
    if out.exists() and not refresh:
        return out
    paths = download_table(table, year, month, data_dir / "raw", session)
    if kind == "price":
        where = {"INTERVENTION": {"0"}}
    else:
        where = {"DUID": duids}
    df = _read_parts(paths, columns, where)
    df["SETTLEMENTDATE"] = to_timestamp(df["SETTLEMENTDATE"])
    for c in numeric:
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("float32")
    for c in ("DUID", "REGIONID"):
        if c in df:
            df[c] = df[c].astype("category")
    if "INTERVENTION" in df:
        df["INTERVENTION"] = pd.to_numeric(df["INTERVENTION"]).astype("int8")
    if "DISPATCHMODE" in df:
        df["DISPATCHMODE"] = pd.to_numeric(df["DISPATCHMODE"], errors="coerce").astype("float32")
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    _cleanup(paths, keep_raw)
    log.info("%s %d-%02d: %d rows -> %s", kind, year, month, len(df), out)
    return out


def fetch(start: date, end: date, data_dir: Path, kinds=("scada", "price", "dispatch"),
          refresh: bool = False, keep_raw: bool = False) -> None:
    """Build the registry (from the last month) and cache every month of the requested tables."""
    data_dir.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    last = months(start, end)[-1]
    if refresh or not (data_dir / "registry.parquet").exists():
        build_registry(*last, data_dir, session, keep_raw)
    duids = set(pd.read_parquet(data_dir / "registry.parquet")["DUID"])
    for y, m in months(start, end):
        for kind in kinds:
            fetch_month(kind, y, m, data_dir, duids, session, refresh, keep_raw)


def load(kind: str, data_dir: Path, start: date | None = None, end: date | None = None,
         columns: list[str] | None = None, duids: list[str] | None = None) -> pd.DataFrame:
    """Concatenate cached monthly Parquet files of one table.

    Optionally only months overlapping start..end, only some columns, and only some units.
    """
    files = sorted((data_dir / kind).glob("*.parquet"))
    if start and end:
        wanted = {f"{y}-{m:02d}" for y, m in months(start, end)}
        files = [f for f in files if f.stem in wanted]
    if not files:
        raise FileNotFoundError(f"no cached {kind} data in {data_dir}; run the 'fetch' command first")
    filters = [("DUID", "in", list(duids))] if duids is not None else None
    df = pd.concat([pd.read_parquet(f, columns=columns, filters=filters) for f in files], ignore_index=True)
    for c in ("DUID", "REGIONID"):
        if c in df:
            df[c] = df[c].astype("category")
    return df
