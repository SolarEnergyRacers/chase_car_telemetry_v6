"""Assemble the rest of a race day and evaluate it.

One primitive, `evaluate()`, answers "can this be driven, and what does it
cost". Everything else is a loop over it: the options table asks it once per
loop count, plan mode asks it once for the count the strategist picked.

The speed allocation is not solved here - `apply_speed_limit()` already does
the water-filling that spreads a time budget over segments with different
caps, and reusing it means the plan and the energy model see the identical
speeds. What this module adds is the bookkeeping that has no route segment:
the control stop, the loop stops, the stop-start losses, and the standing
charge. Those are exactly the terms `total_Ws_for_lap()` cannot see, and
leaving them out is worth several hundred Wh over a day.

Not solved here either: WHERE to stand and for how long. That is the mu
fixpoint (see the strategy notes, section 3) and comes later. For now a
standing phase is something the caller asks for explicitly.
"""

from   dataclasses import dataclass, field, replace
from   datetime import datetime, timedelta
import logging as lg

import numpy as np
import pandas as pd

from ..environment.environment import RouteWeather
from ..simulation.battery import capacity_wh, soc_from_wh, soc_trace
from ..simulation.driving import (
    Ws_for_stop, Ws_for_stop_start, apply_speed_limit, total_Ws_for_lap)
from ..ser_dataclasses import Battery_coeffs, Car_coeffs

log = lg.getLogger(__name__)

# Standing times that are not route segments: the mandatory halt at the
# control stop, and the short halt each time the loop is re-entered.
#
# Deliberately above the regulated minimum. These are the numbers the team
# is confident of HOLDING, and a plan that assumes the theoretical minimum
# turns every ordinary delay into a lost loop. Being 5 and 3 minutes
# generous costs about half a loop on a long day and buys a plan that
# survives contact with a parking lot.
CONTROL_STOP = timedelta(minutes=35)
LOOP_STOP    = timedelta(minutes=8)

# How much of each halt the panel is actually aimed at the sun. The
# regulated minimum (30 min at the control stop, 5 min at a loop break) is
# the charging time; the rest of the halt goes on getting the car ready to
# leave, with the panel flat. Treating the whole halt as tracked
# overestimates a control stop by roughly a tenth and a loop break by more
# than a third.
CONTROL_STOP_TRACKED = timedelta(minutes=28)
LOOP_STOP_TRACKED    = timedelta(minutes=5)

# A driver may not sit at the wheel for more than two hours at a stretch.
# The change costs five minutes and the panel stays flat - nobody sets it
# up for five minutes. Any other halt also counts as a break, so the
# counter restarts at the control stop and at every loop break.
#
# Not a parameter: two hours is the rule, not a setting. Exposing it as a
# knob would invite tuning the plan by relaxing a limit that cannot be
# relaxed.
MAX_DRIVE_TIME     = timedelta(hours=2)
DRIVER_CHANGE      = timedelta(minutes=5)
DRIVER_CHANGE_LABEL = "Fahrerwechsel"

# Regulation floor: driving below 50 km/h where the limit is 100 km/h or
# more is a penalty item. `speed_limit` from Valhalla is unusable (missing
# at 826 of 1088 points on manual_day1), so the routing speed stands in for
# it - which is why this only WARNS and never constrains. Acting on a proxy
# would be worse than reporting it.
V_FLOOR_KMH        = 50.0
V_FLOOR_APPLIES_AT = 100.0
# 1 km/h of slack: a plan that works out to 49.7 km/h is at the floor, not
# below it, and warning about it would train everyone to ignore the warning.
V_FLOOR_TOL_KMH    = 1.0


# ------------------------------------------------------------ route slice ----

def _assumed_limit(route: pd.DataFrame) -> np.ndarray:
    """Legal limit assumed from road_class, per segment.

    Only where OSM has no maxspeed - 64 % of the race distance, and the
    long Karoo stages have none at all. An ASSUMPTION: shown in the plots
    so the gap to v_route is visible, never used for the penalty warning,
    because a guess must not become a legal basis.
    """
    n = len(route) - 1
    if "road_class" not in route.columns:
        return np.full(n, np.nan)
    from ..roadinfo.roadinfo_api import ASSUMED_LIMIT_KMH
    rc = route["road_class"].to_numpy()[:n]
    return np.array([ASSUMED_LIMIT_KMH.get(c, np.nan) for c in rc],
                    dtype=float)


def route_from(route: pd.DataFrame, distance_m: float) -> pd.DataFrame:
    """The remainder of a route from `distance_m` onwards, re-indexed to 0.

    The mirror image of compile_route.truncate_route(): that one cuts the
    tail off, this one cuts the head off. Needed because a plan started
    mid-stage must not pay for the kilometres already driven.

    The leading partial segment is interpolated, and its climb/descent are
    scaled by the same fraction - dropping them would quietly return energy
    the car still has to spend, and the vertical term is asymmetric (2507 J
    per metre up against 1692 J back), so the error only ever goes one way.
    """
    if distance_m <= 0:
        return route.copy()
    if distance_m >= route.index[-1]:
        raise ValueError(
            f"distance {distance_m:.0f} m is at or past the end of the route "
            f"({route.index[-1]:.0f} m)")

    keep = route.index >= distance_m
    if keep.all():                          # exactly at the first node
        out = route.copy()
        return out.set_index(out.index - out.index[0])

    i_next = int(np.argmax(keep))           # first node kept
    prev   = route.iloc[i_next - 1]
    nxt    = route.iloc[i_next]
    d_prev = float(route.index[i_next - 1])
    d_next = float(route.index[i_next])

    out = route[keep].copy()

    # Only interpolate a new first node when the cut falls strictly INSIDE
    # a segment. Cutting exactly on a node used to prepend a duplicate of
    # it, giving a segment of zero length - and a zero-length segment
    # divides a finite energy by a vanishing time, so p_motor came out NaN
    # and poisoned the whole SoC trace from there on. _splice_stops() snaps
    # halts to nodes, so this is the common case, not an edge case.
    if d_prev < distance_m < d_next:
        # build the new first node by interpolating position/altitude, and
        # give it the remaining part of the segment it sits inside
        frac = (distance_m - d_prev) / (d_next - d_prev)
        start = prev.copy()
        for col in ("longitude", "latitude", "altitude"):
            start[col] = prev[col] + frac * (nxt[col] - prev[col])
        start["azimuth"]  = prev["azimuth"]          # same edge, same heading
        start["distance"] = (1.0 - frac) * prev["distance"]
        for col in ("speed_route", "speed_limit"):
            if col in route.columns:
                start[col] = prev[col]
        for col in ("climb", "descent"):
            if col in route.columns:
                start[col] = (1.0 - frac) * prev[col]
        out = pd.concat([pd.DataFrame([start], index=[distance_m]), out])

    return out.set_index(out.index - out.index[0])


# ----------------------------------------------------------------- legs ----

@dataclass
class StopSpec:
    """A standing phase the caller asked for, or one the rules force.

    Position is given either as `km` from the current point, or as `at_time`
    - a wall-clock moment, which can only be turned into a distance once a
    plan exists (see evaluate()'s iteration).

    A NEGATIVE km counts back from the end of the day, so "5 km before the
    finish" is km=-5.0 and stays correct whatever loop count is being
    evaluated. Resolved in build_legs(), where the total is known.

    `tracked_min` splits the halt: that many minutes with the panel aimed at
    the sun, the rest lying flat. None means the whole halt is tracked.
    """
    km: float = None
    minutes: float = 30.0
    label: str = "Standladen"
    tracked_min: float = None
    at_time: datetime = None

    @property
    def duration(self) -> timedelta:
        return timedelta(minutes=self.minutes)

    @property
    def tracked_for(self) -> timedelta:
        if self.tracked_min is None:
            return self.duration
        return timedelta(minutes=min(self.tracked_min, self.minutes))


def _as_stop_specs(items) -> list:
    """Accept StopSpec, or the plain (km, minutes) pairs the CLI parses."""
    out = []
    for it in items or []:
        if isinstance(it, StopSpec):
            out.append(it)
        else:
            km, minutes = it
            out.append(StopSpec(km=float(km), minutes=float(minutes)))
    return out


@dataclass
class Leg:
    """One piece of the remaining day."""
    name: str
    kind: str                       # 'drive' | 'stop'
    route: pd.DataFrame = None      # for 'drive'
    weather: RouteWeather = None
    duration: timedelta = None      # for 'stop'
    tracked_for: timedelta = None   # for 'stop': how much of it is tracked
    lat: float = None               # for 'stop'
    lon: float = None

    @property
    def km(self) -> float:
        return 0.0 if self.route is None else float(self.route.index[-1]) / 1e3

    @property
    def flat_for(self) -> timedelta:
        """Part of the halt with the panel lying flat.

        The regulated halt and the useful charging time are not the same
        thing: at a loop break the panel is aimed for its five regulated
        minutes and lies flat for the rest while the car is made ready to
        leave; at a control stop about 28 of the 35 minutes are tracked. A
        driver change is flat throughout - nobody sets up a panel for five
        minutes.
        """
        if self.duration is None:
            return timedelta(0)
        t = self.tracked_for if self.tracked_for is not None else self.duration
        return max(self.duration - t, timedelta(0))


def build_legs(state, parts: dict, n_loops: int,
               extra_stops: list = None) -> list:
    """Order the remaining legs of the day for a given loop count.

    `parts` maps 'to_control' / 'loop' / 'to_finish' to (route, weather)
    pairs, as produced by load_day(). `n_loops` counts loops still to be
    driven, not loops in total.

    A day can be entered at three points, and the sequence differs for each:
    before the control stop the mandatory stop is still ahead, on a loop the
    remainder of the current loop is fixed and cannot be optimised away, and
    after the control stop only the run to the finish is left.
    """
    legs = []

    if state.part == "to_control":
        route, weather = parts["to_control"]
        legs.append(Leg("ToControlStop", "drive",
                        route_from(route, state.km_in_part * 1e3), weather))
        legs.append(Leg("Kontrollstopp", "stop", duration=CONTROL_STOP,
                        tracked_for=CONTROL_STOP_TRACKED))
        _append_loops(legs, parts, n_loops)
        _append_to_finish(legs, parts)

    elif state.part == "loop":
        route, weather = parts["loop"]
        # the rest of the loop currently being driven is mandatory
        legs.append(Leg("Loop (laufend, Rest)", "drive",
                        route_from(route, state.km_in_part * 1e3), weather))
        legs.append(Leg("Loopstopp", "stop", duration=LOOP_STOP,
                        tracked_for=LOOP_STOP_TRACKED))
        _append_loops(legs, parts, n_loops)
        _append_to_finish(legs, parts)

    elif state.part == "to_finish":
        route, weather = parts["to_finish"]
        legs.append(Leg("FromControlStop", "drive",
                        route_from(route, state.km_in_part * 1e3), weather))

    else:
        raise ValueError(f"unknown part {state.part!r}")

    specs = _as_stop_specs(extra_stops)
    if specs:
        total = sum(l.km for l in legs if l.kind == "drive")
        resolved = []
        for s in specs:
            if s.km is None:
                continue                      # time-based, not placed yet
            km = s.km if s.km >= 0 else total + s.km
            if not 0.0 < km < total:
                log.warning("Stopp bei km %.1f liegt ausserhalb der "
                            "Reststrecke (0..%.1f) - ignoriert", km, total)
                continue
            resolved.append(replace(s, km=km))
        if resolved:
            legs = _splice_stops(legs, resolved)
    return legs


def _append_loops(legs: list, parts: dict, n_loops: int) -> None:
    if n_loops <= 0:
        return
    if "loop" not in parts:
        raise SystemExit("this day has no loop route (not published yet?) - "
                         "cannot plan loops")
    route, weather = parts["loop"]
    for k in range(n_loops):
        legs.append(Leg(f"Loop {k+1}", "drive", route, weather))
        legs.append(Leg(f"Loopstopp {k+1}", "stop", duration=LOOP_STOP,
                        tracked_for=LOOP_STOP_TRACKED))


def _append_to_finish(legs: list, parts: dict) -> None:
    if "to_finish" not in parts:
        # day 6: the control stop IS the nightly stop, there is no run out
        log.info("no 'to_finish' stage for this day - the control stop is "
                 "the end of the day")
        return
    route, weather = parts["to_finish"]
    legs.append(Leg("FromControlStop", "drive", route, weather))


def _splice_stops(legs: list, specs: list) -> list:
    """Insert standing phases at given distances from the current position.

    The bookkeeping detail that matters: after a leg is split, `leg` is the
    REMAINDER and its route index restarts at zero. A second halt in the
    same leg must therefore be measured from the last cut, not from the
    original leg start - otherwise it lands near km 0 of the remainder and
    the placement collapses into a cluster.
    """
    out, done = [], 0.0
    specs = sorted(specs, key=lambda s: s.km)
    for leg in legs:
        if leg.kind != "drive":
            out.append(leg)
            continue
        leg_start, leg_end = done, done + leg.km
        done = leg_end
        cut = 0.0                       # km already split off this leg
        for s in [x for x in specs if leg_start < x.km < leg_end]:
            if leg is None or len(leg.route) < 3:
                break
            # Snap to the node AT OR BEFORE the requested position, clamped
            # so both halves keep at least two nodes. Spacing here is
            # 50-150 m, so the position error is negligible - and it removes
            # every degenerate case: no interpolated stub, no distance lost
            # off the front, no index past the end.
            #
            # Before and not after, because a driver change is placed at the
            # point where two hours of driving are already up. Snapping
            # forward would put the halt just past that point, leaving the
            # overrun in place - and the placement loop would then ask for
            # the same kilometre again, for ever.
            i = int(np.searchsorted(leg.route.index,
                                    (s.km - leg_start - cut) * 1e3,
                                    side="right")) - 1
            i = min(max(i, 1), len(leg.route) - 2)
            local = float(leg.route.index[i])

            head = leg.route[leg.route.index <= local].copy()
            head.iloc[-1, head.columns.get_indexer(
                ["azimuth", "distance"])] = np.nan
            out.append(Leg(leg.name, "drive", head, leg.weather))

            node = leg.route.iloc[i]
            at_km = leg_start + cut + local / 1e3
            out.append(Leg(f"{s.label} km {at_km:.1f}", "stop",
                           duration=s.duration, tracked_for=s.tracked_for,
                           lat=float(node["latitude"]),
                           lon=float(node["longitude"])))
            leg = Leg(f"{leg.name} (nach Stopp)", "drive",
                      route_from(leg.route, local), leg.weather)
            cut += local / 1e3
        if leg is not None:
            out.append(leg)
    return out


# ------------------------------------------------------------- evaluation ----

@dataclass
class DayOption:
    """Result of evaluating one loop count."""
    n_loops: int
    feasible: bool
    reason: str = ""                     # why not, if infeasible
    km: float = 0.0
    drive_time: timedelta = None
    t_min: timedelta = None              # flat out, all segments at their cap
    reserve: timedelta = None            # deadline minus t_min
    avg_kmh: float = 0.0
    min_soc: float = None
    end_soc: float = None
    end_wh: float = None
    wh_above_floor: float = None
    trace: pd.DataFrame = None
    speeds_kmh: np.ndarray = None
    legs: list = field(default_factory=list)
    below_floor_km: float = 0.0          # km below the 50 km/h regulation floor
    capped_km: float = 0.0               # km where the routing cap binds
    capped_cost: timedelta = None        # time those segments cost
    wh_spilled: float = 0.0              # solar thrown away at a full pack
    t_full: object = None                # when the pack first hits the cap
    caps_kmh: np.ndarray = None          # routing cap per segment
    seg_m: np.ndarray = None             # segment lengths, m
    driver_changes: list = field(default_factory=list)   # placed StopSpecs
    stop_time: timedelta = None          # total standing time
    stand_extra: timedelta = None        # leftover time parked before the line
    floor_released: bool = False         # 50 km/h given up to reach the finish

    def wh_loss_note(self) -> str:
        """One line about the pack overflowing, for a plot title."""
        if self.wh_spilled <= 5:
            return ""
        return (f"{self.wh_spilled:.0f} Wh verworfen "
                f"(≈ {self.wh_spilled/14:.0f} km)")


def evaluate(state, parts: dict, n_loops: int, car: Car_coeffs,
             batt: Battery_coeffs, extra_stops: list = None,
             arrive_early: timedelta = timedelta(0),
             driver_changes: list = None,
             auto_driver_change: bool = True,
             v_floor: bool = True) -> DayOption:
    """Plan the rest of the day, keeping to the 50 km/h floor if possible.

    Two passes at most. The first holds the regulation floor: leftover time
    is parked before the finish line instead of being spread over the route
    as a crawl. If that plan runs out of energy, the floor is given up -
    driving below 50 costs a penalty, not arriving costs the classification
    - and the result says so.
    """
    opt = _evaluate_with_changes(state, parts, n_loops, car, batt,
                                 extra_stops, arrive_early, driver_changes,
                                 auto_driver_change, v_floor=v_floor)
    if v_floor and not opt.feasible and opt.reason.startswith("Energie"):
        log.info("50-km/h-Boden aufgegeben: mit ihm reicht die Energie nicht")
        alt = _evaluate_with_changes(state, parts, n_loops, car, batt,
                                     extra_stops, arrive_early,
                                     driver_changes, auto_driver_change,
                                     v_floor=False)
        alt.floor_released = True
        if alt.feasible:
            return alt
        return alt if alt.trace is not None else opt
    return opt


def _evaluate_with_changes(state, parts: dict, n_loops: int,
                           car: Car_coeffs, batt: Battery_coeffs,
                           extra_stops, arrive_early, driver_changes,
                           auto_driver_change, v_floor: bool) -> DayOption:
    """Can the rest of the day be driven with `n_loops` loops still to go?

    The plan uses the WHOLE remaining time window. That is deliberate: for a
    fixed distance, the slowest legal plan is the cheapest one, so if the
    battery survives at maximum time it survives at all - and if it does not,
    no faster plan will save it. One evaluation therefore settles both
    questions.

    Driver changes make this circular: how many are needed depends on the
    driving time, which depends on how much standing time they add. So the
    plan is solved, the changes are placed from the resulting timeline, and
    the plan is solved again until the set of positions stops moving. Two or
    three passes in practice; capped at five.
    """
    fixed = _as_stop_specs(extra_stops) + _as_stop_specs(driver_changes)
    placed = []                       # auto driver changes, and timed stops
    timed_done = False

    # Add one break at a time and re-solve, rather than re-deriving the
    # whole set each pass. Re-deriving oscillates: a change placed exactly
    # at the two-hour mark resets the counter, so the next pass wants it
    # somewhere else, and the pass after that wants it back. Only ever
    # adding is monotone and terminates.
    for _ in range(12):
        opt = _evaluate_once(state, parts, n_loops, car, batt,
                             fixed + placed, arrive_early, v_floor)
        if not opt.feasible or opt.trace is None:
            break
        if not timed_done:
            timed = _resolve_timed(fixed, opt)
            timed_done = True
            if timed:
                placed += timed
                continue
        if not auto_driver_change:
            break
        km = _first_overrun(opt)
        if km is None:
            break
        if any(abs(s.km - km) < 0.5 for s in placed
               if s.label == DRIVER_CHANGE_LABEL):
            log.warning("Fahrerwechsel bei km %.1f behebt den Ueberlauf "
                        "nicht - Platzierung abgebrochen", km)
            break
        placed.append(StopSpec(
            km=km, minutes=DRIVER_CHANGE.total_seconds() / 60,
            label=DRIVER_CHANGE_LABEL, tracked_min=0.0))
    else:
        log.warning("mehr als 12 Fahrerwechsel noetig - abgebrochen")

    # report the changes the caller asked for as well as the automatic
    # ones, otherwise --driver-change looks like it was ignored
    opt.driver_changes = [s for s in (fixed + placed)
                          if s.label == DRIVER_CHANGE_LABEL
                          and s.km is not None]
    return opt


def _first_overrun(opt: DayOption):
    """First km at which continuous driving exceeds MAX_DRIVE_TIME, else None.

    Any halt counts as a break, so the counter restarts at the control
    stop, at every loop break and at every charging stop - which is why
    this reads the finished timeline instead of the distance.
    """
    run, prev_km = 0.0, 0.0
    limit = MAX_DRIVE_TIME.total_seconds()
    for _, r in opt.trace.iterrows():
        if r["kind"] == "stop":
            run = 0.0
            prev_km = float(r["km_total"])
            continue
        run += float(r["dt_s"])
        if run > limit + 1.0:
            # The kilometre BEFORE the offending segment, not its end.
            # _splice_stops() snaps a halt to the node at or before the
            # requested position, and km_total is the segment's END - so
            # returning it would put the halt after the segment that broke
            # the limit, leave the overrun in place, and make the placement
            # loop ask for the same kilometre for ever.
            return prev_km
        prev_km = float(r["km_total"])
    return None


def _resolve_timed(specs: list, opt: DayOption) -> list:
    """Turn time-specified stops into distance-specified ones.

    Needs a plan to know where the car is at a given moment, which is why
    this runs inside evaluate()'s iteration rather than in build_legs().
    """
    out = []
    tr = opt.trace
    for s in specs:
        if s.at_time is None:
            continue
        t = np.array([x.timestamp() for x in tr["time"]])
        km = float(np.interp(s.at_time.timestamp(), t,
                             tr["km_total"].to_numpy()))
        out.append(replace(s, km=km, at_time=None))
    return out


def _evaluate_once(state, parts: dict, n_loops: int, car: Car_coeffs,
                   batt: Battery_coeffs, stops: list,
                   arrive_early: timedelta,
                   v_floor: bool = True) -> DayOption:
    """One pass: fixed set of standing phases, one speed allocation."""
    legs = build_legs(state, parts, n_loops, stops)
    drive_legs = [l for l in legs if l.kind == "drive"]
    stop_time = sum((l.duration for l in legs if l.kind == "stop"),
                    timedelta(0))

    window = (state.t_deadline - arrive_early) - state.t_now
    t_drive = window - stop_time
    km = sum(l.km for l in drive_legs)
    opt = DayOption(n_loops=n_loops, feasible=True, km=km, legs=legs,
                    stop_time=stop_time)

    if t_drive.total_seconds() <= 0:
        opt.feasible = False
        opt.reason = (f"Zeit: Standzeiten allein ({stop_time}) fuellen das "
                      f"Restfenster ({window})")
        return opt

    # one global speed allocation over every driving segment of the day,
    # then split it back per leg. Water filling is separable, so a leg
    # re-solved with its own share of the time gets the same speeds - which
    # is what keeps total_Ws_for_lap() consistent with this allocation.
    dists = np.concatenate([l.route["distance"].to_numpy()[:-1]
                            for l in drive_legs])
    caps = np.concatenate([l.route["speed_route"].to_numpy()[:-1]
                           for l in drive_legs])
    if np.isnan(dists).any() or np.isnan(caps).any():
        raise ValueError("route has NaN in distance/speed_route inside a "
                         "segment - recompile the route")

    t_min = timedelta(seconds=float(np.sum(dists / (caps / 3.6)))) + stop_time
    opt.t_min = t_min
    opt.reserve = window - t_min

    # Keep to the regulation floor instead of spreading the whole window
    # over the route. apply_speed_limit() has no lower bound, so with time
    # to spare it produced 33 km/h over 238 km - below the floor on every
    # segment, and the spare time invisible inside an average speed.
    #
    # Budgeting only the time a >= 50 km/h plan needs makes water filling
    # return exactly min(50, cap) per segment, and what is left becomes a
    # standing phase before the finish line, where the tracked panel is
    # worth most (measured: +152 % over flat at 16:25).
    if v_floor:
        v_low = np.minimum(caps, V_FLOOR_KMH) / 3.6
        t_floor = timedelta(seconds=float(np.sum(dists / v_low)))
        if t_drive > t_floor:
            extra = t_drive - t_floor
            t_drive = t_floor
            # Deliberately not called "Standladen": that name belongs to
            # the halts the strategist asked for with --stop. This one is
            # the time the floor left over, and telling them apart matters
            # both when reading the plan and when filtering the trace.
            legs.append(Leg("Restzeit stehend vor der Ziellinie", "stop",
                            duration=extra, tracked_for=extra))
            stop_time += extra
            opt.stand_extra = extra
            opt.stop_time = stop_time
            opt.legs = legs

    try:
        speeds = apply_speed_limit(dists, caps, t_drive)
    except ValueError:
        opt.feasible = False
        opt.reason = (f"Zeit: {km:.1f} km nicht in {t_drive} fahrbar "
                      f"(Modellgrenzen), fehlen "
                      f"{(t_min - window).total_seconds()/60:.0f} min")
        return opt

    opt.speeds_kmh = speeds * 3.6
    opt.caps_kmh = caps
    opt.seg_m = dists
    opt.drive_time = t_drive
    opt.avg_kmh = km / (t_drive.total_seconds() / 3600.0)

    at_cap = speeds >= (caps / 3.6) - 1e-9
    opt.capped_km = float(np.sum(dists[at_cap])) / 1e3
    if at_cap.any():
        free_v = speeds[~at_cap].mean() if (~at_cap).any() else speeds.mean()
        opt.capped_cost = timedelta(seconds=float(np.sum(
            dists[at_cap] / speeds[at_cap] - dists[at_cap] / free_v)))
    below = ((speeds * 3.6 < V_FLOOR_KMH - V_FLOOR_TOL_KMH)
             & (caps >= V_FLOOR_APPLIES_AT))
    opt.below_floor_km = float(np.sum(dists[below])) / 1e3

    # --- energy, leg by leg, with the stops spliced in where they happen
    frames, t = [], state.t_now
    i0 = 0
    km_run = 0.0        # running distance; the km_total column only exists
                        # after the concat, so a stop cannot read it yet
    for leg in legs:
        if leg.kind == "drive":
            n = len(leg.route) - 1
            leg_speeds = speeds[i0:i0 + n]
            i0 += n
            # speeds, not a time budget: the allocation above is global over
            # the whole day, and handing a leg its share of the time to
            # re-solve is redundant at best.
            detail = total_Ws_for_lap(leg.route, leg.weather, car,
                                      start_time=t, speeds=leg_speeds,
                                      return_detail=True)
            detail = detail.assign(
                leg=leg.name, kind="drive", panel="flat",
                v_route=leg.route["speed_route"].to_numpy()[:-1],
                v_limit=(leg.route["speed_limit"].to_numpy()[:-1]
                         if "speed_limit" in leg.route.columns else np.nan),
                v_limit_est=_assumed_limit(leg.route),
                n_roundabout=(leg.route["n_roundabout"].to_numpy()[:-1]
                              if "n_roundabout" in leg.route.columns else 0),
                n_traffic_signal=(
                    leg.route["n_traffic_signal"].to_numpy()[:-1]
                    if "n_traffic_signal" in leg.route.columns else 0))
            frames.append(detail)
            t = t + timedelta(seconds=float(detail["dt_s"].sum()))
            km_run += leg.km
        else:
            lat, lon = leg.lat, leg.lon
            if lat is None:
                lat, lon = _leg_end_coord(legs, leg)
            weather = _weather_for(legs, leg)
            v_out = speeds[i0] if i0 < len(speeds) else speeds[-1]
            km_now = km_run

            # A halt is split into a tracked part and a flat part, and they
            # are separate rows: the PV power differs by tens of percent
            # between them, and a single averaged row would hide exactly
            # the thing a charging stop is decided on.
            # `is None` and not `or`: timedelta(0) is falsy, so `or` would
            # turn a fully flat halt - a driver change - into a tracked one
            # of the same length PLUS a flat one, doubling the standing time
            tracked = (leg.duration if leg.tracked_for is None
                       else leg.tracked_for)
            first = True
            for dur, panel in ((tracked, "tracked"), (leg.flat_for, "flat")):
                if dur.total_seconds() <= 0:
                    continue
                secs = dur.total_seconds()
                ws_stop = Ws_for_stop(car, weather, t, dur, lat, lon,
                                      tracked=(panel == "tracked"))
                # Braking in and accelerating out again is a MOTOR cost, so
                # it goes in p_motor. Folding it into the number p_solar is
                # derived from made a halt look like it collected less sun
                # than driving at the same moment - 68 W of an 80 km/h
                # stop-start spread over five minutes reads as -7 % on a
                # flat driver change, which is nonsense: flat is flat.
                # It also understated BOTH cumulatives by the same amount,
                # which is exactly the distinction they exist to show.
                ws_ss = Ws_for_stop_start(car, float(v_out)) if first else 0.0
                first = False
                ws = ws_stop + ws_ss
                frames.append(pd.DataFrame({
                    "time": [t],
                    "cum_km": [0.0],
                    "dt_s": [secs],
                    "speed_kmh": [0.0],
                    "altitude_m": [frames[-1]["altitude_m"].iloc[-1]
                                   if frames else np.nan],
                    "p_solar": [-(ws_stop - car.aux_power * secs) / secs],
                    "p_aux": [car.aux_power],
                    "p_motor": [ws_ss / secs],
                    "p_net": [ws / secs],
                    "Ws": [ws],
                    "leg": [leg.name],
                    "kind": ["stop"],
                    "panel": [panel],
                    "v_route": [np.nan],
                    "v_limit": [np.nan],
                    "v_limit_est": [np.nan],
                    "n_roundabout": [0],
                    "n_traffic_signal": [0],
                    "km_total": [km_now],
                }))
                t = t + dur

    detail = pd.concat(frames, ignore_index=True)
    detail["Wh_cum"] = np.cumsum(detail["Ws"].to_numpy()) / 3600.0
    # total_Ws_for_lap() counts cum_km from zero for its own leg, so the
    # concatenated frame has a distance axis that jumps back at every leg
    # boundary. Everything downstream needs one monotone axis.
    seg_km = np.where(detail["kind"].to_numpy() == "drive",
                      detail["speed_kmh"].to_numpy() / 3.6
                      * detail["dt_s"].to_numpy() / 1e3, 0.0)
    detail["km_total"] = np.cumsum(seg_km)

    trace = soc_trace(batt, detail, wh_start=state.pack.wh)
    trace, spilled, t_full = _clip_to_capacity(trace, batt)

    opt.trace = trace
    opt.wh_spilled = spilled
    opt.t_full = t_full
    opt.min_soc = float(trace["soc"].min())
    opt.end_soc = float(trace["soc"].iloc[-1])
    opt.end_wh = float(trace["wh_remaining"].iloc[-1])
    floor = (1.0 - batt.usable) * capacity_wh(batt)
    opt.wh_above_floor = float(trace["wh_remaining"].min() - floor)

    if trace.attrs.get("soc_floor_hit"):
        opt.feasible = False
        opt.reason = (f"Energie: SOC faellt unter den nutzbaren Boden "
                      f"({opt.wh_above_floor:+.0f} Wh am tiefsten Punkt)")
    return opt


@dataclass
class MorningCharge:
    """What the pack can take in before the next day's start."""
    trace: pd.DataFrame = None        # time, p_solar, wh, wh_uncapped
    t_release: datetime = None        # pack unsealed
    t_start: datetime = None          # next day's start
    wh_start: float = 0.0             # end of today
    wh_end: float = 0.0               # at the next start, capped
    offered: float = 0.0              # what the sun would deliver
    absorbed: float = 0.0
    spilled: float = 0.0
    wh_max_arrival: float = 0.0       # highest arrival energy that wastes
                                      # nothing next morning. An UPPER
                                      # BOUND, not a goal: arriving lower
                                      # just means more km were driven,
                                      # and km are the objective while
                                      # energy in the pack is not.
    lat: float = None
    lon: float = None


def morning_charge(weather: RouteWeather, lat: float, lon: float,
                   t_release: datetime, t_start: datetime,
                   car: Car_coeffs, batt: Battery_coeffs,
                   wh_start: float,
                   step: timedelta = timedelta(minutes=10)) -> MorningCharge:
    """Integrate the next morning's charge at the overnight stop.

    The pack is sealed on arrival and released at 06:00, so between those
    two the energy is simply frozen - nothing to model. What matters is the
    window from release to the next start: the panel is set up and tracked,
    and whatever the pack cannot take is gone.

    That last part is the point of the whole calculation. Arriving too full
    means the morning window is wasted, and the morning window is free - it
    costs no race time. So there is a CEILING on sensible arrival energy,

        wh_max_arrival = pack capacity - offered

    and this returns it alongside what actually happens. It is a ceiling
    and not a target: undershooting it costs nothing, because the energy
    that is missing was going to arrive free in the morning anyway.

    The location is the overnight stop, which is the same place as the next
    day's first route point. Weather must therefore be the NEXT day's
    RouteWeather; sampling today's would be one day off.
    """
    n = max(int((t_start - t_release) / step), 1)
    rows, wh, spilled = [], float(wh_start), 0.0
    cap = capacity_wh(batt)
    wh_unc = float(wh_start)
    t = t_release
    for _ in range(n):
        ws = Ws_for_stop(car, weather, t, step, lat, lon, tracked=True)
        wh_unc -= ws / 3600.0
        wh -= ws / 3600.0
        if wh > cap:
            spilled += wh - cap
            wh = cap
        rows.append({"time": t + step, "p_solar": -ws / step.total_seconds(),
                     "wh": wh, "wh_uncapped": wh_unc})
        t = t + step

    df = pd.DataFrame(rows)
    offered = wh_unc - wh_start
    return MorningCharge(
        trace=df, t_release=t_release, t_start=t_start, wh_start=wh_start,
        wh_end=wh, offered=offered, absorbed=wh - wh_start, spilled=spilled,
        wh_max_arrival=cap - offered, lat=lat, lon=lon)


def _clip_to_capacity(trace: pd.DataFrame, batt: Battery_coeffs):
    """Cap the SoC trace at the pack's capacity and count what is lost.

    soc_trace() integrates without an upper bound, so a slow plan on a sunny
    day ends at "100 %" while the surplus quietly disappears into
    soc_from_wh()'s clamp. That surplus is the most actionable number in the
    whole table: it means the plan is too slow, and the answer is another
    loop or more speed, not a better battery.

    Forward pass rather than a clip on the finished column, because once the
    pack is full the following segments start from the cap and not from an
    imaginary higher level - clipping afterwards would keep crediting energy
    that was never stored.

    Returns (trace, wh_spilled, time_first_full).
    """
    cap = capacity_wh(batt)
    ws = trace["Ws"].to_numpy()
    wh = float(trace.attrs.get("wh_start", np.nan))
    if not np.isfinite(wh):
        wh = float(trace["wh_remaining"].iloc[0] + ws[0] / 3600.0)

    out = np.empty(len(ws))
    spilled, t_full = 0.0, None
    for i, w in enumerate(ws):
        wh -= w / 3600.0
        if wh > cap:
            spilled += wh - cap
            wh = cap
            if t_full is None:
                t_full = trace["time"].iloc[i]
        out[i] = wh

    trace = trace.copy()
    trace["wh_remaining"] = out
    trace["soc"] = soc_from_wh(batt, out)
    return trace, spilled, t_full


def _leg_end_coord(legs: list, stop: Leg) -> tuple:
    """Coordinate of a stop that has none of its own: the end of the
    driving leg before it, or the start of the one after it."""
    i = legs.index(stop)
    for j in range(i - 1, -1, -1):
        if legs[j].kind == "drive":
            r = legs[j].route.iloc[-1]
            return float(r["latitude"]), float(r["longitude"])
    for j in range(i + 1, len(legs)):
        if legs[j].kind == "drive":
            r = legs[j].route.iloc[0]
            return float(r["latitude"]), float(r["longitude"])
    raise ValueError("a day with no driving leg at all")


def _weather_for(legs: list, stop: Leg) -> RouteWeather:
    """RouteWeather to use for a stop: the neighbouring driving leg's.

    Every part's RouteWeather covers the whole UTC day at every sampled
    point, so the neighbour's is the right data as long as the stop is
    geographically on that part - which it is, being at one of its ends.
    """
    i = legs.index(stop)
    for j in list(range(i - 1, -1, -1)) + list(range(i + 1, len(legs))):
        if legs[j].kind == "drive":
            return legs[j].weather
    raise ValueError("a day with no driving leg at all")


def sweep_stop(state, parts: dict, n_loops: int, car: Car_coeffs,
               batt: Battery_coeffs, km: float, extra_stops: list = None,
               step_min: int = 15, max_min: int = 120, **kw) -> list:
    """Vary the length of one standing phase and report what each buys.

    The trade is not obvious and not monotone. Driving faster to make room
    in the pack and charging it back at the end raises the end-of-day
    energy, because less of the day's yield is thrown away - measured on
    day 1 with 4 loops, 74 % without a halt against 83 % with 45 minutes.
    Past the optimum it turns around again, since the v^3 term overtakes
    what the panel can put back.

    Two things the reader has to see alongside it: the minimum SoC falls as
    the driving gets faster, so the margin against a cloudy stretch shrinks
    while the end value grows; and energy above the next morning's ceiling
    is worthless, because it would have arrived free anyway.

    Returns [(minutes, DayOption)], stopping one row after the first
    infeasible one.
    """
    base = _as_stop_specs(extra_stops)
    out = []
    for minutes in range(0, max_min + 1, step_min):
        stops = list(base)
        if minutes > 0:
            stops.append(StopSpec(km=km, minutes=float(minutes),
                                  label="Standladen"))
        opt = evaluate(state, parts, n_loops, car, batt,
                       extra_stops=stops, **kw)
        out.append((minutes, opt))
        if not opt.feasible:
            break
    return out


def options(state, parts: dict, car: Car_coeffs, batt: Battery_coeffs,
            n_max: int = 8, **kw) -> list:
    """Evaluate 0..n_max remaining loops, stopping one row after the first
    infeasible one.

    Going one past the limit is the point: "loop 3 fails on time" and "loop
    3 fails on energy" lead to different actions, and a table that simply
    ends at loop 2 says neither.
    """
    out = []
    for n in range(0, n_max + 1):
        try:
            opt = evaluate(state, parts, n, car, batt, **kw)
        except SystemExit:
            break                     # no loop route for this day
        out.append(opt)
        if not opt.feasible:
            break
    return out
