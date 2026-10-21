import numpy as np
import pandas as pd
import pytest

from nem_battery import analysis as A


def test_split_energy_exact_for_linear_ramps():
    dt = 1.0
    dis, chg = A.split_energy(np.array([10.0, -10.0, 10.0, 0.0]), np.array([20.0, -30.0, -30.0, 0.0]), dt)
    np.testing.assert_allclose(dis, [15.0, 0.0, 10 ** 2 / (2 * 40), 0.0])
    np.testing.assert_allclose(chg, [0.0, 20.0, 30 ** 2 / (2 * 40), 0.0])
    # net energy always equals the trapezoid
    np.testing.assert_allclose(dis - chg, [15.0, -20.0, -10.0, 0.0])


def _synthetic(days=2):
    """Two units in SA1: a battery charging at night and discharging in the evening, at fixed prices."""
    end = pd.date_range("2025-10-01 00:05", periods=288 * days, freq="5min")     # AEMO interval-end labels
    start = end - pd.Timedelta("5min")
    hour = start.hour
    price = np.where((hour >= 10) & (hour < 14), -20.0, np.where(hour >= 18, 300.0, 60.0))
    mw = np.where((hour >= 10) & (hour < 14), -50.0, np.where((hour >= 18) & (hour < 20), 100.0, 0.0))
    # SCADA at label t is the reading at the START of the interval ending t
    scada = pd.DataFrame({"SETTLEMENTDATE": end, "DUID": "BAT1", "SCADAVALUE": mw})
    prices = pd.DataFrame({"SETTLEMENTDATE": end, "REGIONID": "SA1", "RRP": price, "RAISE6SECRRP": 2.0})
    for s in A.FCAS_SERVICES:
        if f"{s}RRP" not in prices:
            prices[f"{s}RRP"] = 0.0
    disp = pd.DataFrame({"SETTLEMENTDATE": end, "DUID": "BAT1", "INTERVENTION": 0,
                         "INITIAL_ENERGY_STORAGE": 100.0, **{s: 0.0 for s in A.FCAS_SERVICES}})
    disp["RAISE6SEC"] = 10.0
    return scada, disp, prices


def test_unit_intervals_energy_revenue_and_fcas():
    scada, disp, prices = _synthetic()
    iv = A.unit_intervals(scada, disp, prices, "BAT1", "SA1")
    assert iv.index[0] == pd.Timestamp("2025-10-01 00:00")
    assert iv["valid"].all()
    day = iv.loc["2025-10-01"]
    # 4 h charging at 50 MW = 200 MWh, 2 h discharging at 100 MW = 200 MWh (ramps at the edges cancel out)
    assert day["charge_mwh"].sum() == pytest.approx(200.0, rel=1e-6)
    assert day["discharge_mwh"].sum() == pytest.approx(200.0, rel=1e-6)
    assert day["energy_aud"].sum() > 0
    # 10 MW of raise-6s at 2 AUD/MW/h for 24 h
    assert day["fcas_aud"].sum() == pytest.approx(10 * 2 * 24, rel=1e-9)


def test_summary_and_profile():
    scada, disp, prices = _synthetic()
    iv = A.unit_intervals(scada, disp, prices, "BAT1", "SA1")
    unit = pd.Series({"name": "Battery 1", "region": "SA1", "discharge_mw": 100.0, "charge_mw": 100.0,
                      "energy_mwh": 200.0, "duration_h": 2.0}, name="BAT1")
    s = A.summarize_unit(iv, unit)
    assert s["days"] == 2
    assert s["cycles_per_day"] == pytest.approx(1.0, rel=1e-3)
    assert s["sell_price_pct"] > 0.8 and s["buy_price_pct"] < 0.2
    assert s["avg_sell_price"] > 250 and s["avg_buy_price"] < 0
    prof = A.hourly_profile(iv)
    assert prof.loc[18, "net_mw"] == pytest.approx(100.0)
    assert prof.loc[11, "net_mw"] == pytest.approx(-50.0)


def test_benchmark_beats_or_matches_actual():
    scada, disp, prices = _synthetic()
    iv = A.unit_intervals(scada, disp, prices, "BAT1", "SA1")
    days = pd.date_range("2025-10-01", periods=2, freq="D")
    params = pd.DataFrame({"discharge_mw": 100.0, "charge_mw": 100.0, "energy_mwh": 200.0}, index=days)
    bench = A.benchmark_unit(iv, params, rte=0.95, degradation_cost=10.0)
    assert len(bench) == 2
    assert (bench["opt_aud"] >= bench["same_volume_aud"] - 1e-6).all()
    assert (bench["opt_aud"] >= bench["energy_aud"] * 0.95).all()
    summary = A.summarize_benchmark(bench, params)
    assert 0 < summary["capture_ratio"] <= 1.05
    assert summary["capture_ratio"] == pytest.approx(summary["timing_ratio"] * summary["volume_ratio"])


def test_params_by_day_uses_registration_valid_on_each_day():
    reg = pd.DataFrame({
        "DUID": ["B", "B", "B"], "EFFECTIVEDATE": pd.to_datetime(["2024-01-01", "2025-12-11", "2025-12-11"]),
        "VERSIONNO": [1, 1, 2], "MAXCAPACITY": [460, 460, 460], "MINCAPACITY": [0, 460, 460],
        "MAXSTORAGECAPACITY": [1073, 1500, 1770],
    })
    days = pd.date_range("2025-12-10", periods=3, freq="D")
    p = A.params_by_day(reg, "B", days)
    assert p["energy_mwh"].tolist() == [1073, 1770, 1770]
    assert p["charge_mw"].tolist() == [460, 460, 460]       # MINCAPACITY of 0 falls back to MAXCAPACITY


def test_offered_capacity_shrinks_the_benchmark():
    scada, disp, prices = _synthetic()
    disp["AVAILABILITY"] = np.where(disp["SETTLEMENTDATE"] < pd.Timestamp("2025-10-02 00:05"), 50.0, 100.0)
    iv = A.unit_intervals(scada, disp, prices, "BAT1", "SA1")
    days = pd.date_range("2025-10-01", periods=2, freq="D")
    params = pd.DataFrame({"discharge_mw": 100.0, "charge_mw": 100.0, "energy_mwh": 200.0}, index=days)
    bench = A.benchmark_unit(iv, params, rte=0.9)
    assert bench["offered_share"].tolist() == [0.5, 1.0]
    first, second = bench.iloc[0], bench.iloc[1]
    assert first["opt_aud"] == pytest.approx(0.5 * first["opt_registered_aud"], rel=1e-6)   # problem scales linearly
    assert second["opt_aud"] == pytest.approx(second["opt_registered_aud"], rel=1e-9)
    s = A.summarize_benchmark(bench, params)
    assert s["capture_registered"] <= s["capture_ratio"]
