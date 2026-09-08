"""Live energy monitoring: measured pack voltage/current against the plan.

The morning produces a plan - soc_trace() over the day's route - and the car
then produces `batteryVoltage` and `batteryCurrent` every few seconds. This
module integrates the measurement into an energy state and compares it to
the plan, so the deviation is visible while there is still time to react to
it (drive slower, drop a loop) rather than at the finish line.

Two ideas carry the whole module:

1. The running state is ENERGY, integrated from V*I. Voltage alone is not a
   state estimate on the 3.5-3.8 V plateau (see battery.py). `anchor()`
   exists for the rest phases, where it is.

2. The interesting number is not "how much is left" but "how much less than
   planned, and why". `LiveEnergy.status()` splits the deviation into a
   SOLAR part and a LOAD part:

       P_batt = P_load - P_solar          (all battery-side)

   Both sides are measured (`pvCurrent` gives the solar), so the residual
   against the plan can be attributed. That distinction drives different
   decisions: less sun than forecast is a reason to slow down, while more
   load than modelled at the planned speed means the drive coefficients are
   wrong and the rest of the day's plan is wrong with them.


SIGN AND TOPOLOGY - one settled, one open.

    SIGN: confirmed by the team as unchanged from SER-5, i.e. the pack
    current is NEGATIVE while discharging. `battery.py` uses positive =
    discharge internally, because that is the natural direction for a
    consumption model; `TelemetrySigns.batt_power()` is the single place
    where the two conventions meet. Do not "simplify" either away.

    TOPOLOGY: whether that current is already NET of the solar input is a
    property of the wiring, not of the sign convention, and it is STILL OPEN
    for this car. It only affects the solar/load split, never the total
    energy - but if wrong, the split invents a consumption problem the size
    of the solar input (counted twice: once missing from the solar side, once
    added to the load side), which would send the search to the drive
    coefficients while the fault sits in the sensor path.

    Settling it takes two minutes: park with the array deployed and the motor
    off, in the sun, and run `diagnose_signs()`. If |i_batt| ~ i_solar the
    shunt sees the net pack current (`i_batt_is_net = True`); if i_batt ~ 0
    while i_solar is not, it sees only the load side (False). Then set
    `net_verified=True`.

SOURCE OF THE SAMPLES

    This module takes plain numbers - timestamp, volts, amps - and does not
    know or care where they come from. When the telemetry API is defined,
    the only new code needed is a mapping from its field names onto
    `update()`; `FIELD_MAP` and `update_from_mapping()` exist for exactly
    that, so the API shape stays out of the physics.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging as lg
from pathlib import Path

import numpy as np
import pandas as pd

# ~~~ < include dir hack > ~~~
py_root = Path(__file__).resolve().parents[1]  # data_analysis/src/
if __package__ is None:
    import sys
    sys.path.insert(0, str(py_root.parent))
    __package__ = py_root.name + ".simulation"
elif __package__ == "":
    __package__ = py_root.name + ".simulation"
# ~~~ </include dir hack > ~~~

from ..ser_dataclasses import Battery_coeffs
from .battery import (capacity_wh, soc_from_wh, state_from_measurement,
                      terminal_voltage)

lg = lg.getLogger(__name__)


# Default field names for update_from_mapping(). Placeholders: the telemetry
# API is not defined yet. Override per call or replace this dict once it is;
# nothing else in the module refers to a source-specific name.
FIELD_MAP = {
    "time":    "time",
    "v_pack":  "v_pack",
    "i_batt":  "i_batt",
    "i_solar": "i_solar",
    "odo_km":  "odo_km",
    # optional, only if the API delivers them - see update() on precedence
    "v_solar": "v_solar",
    "p_solar": "p_solar",
}


@dataclass(frozen=True)
class TelemetrySigns:
    """How the telemetry numbers map onto the model's conventions.

    Two separate questions, two separate flags - the sign of a current and
    where its sensor sits are unrelated, and one being settled says nothing
    about the other.
    """
    i_batt_discharge_positive: bool = False
                                 # CONFIRMED by the team: unchanged from
                                 # SER-5, discharge is negative. Consistent
                                 # with that log (driving mean -7.3 A;
                                 # standing in sun with the motor off +0.8 A).
    sign_verified: bool = True    # the line above is a team statement, not a
                                 # placeholder. Flip to False if a rewired
                                 # 2026 loom ever puts it back in doubt.

    i_batt_is_net: bool = True   # True -> the pack current already nets out
                                 # the solar input (shunt in the pack lead,
                                 # MPPTs feeding the bus).
                                 # False -> it is the bus/load current and the
                                 # solar has to be subtracted.
                                 # STILL OPEN for this car. Inferred from the
                                 # SER-5 standstill data (pack +1.01 A while
                                 # the array delivered 1.05 A), but that is a
                                 # wiring property, not a sign convention, and
                                 # a different car may well route it
                                 # differently. Costs the whole solar/load
                                 # split if wrong - a phantom consumption
                                 # problem exactly the size of the solar
                                 # input, doubled.
    net_verified: bool = False   # set True once the standstill test below has
                                 # been run on THIS car.

    i_solar_positive: bool = True # solar current > 0 = generating

    # plausibility gates, applied per sample. A single garbage sample (a
    # dropped CAN byte, a half-written API response) destroys an integral
    # that nothing later re-anchors, so samples outside these bounds are
    # counted and discarded rather than integrated.
    # 31S nominal is 111.6 V, empty 77.5 V, full 130.2 V -> the voltage gates
    # follow from the pack, not from any log.  i_abs_max is set by the 50 A
    # fuse with headroom for regen peaks; tighten it once the real current
    # range is known.
    v_min: float = 70.0
    v_max: float = 135.0
    i_abs_max: float = 120.0

    def batt_power(self, v: float, i_batt: float, p_solar: float = 0.0):
        """Net battery power in W, POSITIVE = drawn from the pack."""
        sign = 1.0 if self.i_batt_discharge_positive else -1.0
        p = sign * v * i_batt
        if not self.i_batt_is_net:
            p = p - p_solar   # measured current was the load side
        return p


class DayPlan:
    """The morning plan, made queryable by time and by distance.

    Wraps a soc_trace() frame. Both lookups matter: the model's actual
    claim is energy per kilometre, so a deviation should be read at the
    same km, while the clock decides whether the day still fits. Reading
    only the time axis mixes "we are behind schedule" into the energy
    deviation and hides both.
    """

    def __init__(self, trace: pd.DataFrame):
        need = {"time", "cum_km", "wh_remaining", "dt_s"}
        if missing := need - set(trace.columns):
            raise ValueError(f"plan trace lacks columns {sorted(missing)} "
                             "- pass the frame from battery.soc_trace()")
        self.trace = trace.reset_index(drop=True)
        t = pd.to_datetime(self.trace["time"], utc=True)
        self.t0 = t.iloc[0]
        self._h = (t - self.t0).dt.total_seconds().to_numpy() / 3600.0
        self._km = self.trace["cum_km"].to_numpy()
        self._wh_rem = self.trace["wh_remaining"].to_numpy()
        dt_h = self.trace["dt_s"].to_numpy() / 3600.0
        # cumulative planned solar and load energy, Wh
        self._wh_solar = np.cumsum(
            self.trace.get("p_solar", pd.Series(0.0, index=self.trace.index))
            .to_numpy() * dt_h)
        load = (self.trace["p_net"].to_numpy()
                + self.trace.get("p_solar",
                                 pd.Series(0.0, index=self.trace.index)
                                 ).to_numpy())
        self._wh_load = np.cumsum(load * dt_h)

    # --- lookups -----------------------------------------------------
    def _hours(self, t) -> float:
        return (pd.Timestamp(t).tz_convert("UTC") - self.t0
                ).total_seconds() / 3600.0

    def at_km(self, km: float) -> dict:
        km = float(np.clip(km, self._km[0], self._km[-1]))
        return {
            "wh_remaining": float(np.interp(km, self._km, self._wh_rem)),
            "wh_solar":     float(np.interp(km, self._km, self._wh_solar)),
            "wh_load":      float(np.interp(km, self._km, self._wh_load)),
            "hours":        float(np.interp(km, self._km, self._h)),
        }

    def at_time(self, t) -> dict:
        h = float(np.clip(self._hours(t), self._h[0], self._h[-1]))
        return {
            "wh_remaining": float(np.interp(h, self._h, self._wh_rem)),
            "wh_solar":     float(np.interp(h, self._h, self._wh_solar)),
            "wh_load":      float(np.interp(h, self._h, self._wh_load)),
            "km":           float(np.interp(h, self._h, self._km)),
        }

    @property
    def total_km(self) -> float:
        return float(self._km[-1])

    @property
    def wh_end(self) -> float:
        return float(self._wh_rem[-1])


class LiveEnergy:
    """Streaming energy integrator with plan comparison.

    Feed it every telemetry sample; ask it for a status whenever the
    dashboard refreshes. Holds the whole day's samples so the deviation can
    be plotted, which is a few hundred kB at 1 Hz - not worth streaming out.

    Typical wiring in the chase car:

        live = LiveEnergy(batt, plan=DayPlan(trace),
                          wh_start=morning["wh_remaining"])
        ...
        live.update(t, v_pack, i_batt, i_solar=i_pv, odo_km=km)
        st = live.status()
    """

    def __init__(
        self,
        batt: Battery_coeffs,
        wh_start: float,
        plan: DayPlan = None,
        signs: TelemetrySigns = None,
        max_gap_s: float = 30.0,
    ):
        """
        Args:
            wh_start: energy in the pack at t0, from the morning anchor
                (battery.state_from_measurement(..., settled=True)).
            max_gap_s: samples further apart than this still get integrated
                (trapezoidally), but the energy bridged that way is counted
                separately and reported in `status()["wh_bridged"]`. A
                dropout while climbing is where an energy count silently
                goes wrong, so it must be visible rather than smoothed over.
        """
        self.batt = batt
        self.plan = plan
        self.signs = signs or TelemetrySigns()
        self.max_gap_s = max_gap_s
        if not self.signs.sign_verified:
            lg.warning(
                "battery current sign unverified (discharge_positive=%s). "
                "Until it is, every number this object produces may run the "
                "wrong way.", self.signs.i_batt_discharge_positive)
        if not self.signs.net_verified:
            lg.warning(
                "unverified: whether the pack current is already net of the "
                "solar input (assuming is_net=%s). Affects the solar/load "
                "split, not the total. Park in the sun with the motor off "
                "for two minutes and run diagnose_signs().",
                self.signs.i_batt_is_net)

        self.wh_start = float(wh_start)
        self.wh_used = 0.0          # net, from the pack
        self.wh_solar = 0.0         # measured generation
        self.wh_bridged = 0.0       # share of wh_used across gaps
        self.wh_spilled = 0.0       # integrated past a full pack, discarded
        self.n_gaps = 0
        self.n_mppt_dropouts = 0
        self.n_v_solar_odd = 0
        self.n_rejected = 0
        self._t_last = None
        self._p_last = None
        self._p_solar_last = 0.0
        self._rows = []
        self._anchors = []

    # --- ingest ------------------------------------------------------
    def update(self, t, v_pack: float, i_batt: float,
               i_solar=None, odo_km: float = None,
               v_solar: float = None, p_solar: float = None) -> dict:
        """Integrate one telemetry sample. Returns the current status().

        Args:
            t: sample timestamp, timezone-aware (or anything pandas parses
                as UTC)
            v_pack: `batteryVoltage`, V
            i_batt: `batteryCurrent`, A, in TELEMETRY convention - the
                sign flip lives in TelemetrySigns, not in the caller
            i_solar: solar current, A. One value per MPPT (a sequence, as
                the telemetry delivers it - four channels on this car) or a
                single total. Kept per-channel in the sample history, so a
                string that has dropped out is visible instead of hiding
                inside a smaller sum. Optional; without any solar input the
                solar/load split in status() is unavailable and only the
                total deviation is reported.
            v_solar: the matching voltage, V. One value per MPPT alongside
                `i_solar` (the products are then formed channel by channel)
                or a single scalar.

                This car reports the MPPT OUTPUT voltage and current, so
                `v_solar` is the bus voltage as each MPPT sees it. Passing
                it is slightly better than letting it default to `v_pack`:
                it carries the drop across the cabling between converter and
                pack sense point, and each channel is multiplied by the
                voltage its own current actually flowed at. It also makes
                _check_mppt_voltages() possible, which is a free check on
                the whole solar path.
            p_solar: solar power in W directly, if the API already provides
                it. Takes precedence over the current.

            Precedence for the solar power:
                p_solar  >  i_solar * v_solar  >  i_solar * v_pack
            Getting this wrong is not a rounding error: an array-side
            current multiplied by the pack voltage is wrong by the whole
            MPPT voltage ratio, which on a solar car is a factor of two or
            more, and it would land in delta_load_wh as a phantom
            consumption problem.
            odo_km: distance covered today, km. Optional but strongly
                recommended: it is what makes the comparison happen at the
                same point on the route instead of at the same clock time.
        """
        t = pd.Timestamp(t)
        t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")

        if not (self.signs.v_min <= v_pack <= self.signs.v_max) \
                or abs(i_batt) > self.signs.i_abs_max \
                or not np.isfinite([v_pack, i_batt]).all():
            self.n_rejected += 1
            lg.debug("rejected sample %s: V=%.1f I=%.1f", t, v_pack, i_batt)
            return self.status()

        p_sol = 0.0
        i_chan = None
        if p_solar is not None:
            # per-channel list or scalar; a NaN channel counts as zero,
            # not as "no solar data" - three live MPPTs and one dead one
            # is still a measurement. (np.isfinite() on a list raised.)
            p_arr = np.atleast_1d(np.asarray(p_solar, dtype=float))
            p_sol = max(0.0, float(np.nansum(p_arr)))
        elif i_solar is not None:
            sgn = 1.0 if self.signs.i_solar_positive else -1.0
            i_chan = sgn * np.atleast_1d(np.asarray(i_solar, dtype=float))
            if v_solar is None:
                v_chan = np.full(i_chan.shape, float(v_pack))
            else:
                v_chan = np.atleast_1d(np.asarray(v_solar, dtype=float))
                if v_chan.size == 1:
                    v_chan = np.full(i_chan.shape, float(v_chan[0]))
                elif v_chan.shape != i_chan.shape:
                    raise ValueError(
                        f"v_solar has {v_chan.size} values for "
                        f"{i_chan.size} solar currents - pass one per "
                        "channel or a single scalar")
            p_chan = np.where(np.isfinite(i_chan * v_chan), i_chan * v_chan, 0.0)
            p_sol = max(0.0, float(np.sum(p_chan)))
            self._check_mppt_channels(t, i_chan)
            if v_solar is not None:
                self._check_mppt_voltages(t, v_chan, v_pack)
        p_solar = p_sol
        p_batt = self.signs.batt_power(v_pack, i_batt, p_solar)

        if self._t_last is not None:
            dt_s = (t - self._t_last).total_seconds()
            if dt_s <= 0:
                self.n_rejected += 1        # out of order / duplicate
                return self.status()
            dt_h = dt_s / 3600.0
            dwh = 0.5 * (self._p_last + p_batt) * dt_h
            self.wh_used += dwh
            self.wh_solar += 0.5 * (self._p_solar_last + p_solar) * dt_h
            # The pack cannot hold more than its capacity. Integrating V*I
            # through a full pack (day 1 morning: the BMS throttles the
            # MPPTs, but the measured pack current is still what it is)
            # would otherwise book energy that was never stored, and the
            # comparison with the capped plan drifts by exactly that.
            over = (self.wh_start - self.wh_used) - capacity_wh(self.batt)
            if over > 0:
                self.wh_used += over
                self.wh_spilled += over
            if dt_s > self.max_gap_s:
                self.wh_bridged += abs(dwh)
                self.n_gaps += 1
                # throttled: a 1 Hz link that has dropped to 0.05 Hz would
                # otherwise produce one warning per sample and bury
                # everything else in the dashboard log.
                if self.n_gaps <= 5 or self.n_gaps % 50 == 0:
                    lg.warning("telemetry gap of %.0f s at %s, %.1f Wh "
                               "bridged (gap #%d, %.0f Wh total)",
                               dt_s, t, dwh, self.n_gaps, self.wh_bridged)

        self._t_last, self._p_last = t, p_batt
        self._p_solar_last = p_solar

        row_chan = ({f"i_mppt{k+1}": float(v) for k, v in enumerate(i_chan)}
                    if i_chan is not None and i_chan.size > 1 else {})
        self._rows.append({
            "time": t, "odo_km": odo_km, "v_pack": v_pack,
            "i_batt": i_batt, "p_batt": p_batt, "p_solar": p_solar,
            **row_chan,
            "wh_used": self.wh_used, "wh_solar": self.wh_solar,
            "wh_remaining": self.wh_start - self.wh_used,
        })
        return self.status()

    def _check_mppt_channels(self, t, i_chan) -> None:
        """Flag a channel that has gone dead while the others deliver.

        Four MPPTs mean a dead one costs roughly a quarter of the array -
        some 300 W at noon - and it does not look like a fault in the total,
        only like a worse day. Threshold is deliberately crude: a
        channel below 15 % of the median while the median is above 0.5 A.
        Partial shading of one string looks the same and is harmless, so
        this is a hint to look at the array, not an alarm.
        """
        if i_chan.size < 2:
            return
        med = float(np.nanmedian(i_chan))
        if med < 0.5:
            return                     # no sun worth speaking of
        dead = np.flatnonzero(i_chan < 0.15 * med)
        if dead.size:
            self.n_mppt_dropouts += 1
            if self.n_mppt_dropouts <= 3 or self.n_mppt_dropouts % 100 == 0:
                lg.warning(
                    "MPPT channel(s) %s near zero at %s while the others "
                    "deliver %.1f A - shading or a dead string (%d samples "
                    "so far)", (dead + 1).tolist(), t, med,
                    self.n_mppt_dropouts)

    def _check_mppt_voltages(self, t, v_chan, v_pack: float) -> None:
        """Sanity-check the MPPT OUTPUT voltages against the pack voltage.

        All converters feed the same bus, so every output voltage must sit
        within a cabling drop of `v_pack`. Two failures this catches for
        free, both of which would otherwise pass as plausible numbers:

        * a channel reading far off the others -> sensor or wiring fault on
          that MPPT, and its power contribution is wrong by that factor.
        * ALL channels well above v_pack -> the field being read is the
          INPUT voltage, not the output. The array sits far above the bus,
          so the solar power would come out too high by that ratio and the
          load side would absorb the difference as a phantom saving.
        """
        if v_chan.size == 0 or not np.isfinite(v_pack):
            return
        tol = max(3.0, 0.05 * v_pack)
        off = np.flatnonzero(np.abs(v_chan - v_pack) > tol)
        if off.size == 0:
            return
        self.n_v_solar_odd += 1
        if self.n_v_solar_odd > 5 and self.n_v_solar_odd % 200:
            return
        if off.size == v_chan.size and np.all(v_chan > v_pack * 1.3):
            lg.warning(
                "all MPPT voltages (%s V) are far above the pack (%.1f V) at "
                "%s - this looks like the INPUT voltage, not the output. The "
                "solar power would be too high by that ratio.",
                np.round(v_chan, 1).tolist(), v_pack, t)
        else:
            lg.warning(
                "MPPT output voltage(s) %s off the pack voltage %.1f V by "
                "more than %.1f V at %s (channels read %s V)",
                (off + 1).tolist(), v_pack, tol, t,
                np.round(v_chan[off], 1).tolist())

    def update_from_mapping(self, sample, mapping: dict = None) -> dict:
        """Ingest one sample given as a mapping (API response, CSV row, ...).

        The adapter boundary. `mapping` translates the source's field names
        onto this module's arguments, so a change in the telemetry API is a
        change to a dict and not to any code that computes something:

            FIELD_MAP = {"time": "ts", "v_pack": "pack_v",
                         "i_batt": "pack_a", "i_solar": "array_a",
                         "odo_km": "trip_km"}
            live.update_from_mapping(api_row, FIELD_MAP)

        A source that reports one current per MPPT can map `i_solar` onto a
        list of names by passing the summed value itself, or map it onto a
        single field holding the sequence - update() sums what it gets.

        Missing optional fields (`i_solar`, `v_solar`, `p_solar`, `odo_km`)
        are passed as None;
        a missing time, voltage or current raises, because silently
        integrating a sample with a guessed timestamp is worse than
        stopping.
        """
        m = {**FIELD_MAP, **(mapping or {})}
        get = (sample.get if hasattr(sample, "get")
               else lambda k, d=None: getattr(sample, k, d))
        args = {}
        for arg in ("time", "v_pack", "i_batt"):
            key = m[arg]
            if get(key) is None:
                raise KeyError(f"sample has no '{key}' for {arg}")
            args[arg] = get(key)
        for arg in ("i_solar", "odo_km", "v_solar", "p_solar"):
            args[arg] = get(m[arg]) if arg in m else None
        return self.update(args["time"], args["v_pack"], args["i_batt"],
                           i_solar=args["i_solar"], odo_km=args["odo_km"],
                           v_solar=args["v_solar"], p_solar=args["p_solar"])

    # --- re-anchor ---------------------------------------------------
    def anchor(self, v_pack: float, i_batt: float = 0.0,
               force: bool = False) -> dict:
        """Re-anchor the energy count on a voltage measurement AT REST.

        Use at the control stop or any stop of a few minutes. The applied
        correction is the interesting output: a consistently large one means
        `cell_capacity_ah`, the OCV curve or the resistances are off, and
        that is worth knowing on day 1 rather than on day 8.

        Refuses to anchor under load unless `force=True`, because on the
        plateau a wrong sag correction moves the estimate by more than the
        drift it is supposed to remove.
        """
        i_model = (i_batt if self.signs.i_batt_discharge_positive
                   else -i_batt)
        st = state_from_measurement(self.batt, v_pack, i_model, settled=True)
        if abs(i_batt) > 2.0 and not force:
            lg.warning("anchor refused: %.1f A is not at rest (would have "
                       "moved the state by %+.0f Wh)", i_batt,
                       st["wh_remaining"] - (self.wh_start - self.wh_used))
            return {"applied": False, **st}

        old = self.wh_start - self.wh_used
        corr = st["wh_remaining"] - old
        self.wh_start += corr
        self._anchors.append({"time": self._t_last, "v_pack": v_pack,
                              "wh_before": old, "wh_after": st["wh_remaining"],
                              "correction_wh": corr})
        lg.info("anchored at %.1f V: %.0f -> %.0f Wh (%+.0f Wh, %+.1f %% of "
                "pack)", v_pack, old, st["wh_remaining"], corr,
                100 * corr / capacity_wh(self.batt))
        return {"applied": True, "correction_wh": corr, **st}

    # --- report ------------------------------------------------------
    def status(self) -> dict:
        """Current state and, if a plan is loaded, the deviation from it.

        Deviation sign convention: POSITIVE = better than planned (more
        energy in the pack than the plan said at this point).
        """
        wh_rem = self.wh_start - self.wh_used
        out = {
            "wh_remaining":  round(wh_rem, 1),
            "soc_percent":   round(100 * soc_from_wh(self.batt, wh_rem), 1),
            "wh_used":       round(self.wh_used, 1),
            "wh_solar":      round(self.wh_solar, 1),
            "wh_bridged":    round(self.wh_bridged, 1),
            "wh_spilled":    round(self.wh_spilled, 1),
            "n_gaps":        self.n_gaps,
            "n_mppt_dropouts": self.n_mppt_dropouts,
            "n_v_solar_odd":   self.n_v_solar_odd,
            "n_rejected":    self.n_rejected,
            "v_pack_pred":   round(terminal_voltage(self.batt, wh_rem), 2),
            "odo_km":        None,
            "delta_wh":      None,
            "delta_solar_wh": None,
            "delta_load_wh": None,
            "schedule_min":  None,
            "projected_end_wh": None,
        }
        if not self._rows:
            return out
        last = self._rows[-1]
        out["odo_km"] = last["odo_km"]
        if self.plan is None:
            return out

        # compare at the same POINT ON THE ROUTE when the odometer is
        # available, otherwise fall back to the clock and say so
        if last["odo_km"] is not None:
            ref = self.plan.at_km(last["odo_km"])
            out["compared_at"] = "km"
            out["schedule_min"] = round(
                ((last["time"] - self.plan.t0).total_seconds() / 60.0
                 - ref["hours"] * 60.0), 1)   # >0 = behind schedule
        else:
            ref = self.plan.at_time(last["time"])
            out["compared_at"] = "time"

        out["delta_wh"] = round(wh_rem - ref["wh_remaining"], 1)
        if self.wh_solar > 0.0:
            out["delta_solar_wh"] = round(self.wh_solar - ref["wh_solar"], 1)
            wh_load = self.wh_used + self.wh_solar
            # >0 = drew MORE than planned, i.e. the bad direction
            out["delta_load_wh"] = round(wh_load - ref["wh_load"], 1)

        out["projected_end_wh"] = self.project_end_wh()
        return out

    def project_end_wh(self, min_km: float = 10.0) -> float:
        """End-of-day energy if the observed deviation keeps its rate.

        Scales the REMAINING planned load and solar by the ratios observed
        so far, rather than carrying the absolute deviation forward flat: a
        car that is 5 % hungrier than modelled will be 5 % hungrier for the
        rest of the day too, and a flat offset would understate that badly
        on a long afternoon.

        Returns None until there is enough distance to form a ratio - an
        extrapolation from the first two kilometres is noise with a decimal
        point.
        """
        if self.plan is None or not self._rows:
            return None
        last = self._rows[-1]
        km = last["odo_km"]
        if km is None or km < min_km:
            return None
        ref = self.plan.at_km(km)
        wh_rem = self.wh_start - self.wh_used

        load_done = self.wh_used + self.wh_solar
        r_load = (load_done / ref["wh_load"]) if ref["wh_load"] > 0 else 1.0
        r_solar = ((self.wh_solar / ref["wh_solar"])
                   if (ref["wh_solar"] > 0 and self.wh_solar > 0) else 1.0)

        load_left = max(0.0, self.plan._wh_load[-1] - ref["wh_load"])
        solar_left = max(0.0, self.plan._wh_solar[-1] - ref["wh_solar"])
        return round(wh_rem - (r_load * load_left - r_solar * solar_left), 1)

    def to_frame(self) -> pd.DataFrame:
        """All accepted samples, for plotting measured against planned."""
        df = pd.DataFrame(self._rows)
        if self.plan is not None and not df.empty:
            if df["odo_km"].notna().any():
                df["wh_plan"] = [
                    self.plan.at_km(k)["wh_remaining"] if pd.notna(k)
                    else np.nan for k in df["odo_km"]]
            else:
                df["wh_plan"] = [self.plan.at_time(t)["wh_remaining"]
                                 for t in df["time"]]
            df["delta_wh"] = df["wh_remaining"] - df["wh_plan"]
        return df

    @property
    def anchors(self) -> pd.DataFrame:
        return pd.DataFrame(self._anchors)


# ------------------------------------------------------------ diagnosis ----

def diagnose_signs(df: pd.DataFrame, v="batteryVoltage", i="batteryCurrent",
                   i_pv="pvCurrent", i_motor="motorCurrent",
                   speed="speed") -> dict:
    """Work out the TelemetrySigns of a car instead of assuming them.

    Works on anything that becomes a DataFrame: a logged CSV, a dump of the
    telemetry API, a hand-recorded two-minute test. Pass the column names of
    the source. Run it before any energy count is believed - see the module
    docstring for why a flipped sign is not obvious from the output.

    Uses two situations that cannot be confused:

    * standing in the sun with no motor current -> the pack must be
      CHARGING, so the discharge sign follows from the sign of `i`.
    * driving -> the pack must be DISCHARGING on average.

    The `speed` and `i_motor` columns are only used to FIND those two
    situations. If the source has no motor current, pass the same column as
    `speed` twice and select the standstill window by hand instead.

    Whether the current is already net of the solar shows up at standstill:
    if |i| is close to i_pv, the shunt sees the net pack current; if it is
    close to zero while i_pv is not, it sees only the load.

    Returns a dict with the suggested field values and the evidence.
    """
    d = df.copy()
    d.columns = [c.strip() for c in d.columns]
    d = d[(d[v] > 60) & (d[v] < 140) & (d[i].abs() < 120)]

    still = d[(d[speed] < 1) & (d[i_pv] > 0.5) & (d[i_motor].abs() < 1e-6)]
    drive = d[d[speed] > 30]
    out = {"n_standing_sun": len(still), "n_driving": len(drive)}

    if len(drive):
        out["i_mean_driving"] = round(float(drive[i].mean()), 2)
        out["i_batt_discharge_positive"] = bool(drive[i].mean() > 0)
    if len(still):
        i_s, pv = float(still[i].mean()), float(still[i_pv].mean())
        out["i_mean_standing_sun"] = round(i_s, 3)
        out["i_pv_mean_standing_sun"] = round(pv, 3)
        out["i_batt_is_net"] = bool(abs(abs(i_s) - pv) < 0.3 * max(pv, 1e-9))
        if "i_batt_discharge_positive" in out:
            charging_positive = i_s > 0
            if charging_positive == out["i_batt_discharge_positive"]:
                out["conflict"] = (
                    "standstill and driving disagree on the sign - check "
                    "whether the standstill window really had no load")
    if not len(still):
        out["note"] = ("no standing-in-sun sample: park the car with the "
                       "array deployed and the motor off for two minutes, "
                       "then re-run. Without it the net/load question stays "
                       "open and the solar-vs-load split is unverified.")
    return out


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # Demonstration only. This log is from SER-5 / ESC2024 - a DIFFERENT car
    # with different wiring - so the output says what diagnose_signs() looks
    # like, not what this car's TelemetrySigns are. Re-run it on a log from
    # the current car to settle i_batt_is_net, then set net_verified=True.
    fp = Path(__file__).parents[4] / "data/analytics/SER5TELE_ESC2024.CSV"
    if fp.exists():
        raw = pd.read_csv(fp, skipinitialspace=True)
        print(f"diagnose_signs on {fp.name} (different car, illustration):")
        for k, val in diagnose_signs(raw).items():
            print(f"  {k:32s} {val}")
    else:
        print(f"no telemetry log at {fp}")
