"""Perfect-foresight energy arbitrage of a battery on 5-minute prices: an LP with lazily added binaries (HiGHS).

For one NEM trading day (288 five-minute intervals) with known regional prices p_t (AUD/MWh, interval length dt hours)
the battery chooses charge c_t and discharge d_t (MW at the connection point) to maximise

    sum_t p_t * dt * (d_t - c_t)  -  deg_cost * sum_t dt * d_t

subject to
    e_t = e_{t-1} + eta * dt * c_t - dt * d_t / eta            stored energy (MWh), eta = sqrt(RTE)
    e_min <= e_t <= e_max,   e_T >= e_end (default e_0)         nothing borrowed from the next day
    0 <= c_t <= P_c,   0 <= d_t <= P_d
    c_t / P_c + d_t / P_d <= 1                                  one inverter, shared between the two directions
    sum_t dt * d_t <= max_discharge_mwh                         optional throughput cap
    c_t <= P_c * u_t,  d_t <= P_d * (1 - u_t),  u_t binary      no charging and discharging in the same interval

The last constraint is what makes this a MILP, and on days when prices sit below zero for hours (common in South
Australia) a binary for every interval makes HiGHS take minutes per day: there are thousands of equally good ways
to spread charging over near-identical negative prices. Most binaries are redundant, though:

* With p_t > 0, charging and discharging at once is never optimal: lowering c_t by a and d_t by eta**2 * a leaves
  e_t unchanged and adds p_t * dt * a * (1 - eta**2) > 0 of revenue.
* With p_t <= 0, "burning" energy through conversion losses earns |p_t| * (1/RTE - 1) per MWh cycled. A cycling cost
  above that removes the incentive, so with a realistic ``degradation_cost`` the LP relaxation is almost always
  already feasible for the MILP.

So the LP is solved first and a binary is added only for intervals where its solution charges and discharges at
once, repeating until none does. The final solution is optimal for a relaxation of the full MILP and feasible for
it, hence optimal for the MILP. The convex-hull inequality c_t/P_c + d_t/P_d <= 1 is valid for the MILP and makes
the relaxation much tighter.

Usage:
    from nem_battery.optimizer import BatteryParams, optimize_day
    res = optimize_day(prices_aud_mwh, BatteryParams(discharge_mw=150, charge_mw=120, energy_mwh=194))
    res.revenue, res.schedule
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy import sparse
from scipy.optimize import Bounds, LinearConstraint, milp

DT_5MIN = 5 / 60
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BatteryParams:
    discharge_mw: float = 1.0
    charge_mw: float = 1.0
    energy_mwh: float = 2.0
    round_trip_efficiency: float = 0.85
    soc_min: float = 0.0             # fraction of energy_mwh
    soc_max: float = 1.0
    degradation_cost: float = 10.0   # AUD per MWh discharged

    @property
    def eta(self) -> float:
        """One-way efficiency, split symmetrically between charging and discharging."""
        return float(np.sqrt(self.round_trip_efficiency))

    @property
    def e_min(self) -> float:
        return self.energy_mwh * self.soc_min

    @property
    def e_max(self) -> float:
        return self.energy_mwh * self.soc_max


@dataclass
class DispatchResult:
    revenue: float           # AUD, net of degradation cost
    gross_revenue: float     # AUD, energy only
    charged_mwh: float
    discharged_mwh: float
    binaries: int            # number of binary variables the final solve needed
    schedule: pd.DataFrame   # price, charge_mw, discharge_mw, energy_mwh (stored energy at interval end)


def optimize_day(prices: pd.Series | np.ndarray, params: BatteryParams, e_start: float | None = None,
                 e_end: float | None = None, max_discharge_mwh: float | None = None,
                 dt_hours: float = DT_5MIN, time_limit: float = 60.0) -> DispatchResult:
    """Solve the arbitrage problem for one horizon of prices.

    ``e_start`` is the stored energy at the beginning; if None, the optimiser chooses it. The stored energy at the end
    must be at least ``e_end`` (default: the starting level). Both are clipped to the allowed range. ``time_limit`` (s)
    applies to each MILP solve; if it is hit, the best solution found is used and a warning is logged.
    """
    p = np.asarray(prices, dtype=float)
    if np.isnan(p).any():
        raise ValueError("prices contain NaN")
    n = len(p)
    e_lo, e_hi = params.e_min, params.e_max
    e0 = None if e_start is None else float(np.clip(e_start, e_lo, e_hi))
    e_end = None if e_end is None else float(np.clip(e_end, e_lo, e_hi))

    binary = np.array([], dtype=int)
    while True:
        x = _solve(p, params, e0, e_end, max_discharge_mwh, dt_hours, binary, time_limit)
        charge, discharge, energy = x[:n].copy(), x[n:2 * n].copy(), x[2 * n:3 * n]
        both = np.flatnonzero((charge > 1e-6) & (discharge > 1e-6))
        new = np.setdiff1d(both, binary)
        if len(new) == 0:
            break
        binary = np.union1d(binary, new)

    charge[charge < 1e-9] = 0.0
    discharge[discharge < 1e-9] = 0.0
    index = prices.index if isinstance(prices, pd.Series) else pd.RangeIndex(n)
    schedule = pd.DataFrame({"price": p, "charge_mw": charge, "discharge_mw": discharge, "energy_mwh": energy},
                            index=index)
    gross = float(np.sum(p * dt_hours * (discharge - charge)))
    discharged = float(discharge.sum() * dt_hours)
    return DispatchResult(
        revenue=gross - params.degradation_cost * discharged,
        gross_revenue=gross,
        charged_mwh=float(charge.sum() * dt_hours),
        discharged_mwh=discharged,
        binaries=len(binary),
        schedule=schedule,
    )


def _solve(p: np.ndarray, params: BatteryParams, e0: float | None, e_end: float | None,
           max_discharge_mwh: float | None, dt_hours: float, binary: np.ndarray, time_limit: float,
           _retry: bool = False) -> np.ndarray:
    """One solve with no-simultaneity binaries on ``binary``; returns [charge, discharge, energy, e_start, u]."""
    n, m = len(p), len(binary)
    eta = params.eta
    pc, pd_ = params.charge_mw, params.discharge_mw
    ic, idd, ie, i0, iu = 0, n, 2 * n, 3 * n, 3 * n + 1
    nv = 3 * n + 1 + m
    cost = np.zeros(nv)
    cost[ic:ic + n] = p * dt_hours
    cost[idd:idd + n] = -(p - params.degradation_cost) * dt_hours

    eye = sparse.identity(n, format="csr")
    shift = sparse.eye(n, k=-1, format="csr")          # e_{t-1}
    zeros_n, zeros_1, zeros_u = sparse.csr_matrix((n, n)), sparse.csr_matrix((n, 1)), sparse.csr_matrix((n, m))
    first = sparse.csr_matrix(([-1.0], ([0], [0])), shape=(n, 1))   # -e_start in the row of t = 0
    # energy balance: e_t - e_{t-1} - eta*dt*c_t + dt/eta*d_t = 0, with e_{-1} = e_start (a variable)
    balance = sparse.hstack([-eta * dt_hours * eye, (dt_hours / eta) * eye, eye - shift, first, zeros_u],
                            format="csr")
    hull = sparse.hstack([eye / pc, eye / pd_, zeros_n, zeros_1, zeros_u], format="csr")
    constraints = [LinearConstraint(balance, 0.0, 0.0), LinearConstraint(hull, -np.inf, 1.0)]
    if e_end is None:                                  # e_T - e_start >= 0
        end = sparse.csr_matrix(([1.0, -1.0], ([0, 0], [ie + n - 1, i0])), shape=(1, nv))
        constraints.append(LinearConstraint(end, 0.0, np.inf))
    if m:
        rows = np.arange(m)
        sel_c = sparse.csr_matrix((np.ones(m), (rows, ic + binary)), shape=(m, nv))
        sel_d = sparse.csr_matrix((np.ones(m), (rows, idd + binary)), shape=(m, nv))
        sel_u = sparse.csr_matrix((np.ones(m), (rows, iu + rows)), shape=(m, nv))
        constraints.append(LinearConstraint(sel_c - pc * sel_u, -np.inf, 0.0))       # c_t <= Pc * u_t
        constraints.append(LinearConstraint(sel_d + pd_ * sel_u, -np.inf, pd_))      # d_t <= Pd * (1 - u_t)
    if max_discharge_mwh is not None:
        thr = sparse.csr_matrix((np.full(n, dt_hours), (np.zeros(n, int), np.arange(idd, idd + n))), shape=(1, nv))
        constraints.append(LinearConstraint(thr, 0.0, max(max_discharge_mwh, 0.0)))

    lb = np.concatenate([np.zeros(2 * n), np.full(n + 1, params.e_min), np.zeros(m)])
    ub = np.concatenate([np.full(n, pc), np.full(n, pd_), np.full(n + 1, params.e_max), np.ones(m)])
    if e0 is not None:
        lb[i0] = ub[i0] = e0
    if e_end is not None:
        lb[ie + n - 1] = e_end                         # e_T >= e_end
    integrality = np.concatenate([np.zeros(3 * n + 1), np.ones(m)])

    res = milp(cost, constraints=constraints, bounds=Bounds(lb, ub), integrality=integrality,
               options={"disp": False, "mip_rel_gap": 1e-6, "time_limit": time_limit})
    if res.success:
        return res.x
    if res.status == 1 and res.x is not None:          # time limit with a feasible solution
        log.warning("MILP hit the %.0f s time limit with %d binaries; using best solution (gap %.2g)",
                    time_limit, m, getattr(res, "mip_gap", float("nan")))
        return res.x
    if max_discharge_mwh and not _retry:
        # HiGHS occasionally ends a degenerate LP with "model status unknown" when the throughput cap is exactly
        # attainable (seen with cap = 1.5 cycles on a 1-hour battery); nudging the cap by 1e-7 resolves it
        return _solve(p, params, e0, e_end, max_discharge_mwh * (1 - 1e-7), dt_hours, binary, time_limit, True)
    raise RuntimeError(f"MILP failed: {res.message}")
