"""Command line interface.

Usage:
    python -m nem_battery fetch --start 2025-10-01 --end 2026-09-30
    python -m nem_battery run   --start 2025-10-01 --end 2026-09-30 --jobs 8
    python -m nem_battery run   --units HPR1 VBB1 --no-fleet          # quick run on two batteries
    python -m nem_battery run   --help

AEMO data are cached as Parquet in ./data (override with --data-dir or NEM_DATA_DIR); results go to ./results and
charts to ./docs/img. ``run`` downloads whatever is missing first.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

import pandas as pd

from nem_battery import analysis as A
from nem_battery import plots
from nem_battery.data import FCAS_SERVICES, fetch, load
from nem_battery.optimizer import BatteryParams, optimize_day

log = logging.getLogger("nem_battery")

# large batteries analysed in detail: two in NSW, one each in VIC and QLD, two in SA
DEFAULT_UNITS = ["WTAHB1", "ERB01", "VBB1", "TARBESS1", "TIB1", "HPR1"]
IV_COLS = ["rrp", "discharge_mwh", "charge_mwh", "net_mwh", "energy_aud", "fcas_aud", "valid", "soc_mwh",
           "availability"]


def _date(s: str) -> date:
    return date.fromisoformat(s)


def cmd_fetch(args):
    fetch(args.start, args.end, args.data_dir, refresh=args.refresh, keep_raw=args.keep_raw)


def _benchmark_job(duid: str, iv: pd.DataFrame, params: pd.DataFrame, rte: float, deg: float, cycles: float):
    logging.basicConfig(level=logging.WARNING)
    return duid, A.benchmark_unit(iv, params, rte=rte, degradation_cost=deg, max_cycles=cycles)


def cmd_run(args):
    cmd_fetch(args)
    d = args.data_dir
    registry = pd.read_parquet(d / "registry.parquet")
    stations = pd.read_parquet(d / "stations.parquet") if (d / "stations.parquet").exists() else None
    units = A.battery_table(registry, stations)
    t0, t1 = pd.Timestamp(args.start), pd.Timestamp(args.end) + pd.Timedelta(days=1)
    n_days = (t1 - t0).days
    scada = load("scada", d, args.start, args.end)
    prices = load("price", d, args.start, args.end)
    dispatch = load("dispatch", d, args.start, args.end,
                    columns=["SETTLEMENTDATE", "DUID", "INTERVENTION", "AVAILABILITY", "INITIAL_ENERGY_STORAGE"]
                    + FCAS_SERVICES)

    # 5-minute frames for every battery; the fleet is every unit of --min-mw or more with data on (almost) every day
    frames = {}
    for duid, u in units.iterrows():
        if u["discharge_mw"] < args.min_mw and duid not in args.units:
            continue
        iv = A.unit_intervals(scada, dispatch, prices, duid, u["region"]).loc[t0:t1 - pd.Timedelta("1s")]
        full_days = (iv["valid"].groupby(iv.index.normalize()).mean() >= 0.95).sum()
        if duid in args.units or (args.fleet and full_days >= 0.98 * n_days):
            frames[duid] = iv
    missing = [u for u in args.units if u not in frames]
    if missing:
        raise SystemExit(f"no data for {missing}")
    log.info("%d batteries selected (%d analysed in detail)", len(frames), len(args.units))
    del scada, dispatch

    days = pd.date_range(t0, t1 - pd.Timedelta(days=1), freq="D")
    params = {duid: A.params_by_day(registry, duid, days) for duid in frames}
    summaries = {duid: A.summarize_unit(iv, units.loc[duid], params[duid]) for duid, iv in frames.items()}

    # perfect-foresight benchmarks, slowest (most negative prices: SA, VIC) first
    order = sorted(frames, key=lambda x: units.loc[x, "region"] not in ("SA1", "VIC1"))
    benches = {}
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        futures = [pool.submit(_benchmark_job, duid, frames[duid][IV_COLS], params[duid], args.rte, args.deg_cost,
                               args.cycles) for duid in order]
        for f in futures:
            duid, bench = f.result()
            benches[duid] = bench
            summaries[duid].update(A.summarize_benchmark(bench, params[duid]))
            s = summaries[duid]
            log.info("%-9s captured %.0f%% of offered / %.0f%% of registered optimum (timing %.0f%%)", duid,
                     100 * s["capture_ratio"], 100 * s["capture_registered"], 100 * s["timing_ratio"])

    out = args.results_dir
    out.mkdir(parents=True, exist_ok=True)
    fleet = pd.DataFrame(summaries.values()).sort_values("discharge_mw", ascending=False)
    fleet.to_csv(out / "fleet_summary.csv", index=False, float_format="%.4g")
    pd.concat({u: benches[u] for u in args.units}, names=["duid"]).to_csv(out / "daily_selected.csv",
                                                                          float_format="%.6g")
    profiles = pd.concat({u: A.hourly_profile(frames[u]) for u in args.units}, names=["duid"])
    profiles.to_csv(out / "hourly_profiles.csv", float_format="%.4g")
    monthly = pd.concat({u: frames[u][["energy_aud", "fcas_aud", "discharge_mwh"]].resample("MS").sum()
                         for u in args.units}, names=["duid"])
    monthly.to_csv(out / "monthly_selected.csv", float_format="%.6g")

    sel = fleet.set_index("duid").loc[args.units]
    img = args.img_dir
    plots.capture_bars(sel, img / "capture.png")
    plots.daily_profiles(profiles, sel, img / "daily_profiles.png")
    plots.revenue_concentration(benches, sel, img / "revenue_concentration.png")
    if args.fleet:
        plots.fleet_scatter(fleet, args.units, img / "fleet_capture.png")
    ex = args.example or args.units[-1]
    bench = benches[ex]
    best = (bench["opt_aud"] - bench["energy_aud"]).idxmax() if args.example_day is None else pd.Timestamp(args.example_day)
    day_iv = frames[ex].loc[best:best + pd.Timedelta(hours=23, minutes=55)]
    p = params[ex].loc[best] * bench.loc[best, "offered_share"]       # same capacity as the headline benchmark
    bp = BatteryParams(discharge_mw=p["discharge_mw"], charge_mw=p["charge_mw"], energy_mwh=p["energy_mwh"],
                       round_trip_efficiency=args.rte, degradation_cost=args.deg_cost)
    opt = optimize_day(day_iv["rrp"], bp, max_discharge_mwh=args.cycles * p["energy_mwh"] if args.cycles else None)
    title = (f"{units.loc[ex, 'name']}, {best:%d %b %Y}: actual vs. perfect foresight "
             f"({p['discharge_mw']:.0f} MW / {p['energy_mwh']:.0f} MWh offered)")
    plots.example_day(day_iv, opt.schedule, title, img / "example_day.png")

    (out / "run.json").write_text(json.dumps({
        "start": str(args.start), "end": str(args.end), "units": args.units, "fleet_size": len(frames),
        "rte": args.rte, "degradation_cost_aud_per_mwh": args.deg_cost, "max_cycles_per_day": args.cycles,
        "example": {"duid": ex, "day": str(best.date())},
    }, indent=2))
    log.info("results in %s, charts in %s", out, img)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="nem-battery", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("fetch", "run"):
        p = sub.add_parser(name)
        p.add_argument("--start", type=_date, default=date(2025, 10, 1))
        p.add_argument("--end", type=_date, default=date(2026, 9, 30), help="inclusive")
        p.add_argument("--data-dir", type=Path, default=Path(os.environ.get("NEM_DATA_DIR", "data")))
        p.add_argument("--refresh", action="store_true", help="re-download cached months")
        p.add_argument("--keep-raw", action="store_true", help="keep the downloaded AEMO zip files")
        if name == "run":
            p.add_argument("--units", nargs="+", default=DEFAULT_UNITS, help="DUIDs analysed in detail")
            p.add_argument("--no-fleet", dest="fleet", action="store_false",
                           help="skip the other batteries (fleet-wide comparison)")
            p.add_argument("--min-mw", type=float, default=20.0, help="smallest battery in the fleet comparison")
            p.add_argument("--rte", type=float, default=0.85, help="round-trip efficiency of the benchmark")
            p.add_argument("--deg-cost", type=float, default=10.0, help="AUD per MWh discharged in the benchmark")
            p.add_argument("--cycles", type=float, default=1.5, help="max full cycles per day in the benchmark")
            p.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1))
            p.add_argument("--example", help="DUID for the example-day chart (default: last of --units)")
            p.add_argument("--example-day", help="YYYY-MM-DD (default: day with the largest gap to the optimum)")
            p.add_argument("--results-dir", type=Path, default=Path("results"))
            p.add_argument("--img-dir", type=Path, default=Path("docs/img"))
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    {"fetch": cmd_fetch, "run": cmd_run}[args.cmd](args)


if __name__ == "__main__":
    main()
