"""The two displays from section 10 of the strategy notes.

    soc_plot()  - energy: separate cumulatives, pack limits, standing phases
    st_plot()   - time and place: the distance-time diagram

Deliberately two plots and not one dashboard. They answer different
questions and get looked at at different moments: the SoC plot says whether
the model is still describing reality, the distance-time diagram says
whether the day still fits in the window and where a standing phase could
go.

matplotlib is imported lazily and forced onto the Agg backend, so importing
this module never opens a window and never fails on a headless machine.
Nothing else in the strategy package depends on it.
"""

from   datetime import timedelta
import logging as lg

import numpy as np

from ..simulation.battery import capacity_wh
from .dayplan import V_FLOOR_APPLIES_AT, V_FLOOR_KMH
from .inputs import RACE_TZ

log = lg.getLogger(__name__)


def _plt(interactive: bool = False):
    import matplotlib
    if not interactive:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def _hours(times, tz=RACE_TZ):
    """Race-local decimal hours - a plain numeric axis.

    Deliberately not matplotlib's date locators: the axis has to be read
    against a wall clock and against the 17:00 deadline, and hours as
    numbers put the tick marks exactly where the day is divided.
    """
    t = [x.astimezone(tz) for x in times]
    return np.array([x.hour + x.minute / 60 + x.second / 3600 for x in t])


# ------------------------------------------------------------------- energy ----

def soc_plot(opt, state, batt, path=None, interactive=False):
    """Energy over the remaining distance.

    Solar yield and consumption as SEPARATE cumulatives, not only their
    difference. When the measurement drifts off the plan, the two causes
    call for opposite reactions - less sun means slow down, more load means
    the coefficients are wrong and the rest of the plan with them - and in
    the difference they are indistinguishable.
    """
    if opt.trace is None:
        raise ValueError("keine Trace - Option war nicht machbar")
    plt = _plt(interactive)
    tr = opt.trace
    km = tr["km_total"].to_numpy()
    dt = tr["dt_s"].to_numpy()
    cap = capacity_wh(batt)
    floor = (1.0 - batt.usable) * cap

    solar = np.cumsum(tr["p_solar"].to_numpy() * dt) / 3600.0
    load = np.cumsum((tr["p_motor"].to_numpy() + tr["p_aux"].to_numpy())
                     * dt) / 3600.0

    fig, (ax, ax2) = plt.subplots(
        2, 1, figsize=(11, 7.5), sharex=True, layout="constrained",
        gridspec_kw={"height_ratios": [3, 2]})

    # --- pack
    ax.axhspan(0, floor, color="tab:red", alpha=0.08, lw=0)
    ax.axhline(floor, color="tab:red", lw=1, ls="--")
    ax.axhline(cap, color="tab:blue", lw=1, ls="--")
    ax.text(km[0], floor, f"  nutzbarer Boden {floor:.0f} Wh",
            va="bottom", fontsize=8, color="tab:red")
    ax.text(km[0], cap, f"  Packdeckel {cap:.0f} Wh", va="top",
            fontsize=8, color="tab:blue")
    ax.plot(km, tr["wh_remaining"], color="k", lw=2, label="Pack, geplant")

    stops = tr[tr["kind"] == "stop"]
    if not stops.empty:
        ax.plot(stops["km_total"], stops["wh_remaining"], "o", ms=7,
                mfc="gold", mec="k", zorder=5, label="Standphase")
        for _, r in stops.iterrows():
            ax.annotate(f"{-r['Ws']/3600:+.0f} Wh\n{r['dt_s']/60:.0f} min",
                        (r["km_total"], r["wh_remaining"]),
                        textcoords="offset points", xytext=(6, -18),
                        fontsize=7)

    # the anchor is where the number came from, not a point on the curve
    ax.plot([km[0]], [state.pack.wh], "s", ms=8, mfc="w", mec="k", zorder=6)
    ax.annotate(f"Start {state.pack.wh:.0f} Wh\n{state.pack.trust}",
                (km[0], state.pack.wh), textcoords="offset points",
                xytext=(8, 8), fontsize=8)

    if opt.wh_spilled > 5:
        ax.set_title(f"{opt.wh_loss_note()}", fontsize=9, color="tab:orange",
                     loc="right")

    ax.set_ylabel("Energie im Pack [Wh]")
    ax.legend(loc="lower left", fontsize=8, framealpha=0.9)
    ax.grid(alpha=0.25)
    ax.set_title(f"Tag {state.day} · {opt.n_loops} Loop(s) · {opt.km:.1f} km "
                 f"· Ø {opt.avg_kmh:.1f} km/h", loc="left", fontsize=10)

    # --- separate cumulatives
    ax2.plot(km, solar, color="tab:orange", lw=1.8,
             label=f"Solarertrag kumuliert ({solar[-1]:.0f} Wh)")
    ax2.plot(km, load, color="tab:purple", lw=1.8,
             label=f"Verbrauch kumuliert ({load[-1]:.0f} Wh)")
    ax2.fill_between(km, solar, load, where=solar >= load,
                     color="tab:orange", alpha=0.12, lw=0)
    ax2.fill_between(km, solar, load, where=solar < load,
                     color="tab:purple", alpha=0.12, lw=0)
    ax2.set_xlabel("Strecke ab dem aktuellen Punkt [km]")
    ax2.set_ylabel("kumuliert [Wh]")
    ax2.legend(loc="upper left", fontsize=8, framealpha=0.9)
    ax2.grid(alpha=0.25)

    return _finish(fig, plt, path, interactive)


# ------------------------------------------------------- distance and time ----

def st_plot(opt, state, path=None, interactive=False):
    """The distance-time diagram: slope is speed, horizontal is standing.

    Weather enters as the colour of the plan line rather than as a
    background raster. The raster would be the honest picture of a field
    over (distance, time), but the car only ever occupies one point of it
    per moment, and the decision is about the line - so the line carries
    the irradiance it actually sees.
    """
    if opt.trace is None:
        raise ValueError("keine Trace - Option war nicht machbar")
    plt = _plt(interactive)
    from matplotlib.collections import LineCollection

    tr = opt.trace
    km = tr["km_total"].to_numpy()
    h = _hours(tr["time"])
    ghi = tr["p_solar"].to_numpy()

    fig, ax = plt.subplots(figsize=(11, 6.5), layout="constrained")

    h_dead = _hours([state.t_deadline])[0]
    ax.axvline(h_dead, color="tab:red", lw=1.5)
    ax.text(h_dead, km[-1] * 0.5, " Deadline ", rotation=90, va="center",
            ha="right", fontsize=8, color="tab:red")
    ax.axhline(opt.km, color="k", lw=1, ls=":")
    ax.text(h[0], opt.km, f" Tagesziel {opt.km:.1f} km", va="bottom",
            fontsize=8)

    # earliest possible arrival: everything at its routing cap
    if opt.t_min is not None:
        h_min = _hours([state.t_now + opt.t_min])[0]
        ax.plot([h[0], h_min], [0, opt.km], color="tab:green", lw=1, ls="--")
        ax.text(h_min, opt.km * 0.93,
                f"Vollgas waere hier\n{_fmt(opt.reserve)} Reserve",
                fontsize=7.5, color="tab:green", ha="right", va="top")

    seg = np.stack([np.column_stack([h[:-1], km[:-1]]),
                    np.column_stack([h[1:], km[1:]])], axis=1)
    lc = LineCollection(seg, cmap="YlOrBr", linewidths=3,
                        norm=plt.Normalize(0, max(ghi.max(), 1.0)))
    lc.set_array(0.5 * (ghi[:-1] + ghi[1:]))
    ax.add_collection(lc)
    fig.colorbar(lc, ax=ax, pad=0.01,
                 label="Solarleistung auf dem Panel [W]")

    stops = tr[tr["kind"] == "stop"]
    for _, r in stops.iterrows():
        h0 = _hours([r["time"]])[0]
        h1 = h0 + r["dt_s"] / 3600.0
        ax.plot([h0, h1], [r["km_total"]] * 2, color="tab:blue", lw=6,
                solid_capstyle="butt", zorder=4)
        ax.annotate(f"{r['leg']}\n{r['dt_s']/60:.0f} min",
                    ((h0 + h1) / 2, r["km_total"]),
                    textcoords="offset points", xytext=(0, 8),
                    ha="center", fontsize=7)

    ax.set_xlim(h[0] - 0.15, max(h_dead, h[-1]) + 0.35)
    ax.set_ylim(-opt.km * 0.04, opt.km * 1.12)
    ax.set_xticks(np.arange(np.floor(h[0]), np.ceil(h_dead) + 0.1, 1.0))
    ax.set_xticklabels([f"{int(x):02d}:00" for x in ax.get_xticks()])
    ax.set_xlabel("Tageszeit [SAST]")
    ax.set_ylabel("Strecke ab dem aktuellen Punkt [km]")
    ax.set_title(f"Tag {state.day} · Steigung = Geschwindigkeit, "
                 f"waagrecht = Stehen", loc="left", fontsize=10)
    ax.grid(alpha=0.25)

    if opt.below_floor_km > 0:
        ax.text(0.99, 0.02,
                f"{opt.below_floor_km:.1f} km unter {V_FLOOR_KMH:.0f} km/h "
                f"auf Strassen mit Routing ≥ {V_FLOOR_APPLIES_AT:.0f}",
                transform=ax.transAxes, ha="right", fontsize=8,
                color="tab:red")

    return _finish(fig, plt, path, interactive)


# -------------------------------------------------------------------- utils ----

def _fmt(td: timedelta) -> str:
    if td is None:
        return "-"
    s = int(td.total_seconds())
    return f"{s//3600}:{(s%3600)//60:02d}"


def _finish(fig, plt, path, interactive):
    # no layout call here: the engine is set when the figure is created.
    # tight_layout() cannot handle the colorbar in st_plot() and warns, and
    # switching the engine afterwards trips over the gridspec in soc_plot().
    if path:
        fig.savefig(path, dpi=130)
        plt.close(fig)
        return str(path)
    if interactive:
        plt.show()
        return None
    plt.close(fig)
    return None


def write_both(opt, state, batt, prefix, interactive=False) -> list:
    """Both displays, named after the day and the loop count.

    The loop count belongs in the filename: at a control stop several
    options get plotted within a few minutes, and two files called
    plan.png are worse than none.
    """
    stem = f"{prefix}_tag{state.day}_{opt.n_loops}loops"
    return [soc_plot(opt, state, batt, path=f"{stem}_soc.png",
                     interactive=interactive),
            st_plot(opt, state, path=f"{stem}_strecke_zeit.png",
                    interactive=interactive)]
