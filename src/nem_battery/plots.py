"""Static charts (PNG) for the README.

Usage:
    from nem_battery import plots
    plots.capture_bars(summary_of_selected_units, Path("docs/img/capture.png"))
"""
from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.transforms import Bbox  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

SURFACE, INK, INK2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
SLOTS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
CHARGE, DISCHARGE = "#2a78d6", "#e34948"       # diverging poles: energy in / energy out
FLEET_GRAY = "#c3c2b7"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": AXIS, "axes.labelcolor": INK2, "axes.titlecolor": INK, "axes.titlesize": 11,
    "axes.titleweight": "bold", "axes.titlelocation": "left", "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8, "axes.axisbelow": True,
    "xtick.color": MUTED, "ytick.color": MUTED, "xtick.labelcolor": INK2, "ytick.labelcolor": INK2,
    "font.size": 9, "legend.frameon": False, "legend.labelcolor": INK2, "lines.linewidth": 2,
    "lines.solid_capstyle": "round", "font.family": ["Segoe UI", "DejaVu Sans", "sans-serif"],
})


def _save(fig, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def _short(name: str) -> str:
    return (name.replace("Battery Energy Storage System", "BESS").replace("Energy Storage System", "ESS")
            .replace("Super Battery", "SB").strip())


def capture_bars(sel: pd.DataFrame, path: Path):
    """Actual energy (+ FCAS) revenue vs. the two perfect-foresight benchmarks, k AUD per MW-year."""
    names = [_short(n) for n in sel["name"]]
    y = np.arange(len(sel))[::-1] * 1.1
    h = 0.21
    fig, ax = plt.subplots(figsize=(9, 1.0 * len(sel) + 1.6))
    act, fcas = sel["bench_actual_k_aud_per_mw_yr"], sel["bench_fcas_k_aud_per_mw_yr"]
    bars = [
        (y + 1.5 * h, None, "Actual: energy arbitrage", SLOTS[0]),
        (y + 0.5 * h, "same_volume_k_aud_per_mw_yr", "Perfect timing, actual daily volume", SLOTS[1]),
        (y - 0.5 * h, "opt_k_aud_per_mw_yr", "Perfect foresight, capacity offered to the market", SLOTS[2]),
        (y - 1.5 * h, "opt_registered_k_aud_per_mw_yr", "Perfect foresight, registered capacity", SLOTS[4]),
    ]
    ax.barh(y + 1.5 * h, act, h * 0.85, color=SLOTS[0], label=bars[0][2])
    ax.barh(y + 1.5 * h, fcas, h * 0.85, left=act, color=SLOTS[3], label="Actual: FCAS (on top)")
    for yy, col, label, color in bars[1:]:
        ax.barh(yy, sel[col], h * 0.85, color=color, label=label)
    for yi, a, f, o, c, r, cr in zip(y, act, fcas, sel["opt_k_aud_per_mw_yr"], sel["capture_ratio"],
                                     sel["opt_registered_k_aud_per_mw_yr"], sel["capture_registered"]):
        ax.text(max(a, 0) + f + 2, yi + 1.5 * h, f"{a + f:.0f}", va="center", color=INK2, fontsize=7.5)
        ax.text(o + 2, yi - 0.5 * h, f"{o:.0f}  (actual = {100 * c:.0f}%)", va="center", color=INK2, fontsize=7.5)
        ax.text(r + 2, yi - 1.5 * h, f"{r:.0f}  ({100 * cr:.0f}%)", va="center", color=INK2, fontsize=7.5)
    ax.set_yticks(y, [f"{n}\n{r[:-1]} · {mw:.0f} MW / {e:.0f} MWh" for n, r, mw, e in
                      zip(names, sel["region"], sel["discharge_mw"], sel["energy_mwh"])])
    ax.grid(axis="y", visible=False)
    ax.axvline(0, color=AXIS, linewidth=0.8)
    ax.set_xlabel("k AUD per registered MW per year")
    ax.set_xlim(min(0, act.min() * 1.2), sel["opt_registered_k_aud_per_mw_yr"].max() * 1.25)
    ax.set_title("What the batteries earned vs. perfect-foresight energy arbitrage")
    ax.legend(loc="upper center", bbox_to_anchor=(0.45, -0.09), ncol=2, fontsize=8)
    _save(fig, path)


def daily_profiles(profiles: pd.DataFrame, sel: pd.DataFrame, path: Path):
    """Average net output by hour of day (share of rated power), one panel per battery, plus regional prices."""
    units = list(sel.index)
    ncol = 3
    nrow = int(np.ceil(len(units) / ncol))
    fig, axes = plt.subplots(nrow + 1, ncol, figsize=(11, 2.4 * (nrow + 1)), sharex=True,
                             gridspec_kw={"height_ratios": [1] * nrow + [1.1]})
    lim = 0
    for ax, duid in zip(axes[:nrow].flat, units):
        prof = profiles.loc[duid]
        share = 100 * prof["net_mw"] / sel.loc[duid, "discharge_mw"]
        ax.bar(share.index, share.clip(lower=0), 0.8, color=DISCHARGE)
        ax.bar(share.index, share.clip(upper=0), 0.8, color=CHARGE)
        ax.axhline(0, color=AXIS, linewidth=0.8)
        ax.set_title(_short(sel.loc[duid, "name"]), fontsize=9)
        lim = max(lim, share.abs().max())
    for ax in axes[:nrow].flat:
        ax.set_ylim(-lim * 1.1, lim * 1.1)
        ax.grid(axis="x", visible=False)
        ax.set_xticks(range(0, 24, 6))
        ax.tick_params(labelbottom=True)
    for ax in axes[:nrow, 0]:
        ax.set_ylabel("% of rated MW")
    for ax in list(axes[:nrow].flat)[len(units):]:
        ax.set_visible(False)
    # bottom row: one wide panel with average price by hour per region
    gs = axes[nrow, 0].get_gridspec()
    for ax in axes[nrow]:
        ax.remove()
    pax = fig.add_subplot(gs[nrow, :])
    regions = sorted(sel["region"].unique())
    for i, reg in enumerate(regions):
        duid = sel.index[sel["region"] == reg][0]
        rrp = profiles.loc[duid, "rrp"]
        pax.plot(rrp.index, rrp.to_numpy(), color=SLOTS[i], label=reg[:-1])
    pax.axhline(0, color=AXIS, linewidth=0.8)
    pax.set_xticks(range(0, 24, 3))
    pax.set_xlim(-0.6, 23.6)
    pax.set_xlabel("Hour of day (NEM time, AEST)")
    pax.set_ylabel("AUD/MWh")
    pax.set_title("Average spot price by hour", fontsize=9)
    pax.legend(ncol=len(regions), loc="upper left", fontsize=8)
    fig.suptitle("Average daily profile: discharging (red) and charging (blue)", x=0.01, ha="left",
                 fontweight="bold", color=INK, fontsize=11)
    fig.tight_layout()
    _save(fig, path)


def revenue_concentration(benches: dict[str, pd.DataFrame], sel: pd.DataFrame, path: Path):
    """Cumulative share of annual energy revenue earned on the N best days."""
    fig, ax = plt.subplots(figsize=(8, 4.2))
    for i, duid in enumerate(sel.index):
        daily = benches[duid]["energy_aud"].sort_values(ascending=False)
        cum = 100 * daily.cumsum() / daily.sum()
        x = np.arange(1, len(cum) + 1)
        ax.plot(x, cum.to_numpy(), color=SLOTS[i], label=_short(sel.loc[duid, "name"]))
    ax.axvline(10, color=AXIS, linewidth=1)
    ax.axhline(100, color=AXIS, linewidth=1)
    ax.text(11, ax.get_ylim()[1] * 0.97, "10 best days", color=MUTED, fontsize=8, va="top")
    ax.text(1.05, 102, "100% = full-year net revenue",
            color=MUTED, fontsize=8, va="bottom")
    ax.set_xscale("log")
    ax.set_xlim(1, 365)
    ax.set_xticks([1, 3, 10, 30, 100, 365], ["1", "3", "10", "30", "100", "365"])
    ax.set_ylim(0, None)
    ax.set_xlabel("Number of best days (log scale)")
    ax.set_ylabel("Cumulative % of annual energy revenue")
    ax.set_title("A few price spikes make the year")
    ax.legend(fontsize=8, loc="center left", bbox_to_anchor=(1.01, 0.5))
    _save(fig, path)


def fleet_scatter(fleet: pd.DataFrame, highlight: list[str], path: Path):
    """Capture ratio vs. cycles per day for every battery; the detailed ones are labelled."""
    f = fleet.dropna(subset=["capture_ratio"])
    hi = f[f["duid"].isin(highlight)]
    lo = f[~f["duid"].isin(highlight)]
    fig, ax = plt.subplots(figsize=(8, 5))
    size = lambda mw: 12 + mw / 4  # noqa: E731
    ax.scatter(lo["cycles_per_day"], 100 * lo["capture_ratio"], s=size(lo["discharge_mw"]), color=FLEET_GRAY,
               edgecolor=SURFACE, linewidth=1.5, label="Other NEM batteries", zorder=2)
    ax.scatter(hi["cycles_per_day"], 100 * hi["capture_ratio"], s=size(hi["discharge_mw"]), color=SLOTS[0],
               edgecolor=SURFACE, linewidth=1.5, label="Analysed in detail", zorder=3)
    ax.set_xlim(0, None)
    ax.set_ylim(0, None)
    renderer = fig.canvas.get_renderer()
    # highlighted dots are obstacles too, so a label never covers another analysed battery
    xy = ax.transData.transform(np.c_[hi["cycles_per_day"], 100 * hi["capture_ratio"]])
    placed = [Bbox.from_extents(x - 7, y - 7, x + 7, y + 7) for x, y in xy]
    for _, r in hi.sort_values("capture_ratio", ascending=False).iterrows():
        # first candidate position (right/left, above/below the dot) whose label overlaps no earlier label
        for dx, dy, ha in ((7, 3, "left"), (7, -11, "left"), (-7, 3, "right"), (-7, -11, "right")):
            t = ax.annotate(_short(r["name"]), (r["cycles_per_day"], 100 * r["capture_ratio"]), xytext=(dx, dy),
                            textcoords="offset points", ha=ha, color=INK2, fontsize=8)
            box = t.get_window_extent(renderer)
            own = Bbox.from_extents(*ax.transData.transform((r["cycles_per_day"], 100 * r["capture_ratio"])).repeat(2))
            if not any(box.overlaps(b) and not b.contains(*own.p0) for b in placed) or ha == "right" and dy < 0:
                placed.append(box)
                break
            t.remove()
    ax.set_xlabel("Actual full cycles per day")
    ax.set_ylabel("Actual as % of perfect foresight (offered capacity)")
    ax.set_title(f"All {len(f)} grid batteries with a full year of data (marker size = MW)")
    ax.legend(fontsize=8, loc="upper left")
    _save(fig, path)


def example_day(actual: pd.DataFrame, optimal: pd.DataFrame, title: str, path: Path):
    """Price, actual vs. optimal power, and stored energy over one day (three panels, shared time axis)."""
    fig, (a1, a2, a3) = plt.subplots(3, 1, figsize=(9, 7), sharex=True, gridspec_kw={"height_ratios": [1, 1.2, 1]})
    t = actual.index
    a1.step(t, actual["rrp"], where="post", color=INK2, linewidth=1.5)
    a1.set_ylabel("AUD/MWh")
    a1.set_title(title)
    act_mw = actual["net_mwh"] * 12
    opt_mw = optimal["discharge_mw"] - optimal["charge_mw"]
    a2.step(t, act_mw, where="post", color=SLOTS[0], label="Actual")
    a2.step(t, opt_mw.to_numpy(), where="post", color=SLOTS[1], label="Perfect foresight")
    a2.axhline(0, color=AXIS, linewidth=0.8)
    a2.set_ylabel("MW (+ discharge, - charge)")
    a2.legend(fontsize=8, loc="upper left")
    if actual["soc_mwh"].notna().any():
        a3.plot(t, actual["soc_mwh"], color=SLOTS[0], label="Actual (reported)")
    a3.plot(t + pd.Timedelta("5min"), optimal["energy_mwh"].to_numpy(), color=SLOTS[1], label="Perfect foresight")
    a3.set_ylabel("Stored energy, MWh")
    a3.set_ylim(0, None)
    a3.set_title("Stored energy (actual = as reported to AEMO)", fontsize=9, fontweight="normal")
    a3.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    a3.set_xlabel("NEM time (AEST)")
    fig.tight_layout()
    _save(fig, path)
