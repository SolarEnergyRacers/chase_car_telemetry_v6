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

from   dataclasses import dataclass, field
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

# Standing times that are not route segments. The control stop is the
# regulated 30 minutes; the loop stop is the short halt each time the loop
# is re-entered.
# TODO verify both against the Sporting Regulations before the race - they
# come from the project notes, not from a clause read here.
CONTROL_STOP = timedelta(minutes=30)
LOOP_STOP    = timedelta(minutes=5)

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

    if distance_m > d_prev:
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
class Leg:
    """One piece of the remaining day."""
    name: str
    kind: str                       # 'drive' | 'stop'
    route: pd.DataFrame = None      # for 'drive'
    weather: RouteWeather = None
    duration: timedelta = None      # for 'stop'
    tracked: bool = True            # for 'stop': panel aimed at the sun
    lat: float = None               # for 'stop'
    lon: float = None

    @property
    def km(self) -> float:
        return 0.0 if self.route is None else float(self.route.index[-1]) / 1e3


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

    extra_stops: list of (km_from_here, minutes) standing phases the caller
    asked for. Placed by splitting the affected driving leg, so the SoC
    trace shows the charge where it happens and not smeared over the day.
    """
    legs = []

    if state.part == "to_control":
        route, weather = parts["to_control"]
        legs.append(Leg("ToControlStop", "drive",
                        route_from(route, state.km_in_part * 1e3), weather))
        legs.append(Leg("Kontrollstopp", "stop", duration=CONTROL_STOP,
                        tracked=True))
        _append_loops(legs, parts, n_loops)
        _append_to_finish(legs, parts)

    elif state.part == "loop":
        route, weather = parts["loop"]
        # the rest of the loop currently being driven is mandatory
        legs.append(Leg("Loop (laufend, Rest)", "drive",
                        route_from(route, state.km_in_part * 1e3), weather))
        legs.append(Leg("Loopstopp", "stop", duration=LOOP_STOP, tracked=True))
        _append_loops(legs, parts, n_loops)
        _append_to_finish(legs, parts)

    elif state.part == "to_finish":
        route, weather = parts["to_finish"]
        legs.append(Leg("FromControlStop", "drive",
                        route_from(route, state.km_in_part * 1e3), weather))

    else:
        raise ValueError(f"unknown part {state.part!r}")

    if extra_stops:
        legs = _splice_stops(legs, extra_stops)
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
                        tracked=True))


def _append_to_finish(legs: list, parts: dict) -> None:
    if "to_finish" not in parts:
        # day 6: the control stop IS the nightly stop, there is no run out
        log.info("no 'to_finish' stage for this day - the control stop is "
                 "the end of the day")
        return
    route, weather = parts["to_finish"]
    legs.append(Leg("FromControlStop", "drive", route, weather))


def _splice_stops(legs: list, extra_stops: list) -> list:
    """Insert standing phases at given distances from the current position."""
    out, done = [], 0.0
    stops = sorted((float(km), float(m)) for km, m in extra_stops)
    for leg in legs:
        if leg.kind != "drive":
            out.append(leg)
            continue
        leg_start, leg_end = done, done + leg.km
        inside = [s for s in stops if leg_start < s[0] < leg_end]
        cut_from = 0.0
        for km, minutes in inside:
            local = (km - leg_start) * 1e3          # m into this leg
            head = leg.route[leg.route.index <= local].copy()
            if len(head) >= 2:
                head = head.copy()
                head.iloc[-1, head.columns.get_indexer(
                    ["azimuth", "distance"])] = np.nan
                out.append(Leg(leg.name, "drive", head, leg.weather))
            node = leg.route.iloc[
                int(np.searchsorted(leg.route.index, local))]
            out.append(Leg(f"Standladen bei km {km:.1f}", "stop",
                           duration=timedelta(minutes=minutes), tracked=True,
                           lat=float(node["latitude"]),
                           lon=float(node["longitude"])))
            leg = Leg(f"{leg.name} (nach Stopp)", "drive",
                      route_from(leg.route, local), leg.weather)
            cut_from = local
        out.append(leg)
        done = leg_end
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


    def wh_loss_note(self) -> str:
        """One line about the pack overflowing, for a plot title."""
        if self.wh_spilled <= 5:
            return ""
        return (f"{self.wh_spilled:.0f} Wh verworfen "
                f"(≈ {self.wh_spilled/14:.0f} km) - Plan zu langsam")


def evaluate(state, parts: dict, n_loops: int, car: Car_coeffs,
             batt: Battery_coeffs, extra_stops: list = None,
             arrive_early: timedelta = timedelta(0)) -> DayOption:
    """Can the rest of the day be driven with `n_loops` loops still to go?

    The plan uses the WHOLE remaining time window. That is deliberate: for a
    fixed distance, the slowest legal plan is the cheapest one, so if the
    battery survives at maximum time it survives at all - and if it does not,
    no faster plan will save it. One evaluation therefore settles both
    questions.

    `arrive_early` shortens the window, for cases where crossing the line
    early is wanted. Charging is not allowed past the finish line and the
    pack is sealed on arrival, so the default is zero: never arrive early.

    Returns a DayOption. `feasible=False` carries the reason instead of
    raising - the options table needs to print the failing rows too, since
    "loop 3 fails on time, not on energy" is the useful half of the answer.
    """
    legs = build_legs(state, parts, n_loops, extra_stops)
    drive_legs = [l for l in legs if l.kind == "drive"]
    stop_time = sum((l.duration for l in legs if l.kind == "stop"),
                    timedelta(0))

    window = (state.t_deadline - arrive_early) - state.t_now
    t_drive = window - stop_time
    km = sum(l.km for l in drive_legs)
    opt = DayOption(n_loops=n_loops, feasible=True, km=km, legs=legs)

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
    caps  = np.concatenate([l.route["speed_route"].to_numpy()[:-1]
                            for l in drive_legs])
    if np.isnan(dists).any() or np.isnan(caps).any():
        raise ValueError("route has NaN in distance/speed_route inside a "
                         "segment - recompile the route")

    t_min = timedelta(seconds=float(np.sum(dists / (caps / 3.6)))) + stop_time
    opt.t_min = t_min
    opt.reserve = window - t_min

    try:
        speeds = apply_speed_limit(dists, caps, t_drive)
    except ValueError as e:
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
    for leg in legs:
        if leg.kind == "drive":
            n = len(leg.route) - 1
            leg_speeds = speeds[i0:i0 + n]
            i0 += n
            # speeds, not a time budget: the allocation above is global over
            # the whole day, and handing a leg its share of the time to
            # re-solve is redundant at best. A fully capped leg sits exactly
            # at its minimum time, and re-solving that is one rounding error
            # away from "not feasible".
            detail = total_Ws_for_lap(leg.route, leg.weather, car,
                                      start_time=t, speeds=leg_speeds,
                                      return_detail=True)
            detail = detail.assign(leg=leg.name, kind="drive")
            frames.append(detail)
            t = t + timedelta(seconds=float(detail["dt_s"].sum()))
        else:
            lat, lon = leg.lat, leg.lon
            if lat is None:
                prev = frames[-1] if frames else None
                # a stop with no coordinate of its own sits where the
                # previous leg ended; for the control stop that is the
                # control stop itself
                ref = _leg_end_coord(legs, leg)
                lat, lon = ref
            ws = Ws_for_stop(car, _weather_for(legs, leg), t, leg.duration,
                             lat, lon, tracked=leg.tracked)
            # stop-start: braking in and accelerating out again
            v_out = speeds[i0] if i0 < len(speeds) else speeds[-1]
            ws += Ws_for_stop_start(car, float(v_out))
            # time = START of the stop, not its middle. Ws_for_stop()
            # samples the irradiance at the midpoint internally, which is
            # right; but a printed "ab 13:52" for a stop that begins at
            # 13:39 is simply wrong to act on.
            frames.append(pd.DataFrame({
                "time": [t],
                "cum_km": [frames[-1]["cum_km"].iloc[-1] if frames else 0.0],
                "dt_s": [leg.duration.total_seconds()],
                "speed_kmh": [0.0],
                "p_solar": [-(ws - car.aux_power * leg.duration.total_seconds()
                              - Ws_for_stop_start(car, float(v_out)))
                            / leg.duration.total_seconds()],
                "p_aux": [car.aux_power],
                "p_motor": [0.0],
                "p_net": [ws / leg.duration.total_seconds()],
                "Ws": [ws],
                "leg": [leg.name],
                "kind": ["stop"],
            }))
            t = t + leg.duration

    detail = pd.concat(frames, ignore_index=True)
    detail["Wh_cum"] = np.cumsum(detail["Ws"].to_numpy()) / 3600.0
    # total_Ws_for_lap() counts cum_km from zero for its own leg, so the
    # concatenated frame has a distance axis that jumps back at every leg
    # boundary. Everything downstream - zones, standing phases, plots -
    # needs one monotone axis over the whole remaining day.
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


def options(state, parts: dict, car: Car_coeffs, batt: Battery_coeffs,
            n_max: int = 8, extra_stops: list = None) -> list:
    """Evaluate 0..n_max remaining loops, stopping one row after the first
    infeasible one.

    Going one past the limit is the point: "loop 3 fails on time" and "loop
    3 fails on energy" lead to different actions, and a table that simply
    ends at loop 2 says neither.
    """
    out = []
    for n in range(0, n_max + 1):
        try:
            opt = evaluate(state, parts, n, car, batt, extra_stops)
        except SystemExit:
            break                     # no loop route for this day
        out.append(opt)
        if not opt.feasible:
            break
    return out
