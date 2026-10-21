import numpy as np
import pytest
from scipy.optimize import Bounds, LinearConstraint, milp

from nem_battery import BatteryParams, optimize_day
from nem_battery.optimizer import DT_5MIN

LOSSLESS = BatteryParams(discharge_mw=1.0, charge_mw=1.0, energy_mwh=1.0, round_trip_efficiency=1.0,
                         degradation_cost=1.0)


def test_flat_prices_do_nothing():
    res = optimize_day(np.full(288, 80.0), BatteryParams())
    assert res.discharged_mwh == pytest.approx(0.0, abs=1e-6)
    assert res.revenue == pytest.approx(0.0, abs=1e-6)


def test_buys_low_sells_high_lossless():
    # 12 cheap hours then 12 expensive hours; 1 MW / 1 MWh battery that must end where it started
    prices = np.r_[np.full(144, 20.0), np.full(144, 300.0)]
    res = optimize_day(prices, LOSSLESS, e_start=0.0)
    assert res.gross_revenue == pytest.approx(280.0, rel=1e-6)
    assert res.revenue == pytest.approx(279.0, rel=1e-6)
    s = res.schedule
    assert s["charge_mw"].iloc[144:].sum() == pytest.approx(0.0, abs=1e-9)
    assert s["discharge_mw"].iloc[:144].sum() == pytest.approx(0.0, abs=1e-9)


def test_free_start_cannot_borrow_energy():
    # expensive first, cheap later: with a free start the optimiser may start full, but must end at least as full
    prices = np.r_[np.full(144, 300.0), np.full(144, 20.0)]
    res = optimize_day(prices, LOSSLESS)
    assert res.gross_revenue == pytest.approx(280.0, rel=1e-6)
    assert res.schedule["energy_mwh"].iloc[-1] >= 1.0 - 1e-6


def test_efficiency_losses_reduce_revenue():
    prices = np.r_[np.full(144, 20.0), np.full(144, 300.0)]
    p = BatteryParams(discharge_mw=1, charge_mw=1, energy_mwh=1, round_trip_efficiency=0.81, degradation_cost=0)
    res = optimize_day(prices, p, e_start=0.0)
    # 1 MWh stored -> 0.9 MWh sold, 1/0.9 MWh bought
    assert res.revenue == pytest.approx(300 * 0.9 - 20 / 0.9, rel=1e-4)


def test_constraints_hold():
    rng = np.random.default_rng(0)
    prices = rng.normal(80, 150, 288)
    params = BatteryParams(discharge_mw=100, charge_mw=80, energy_mwh=150, round_trip_efficiency=0.85)
    res = optimize_day(prices, params, e_start=50, max_discharge_mwh=150)
    s = res.schedule
    assert s["energy_mwh"].min() >= -1e-6
    assert s["energy_mwh"].max() <= 150 + 1e-6
    assert s["energy_mwh"].iloc[-1] >= 50 - 1e-6
    assert s["charge_mw"].max() <= 80 + 1e-6
    assert ((s["charge_mw"] > 1e-6) & (s["discharge_mw"] > 1e-6)).sum() == 0
    assert (s["charge_mw"] / 80 + s["discharge_mw"] / 100).max() <= 1 + 1e-6
    assert res.discharged_mwh <= 150 + 1e-6
    # energy balance
    eta = params.eta
    e = 50 + np.cumsum(eta * DT_5MIN * s["charge_mw"] - DT_5MIN * s["discharge_mw"] / eta)
    np.testing.assert_allclose(e, s["energy_mwh"], atol=1e-6)


def test_throughput_cap_binds():
    prices = np.tile(np.r_[np.full(36, 10.0), np.full(36, 400.0)], 4)    # four spreads a day
    p = BatteryParams(discharge_mw=1, charge_mw=1, energy_mwh=1, round_trip_efficiency=0.9)
    free = optimize_day(prices, p, e_start=0.0)
    capped = optimize_day(prices, p, e_start=0.0, max_discharge_mwh=1.0)
    assert capped.discharged_mwh == pytest.approx(1.0, rel=1e-6)
    assert free.discharged_mwh > 2.0
    assert capped.revenue < free.revenue


def _full_milp(prices, params, e_start, dt=DT_5MIN):
    """Reference model with a binary in EVERY interval, for small instances."""
    n = len(prices)
    eta, pc, pd_ = params.eta, params.charge_mw, params.discharge_mw
    nv = 4 * n
    cost = np.r_[prices * dt, -(prices - params.degradation_cost) * dt, np.zeros(2 * n)]
    a = np.zeros((n, nv))
    for t in range(n):
        a[t, t], a[t, n + t], a[t, 2 * n + t] = -eta * dt, dt / eta, 1.0
        if t:
            a[t, 2 * n + t - 1] = -1.0
    rhs = np.zeros(n)
    rhs[0] = e_start
    mode_c = np.hstack([np.eye(n), np.zeros((n, 2 * n)), -pc * np.eye(n)])
    mode_d = np.hstack([np.zeros((n, n)), np.eye(n), np.zeros((n, n)), pd_ * np.eye(n)])
    lb = np.r_[np.zeros(2 * n), np.full(n, params.e_min), np.zeros(n)]
    ub = np.r_[np.full(n, pc), np.full(n, pd_), np.full(n, params.e_max), np.ones(n)]
    lb[3 * n - 1] = e_start
    res = milp(cost, constraints=[LinearConstraint(a, rhs, rhs), LinearConstraint(mode_c, -np.inf, 0),
                                  LinearConstraint(mode_d, -np.inf, pd_)],
               bounds=Bounds(lb, ub), integrality=np.r_[np.zeros(3 * n), np.ones(n)])
    return -res.fun


@pytest.mark.parametrize("seed", range(5))
def test_lazy_binaries_match_full_milp(seed):
    # deep negative prices with a small battery: the LP relaxation would "burn" energy, binaries must kick in
    rng = np.random.default_rng(seed)
    prices = np.r_[rng.uniform(-900, -100, 12), rng.uniform(-50, 300, 12)]
    params = BatteryParams(discharge_mw=1, charge_mw=1, energy_mwh=0.25, round_trip_efficiency=0.8,
                           degradation_cost=0)
    res = optimize_day(prices, params, e_start=0.0)
    assert res.binaries > 0                 # the LP alone would have charged and discharged at once
    assert res.revenue == pytest.approx(_full_milp(prices, params, 0.0), rel=1e-6, abs=1e-6)
    s = res.schedule
    assert ((s["charge_mw"] > 1e-6) & (s["discharge_mw"] > 1e-6)).sum() == 0


def test_negative_prices_are_used_for_charging():
    prices = np.r_[np.full(96, -50.0), np.full(192, 150.0)]
    res = optimize_day(prices, BatteryParams(round_trip_efficiency=0.9), e_start=0.0)
    assert res.schedule["charge_mw"].iloc[:96].sum() > 0
    assert res.revenue > 0


def test_nan_prices_rejected():
    with pytest.raises(ValueError):
        optimize_day(np.r_[np.full(10, 50.0), np.nan], BatteryParams())


def test_retries_once_when_highs_reports_unknown_status(monkeypatch):
    import nem_battery.optimizer as opt
    from types import SimpleNamespace

    real, calls = opt.milp, []

    def flaky(*args, **kwargs):
        calls.append(kwargs["constraints"][-1].ub[0])       # throughput cap of this attempt
        if len(calls) == 1:
            return SimpleNamespace(success=False, status=4, x=None, message="HiGHS Status 15: model_status is Unknown")
        return real(*args, **kwargs)

    monkeypatch.setattr(opt, "milp", flaky)
    res = optimize_day(np.r_[np.full(144, 20.0), np.full(144, 300.0)], BatteryParams(), max_discharge_mwh=3.0)
    assert len(calls) == 2 and calls[1] < calls[0]
    assert 0 < res.discharged_mwh <= 3.0
