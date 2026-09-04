"""The two displays from section 10 of the strategy notes.

    soc_plot()    - energy: pack with terrain behind it, instantaneous
                    power, separate cumulatives
    st_plot()     - time and place: the distance-time diagram
    speed_plot()  - target speed against the routing estimate and the
                    legal limit
    morning_plot()- the next morning's charge window at the overnight stop

Deliberately two plots and not one dashboard. They answer different
questions and get looked at at different moments: the SoC plot says whether
the model is still describing reality, the distance-time diagram says
whether the day still fits in the window and where a standing phase could
go.

Interactive by default: the figures open in a matplotlib window with pan,
zoom and a save button, because the interesting question is usually local
("what happens between km 95 and 110") and a PNG cannot be zoomed. Writing
files stays available for the log.

Every y axis rescales to the visible x range: a plot of a whole day is
unreadable at the resolution a single decision needs.

Click-to-read-a-value exists (_attach_cursors, cursor_text) but is off by
default - render(cursors=True) switches it on and it then wants
mplcursors installed.

matplotlib is imported lazily, and the Agg backend is only forced when
nothing will be shown - so importing this module never opens a window and
never fails on a headless machine. Nothing else in the strategy package
depends on it.
"""

from   datetime import timedelta
import logging as lg

import numpy as np

from ..simulation.battery import capacity_wh
from .dayplan import V_FLOOR_APPLIES_AT, V_FLOOR_KMH
from .inputs import RACE_TZ

log = lg.getLogger(__name__)


def _plt(interactive: bool = False):
    """pyplot, with the backend decided before the first import.

    The order matters: matplotlib.use() has to run before pyplot is
    imported, otherwise the choice is silently ignored and a headless run
    dies on the window instead.
    """
    import matplotlib
    if not interactive:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def _backend_can_show() -> tuple:
    """Is the active backend able to open a window?

    plt.show() on a non-interactive backend returns immediately and does
    nothing. Silently. That is the worst possible outcome at a control
    stop - you assume the plot is coming and it never does - so it gets
    checked and reported instead.

    The list of interactive backends has moved twice: rcsetup.interactive_bk
    in older matplotlib, backend_registry from 3.9, and neither is
    guaranteed. All three routes are tried, and if none works the answer
    falls back to the names of the file-only backends, which have not
    changed in a decade. A diagnostic must never be the thing that breaks.
    """
    import matplotlib
    be = matplotlib.get_backend()
    names = None
    try:                                    # matplotlib >= 3.9
        from matplotlib.backends.registry import (BackendFilter,
                                                  backend_registry)
        names = backend_registry.list_builtin(BackendFilter.INTERACTIVE)
    except Exception:
        try:                                # older matplotlib
            from matplotlib import rcsetup
            names = list(rcsetup.interactive_bk)
        except Exception:
            names = None

    if names:
        return be.lower() in [n.lower() for n in names], be
    return be.lower() not in {"agg", "pdf", "ps", "svg", "cairo",
                              "template", "pgf"}, be


def _hours(times, tz=RACE_TZ):
    """Race-local decimal hours - a plain numeric axis.

    Deliberately not matplotlib's date locators: the axis has to be read
    against a wall clock and against the 17:00 deadline, and hours as
    numbers put the tick marks exactly where the day is divided.
    """
    t = [x.astimezone(tz) for x in times]
    return np.array([x.hour + x.minute / 60 + x.second / 3600 for x in t])


# ------------------------------------------------------------------- energy ----

def soc_plot(opt, state, batt, plt):
    """Energy over the remaining distance.

    Solar yield and consumption as SEPARATE cumulatives, not only their
    difference. When the measurement drifts off the plan, the two causes
    call for opposite reactions - less sun means slow down, more load means
    the coefficients are wrong and the rest of the plan with them - and in
    the difference they are indistinguishable.
    """
    tr = opt.trace
    km = tr["km_total"].to_numpy()
    dt = tr["dt_s"].to_numpy()
    cap = capacity_wh(batt)
    floor = (1.0 - batt.usable) * cap

    # Fixed margins instead of constrained_layout. This figure carries two
    # twin axes - terrain on the right, the clock on top - and
    # constrained_layout does not handle twins: it gives up with "axes
    # sizes collapsed to zero" and lays the figure out wrong. Fixed margins
    # are deterministic and need no solver.
    fig, (ax, axp) = plt.subplots(
        2, 1, figsize=(12, 7.5), sharex=True,
        gridspec_kw={"height_ratios": [3, 2]})
    fig.subplots_adjust(left=0.075, right=0.925, top=0.90, bottom=0.08,
                        hspace=0.10)

    # --- terrain behind the pack curve, on its own axis.
    # Not decoration: the steps in the pack curve line up with the climbs,
    # and seeing that connection is what makes an unexpected drop readable
    # as "this is the pass" instead of "the model is wrong".
    axt = ax.twinx()
    alt = tr["altitude_m"].to_numpy()
    ok_alt = np.isfinite(alt)
    if ok_alt.any():
        a_lo, a_hi = np.nanmin(alt), np.nanmax(alt)
        axt.fill_between(km[ok_alt], a_lo, alt[ok_alt], color="grey",
                         alpha=0.16, lw=0, zorder=0)
        _tag(axt.plot(km[ok_alt], alt[ok_alt], color="grey", lw=0.8,
                      alpha=0.5, zorder=0))
        # keep the terrain in the lower half so it stays subordinate to the
        # pack curve without being squashed into an unreadable strip.
        # As a factor rather than fixed limits, so it survives zooming.
        axt._y_squeeze = 1.9
        axt.set_ylabel("Höhe [m]", color="grey", fontsize=9)
        axt.tick_params(axis="y", colors="grey", labelsize=8)
        axt.text(km[-1], a_hi, f"  {a_lo:.0f}-{a_hi:.0f} m ",
                 color="grey", fontsize=7, ha="right", va="bottom")
    axt._x_kind = "km"
    axt.set_zorder(0)
    ax.set_zorder(1)
    ax.patch.set_visible(False)

    # --- pack
    ax.axhspan(0, floor, color="tab:red", alpha=0.08, lw=0)
    ax.axhline(floor, color="tab:red", lw=1, ls="--")
    ax.axhline(cap, color="tab:blue", lw=1, ls="--")
    ax.text(km[0], floor, f"  nutzbarer Boden {floor:.0f} Wh",
            va="bottom", fontsize=8, color="tab:red")
    ax.text(km[0], cap, f"  Packdeckel {cap:.0f} Wh", va="top",
            fontsize=8, color="tab:blue")
    _tag(ax.plot(km, tr["wh_remaining"], color="k", lw=2,
                 label="Pack, geplant"))

    stops = _stop_blocks(tr)
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
    # place the label away from the title when the pack starts near full,
    # which on day 1 it always does
    high = state.pack.wh > 0.75 * cap
    ax.annotate(f"Start {state.pack.wh:.0f} Wh\n{state.pack.trust}",
                (km[0], state.pack.wh), textcoords="offset points",
                xytext=(10, -24 if high else 10), fontsize=8,
                va="top" if high else "bottom")

    if opt.wh_spilled > 5:
        ax.text(0.995, 0.05, opt.wh_loss_note(), transform=ax.transAxes,
                ha="right", fontsize=9, color="tab:orange")

    # --- clock along the top. The x axis is distance because that is what
    # the plan is expressed in, but "when does the PV curve peak" and "when
    # do we reach the pass" are time questions. Both readings of the same
    # curve, so a second axis rather than a second plot. Ticks are placed at
    # the distance where each whole hour falls - exact, and no assumption
    # that time and distance are proportional.
    _add_time_axis(ax, tr, km)

    ax.set_ylabel("Energie im Pack [Wh]")
    ax.legend(loc="lower left", fontsize=8, framealpha=0.9)
    ax.grid(alpha=0.25)
    ax.set_title(f"Tag {state.day} · {opt.n_loops} Loop(s) · {opt.km:.1f} km "
                 f"· Ø {opt.avg_kmh:.1f} km/h", loc="left", fontsize=10)

    # --- instantaneous power. Not the same information as the cumulative
    # below: the cumulative says how the day adds up, this says what the
    # panel is doing right now at this place, which is the number that can
    # be held against the measured MPPT output while driving.
    p_solar = tr["p_solar"].to_numpy()
    p_load = tr["p_motor"].to_numpy() + tr["p_aux"].to_numpy()
    drive = tr["kind"].to_numpy() == "drive"

    # Drop only genuinely degenerate segments. stitch_routes() joins the
    # manual day-1 stages with filler segments of 0 m and 2 m, and a
    # zero-length segment carrying an altitude difference divides a finite
    # energy by a vanishing time - 330 kW at km 12 on this route. The
    # ENERGY is unaffected (force times zero distance is zero), only the
    # power is nonsense. Filtering on length rather than on duration keeps
    # the several hundred short-but-real segments in the picture.
    seg_m = tr["speed_kmh"].to_numpy() / 3.6 * tr["dt_s"].to_numpy()
    real = drive & (seg_m >= 5.0) & np.isfinite(p_load)
    n_drop = int(drive.sum() - real.sum())

    _tag(axp.plot(km[real], p_solar[real], color="tab:orange", lw=1.6,
                  label=f"PV-Leistung (Ø {p_solar[real].mean():.0f} W, "
                        f"max {p_solar[real].max():.0f} W)"))
    # deliberately NOT tagged for the autoscaler: the raw per-segment trace
    # spikes to several kW on short steep segments and would set the scale
    # for the whole panel. It is background texture; the smoothed line and
    # the solar curve are what the axis should fit.
    axp.plot(km[real], p_load[real], color="tab:purple", lw=0.5, alpha=0.35)
    ks, ls = _smooth_over_km(km[real], p_load[real], window_km=2.0)
    _tag(axp.plot(ks, ls, color="tab:purple", lw=1.8,
                  label=f"Verbrauch, 2-km-Mittel "
                        f"(Ø fahrend {p_load[real].mean():.0f} W)"))
    if n_drop:
        axp.text(0.995, 0.04,
                 f"{n_drop} entartete(s) Segment(e) ausgelassen "
                 f"(unter 5 m, Stitch-Artefakte)",
                 transform=axp.transAxes, ha="right", fontsize=7,
                 color="grey")
    axp.axhline(0, color="k", lw=0.6)
    for _, r in stops.iterrows():
        axp.axvline(r["km_total"], color="tab:blue", lw=4, alpha=0.25)
    axp.set_ylabel("Leistung [W]")
    axp.legend(loc="upper left", fontsize=8, framealpha=0.9, ncols=2)
    axp.grid(alpha=0.25)

    # The running totals used to sit here as a third panel. They moved to
    # the distance-time figure: on a distance axis a standing phase has
    # zero width, so the very event the totals are meant to explain - solar
    # climbing while consumption stays flat - is invisible.
    axp.set_xlabel("Strecke ab dem aktuellen Punkt [km]")

    for a in (ax, axp):
        a._x_kind = "km"
    # always show the pack axis down to zero, so a deep plan reads as deep
    # and not as empty
    ax._y_min = 0.0
    _autoscale_y_on_zoom(fig)
    return fig


# ------------------------------------------------------- distance and time ----

def st_plot(opt, state, plt):
    """The distance-time diagram: slope is speed, horizontal is standing.

    Weather enters as the colour of the plan line rather than as a
    background raster. The raster would be the honest picture of a field
    over (distance, time), but the car only ever occupies one point of it
    per moment, and the decision is about the line - so the line carries
    the irradiance it actually sees.
    """
    from matplotlib.collections import LineCollection

    tr = opt.trace
    km = tr["km_total"].to_numpy()
    h = _hours(tr["time"])
    ghi = tr["p_solar"].to_numpy()

    fig, (ax, axp, axc) = plt.subplots(
        3, 1, figsize=(11, 10), sharex=True, layout="constrained",
        gridspec_kw={"height_ratios": [3, 1.6, 1.6]})

    h_dead = _hours([state.t_deadline])[0]
    ax.axvline(h_dead, color="tab:red", lw=1.5)
    ax.text(h_dead, km[-1] * 0.5, " Deadline ", rotation=90, va="center",
            ha="right", fontsize=8, color="tab:red")
    ax.axhline(opt.km, color="k", lw=1, ls=":")
    ax.text(h[0], opt.km, f" Tagesziel {opt.km:.1f} km", va="bottom",
            fontsize=8)

    seg = np.stack([np.column_stack([h[:-1], km[:-1]]),
                    np.column_stack([h[1:], km[1:]])], axis=1)
    lc = LineCollection(seg, cmap="YlOrBr", linewidths=3,
                        norm=plt.Normalize(0, max(ghi.max(), 1.0)))
    lc.set_array(0.5 * (ghi[:-1] + ghi[1:]))
    ax.add_collection(lc)
    # the plan is a LineCollection, which the y autoscaler cannot read.
    # An invisible Line2D with the same data gives it something to measure
    # without changing the picture.
    _tag(ax.plot(h, km, lw=0, alpha=0, label="_Plan"))
    fig.colorbar(lc, ax=ax, pad=0.01,
                 label="Solarleistung auf dem Panel [W]")

    for _, r in _stop_blocks(tr).iterrows():
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
    axc.set_xticks(np.arange(np.floor(h[0]), np.ceil(h_dead) + 0.1, 1.0))
    axc.set_xticklabels([f"{int(x):02d}:00" for x in axc.get_xticks()])
    axc.set_xlabel("Tageszeit [SAST]")
    ax.set_ylabel("Strecke ab dem aktuellen Punkt [km]")
    ax.set_title(f"Tag {state.day} · Steigung = Geschwindigkeit, "
                 f"waagrecht = Stehen", loc="left", fontsize=10)
    ax.grid(alpha=0.25)

    _power_over_time(axp, tr)
    _cumulatives_over_time(axc, tr)

    if opt.below_floor_km > 0:
        ax.text(0.99, 0.02,
                f"{opt.below_floor_km:.1f} km unter {V_FLOOR_KMH:.0f} km/h "
                f"auf Strassen mit Routing ≥ {V_FLOOR_APPLIES_AT:.0f}",
                transform=ax.transAxes, ha="right", fontsize=8,
                color="tab:red")

    for a in (ax, axp, axc):
        a._x_kind = "h"
    _autoscale_y_on_zoom(fig)
    return fig


def _power_over_time(axp, tr):
    """PV power against the CLOCK, standing phases included.

    Not the same picture as power against distance, and the difference is
    the whole point: a standing phase has zero width on a distance axis, so
    the tracked panel - the reason to stand at all - is invisible there.
    Against time it occupies its real 35 minutes, and the step up from flat
    to tracked is the thing you are deciding about.

    Drawn as steps over the actual row durations rather than as a line
    through row midpoints, because a 35-minute stop next to two-second
    driving segments makes any interpolation meaningless.
    """
    h = _hours(tr["time"])
    dt_h = tr["dt_s"].to_numpy() / 3600.0
    edges = np.append(h, h[-1] + dt_h[-1])
    p = tr["p_solar"].to_numpy()
    is_stop = tr["kind"].to_numpy() == "stop"

    # carrier for the y autoscaler and for the cursor: the visible curve is
    # a stairs patch, which neither of them can read
    _tag(axp.plot(h, p, lw=0, alpha=0, label="_PV-Leistung"))
    axp.stairs(p, edges, color="tab:orange", lw=1.2, alpha=0.85,
               label="PV-Leistung, flach (fahrend)")
    axp.stairs(np.where(is_stop, p, np.nan), edges, fill=True,
               color="tab:blue", alpha=0.35,
               label="PV-Leistung, nachgefuehrt (stehend)")

    # What the tracking is worth, compared AT THE SAME TIME OF DAY.
    # Comparing the mean over the stops against the mean over the whole
    # driving day would mix the tracking gain with the sun's elevation:
    # the stops sit around midday, the driving average is dragged down by
    # 09:00 and 17:00. That inflates the figure by a factor of two or more,
    # and it hides the result that actually matters - the bonus GROWS as
    # the sun gets low, because tracked/flat tends towards 1/sin(elevation).
    drive = ~is_stop & (dt_h > 0)
    # Only the TRACKED part of a halt gets a percentage. The flat part is
    # by definition at the flat level, so its label would always read 0 %
    # and would print on top of the one that matters.
    aimed = is_stop & (tr["panel"].to_numpy() == "tracked") & (dt_h > 0)
    if aimed.any() and drive.any():
        h_mid = h + dt_h / 2.0
        flat_at = np.interp(h_mid[aimed], h_mid[drive], p[drive])
        gain = p[aimed] / np.maximum(flat_at, 1.0) - 1.0
        for hm, pt, g in zip(h_mid[aimed], p[aimed], gain):
            axp.annotate(f"{100*g:+.0f} %", (hm, pt),
                         textcoords="offset points", xytext=(0, 4),
                         ha="center", fontsize=7, color="tab:blue")
        w = dt_h[aimed]
        axp.annotate(
            f"nachgefuehrt gegen flach zur gleichen Tageszeit: "
            f"{100*np.average(gain, weights=w):+.0f} % im Mittel, "
            f"{100*gain.min():+.0f} bis {100*gain.max():+.0f} %",
            (0.5, 0.06), xycoords="axes fraction", ha="center", fontsize=8)

    axp.set_ylabel("PV-Leistung [W]")
    axp._y_squeeze = 1.35
    axp.legend(loc="upper left", fontsize=8, framealpha=0.9, ncols=2)
    axp.grid(alpha=0.25)


# ---------------------------------------------------------------- speeds ----

def _cumulatives_over_time(axc, tr):
    """Solar yield and consumption as SEPARATE running totals, over time.

    Not only their difference: when the measurement drifts off the plan the
    two causes call for opposite reactions - less sun means slow down, more
    load means the coefficients are wrong and the rest of the plan with
    them - and in the difference they are indistinguishable.

    On the clock rather than on distance, which is why this lives here and
    not in the energy figure: on a distance axis a standing phase has zero
    width, so the very event the totals are meant to explain - solar
    climbing while consumption stays flat - cannot be seen at all.
    """
    # A row's energy has to be plotted at the END of that row, not at its
    # start. tr["time"] is the start; cumsum()[i] already contains row i.
    # Pairing them draws a 35-minute stop as a vertical jump followed by a
    # flat line, when the truth is a steeper slope for 35 minutes. On
    # two-second driving segments the difference is invisible, which is
    # exactly why this survived until someone looked at a standing phase.
    h = _hours(tr["time"])
    dt = tr["dt_s"].to_numpy() / 3600.0
    edges = np.append(h[0], h + dt)
    solar = np.append(0.0, np.cumsum(tr["p_solar"].to_numpy() * dt))
    load = np.append(0.0, np.cumsum((tr["p_motor"].to_numpy()
                                     + tr["p_aux"].to_numpy()) * dt))

    _tag(axc.plot(edges, solar, color="tab:orange", lw=1.8,
                  label=f"Solarertrag kumuliert ({solar[-1]:.0f} Wh)"))
    _tag(axc.plot(edges, load, color="tab:purple", lw=1.8,
                  label=f"Verbrauch kumuliert ({load[-1]:.0f} Wh)"))
    axc.fill_between(edges, solar, load, where=solar >= load,
                     color="tab:orange", alpha=0.12, lw=0)
    axc.fill_between(edges, solar, load, where=solar < load,
                     color="tab:purple", alpha=0.12, lw=0)
    axc.set_ylabel("kumuliert [Wh]")
    axc.legend(loc="upper left", fontsize=8, framealpha=0.9)
    axc.grid(alpha=0.25)


def morning_plot(mc, state, batt, plt):
    """The next morning's charge window at the overnight stop.

    A plot of its own rather than an appendix to the energy figure: that
    figure has distance on its x axis, and the night plus the morning
    window cover no distance at all. Appended there they would be a jump
    with no width - the same mistake the running totals had.

    What to look for: whether the curve runs into the pack ceiling before
    the start. If it does, the day before ended too full and the difference
    is free energy thrown away, since the morning window costs no race
    time.
    """
    df = mc.trace
    cap = capacity_wh(batt)
    h = _hours(df["time"])

    fig, ax = plt.subplots(figsize=(9.5, 5), layout="constrained")

    ax.axhline(cap, color="tab:blue", lw=1, ls="--")
    ax.text(h[0], cap, f"  Packdeckel {cap:.0f} Wh", va="bottom",
            fontsize=8, color="tab:blue")

    if mc.spilled > 1:
        _tag(ax.plot(h, df["wh_uncapped"], color="tab:red", lw=1.0, ls=":",
                     label=f"ohne Deckel ({mc.spilled:.0f} Wh verworfen)"))
    _tag(ax.plot(h, df["wh"], color="k", lw=2.2, label="Pack"))
    ax.plot([h[0]], [mc.wh_start], "s", ms=8, mfc="w", mec="k", zorder=5)
    ax.annotate(f"Ankunft gestern\n{mc.wh_start:.0f} Wh "
                f"({100*mc.wh_start/cap:.0f} %)",
                (h[0], mc.wh_start), textcoords="offset points",
                xytext=(10, -6), fontsize=8, va="top")

    # Ceiling on sensible arrival energy: above it, part of the free
    # morning window cannot be taken in. Below it nothing is lost - the
    # missing energy arrives free in the morning - so the shaded band is
    # the side to stay off, not a line to hit.
    ax.axhline(mc.wh_max_arrival, color="tab:green", lw=1.2, ls="-.")
    ax.axhspan(mc.wh_max_arrival, cap, color="tab:green", alpha=0.07, lw=0)
    ax.text(h[-1], mc.wh_max_arrival,
            f"max. Ankunft ohne Verlust {mc.wh_max_arrival:.0f} Wh "
            f"({100*mc.wh_max_arrival/cap:.0f} %)  ",
            ha="right", va="bottom", fontsize=8, color="tab:green")

    axp = ax.twinx()
    axp.fill_between(h, 0, df["p_solar"], color="tab:orange", alpha=0.18,
                     lw=0)
    _tag(axp.plot(h, df["p_solar"], color="tab:orange", lw=1.2,
                  label="PV-Leistung, nachgefuehrt"))
    axp.set_ylabel("PV-Leistung [W]", color="tab:orange", fontsize=9)
    axp.tick_params(axis="y", colors="tab:orange", labelsize=8)
    axp._y_squeeze = 2.6
    axp._x_kind = "h"

    from matplotlib.ticker import FuncFormatter
    ax.xaxis.set_major_formatter(FuncFormatter(_hhmm))
    ax.set_xlabel("Tageszeit [SAST] am Folgetag")
    ax.set_ylabel("Energie im Pack [Wh]")
    ax._y_min = 0.0
    ax._x_kind = "h"
    ax.set_title(
        f"Morgenfenster Tag {state.day + 1} · "
        f"{mc.t_release:%H:%M}-{mc.t_start:%H:%M} · "
        f"angeboten {mc.offered:.0f} Wh, aufgenommen {mc.absorbed:.0f} Wh"
        + (f", verworfen {mc.spilled:.0f} Wh" if mc.spilled > 1 else ""),
        loc="left", fontsize=10)
    ax.legend(loc="lower right", fontsize=8, framealpha=0.9)
    ax.grid(alpha=0.25)
    _autoscale_y_on_zoom(fig)
    return fig


def speed_plot(opt, state, plt):
    """Target speed against both ceilings.

    Three different things that are easy to confuse:

      Soll        what the plan asks the driver to do
      v_route     Valhalla's ESTIMATE of the achievable average, including
                  traffic and towns. A guess, not a rule.
      v_limit     the posted legal limit. A hard rule - and missing from
                  most of the compiled routes, which is why it can only be
                  drawn where it exists rather than used as a constraint.

    The plot exists to find nonsense in v_route: a 5 km/h segment at a
    junction costs real minutes in the plan, and the only way to judge it
    is to see it in context.
    """
    tr = opt.trace
    d = tr[tr["kind"] == "drive"]
    km = d["km_total"].to_numpy()

    fig, ax = plt.subplots(figsize=(12, 5.5), layout="constrained")

    ax.axhline(V_FLOOR_KMH, color="tab:red", lw=1, ls="--")
    ax.text(km[0], V_FLOOR_KMH, f" Penalty-Grenze {V_FLOOR_KMH:.0f} km/h "
            f"(wo Limit ≥ {V_FLOOR_APPLIES_AT:.0f})", color="tab:red",
            fontsize=7.5, va="bottom")

    v_route = d["v_route"].to_numpy()
    v_limit = d["v_limit"].to_numpy()
    v_soll = d["speed_kmh"].to_numpy()

    _tag(ax.step(km, v_route, where="post", color="tab:blue", lw=1.0,
                 alpha=0.65, label="v_route (Valhalla-Schaetzung)"))
    if np.isfinite(v_limit).any():
        n_have = int(np.isfinite(v_limit).sum())
        _tag(ax.step(km, v_limit, where="post", color="tab:green", lw=1.2,
                alpha=0.8,
                label=f"v_limit (gesetzlich, nur {100*n_have/len(v_limit):.0f} % "
                      f"der Segmente)"))
    else:
        ax.text(0.5, 0.95, "keine v_limit-Daten in dieser Route",
                transform=ax.transAxes, ha="center", va="top", fontsize=8,
                color="tab:green")

    _tag(ax.step(km, v_soll, where="post", color="k", lw=1.6,
                 label=f"Soll (Ø {opt.avg_kmh:.1f} km/h)"))

    # Where the routing estimate is what holds the plan back. Note that
    # Soll can never EXCEED v_route - apply_speed_limit() clamps it - so
    # this marks where the two touch, i.e. where the plan would go faster
    # if the estimate allowed. Everywhere else the plan is slower by
    # choice, because there is time to spare.
    at_cap = v_soll >= v_route - 0.05
    if at_cap.any():
        ax.fill_between(km, 0, v_soll, where=at_cap, color="tab:blue",
                        alpha=0.13, lw=0, step="post",
                        label=f"Soll am Deckel: v_route begrenzt hier "
                              f"({opt.capped_km:.0f} km"
                              + (f", {_fmt(opt.capped_cost)}"
                                 if opt.capped_cost else "") + ")")
    below = (v_soll < V_FLOOR_KMH - 1.0) & (v_route >= V_FLOOR_APPLIES_AT)
    if below.any():
        ax.fill_between(km, 0, v_soll, where=below, color="tab:red",
                        alpha=0.2, lw=0, step="post",
                        label=f"unter der Penalty-Grenze "
                              f"({opt.below_floor_km:.1f} km)")

    for _, r in tr[tr["kind"] == "stop"].iterrows():
        ax.axvline(r["km_total"], color="grey", lw=3, alpha=0.3)

    ax.set_xlabel("Strecke ab dem aktuellen Punkt [km]")
    ax.set_ylabel("Geschwindigkeit [km/h]")
    ax._y_squeeze = 1.22
    ax._x_kind = "km"
    ax.set_title(f"Tag {state.day} · {opt.n_loops} Loop(s) · Soll gegen die "
                 f"beiden Obergrenzen", loc="left", fontsize=10)
    ax.legend(loc="upper right", fontsize=8, framealpha=0.92, ncols=2)
    ax.grid(alpha=0.25)
    _autoscale_y_on_zoom(fig)
    return fig


# -------------------------------------------------------------------- utils ----

def _attach_cursors(figs) -> bool:
    """Click a curve to read off its value. Optional dependency.

    Only worth wiring up for an interactive window, and only for the
    curves that carry data - the same set the y autoscaler uses, marked
    with gid 'ys'. Missing mplcursors is not an error: the plots are
    complete without it, so this reports once and moves on.
    """
    try:
        import mplcursors
    except ImportError:
        log.info("mplcursors nicht installiert - keine Werteanzeige beim "
                 "Klick auf eine Kurve. Mit 'pip install mplcursors'.")
        return False

    lines = [ln for _, fig in figs for ax in fig.axes
             for ln in ax.get_lines() if ln.get_gid() == "ys"]
    if not lines:
        return False

    cur = mplcursors.cursor(lines, hover=False, multiple=False)

    @cur.connect("add")
    def _(sel):
        sel.annotation.set_text(
            cursor_text(sel.artist, sel.target[0], sel.target[1]))
        sel.annotation.get_bbox_patch().set(fc="white", alpha=0.92)

    return True


def cursor_text(artist, x: float, y: float) -> str:
    """The label shown when a curve is clicked.

    Split out from the mplcursors callback so it can be checked without a
    GUI event: the wiring is one line, the formatting is where mistakes
    live - a distance read as a clock time, or a value with the wrong unit.
    """
    ax = artist.axes
    name = artist.get_label().lstrip("_")
    # distance on some plots, clock on others. Reading that off the axis
    # label would break the moment a label is reworded, so the plots tag
    # their axes with _x_kind instead.
    if getattr(ax, "_x_kind", "km") == "h":
        xs = f"{int(x) % 24:02d}:{int(round((x % 1) * 60)) % 60:02d}"
    else:
        xs = f"km {x:.2f}"
    unit, lab = "", ax.get_ylabel()
    if "[" in lab and "]" in lab:
        unit = " " + lab[lab.index("[") + 1:lab.index("]")]
    return (f"{name}\n" if name else "") + f"{xs}\n{y:,.1f}{unit}"


def _autoscale_y_on_zoom(fig):
    """Rescale every y axis whenever the x range changes.

    Without this, zooming into 50 km of a 350 km day leaves the y axes at
    their full-day limits: the pack curve becomes a flat line at the top of
    the frame and the load curve runs off it. The data is there, it is just
    unreadable, which defeats the point of an interactive window.

    Only artists tagged with gid 'ys' are considered. Axis-spanning
    decoration - the pack ceiling, the usable floor, the penalty line -
    would otherwise dominate every range and pin the limits open.

    An axes may carry `_y_squeeze`: a factor that leaves headroom above the
    data, used to keep the terrain profile in the lower part of the pack
    panel instead of filling it.
    """
    def make(ax):
        def cb(a):
            x0, x1 = a.get_xlim()
            lo, hi = np.inf, -np.inf
            for ln in a.get_lines():
                if ln.get_gid() != "ys":
                    continue
                x = np.asarray(ln.get_xdata(), dtype=float)
                y = np.asarray(ln.get_ydata(), dtype=float)
                m = (x >= x0) & (x <= x1) & np.isfinite(y)
                if m.any():
                    lo, hi = min(lo, y[m].min()), max(hi, y[m].max())
            if not np.isfinite(lo):
                return
            if hi <= lo:
                lo, hi = lo - 1.0, hi + 1.0
            # an axes may pin its lower bound: on the pack panel, seeing
            # the axis reach zero is what distinguishes "low" from "empty"
            pin = getattr(a, "_y_min", None)
            if pin is not None:
                lo = min(lo, pin)
            r = hi - lo
            squeeze = getattr(a, "_y_squeeze", 1.0)
            bottom = pin if pin is not None else lo - 0.08 * r
            a.set_ylim(bottom, hi + 0.08 * r + (squeeze - 1.0) * r)
        return cb

    for ax in fig.axes:
        ax.callbacks.connect("xlim_changed", make(ax))
        make(ax)(ax)                    # same rule for the initial view


def _tag(artists):
    """Mark artists as the ones that define the y range. See above."""
    for a in np.atleast_1d(artists):
        if hasattr(a, "set_gid"):
            a.set_gid("ys")
    return artists


def _stop_blocks(tr) -> "pd.DataFrame":
    """One row per standing phase, not one per panel state.

    evaluate() splits every halt into a tracked and a flat part, because
    the PV power differs by tens of percent between them and the energy
    has to be right. For LABELLING that split is noise: two markers land
    on the same spot and two captions print on top of each other. So the
    plots aggregate back to one block per halt, with the total duration
    and the total energy.
    """
    s = tr[tr["kind"] == "stop"]
    if s.empty:
        return s
    blocks = (s["leg"] != s["leg"].shift()).cumsum()
    return s.groupby(blocks, as_index=False).agg(
        leg=("leg", "first"), time=("time", "first"),
        dt_s=("dt_s", "sum"), Ws=("Ws", "sum"),
        km_total=("km_total", "first"),
        wh_remaining=("wh_remaining", "last"))


def _hhmm(v: float, _=None) -> str:
    """Decimal hours as a clock time. The axes carry hours as plain numbers
    so that ticks land where the day is divided; this puts the colon back
    for the reader."""
    return f"{int(v) % 24:02d}:{int(round((v % 1) * 60)) % 60:02d}"


def _nice_hour_step(span_h: float) -> float:
    """Tick spacing in hours for a visible time span."""
    for limit, step in ((4.0, 1.0), (1.5, 0.5), (0.6, 0.25), (0.0, 1 / 12)):
        if span_h > limit:
            return step
    return 1 / 12


def _add_time_axis(ax, tr, km):
    """Clock on top of a distance axis, kept in sync with any zoom.

    A twiny rather than secondary_xaxis: secondary_xaxis follows the zoom
    by itself, but in a three-panel figure with constrained_layout it drives
    the layout solver into "axes sizes collapsed to zero" and the figure
    comes out mangled. A twiny lays out cleanly; it just does not know
    about the zoom, so its ticks are recomputed on every xlim change.

    The mapping is the plan's own km-to-time relation, interpolated - which
    is why the hour marks bunch up where the car stands still.
    """
    h = _hours(tr["time"])
    if len(h) < 2:
        return None

    top = ax.twiny()
    top.set_xlabel("Tageszeit [SAST]", fontsize=9)
    top.tick_params(axis="x", labelsize=8)

    def sync(_=None):
        x0, x1 = ax.get_xlim()
        top.set_xlim(x0, x1)
        h0, h1 = np.interp([x0, x1], km, h)
        if not np.isfinite(h0) or h1 <= h0:
            return
        step = _nice_hour_step(h1 - h0)
        marks = np.arange(np.ceil(h0 / step) * step, h1 + 1e-9, step)
        if len(marks) == 0:
            return
        top.set_xticks(np.interp(marks, h, km))
        top.set_xticklabels(
            [f"{int(v) % 24:02d}:{int(round((v % 1) * 60)) % 60:02d}"
             for v in marks])

    sync()
    ax.callbacks.connect("xlim_changed", sync)
    return top


def _smooth_over_km(km, y, window_km=2.0):
    """Box mean of y over distance, on a uniform grid.

    Smoothing over the sample index would weight a 10 m segment like a
    500 m one; the route has both. So resample onto an even grid first,
    then average - the result is a mean per kilometre, which is what the
    eye is looking for in a power trace.
    """
    if len(km) < 4:
        return km, y
    step = 0.05
    grid = np.arange(km[0], km[-1] + step, step)
    yi = np.interp(grid, km, y)
    n = max(int(window_km / step), 1)
    kern = np.ones(n) / n
    sm = np.convolve(yi, kern, mode="same")
    # the box mean runs off the ends, so trim half a window on each side
    half = n // 2
    return (grid[half:-half], sm[half:-half]) if len(grid) > n else (grid, sm)


def _fmt(td: timedelta) -> str:
    if td is None:
        return "-"
    s = int(td.total_seconds())
    return f"{s//3600}:{(s%3600)//60:02d}"


def render(opt, state, batt, png_prefix: str = None,
           show: bool = True, cursors: bool = False, morning=None) -> list:
    """Both displays. Interactive window, PNG files, or both.

    One plt.show() at the end rather than one per figure: show() blocks
    until the windows are closed, so calling it inside each plot would
    force them to be looked at one after the other instead of side by side.

    The loop count goes into the filename because at a control stop several
    options get plotted within a few minutes, and two files called plan.png
    are worse than none.
    """
    if opt.trace is None:
        raise ValueError("keine Trace - Option war nicht machbar")
    plt = _plt(interactive=show)

    if show:
        try:
            can, backend = _backend_can_show()
        except Exception as e:
            # never let the check kill the plot it is checking
            log.debug("Backend-Pruefung fehlgeschlagen: %r", e)
            can, backend = True, "unbekannt"
        if not can:
            log.warning(
                "matplotlib-Backend %r kann kein Fenster oeffnen - es wird "
                "stattdessen PNG geschrieben. Fuer Fenster ein GUI-Backend "
                "installieren (Windows: 'pip install pyqt5', oder Python mit "
                "tkinter) oder MPLBACKEND setzen.", backend)
            show = False
            png_prefix = png_prefix or "strategie"

    figs = [("soc", soc_plot(opt, state, batt, plt)),
            ("strecke_zeit", st_plot(opt, state, plt)),
            ("geschwindigkeit", speed_plot(opt, state, plt))]
    if morning is not None:
        figs.append(("morgenfenster",
                     morning_plot(morning, state, batt, plt)))

    paths = []
    if png_prefix:
        stem = f"{png_prefix}_tag{state.day}_{opt.n_loops}loops"
        for name, fig in figs:
            fn = f"{stem}_{name}.png"
            fig.savefig(fn, dpi=130)
            paths.append(fn)

    if show:
        if cursors:
            _attach_cursors(figs)
        plt.show()
    else:
        for _, fig in figs:
            plt.close(fig)
    return paths
