"""Live view of a plan: where the car is on it, and how far off it is.

Inputs are a `planfile.Plan` and the telemetry rows/GPS fixes as
`telemetry.ser_client` delivers them. Output is one dict per tick
(`LiveTracker.status()`), JSON-serialisable, which the web page renders.
Nothing here re-plans: when the plan no longer fits, the strategist runs
point_strategy.py again and loads the new file.

Three questions, three groups of fields:

  WHERE   km along the plan (the unrolled day, unique even on loops), the
          leg, the cross-track error, the target speed here and ahead, the
          next standing phase / roundabout / traffic signal, the ETA of the
          next control stop, loop end or finish, and the driver-change
          countdown.

  ENERGY  planned pack energy at THIS km against (a) the energy integrated
          from V*I since the plan's start (LiveEnergy) and (b) the SoC read
          off the terminal voltage with the sag corrected, given as a BAND
          because on the plateau that reading is a plausibility check and
          not a state estimate. The two should overlap; when they stop
          overlapping, one of them is lying and the integrator is the one
          that drifts.

  SUN     planned against measured MPPT power now, and the cumulative solar
          yield against the forecast since the plan started.

Sign convention for deviations, same as live_monitor: POSITIVE = better
than planned.
"""

from __future__ import annotations

from   collections import deque
from   dataclasses import dataclass, field
from   datetime import timedelta
import logging as lg
import math

import numpy as np
import pandas as pd

from ..simulation.live_monitor import LiveEnergy, TelemetrySigns
from ..simulation.battery import (capacity_wh, pack_r_ohm, soc_from_ocv_cell,
                                  soc_from_wh, wh_from_soc)
from .planfile import Plan

log = lg.getLogger(__name__)

RACE_TZ = "Africa/Johannesburg"

# --- constants of the view --------------------------------------------------

# how far the speed strip looks ahead and behind, km
STRIP_AHEAD_KM = 15.0
STRIP_BEHIND_KM = 2.0
# a GPS fix older than this is not "now" any more
GPS_FRESH_S = 6.0
# a telemetry row older than this is not "now" any more. Wider than one
# poll interval so a single dropped request does not flip the speed
# display to the GPS and back.
CAN_FRESH_S = 5.0
# below this speed the car counts as standing (roundabout crawl is above)
STANDING_KMH = 3.0
# a halt at least this long counts as a driver break - the rule says any
# halt does, but a two-minute traffic-light queue should not reset a
# two-hour counter that the strategist is watching
BREAK_MIN_S = 180.0
MAX_DRIVE_S = 2 * 3600.0
# projection: accept a fix this far off the plan; further is "neben der
# Route" (wrong loop pass, detour, or the plan does not start here)
CROSS_OK_M = 250.0
CROSS_LOST_M = 1500.0
# While the car stands, its kilometre is frozen (GPS jitter is not
# movement). A fix further than this from the frozen point is believed
# anyway - otherwise a speed signal stuck at 0 would pin the position.
FREEZE_BREAK_KM = 0.15

# SoC-from-voltage band. The pack resistance is a guess (cell 0.010-0.016 ohm
# against 0.015 assumed, plus a 10 mohm wiring placeholder), and the OCV
# curve is generic. The band spans both:
#   resistance   R * R_BAND[0] .. R * R_BAND[1]
#   curve        +-OCV_TOL_V per cell (1.3 % SoC per 10 mV on the plateau)
# Assumptions, not measurements - tighten after the standstill test.
R_BAND = (0.6, 1.4)
OCV_TOL_V = 0.020


def _to_local(t) -> str:
    if t is None or (isinstance(t, float) and math.isnan(t)):
        return None
    return pd.Timestamp(t).tz_convert(RACE_TZ).strftime("%H:%M:%S")


def _hm(seconds) -> str:
    if seconds is None or not np.isfinite(seconds):
        return "-"
    s = int(round(seconds))
    sign = "-" if s < 0 else ""
    s = abs(s)
    return f"{sign}{s//3600}:{(s%3600)//60:02d}"


def _f(x, nd=1):
    """float or None, JSON-safe."""
    if x is None:
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return None if not np.isfinite(x) else round(x, nd)


# ----------------------------------------------------------- SoC from V ----

def soc_band_from_voltage(batt, v_pack: float, i_model: float) -> dict:
    """SoC and Wh from the terminal voltage, as (lo, mid, hi).

    `i_model` is POSITIVE = discharge (battery.py convention). The band
    combines the resistance range with the OCV tolerance; the mid value is
    plain state_from_measurement().
    """
    r = pack_r_ohm(batt)
    S = batt.serial_cells
    socs = []
    for rf in (R_BAND[0], 1.0, R_BAND[1]):
        v_ocv_cell = (v_pack + i_model * r * rf) / S
        for dv in (-OCV_TOL_V, 0.0, OCV_TOL_V):
            socs.append(float(soc_from_ocv_cell(batt, v_ocv_cell + dv)))
    mid = float(soc_from_ocv_cell(batt, (v_pack + i_model * r) / S))
    lo, hi = min(socs), max(socs)
    return {"soc": mid, "soc_lo": lo, "soc_hi": hi,
            "wh": float(wh_from_soc(batt, mid)),
            "wh_lo": float(wh_from_soc(batt, lo)),
            "wh_hi": float(wh_from_soc(batt, hi)),
            "v_ocv_cell": (v_pack + i_model * r) / S,
            "sag_v": i_model * r}


# ------------------------------------------------------------- tracker ----

@dataclass
class Position:
    km: float = None
    cross_m: float = None
    source: str = None            # 'gps' | 'manual' | None
    t_fix: pd.Timestamp = None
    lat: float = None
    lon: float = None
    gps_speed_kmh: float = None
    off_route: bool = False
    hold: bool = False            # manual position, GPS fixes ignored
    frozen: bool = False          # standing: km held against GPS jitter
    n_fixes: int = 0
    n_rejected: int = 0


class LiveTracker:
    """Plan + telemetry -> status dict. One instance per loaded plan."""

    def __init__(self, plan: Plan, batt, signs: TelemetrySigns = None,
                 strip_ahead_km: float = STRIP_AHEAD_KM,
                 strip_behind_km: float = STRIP_BEHIND_KM):
        self.plan = plan
        self.batt = batt
        self.signs = signs or TelemetrySigns()
        self.strip_ahead_km = strip_ahead_km
        self.strip_behind_km = strip_behind_km

        self.energy = LiveEnergy(batt, wh_start=plan.wh_start,
                                 plan=plan.as_dayplan())
        self.pos = Position()
        self._stops = plan.stops()
        self._features = plan.features()
        self._legs = plan.legs()

        # last telemetry row (raw), and short histories for means
        self.last_row = None
        self.last_row_time = None
        self._recent = deque(maxlen=120)      # (t, v, i, p_solar, speed)
        self.n_rows = 0
        # driver break bookkeeping, from speed
        self._drive_start = None              # time driving began
        self._still_since = None              # time speed fell below STANDING
        self._last_break_end = None
        self.driver_log = []                  # confirmed changes, newest last
        self.stop_log = []                    # confirmed halt arrivals
        # trail for the energy chart: (km, wh_live, wh_v_mid)
        self._trail = []
        self._trail_last_km = -1.0
        self.notes = []

    # --- position ------------------------------------------------------
    def set_position_km(self, km: float) -> None:
        """Manual override - the GPS is down or the car is on a detour."""
        km = float(np.clip(km, 0.0, self.plan.total_km))
        self.pos.km = km
        self.pos.source = "manual"
        self.pos.hold = True
        self.pos.cross_m = None
        self.pos.off_route = False
        self.pos.lat, self.pos.lon = self.plan.coord_at_km(km)
        log.info("Position von Hand auf km %.2f gesetzt", km)

    def release_position(self) -> None:
        """Back to GPS. The next fix is searched from the held km on, so a
        manual km also works as a re-seed after a wrong loop pass."""
        self.pos.hold = False
        self.pos.source = "manual"      # tells update_gps to search from km

    def seed_position(self, km: float, fix: dict = None) -> None:
        """Set the position from a known kilometre - after a backfill.

        The backfill projects the whole GPS history sequentially and knows
        which pass of the loop the car is on. Without handing that result
        over, the next live fix would be the tracker's FIRST fix and get a
        global search: at a loop stop every pass shares the same
        coordinate, and the search picked one of them at random. Measured
        against the mock: the car stood at the control stop (km 22.8) and
        the display read km 90.7 - the third loop stop, same place, three
        loops later, and with it a schedule deviation of -138 min.
        """
        self.pos.km = float(np.clip(km, 0.0, self.plan.total_km))
        self.pos.source = "gps"
        self.pos.frozen = False
        if fix and fix.get("lat") is not None:
            self.pos.lat, self.pos.lon = float(fix["lat"]), float(fix["lon"])
            self.pos.t_fix = fix.get("time")
            self.pos.gps_speed_kmh = fix.get("speed_kmh")
        else:
            self.pos.lat, self.pos.lon = self.plan.coord_at_km(self.pos.km)
        log.info("Position aus der Historie gesetzt: km %.2f", self.pos.km)

    def update_gps(self, fix: dict) -> None:
        """Project one GPS fix onto the plan (sequential, loop-safe)."""
        if fix is None or fix.get("lat") is None or fix.get("lon") is None:
            return
        t = fix.get("time")
        if t is not None and self.pos.t_fix is not None and t <= self.pos.t_fix:
            return                                  # same fix as before
        lat, lon = float(fix["lat"]), float(fix["lon"])
        self.pos.n_fixes += 1
        self.pos.gps_speed_kmh = fix.get("speed_kmh")
        self.pos.t_fix = t
        if self.pos.hold:
            return                  # held by hand; speed and age still count

        km_prev = self.pos.km
        try:
            if km_prev is None:
                # First fix and no history: a plan is computed for where
                # the car is NOW, so it starts at km 0 - look there first.
                # A global search would be free to pick any pass of a loop,
                # since they share their coordinates. If the car turns out
                # not to be near the start (plan loaded late, backfill
                # failed), the cross-track says so and the global search
                # takes over.
                km, cross = self.plan.project(lat, lon, after_km=0.0,
                                              window_km=5.0)
                if cross > CROSS_OK_M:
                    km2, cross2 = self.plan.project(lat, lon)
                    if cross2 < cross:
                        km, cross = km2, cross2
            elif self.pos.source == "manual":
                # after a manual km: that km is the strategist's statement
                # of WHICH pass of the loop the car is on, so search a
                # window around it - a loop is ~22 km, so +-10 km resolves
                # the pass without pinning the car to the exact number
                try:
                    km, cross = self.plan.project(
                        lat, lon, after_km=max(km_prev - 10.0, 0.0),
                        window_km=20.0)
                except ValueError:
                    km, cross = self.plan.project(lat, lon)
                if cross > CROSS_OK_M:
                    km2, cross2 = self.plan.project(lat, lon)
                    if cross2 < cross:
                        km, cross = km2, cross2
            else:
                # how far can the car have moved since the last fix?
                dt = ((t - self.pos.t_fix).total_seconds()
                      if (t is not None and self.pos.t_fix is not None) else 10.0)
                v = self.pos.gps_speed_kmh or 100.0
                window = max(2.0, 1.5 * v / 3600.0 * max(dt, 1.0) + 1.0)
                km, cross = self.plan.project(lat, lon,
                                              after_km=max(km_prev - 0.3, 0.0),
                                              window_km=window)
                if cross > CROSS_OK_M:
                    # not where the plan expects - maybe a jump (plan
                    # loaded late, GPS came back after a hole). Search
                    # ahead of the last point without a window.
                    km2, cross2 = self.plan.project(
                        lat, lon, after_km=max(km_prev - 0.3, 0.0))
                    if cross2 < cross:
                        km, cross = km2, cross2
        except ValueError as e:
            log.warning("Projektion fehlgeschlagen: %s", e)
            self.pos.n_rejected += 1
            return

        self.pos.lat, self.pos.lon = lat, lon
        self.pos.cross_m = float(cross)
        if cross > CROSS_LOST_M:
            # keep the old km; a fix a kilometre off the plan is a detour
            # or the wrong day, not progress
            self.pos.off_route = True
            self.pos.n_rejected += 1
            return
        self.pos.off_route = cross > CROSS_OK_M
        # Standing still is not moving. A parked car's fixes wander by a
        # few metres, and at a halt those metres sit exactly on the node
        # where the plan steps: the leg, the schedule deviation and every
        # ETA flipped between two answers once a second (measured at the
        # control stop: 0 vs +15 min, and 32.7 vs 67.8 min to the next
        # loop stop, because the 35 min halt was counted or not).
        #
        # So while the car stands, the kilometre is frozen. The escape
        # hatch is a real move: if a fix lands further than FREEZE_BREAK_KM
        # away, it is believed, so a stuck speed signal cannot pin the
        # position for ever.
        if (self.pos.km is not None and self._is_standing()
                and abs(km - self.pos.km) < FREEZE_BREAK_KM):
            self.pos.frozen = True
            return
        self.pos.frozen = False
        self.pos.km = float(km)
        self.pos.source = "gps"

    # --- telemetry -----------------------------------------------------
    def ingest(self, df: pd.DataFrame, km_series: pd.Series = None) -> int:
        """Feed telemetry rows (UTC index, ser_client columns).

        `km_series` optionally gives the km along the plan per row (from a
        GPS history); otherwise every row is booked at the current
        position. Returns the number of rows accepted by the integrator.
        """
        if df is None or df.empty:
            return 0
        n0 = len(self.energy._rows)
        mppt = [c for c in ("mppt1_power", "mppt2_power", "mppt3_power",
                            "mppt4_power") if c in df.columns]
        has_v = "battery_voltage" in df.columns
        has_i = "battery_current" in df.columns
        for t, row in df.iterrows():
            self.n_rows += 1
            speed = row.get("speed")
            v = row.get("battery_voltage") if has_v else np.nan
            i = row.get("battery_current") if has_i else np.nan
            p_sol = [0.0 if pd.isna(row[c]) else float(row[c]) for c in mppt]
            km = (float(km_series.get(t, np.nan)) if km_series is not None
                  else np.nan)
            if not np.isfinite(km):
                km = self.pos.km
            self._track_driving(t, speed)
            self.last_row_time = t
            self.last_row = {"time": t, "speed": speed, "v": v, "i": i,
                             "p_solar": float(np.sum(p_sol)) if p_sol else None,
                             "mppt": p_sol, "gap": bool(row.get("gap", False))}
            if pd.notna(v) and pd.notna(i):
                self.energy.update(t, float(v), float(i), p_solar=p_sol,
                                   odo_km=km)
                self._recent.append((t, float(v), float(i),
                                     float(np.sum(p_sol)) if p_sol else 0.0,
                                     speed))
                if km is not None:
                    self._push_trail(km)
        return len(self.energy._rows) - n0

    def _track_driving(self, t, speed) -> None:
        """Drive-time counter for the two-hour rule, from the speed signal."""
        if speed is None or pd.isna(speed):
            return
        moving = float(speed) >= STANDING_KMH
        if moving:
            if self._still_since is not None:
                stood = (t - self._still_since).total_seconds()
                if stood >= BREAK_MIN_S:
                    self._last_break_end = t
                    self._drive_start = t
                self._still_since = None
            if self._drive_start is None:
                self._drive_start = t
        else:
            if self._still_since is None:
                self._still_since = t

    def _is_standing(self) -> bool:
        """Does the car stand right now, by the last speed reading?

        The CAN speed decides, because it is the car's own measurement;
        the GPS speed of a parked phone wanders around 0-2 km/h. Unknown
        counts as NOT standing - freezing the position on missing data
        would be the wrong way round.
        """
        row = self.last_row
        if row is None or row["speed"] is None or pd.isna(row["speed"]):
            return False
        return float(row["speed"]) < STANDING_KMH

    def drive_time_s(self, now) -> float:
        """Seconds at the wheel since the last halt of BREAK_MIN_S or more."""
        if self._drive_start is None:
            return None
        if self._still_since is not None:
            stood = (now - self._still_since).total_seconds()
            if stood >= BREAK_MIN_S:
                return 0.0
        return max((now - self._drive_start).total_seconds(), 0.0)

    def log_driver_change(self, now, at=None, note: str = "") -> dict:
        """Confirm that the driver was changed - the two hours start over.

        The counter itself runs off the speed signal and cannot see who is
        at the wheel: a five-minute halt looks the same whether the driver
        swapped or the car waited at a level crossing. A change can also
        happen EARLIER than planned, at a stop that was made for another
        reason. So the confirmation is a hand entry, and it wins over the
        automatic counter.

        `at` may be a timestamp in the past (someone confirms three
        minutes later, or at the next stop). It is clamped to now, because
        a change in the future is not a fact.
        """
        t = now if at is None else pd.Timestamp(at)
        t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
        if t > now:
            t = now
        km = self._km_at_time(t)
        # keep _still_since: standing still counts as a break either way,
        # and clearing it would start the counter while the car is parked
        entry = {"time": t, "km": km, "note": note}
        self.driver_log.append(entry)
        self.driver_log.sort(key=lambda e: e["time"])
        # The counter runs from the LATEST reset, which is the newest
        # confirmed change or a break the speed signal already saw -
        # whichever is later. Without the max(), back-dating a change
        # after a newer one would hand back time that has been driven.
        newest = self.driver_log[-1]["time"]
        self._drive_start = (newest if self._drive_start is None
                             else max(newest, self._drive_start))
        self._last_break_end = self._drive_start
        self.notes.append(f"Fahrerwechsel {_to_local(t)}"
                          + (f" bei km {km:.1f}" if km is not None else "")
                          + (f" - {note}" if note else ""))
        log.info("Fahrerwechsel eingetragen: %s, km %s", t, km)
        return entry

    def _km_at_time(self, t) -> float:
        """Where the car was at `t`, from the integrator's own history.

        Matters for a back-dated driver change: the km of "now" is not the
        km of five minutes ago, and on a loop that difference is a
        different place, not just a different number. Falls back to the
        current position when the moment is not in the history.
        """
        rows = self.energy._rows
        if not rows:
            return self.pos.km
        t = pd.Timestamp(t)
        times = np.array([r["time"].timestamp() for r in rows])
        kms = np.array([np.nan if r["odo_km"] is None else r["odo_km"]
                        for r in rows], dtype=float)
        ok = np.isfinite(kms)
        if not ok.any():
            return self.pos.km
        ts = t.timestamp()
        if ts < times[ok][0] or ts > times[ok][-1] + 60:
            return self.pos.km
        return float(np.interp(ts, times[ok], kms[ok]))

    def adopt_driver_log(self, entries: list) -> None:
        """Take over confirmed driver changes from a previous plan.

        The times carry over, the kilometres do not: each plan counts km
        from its own start point. The last change also restores the
        counter, so loading a new plan does not hand the driver two fresh
        hours he has not got.
        """
        self.driver_log = [{"time": e["time"], "km": None,
                            "note": (e.get("note") or "").strip()}
                           for e in entries]
        if self.driver_log:
            self._drive_start = self.driver_log[-1]["time"]
            self._last_break_end = self._drive_start

    def log_stop(self, now, at=None, note: str = "") -> dict:
        """Confirm WHEN the car stopped at the halt it is standing at.

        The arrival is otherwise read off the speed signal, and that
        signal only exists from the first integrated sample onwards. Load
        a plan at the control stop - the normal case, `--time now` - and
        the halt appears to start at the moment the plan was loaded:
        measured 07:30 and 07:34 instead of the true 07:25, and every
        "leave at" and ETA behind it moves with the error.

        The confirmed time wins over the detected one, for the halt the
        car is standing at. Confirming twice corrects the entry rather
        than adding a second one - there is only one arrival per halt.
        """
        t = now if at is None else pd.Timestamp(at)
        t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
        if t > now:
            t = now
        km = self.pos.km
        r = (self.plan.stop_at(km, now, standing=True)
             if km is not None else None)
        name = None if r is None else str(r["name"])
        entry = {"time": t, "km": km, "stop": name, "note": note}
        self.stop_log = [e for e in self.stop_log if e["stop"] != name or name is None]
        self.stop_log.append(entry)
        self.stop_log.sort(key=lambda e: e["time"])
        self.notes.append(f"Stopp {name or 'ohne Zuordnung'} angekommen "
                          f"{_to_local(t)}")
        log.info("Stopp bestaetigt: %s, Ankunft %s (km %s)", name, t, km)
        return entry

    def confirmed_arrival(self, stop_name: str):
        """The confirmed arrival for a halt, or None."""
        for e in reversed(self.stop_log):
            if e["stop"] == stop_name:
                return e["time"]
        return None

    def seed_standstill(self, df: pd.DataFrame) -> bool:
        """Feed speed-only rows from BEFORE the plan start into the counters.

        Recovers the arrival at a halt that began before the plan was
        computed, without touching the energy integral - that one has to
        start at the plan's own start value, not earlier. Returns True if
        the car turns out to have been standing already.
        """
        if df is None or df.empty or "speed" not in df.columns:
            return False
        for t, row in df.iterrows():
            self._track_driving(t, row.get("speed"))
        return self._still_since is not None

    def anchor(self) -> dict:
        """Re-anchor the integrator on the latest voltage (must be at rest)."""
        if self.last_row is None or pd.isna(self.last_row["v"]):
            return {"applied": False, "reason": "keine Spannung"}
        v, i = float(self.last_row["v"]), float(self.last_row["i"] or 0.0)
        res = self.energy.anchor(v, i)
        if res.get("applied"):
            self.notes.append(f"Anker {_to_local(self.last_row['time'])}: "
                              f"{res['correction_wh']:+.0f} Wh")
        return res

    # --- derived -------------------------------------------------------
    def _recent_means(self, seconds: float = 60.0) -> dict:
        if not self._recent:
            return {}
        t_last = self._recent[-1][0]
        rows = [r for r in self._recent
                if (t_last - r[0]).total_seconds() <= seconds]
        v = np.mean([r[1] for r in rows])
        i = np.mean([r[2] for r in rows])
        ps = np.mean([r[3] for r in rows])
        p_batt = float(np.mean([self.signs.batt_power(r[1], r[2], r[3])
                                for r in rows]))
        return {"v": float(v), "i": float(i), "p_solar": float(ps),
                "p_batt": p_batt, "n": len(rows)}

    def _push_trail(self, km: float) -> None:
        """One point per quarter kilometre for the energy chart."""
        if km is None or not self._recent or km - self._trail_last_km < 0.25:
            return
        st = self.energy.status()
        band = self._voltage_band()
        self._trail.append([round(km, 3), round(st["wh_remaining"], 1),
                            None if band is None else round(band["wh"], 1)])
        self._trail_last_km = km

    def _voltage_band(self) -> dict:
        m = self._recent_means(20.0)
        if not m:
            return None
        i_model = m["i"] if self.signs.i_batt_discharge_positive else -m["i"]
        return soc_band_from_voltage(self.batt, m["v"], i_model)

    def current_speed(self, now) -> tuple:
        """(speed_kmh, source) - the CAN speed, with GPS as the fallback.

        CAN first because it is the car's own measurement, arrives every
        second and is the number the driver has in front of him; the GPS
        speed comes from a phone at an irregular 2-40 s and is a chord
        through the corner. CAN is 8-bit unsigned and thus 1 km/h coarse -
        good enough to hold a target speed, and the odometer does not hang
        on it (that comes from the projected position).

        A stale row is still reported, with the source marked `_alt`, so
        the page can grey it out instead of showing a hole. A row inside a
        telemetry gap has no speed at all (see ser_client.clean) - then
        GPS is genuinely better than a zero.
        """
        row = self.last_row
        fresh_can = (row is not None and row["speed"] is not None
                     and pd.notna(row["speed"])
                     and self.last_row_time is not None
                     and (now - self.last_row_time).total_seconds() <= CAN_FRESH_S)
        if fresh_can:
            return float(row["speed"]), "can"
        if (self.pos.gps_speed_kmh is not None and self.pos.t_fix is not None
                and (now - self.pos.t_fix).total_seconds() <= GPS_FRESH_S):
            return float(self.pos.gps_speed_kmh), "gps"
        if row is not None and row["speed"] is not None and pd.notna(row["speed"]):
            return float(row["speed"]), "can_alt"
        if self.pos.gps_speed_kmh is not None:
            return float(self.pos.gps_speed_kmh), "gps_alt"
        return None, None

    def _halt_state(self, now, km: float) -> dict:
        """If the car stands in a planned halt: since when, and until when.

        Every halt counts from the ACTUAL arrival, so a car that reached
        the control stop five minutes late leaves five minutes late however
        long it has stood - the schedule deviation is frozen at the arrival
        delay, and every ETA beyond carries it.

        Two departure times, and they are different questions:

          free_at   arrival + the REGULATED minimum (30 min at the control
                    stop, 5 at a loop break). From here on driving on is
                    allowed - the moment to start getting ready.
          leave     arrival + what the PLAN budgets (35 / 8 min). The extra
                    minutes are the ones the plan spends on getting in and
                    out, so this is what the ETAs are built on; using
                    `free_at` would make every arrival five minutes
                    optimistic while the car is still parked.

        Source of the arrival, in order: a confirmed one, else the speed
        signal (`_still_since`), else the halt is not reported at all.
        """
        standing = self._is_standing() or self._still_since is not None
        r = self.plan.stop_at(km, now, standing=standing)
        if r is None:
            return {"at_stop": None, "remaining_s": 0.0, "late_min": None}
        name = str(r["name"])
        confirmed = self.confirmed_arrival(name)
        arrived = confirmed if confirmed is not None else self._still_since
        if arrived is None:
            return {"at_stop": None, "remaining_s": 0.0, "late_min": None}
        late_min = (arrived - r["t_arrive"]).total_seconds() / 60.0
        leave = arrived + pd.Timedelta(seconds=float(r["dur_s"]))
        reg = r["reg_s"]
        free_at = (leave if reg is None
                   else arrived + pd.Timedelta(seconds=float(reg)))
        return {"at_stop": name, "km": float(r["km"]),
                "remaining_s": max((leave - now).total_seconds(), 0.0),
                "free_s": max((free_at - now).total_seconds(), 0.0),
                "late_min": late_min, "arrived": arrived, "leave": leave,
                "free_at": free_at, "reg_s": (None if reg is None
                                              else float(reg)),
                "dur_s": float(r["dur_s"]),
                "source": "confirmed" if confirmed is not None else "speed"}

    def _eta(self, now, km_now: float, km_target: float,
             extra_s: float = 0.0) -> dict:
        """Planned arrival, and arrival at the plan's pace from here.

        `extra_s`: time still to be spent where the car stands now (the
        rest of a planned halt) before the drive to km_target begins.
        """
        t_plan = self.plan.time_at_km_arrival(km_target)
        travel = self.plan.travel_time_s(km_now, km_target, include_stops=True)
        t_live = now + pd.Timedelta(seconds=travel + extra_s)
        return {"km": _f(km_target, 2), "dist_km": _f(km_target - km_now, 2),
                "t_plan": _to_local(t_plan), "t_live": _to_local(t_live),
                "delta_min": _f((t_live - t_plan).total_seconds() / 60.0, 1),
                "travel_s": _f(travel + extra_s, 0)}

    def _upcoming(self, now, km_now: float, ahead_km: float = None,
                  extra_s: float = 0.0) -> list:
        """Standing phases and features ahead, nearest first."""
        out = []
        for _, s in self._stops.iterrows():
            if s["km"] > km_now + 0.05:
                out.append({"type": "stop", "kind": s["kind"], "name": s["name"],
                            "dur_min": _f(s["dur_s"] / 60.0, 0),
                            "wh_gain": _f(s["wh_gain"], 0),
                            **self._eta(now, km_now, float(s["km"]), extra_s)})
        # roundabouts and signals only in the near field - the day-1 loop
        # has seven of them per pass, and listing all forty would push the
        # control stop off the table
        for _, f in self._features.iterrows():
            if km_now + 0.02 < f["km"] <= km_now + self.strip_ahead_km:
                out.append({"type": "feature", "kind": f["kind"],
                            "name": ("Kreisel" if f["kind"] == "roundabout"
                                     else "Ampel"),
                            "km": _f(f["km"], 2), "dist_km": _f(f["km"] - km_now, 2)})
        for lg_ in self._legs:
            if lg_["km_end"] > km_now + 0.05 and lg_["leg"].startswith("Loop"):
                pass    # loop ends coincide with the loop stops above
        fin = {"type": "finish", "kind": "finish", "name": "Ziel",
               **self._eta(now, km_now, self.plan.total_km, extra_s)}
        # the one deadline that costs: minutes past the official finish
        # time at the pace the plan assumes from here
        t_dl = pd.Timestamp(self.plan.meta["t_deadline"])
        t_live = now + pd.Timedelta(seconds=fin["travel_s"] or 0)
        fin["deadline"] = _to_local(t_dl)
        fin["late_min"] = _f((t_live - t_dl).total_seconds() / 60.0, 1)
        out.append(fin)
        out.sort(key=lambda d: d["dist_km"] if d.get("dist_km") is not None
                 else 1e9)
        if ahead_km is not None:
            out = [d for d in out if d.get("dist_km") is None
                   or d["dist_km"] <= ahead_km]
        return out

    def _strip(self, km_now: float) -> dict:
        """Speed profile around the car for the chart."""
        a = max(km_now - self.strip_behind_km, 0.0)
        b = min(km_now + self.strip_ahead_km, self.plan.total_km)
        w = self.plan.window(a, b)
        stops = self._stops[(self._stops["km"] >= a) & (self._stops["km"] <= b)]
        feats = self._features[(self._features["km"] >= a)
                               & (self._features["km"] <= b)]
        legs = [l for l in self._legs if a <= l["km_start"] <= b]
        return {
            "km_from": _f(a, 2), "km_to": _f(b, 2),
            "km_start": [_f(x, 3) for x in w["km_start"]],
            "km_end": [_f(x, 3) for x in w["km_end"]],
            "v_soll": [_f(x, 1) for x in w["speed_kmh"]],
            "v_route": [_f(x, 1) for x in w["v_route"]],
            "v_limit": [_f(x, 0) for x in w["v_limit"]],
            "v_limit_est": [_f(x, 0) for x in w["v_limit_est"]],
            "alt": [_f(x, 0) for x in w["altitude_m"]],
            "stops": [{"km": _f(s["km"], 3), "kind": s["kind"],
                       "name": s["name"]} for _, s in stops.iterrows()],
            "features": [{"km": _f(f["km"], 3), "kind": f["kind"]}
                         for _, f in feats.iterrows()],
            "legs": [{"km": _f(l["km_start"], 3), "leg": l["leg"]}
                     for l in legs],
        }

    def _energy_curve(self, step_km: float = 0.5) -> dict:
        """Whole-day planned pack energy and floor, downsampled for the chart."""
        tr = self.plan.tr
        ke = tr["km_end"].to_numpy(dtype=float)
        grid = np.arange(0.0, self.plan.total_km + step_km, step_km)
        grid = np.clip(grid, 0.0, self.plan.total_km)
        wh = np.interp(grid, np.concatenate([[0.0], ke]),
                       np.concatenate([[self.plan.wh_start],
                                       tr["wh_remaining"].to_numpy(dtype=float)]))
        fl = np.interp(grid, ke, tr["wh_floor"].to_numpy(dtype=float))
        return {"km": [round(float(x), 2) for x in grid],
                "wh_plan": [round(float(x), 1) for x in wh],
                "wh_floor": [round(float(x), 1) for x in fl],
                "wh_cap": float(capacity_wh(self.batt)),
                "stops": [{"km": _f(s["km"], 2), "kind": s["kind"]}
                          for _, s in self._stops.iterrows()],
                "trail": self._trail}

    # --- the status ----------------------------------------------------
    def status(self, now=None) -> dict:
        now = pd.Timestamp.now("UTC") if now is None else pd.Timestamp(now)
        plan = self.plan
        km = self.pos.km
        out = {
            "now": _to_local(now),
            "plan": {"label": plan.label, "file": (plan.path.name if plan.path
                                                   else None),
                     "n_loops": plan.meta.get("n_loops"),
                     "total_km": _f(plan.total_km, 1),
                     "t_start": _to_local(plan.t_start),
                     "t_finish": _to_local(plan.t_finish),
                     "t_deadline": _to_local(pd.Timestamp(plan.meta["t_deadline"])),
                     "wh_start": _f(plan.wh_start, 0),
                     "end_soc": _f(100 * plan.meta.get("end_soc", np.nan), 0),
                     "cloud_margin": _f(100 * (plan.meta.get("cloud_margin")
                                               or np.nan), 0),
                     "pack_source": plan.meta["pack"].get("source"),
                     "pack_trust": plan.meta["pack"].get("trust")},
            "position": None, "speed": None, "next": None, "finish": None,
            "upcoming": [],
            "driver": None, "energy": None, "sun": None, "strip": None,
            "curve": None, "telemetry": self._telemetry_health(now),
            "notes": list(self.notes[-5:]),
        }

        # --- position and speed
        v_now, v_src = self.current_speed(now)
        halt = (self._halt_state(now, km) if km is not None
                else {"at_stop": None, "remaining_s": 0.0, "late_min": None})
        km_eff = halt["km"] if halt["at_stop"] else km
        if km is not None:
            # `km_eff` above is ONE authority while standing at a halt:
            # its own node. The projected kilometre wanders by metres, and
            # at a halt those metres straddle the step in the plan - leg,
            # schedule and every ETA flipped between two answers once a
            # second. Where the halt is recognised, its km decides.
            here = plan.speed_at_km(km_eff)
            t_plan_here = plan.time_at_km(km_eff)
            extra_s = halt["remaining_s"]
            if halt["at_stop"] and halt["late_min"] is not None:
                # frozen at the arrival delay, unless the car overstays
                over = (now - (halt["leave"])).total_seconds() / 60.0
                sched = halt["late_min"] + max(over, 0.0)
            else:
                sched = plan.schedule_min(km_eff, now)           # >0 late
            ref = plan.ref_at(km_eff, now, standing=bool(halt["at_stop"]))
            out["position"] = {
                "at_stop": ref["at_stop"],
                "halt_remaining_s": _f(extra_s, 0),
                "halt_leave": (_to_local(halt["leave"]) if halt.get("leave") is not None
                               else None),
                "halt_free_at": (_to_local(halt["free_at"])
                                 if halt.get("free_at") is not None else None),
                "halt_free_s": _f(halt.get("free_s"), 0),
                "halt_arrived": (_to_local(halt["arrived"])
                                 if halt.get("arrived") is not None else None),
                "halt_source": halt.get("source"),
                "stops_confirmed": [{"t": _to_local(e["time"]),
                                     "stop": e["stop"]}
                                    for e in self.stop_log[-4:]],
                "halt_reg_min": _f((halt.get("reg_s") or np.nan) / 60.0, 0),
                "halt_dur_min": _f((halt.get("dur_s") or np.nan) / 60.0, 0),
                "frozen": bool(self.pos.frozen),
                "km": _f(km_eff, 2), "leg": here["leg"], "leg_km": _f(here["leg_km"], 2),
                "leg_len": _f(next((l["km_end"] - l["km_start"]
                                    for l in self._legs if l["leg"] == here["leg"]),
                                   np.nan), 1),
                "source": self.pos.source, "cross_m": _f(self.pos.cross_m, 0),
                "off_route": bool(self.pos.off_route),
                "lat": _f(self.pos.lat, 6), "lon": _f(self.pos.lon, 6),
                "fix_age_s": (_f((now - self.pos.t_fix).total_seconds(), 0)
                              if self.pos.t_fix is not None else None),
                "altitude_m": _f(here["altitude_m"], 0),
                "schedule_min": _f(sched, 1),
                "t_plan_here": _to_local(t_plan_here),
                "remaining_km": _f(plan.total_km - km_eff, 1),
            }
            out["speed"] = {
                "now": _f(v_now, 0), "source": v_src,
                "soll": _f(here["v_soll"], 0),
                "delta": _f((v_now - here["v_soll"]) if v_now is not None else None, 0),
                "v_route": _f(here["v_route"], 0),
                "v_limit": _f(here["v_limit"], 0),
                "v_limit_est": _f(here["v_limit_est"], 0),
                "at_cap": bool(here["v_soll"] >= here["v_route"] - 0.5),
                "next_zones": self._next_zones(km_eff),
            }
            up = self._upcoming(now, km_eff, extra_s=extra_s)
            out["upcoming"] = up[:12]
            nxt = next((u for u in up if u["type"] == "finish"
                        or (u["type"] == "stop" and u["kind"] in
                            ("control", "loop"))), None)
            out["next"] = nxt
            out["finish"] = next((u for u in up if u["type"] == "finish"), None)
            out["strip"] = self._strip(km_eff)
            out["curve"] = self._energy_curve()
        else:
            out["speed"] = {"now": _f(v_now, 0), "source": v_src}

        # --- driver change
        drive_s = self.drive_time_s(now)
        nxt_dc = None
        if km is not None:
            dcs = self._stops[(self._stops["kind"] == "driver")
                              & (self._stops["km"] > km_eff + 0.05)]
            if len(dcs):
                s = dcs.iloc[0]
                nxt_dc = {"name": s["name"],
                          **self._eta(now, km_eff, float(s["km"]),
                                      halt["remaining_s"])}
        left_s = None if drive_s is None else MAX_DRIVE_S - drive_s
        latest = None
        if km is not None and left_s is not None:
            # where the two hours run out if nothing else interrupts, and
            # which planned halt takes the question away first
            d = plan.drive_deadline(km_eff, max(left_s, 0.0), BREAK_MIN_S)
            if d["km"] is not None:
                latest = {"km": _f(d["km"], 1), "reset_by": d["reset_by"],
                          "t": _to_local(now + pd.Timedelta(seconds=d["after_s"])),
                          "in_s": _f(d["after_s"], 0),
                          "in_km": _f(d["km"] - km, 1)}
        out["driver"] = {
            "drive_s": _f(drive_s, 0), "drive_hm": _hm(drive_s),
            "left_s": _f(left_s, 0),
            "left_hm": _hm(left_s) if left_s is not None else "-",
            "overdue": bool(drive_s is not None and drive_s > MAX_DRIVE_S),
            "standing": self._still_since is not None,
            "next_change": nxt_dc,
            "latest": latest,
            "planned_km": [_f(x, 1) for x in plan.meta.get("driver_changes_km", [])],
            "log": [{"t": _to_local(e["time"]), "km": _f(e["km"], 1),
                     "note": e.get("note") or ""}
                    for e in self.driver_log[-6:]],
            "n_changes": len(self.driver_log),
        }

        # --- energy
        st = self.energy.status()
        means = self._recent_means(60.0)
        band = self._voltage_band()
        e = {
            "wh_live": _f(st["wh_remaining"], 0),
            "soc_live": _f(st["soc_percent"], 1),
            "wh_used": _f(st["wh_used"], 0), "wh_solar": _f(st["wh_solar"], 0),
            "compared_at": st.get("compared_at"),
            "delta_wh": _f(st["delta_wh"], 0),
            "delta_solar_wh": _f(st["delta_solar_wh"], 0),
            "delta_load_wh": _f(st["delta_load_wh"], 0),
            "projected_end_wh": _f(st["projected_end_wh"], 0),
            "projected_end_soc": (_f(100 * soc_from_wh(self.batt, st["projected_end_wh"]), 0)
                                  if st["projected_end_wh"] is not None else None),
            "n_gaps": st["n_gaps"], "wh_bridged": _f(st["wh_bridged"], 0),
            "n_rejected": st["n_rejected"],
            "v_pack": _f(means.get("v"), 1), "i_batt": _f(means.get("i"), 1),
            "p_batt": _f(means.get("p_batt"), 0),
            "cap_wh": _f(capacity_wh(self.batt), 0),
            "floor_wh": _f(plan.meta["pack"].get("floor_wh"), 0),
            "n_samples": len(self.energy._rows),
            # where the Ist curve comes from: the plan's own start value,
            # integrated forward from the plan's start time. Shown on the
            # page because "1673 Wh" without its origin invites the
            # question what happens on a plan change.
            "integrated_from": (_to_local(self.energy._rows[0]["time"])
                                if self.energy._rows else None),
            "wh_start": _f(self.energy.wh_start, 0),
            "wh_start_plan": _f(plan.wh_start, 0),
            "wh_start_source": plan.meta["pack"].get("source"),
            "wh_start_trust": plan.meta["pack"].get("trust"),
            "n_anchors": len(self.energy._anchors),
            "wh_spilled": _f(self.energy.wh_spilled, 0),
        }
        if band is not None:
            e.update({"soc_v": _f(100 * band["soc"], 1),
                      "soc_v_lo": _f(100 * band["soc_lo"], 1),
                      "soc_v_hi": _f(100 * band["soc_hi"], 1),
                      "wh_v": _f(band["wh"], 0), "wh_v_lo": _f(band["wh_lo"], 0),
                      "wh_v_hi": _f(band["wh_hi"], 0),
                      "sag_v": _f(band["sag_v"], 2),
                      "v_ocv_cell": _f(band["v_ocv_cell"], 3),
                      # Compared in PERCENT, with half a point of slack -
                      # the same quantity the page prints. In Wh the check
                      # contradicted its own display at the top of the OCV
                      # curve, where one percent is ~30 Wh: "98 % · Band
                      # 98-100 % · Ist AUSSERHALB".
                      "live_in_band": bool(
                          100 * band["soc_lo"] - 0.5
                          <= st["soc_percent"]
                          <= 100 * band["soc_hi"] + 0.5)})
        if km is not None:
            # by km while driving, by time inside a planned halt - see
            # Plan.ref_at(). LiveEnergy.status() compares by km only, so
            # the deltas are formed here from its integrals.
            p = plan.ref_at(km_eff, now, standing=bool(halt["at_stop"]))
            wh_live = st["wh_remaining"]
            e.update({"wh_plan": _f(p["wh_remaining"], 0),
                      "soc_plan": _f(100 * p["soc"], 1),
                      "wh_floor": _f(p["wh_floor"], 0),
                      "wh_solar_plan": _f(p["wh_solar"], 0),
                      "wh_load_plan": _f(p["wh_load"], 0),
                      "delta_wh": _f(wh_live - p["wh_remaining"], 0),
                      "compared_at": ("stop" if p["at_stop"] else "km"),
                      "margin_wh": _f(wh_live - p["wh_floor"], 0),
                      "end_wh_plan": _f(plan.meta.get("end_wh"), 0)})
            if self.energy.wh_solar > 0:
                e["delta_solar_wh"] = _f(self.energy.wh_solar - p["wh_solar"], 0)
                e["delta_load_wh"] = _f((self.energy.wh_used + self.energy.wh_solar)
                                        - p["wh_load"], 0)
            if self.energy.wh_spilled > 5:
                e["split_note"] = ("Pack war voll: Solar/Last-Aufteilung "
                                   "nicht aussagekraeftig")
            proj = self._project_end(km_eff, p)
            e["projected_end_wh"] = _f(proj, 0)
            e["projected_end_soc"] = (_f(100 * soc_from_wh(self.batt, proj), 0)
                                      if proj is not None else None)
        out["energy"] = e

        # --- sun
        sun = {"p_now": _f(means.get("p_solar"), 0),
               "p_last": _f(self.last_row["p_solar"] if self.last_row else None, 0),
               "mppt": ([_f(x, 0) for x in self.last_row["mppt"]]
                        if self.last_row else []),
               "wh_measured": _f(st["wh_solar"], 0)}
        if km is not None:
            here = plan.speed_at_km(km_eff)
            sun.update({"p_plan": _f(here["p_solar_plan"], 0),
                        "delta_p": _f((means.get("p_solar") - here["p_solar_plan"])
                                      if means else None, 0),
                        "wh_plan": _f(ref["wh_solar"], 0),
                        "p_net_plan": _f(here["p_net_plan"], 0)})
        out["sun"] = sun
        return out

    def _project_end(self, km: float, ref: dict, min_km: float = 10.0):
        """End-of-day energy if the observed load and solar RATIOS hold.

        Same idea as LiveEnergy.project_end_wh(), but against the
        time-aware reference: read at km inside a halt, the plan's
        cumulative solar already contains the whole halt, and the ratio
        collapses - the projection then loses a kWh that is merely not yet
        collected. Clipped to the pack, as the real pack would be.
        """
        if km < min_km:
            return None
        wh_live = self.energy.wh_start - self.energy.wh_used
        load_done = self.energy.wh_used + self.energy.wh_solar
        r_load = load_done / ref["wh_load"] if ref["wh_load"] > 50 else 1.0
        r_solar = (self.energy.wh_solar / ref["wh_solar"]
                   if (ref["wh_solar"] > 50 and self.energy.wh_solar > 0) else 1.0)
        load_left = max(0.0, float(self.plan._wh_load_cum[-1]) - ref["wh_load"])
        solar_left = max(0.0, float(self.plan._wh_solar_cum[-1]) - ref["wh_solar"])
        end = wh_live - (r_load * load_left - r_solar * solar_left)
        return float(min(end, capacity_wh(self.batt)))

    def _next_zones(self, km: float, n: int = 4) -> list:
        """The next target-speed changes ahead, merged to 5 km/h zones."""
        w = self.plan.window(km, km + 40.0)
        if w.empty:
            return []
        v = (w["speed_kmh"].to_numpy(dtype=float) / 5.0).round() * 5.0
        ks = w["km_start"].to_numpy(dtype=float)
        ke = w["km_end"].to_numpy(dtype=float)
        zones = []
        cur_v, cur_start = v[0], max(ks[0], km)
        for j in range(1, len(v)):
            if v[j] != cur_v:
                zones.append((cur_start, ks[j], cur_v))
                cur_v, cur_start = v[j], ks[j]
        zones.append((cur_start, ke[-1], cur_v))
        # drop zones shorter than 300 m by merging them into the next one
        merged = []
        for a, b, vv in zones:
            if merged and (b - a) < 0.3:
                merged[-1] = (merged[-1][0], b, merged[-1][2])
            else:
                merged.append((a, b, vv))
        out = []
        for a, b, vv in merged[:n + 1]:
            out.append({"from_km": _f(a, 2), "to_km": _f(b, 2),
                        "in_km": _f(a - km, 2), "len_km": _f(b - a, 2),
                        "v": _f(vv, 0)})
        return out

    def _telemetry_health(self, now) -> dict:
        age = ((now - self.last_row_time).total_seconds()
               if self.last_row_time is not None else None)
        return {"rows": self.n_rows, "last_row_age_s": _f(age, 0),
                "gap": bool(self.last_row and self.last_row["gap"]),
                "gps_fixes": self.pos.n_fixes,
                "gps_rejected": self.pos.n_rejected,
                "sign_verified": self.signs.sign_verified,
                "net_verified": self.signs.net_verified}


# ------------------------------------------------------------ backfill ----

def km_series_from_gps(plan: Plan, gps: pd.DataFrame, index: pd.DatetimeIndex
                       ) -> pd.Series:
    """Project a GPS history onto the plan and interpolate km per row time.

    Sequential projection with a forward window, like the live tracker, so
    the loop passes come out in order. Used when a plan is loaded during
    the day and the integrator has to catch up on hours of telemetry with
    the right km attached to every second.
    """
    if gps is None or gps.empty:
        return None
    kms, times = [], []
    km_prev = None
    for t, r in gps.iterrows():
        try:
            if km_prev is None:
                km, cross = plan.project(float(r["lat"]), float(r["lon"]))
            else:
                km, cross = plan.project(float(r["lat"]), float(r["lon"]),
                                         after_km=max(km_prev - 0.3, 0.0),
                                         window_km=8.0)
                if cross > CROSS_OK_M:
                    km2, cross2 = plan.project(float(r["lat"]), float(r["lon"]),
                                               after_km=max(km_prev - 0.3, 0.0))
                    if cross2 < cross:
                        km, cross = km2, cross2
        except ValueError:
            continue
        if cross > CROSS_LOST_M:
            continue
        km_prev = km
        kms.append(km)
        times.append(t)
    if len(kms) < 2:
        return None
    s = pd.Series(kms, index=pd.DatetimeIndex(times))
    s = s[~s.index.duplicated(keep="last")].sort_index()
    from .planfile import _secs
    x = _secs(s.index)
    xi = _secs(index)
    y = np.interp(xi, x, s.to_numpy(), left=np.nan, right=np.nan)
    return pd.Series(y, index=index)
