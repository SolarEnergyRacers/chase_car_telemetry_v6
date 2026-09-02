"""Battery state: SoC, remaining energy and terminal voltage.

Ported from SER_strategy_sosol_2026/myFunctions.py
(`calculate_soc_wh_under_load`, `calculate_battery_state_from_wh`).
Kept from the original: the internal-resistance correction from terminal
voltage to open-circuit voltage, and the OCV/SoC interpolation table.
Changed on purpose:

1. No module-level `import config`. All pack parameters live in
   `Battery_coeffs` (ser_dataclasses.py), like Car_coeffs, so a scenario can
   be run against a different pack without touching a global.

2. Energy is the integral of OCV over charge, not `soc * V_nominal * Ah`.
   The old linear form overstates the remaining energy, and it does so
   exactly where it matters - near the floor:
       soc = 90 %  ->  +0.4 %
       soc = 50 %  ->  +5.8 %
       soc = 30 %  ->  +9.4 %
       soc = 20 %  -> +12.1 %
   At 20 % that is ~300 Wh of the pack, i.e. about 20 km of range that does
   not exist. Full-pack energy from the OCV integral is 2909 Wh against
   2879 Wh nominal, so the top of the curve is not the problem; the sag
   below the 3.5 V knee is.

3. Both directions use the same curve. `soc_from_wh(wh_from_soc(x)) == x`
   holds to interpolation accuracy, which was not true of the two original
   functions (one divided by nominal Wh, the other multiplied by it, but
   the SoC in between came from a table with a different implied mean
   voltage).

4. Pack resistance includes a wiring/fuse/contactor/shunt term
   (`pack_r_extra_ohm`), because the cell datasheet resistance is not what
   a voltage sensor at the pack terminals sees.

DELIBERATELY NOT MODELLED: the individual cell. Everything here works on
the total pack voltage, i.e. pack / 31 is treated as "the" cell. The BMS
does report `voltageMin` / `voltageAvg` / `voltageMax` (see
data/analytics/SER5TELE_ESC2024.CSV), and the weakest cell is what actually
hits the cut-off - but the energy left below the point where the spread
starts to matter is a few tens of Wh, which does not change a loop
decision. Revisit together with a better OCV curve; until then the pack
average is the number, and `voltageMin` is something to watch on the
dashboard rather than to compute with.

HOW TO USE IT IN THE STRATEGY - the important part:

    Run the day on ENERGY, anchor on VOLTAGE.

`total_Ws_for_lap()` and `Ws_for_stop()` already produce Ws. Accumulate
those (`soc_trace()`), and use a voltage measurement only to re-anchor the
accumulator - morning before the start, at the control stop, evening.
Reason: on the 3.5-3.8 V plateau the curve gives 1.3 % SoC per 10 mV of
cell voltage, and the load sag it has to be corrected for is 1.5-2.6 V at
the pack at 25 A (0.5-0.9 V at 10 A). A 20 % resistance error at cruise is
already several percent of SoC, so a voltage reading under load is a sanity
check, not a state estimate.

Anchor only when the pack has been at rest for a few minutes. Straight
after load the cell voltage recovers over minutes (diffusion), and the
single-resistor correction does not model that; while charging from the
array the same effect has the opposite sign and the anchor comes out too
high.
"""

from __future__ import annotations

from functools import lru_cache
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

lg = lg.getLogger(__name__)

_GRID = 2001  # points on the SoC grid of the energy curve. 0.05 % steps;
              # the interpolation error against a 10x finer grid is < 0.01 Wh
              # per cell, i.e. below 2 Wh on the pack.


# ---------------------------------------------------------------- curve ----

@lru_cache(maxsize=8)
def _energy_curve(ocv_soc: tuple, ocv_v: tuple, capacity_ah: float):
    """(soc_grid, wh_per_cell_grid) - energy in a cell from empty to soc.

    wh(soc) = integral_0^soc OCV(s) * capacity_ah ds, trapezoidal on _GRID.
    Cached because it is called per segment inside soc_trace(); the key is
    the curve plus the capacity, so a changed pack invalidates it.
    """
    soc = np.linspace(0.0, 1.0, _GRID)
    v = np.interp(soc, np.asarray(ocv_soc), np.asarray(ocv_v))
    dwh = np.diff(soc) * capacity_ah * 0.5 * (v[:-1] + v[1:])
    return soc, np.concatenate(([0.0], np.cumsum(dwh)))


def pack_r_ohm(batt: Battery_coeffs) -> float:
    """Pack series resistance, ohm. S cells in series, P strings parallel."""
    return (batt.cell_r_i_ohm * batt.serial_cells / batt.parallel_cells
            + batt.pack_r_extra_ohm)


def capacity_wh(batt: Battery_coeffs) -> float:
    """Full-pack energy from the OCV integral, Wh. Use this rather than
    Car_coeffs.battery_cap (nominal V * Ah) - see module docstring."""
    _, wh = _energy_curve(batt.ocv_soc, batt.ocv_v, batt.cell_capacity_ah)
    return float(wh[-1]) * batt.serial_cells * batt.parallel_cells


def usable_wh(batt: Battery_coeffs) -> float:
    """Energy the strategy is allowed to plan with, Wh."""
    return capacity_wh(batt) * batt.usable


# ------------------------------------------------------- soc <-> energy ----

def wh_from_soc(batt: Battery_coeffs, soc):
    """Remaining pack energy in Wh at state of charge `soc` (0..1)."""
    soc_g, wh_g = _energy_curve(batt.ocv_soc, batt.ocv_v,
                                batt.cell_capacity_ah)
    soc = np.clip(np.asarray(soc, dtype=float), 0.0, 1.0)
    wh = np.interp(soc, soc_g, wh_g) * batt.serial_cells * batt.parallel_cells
    return float(wh) if np.isscalar(soc) or wh.ndim == 0 else wh


def soc_from_wh(batt: Battery_coeffs, wh):
    """State of charge (0..1) for a remaining pack energy in Wh."""
    soc_g, wh_g = _energy_curve(batt.ocv_soc, batt.ocv_v,
                                batt.cell_capacity_ah)
    wh_cell = (np.asarray(wh, dtype=float)
               / (batt.serial_cells * batt.parallel_cells))
    soc = np.interp(np.clip(wh_cell, 0.0, wh_g[-1]), wh_g, soc_g)
    return float(soc) if np.isscalar(wh) or soc.ndim == 0 else soc


def soc_from_ocv_cell(batt: Battery_coeffs, v_cell):
    """State of charge from a single cell's OPEN-CIRCUIT voltage."""
    return np.interp(np.asarray(v_cell, dtype=float),
                     np.asarray(batt.ocv_v), np.asarray(batt.ocv_soc))


def ocv_cell_from_soc(batt: Battery_coeffs, soc):
    """Open-circuit voltage of one cell at state of charge `soc`."""
    return np.interp(np.clip(np.asarray(soc, dtype=float), 0.0, 1.0),
                     np.asarray(batt.ocv_soc), np.asarray(batt.ocv_v))


# --------------------------------------------------------- measurement ----

def state_from_measurement(
    batt: Battery_coeffs,
    v_pack: float,
    i_pack: float = 0.0,
    settled: bool = False,
) -> dict:
    """Pack state from a terminal-voltage (and optional current) reading.

    Replaces `calculate_soc_wh_under_load()`. Works on the total pack
    voltage; `v_ocv_cell` is only pack / serial_cells, reported because the
    OCV curve is per cell and it makes a wrong `serial_cells` obvious.

    Args:
        v_pack: measured pack voltage, V (telemetry `batteryVoltage`)
        i_pack: measured pack current, A, POSITIVE = discharge
            (telemetry `batteryCurrent` - check its sign convention once
            against a known charging phase before trusting this).
        settled: True if the pack has been at rest for some minutes. Only
            then is the returned SoC good enough to re-anchor an energy
            accumulator on; otherwise treat it as a plausibility check.

    Returns a dict with v_ocv_pack, v_ocv_cell, sag_v, soc, soc_percent,
    wh_remaining, wh_above_floor and `trust`.
    """
    r = pack_r_ohm(batt)
    sag = i_pack * r                       # >0 discharge -> terminal below OCV
    v_ocv_pack = v_pack + sag
    v_ocv_cell = v_ocv_pack / batt.serial_cells

    if not (batt.ocv_v[0] - 0.15 <= v_ocv_cell <= batt.ocv_v[-1] + 0.05):
        lg.warning(
            "cell OCV %.3f V is outside the curve (%.2f..%.2f V). Check the "
            "sign of i_pack, serial_cells and the resistance.",
            v_ocv_cell, batt.ocv_v[0], batt.ocv_v[-1])

    soc = float(soc_from_ocv_cell(batt, v_ocv_cell))
    wh = wh_from_soc(batt, soc)

    on_plateau = 3.45 <= v_ocv_cell <= 3.95
    trust = ("anchor" if settled and not on_plateau else
             "anchor_weak" if settled else "check_only")

    return {
        "v_ocv_pack":     round(v_ocv_pack, 3),
        "v_ocv_cell":     round(v_ocv_cell, 4),
        "sag_v":          round(sag, 3),
        "soc":            soc,
        "soc_percent":    round(soc * 100, 1),
        "wh_remaining":   round(wh, 1),
        "wh_above_floor": round(wh - (1.0 - batt.usable) * capacity_wh(batt), 1),
        "trust":          trust,
    }


def terminal_voltage(batt: Battery_coeffs, wh_remaining, i_pack=0.0):
    """Expected terminal voltage, V, at a remaining energy and a current.

    Replaces `calculate_battery_state_from_wh()`, plus the load term the
    original did not have. Two uses: plotting a predicted voltage next to
    the measured one, and checking that a planned power stays inside the
    50 A fuse - `p / terminal_voltage(...)` is the current, and at a low SoC
    that is noticeably more current for the same power.
    """
    soc = soc_from_wh(batt, wh_remaining)
    v = ocv_cell_from_soc(batt, soc) * batt.serial_cells - np.asarray(
        i_pack, dtype=float) * pack_r_ohm(batt)
    return float(v) if np.ndim(v) == 0 else v


def measured_wh(timestamps, v_pack, i_pack) -> float:
    """Energy drawn from the pack over a telemetry window, Wh.

    Trapezoidal integral of v*i. This is the ground truth the model gets
    compared against, and the same P_batt series the day-1 coefficient fit
    needs (`drive_basis()` solves P_batt - aux + P_solar = basis @ coeffs).
    Positive = discharged.

    Args:
        timestamps: pandas DatetimeIndex or Series, or seconds as float
    """
    t = pd.to_datetime(pd.Series(timestamps))
    if t.notna().all():
        h = (t - t.iloc[0]).dt.total_seconds().to_numpy() / 3600.0
    else:
        h = np.asarray(timestamps, dtype=float) / 3600.0
    p = np.asarray(v_pack, dtype=float) * np.asarray(i_pack, dtype=float)
    trapz = getattr(np, "trapezoid", None) or np.trapz
    return float(trapz(p, h))


# ------------------------------------------------------------- day trace ---

def soc_trace(
    batt: Battery_coeffs,
    detail: pd.DataFrame,
    wh_start: float = None,
    soc_start: float = None,
    extra_ws: float = 0.0,
) -> pd.DataFrame:
    """Add battery state columns to a `total_Ws_for_lap(return_detail=True)`
    frame. Returns a copy; does not modify `detail`.

    This is the join between the drive model and the pack: driving.py knows
    the energy per segment, this knows what that does to the battery.

    Args:
        wh_start / soc_start: initial state, exactly one of them. Take it
            from state_from_measurement() at the morning anchor, or from
            usable_wh() for a full-pack scenario.
        extra_ws: energy in Ws to spread over the trace before it starts -
            stop-start losses (Ws_for_stop_start) and standing phases
            (Ws_for_stop, negative when charging) have no route segment, so
            they cannot appear in `detail`. Pass their sum here to keep the
            end state right, or splice them in yourself if you care where
            in the day they fall.

    New columns: wh_used_cum, wh_remaining, soc, v_pack_pred, i_pack_pred.
    Sets `.attrs["soc_floor_hit"]` and logs a warning if the trace crosses
    the usable floor - that is the signal for the loop count, so it must
    not be silent.
    """
    if (wh_start is None) == (soc_start is None):
        raise ValueError("give exactly one of wh_start / soc_start")
    if wh_start is None:
        wh_start = wh_from_soc(batt, soc_start)

    out = detail.copy()
    used = (np.cumsum(out["Ws"].to_numpy()) + extra_ws) / 3600.0
    out["wh_used_cum"] = used
    out["wh_remaining"] = wh_start - used
    out["soc"] = soc_from_wh(batt, out["wh_remaining"].to_numpy())
    out["v_pack_pred"] = terminal_voltage(batt, out["wh_remaining"].to_numpy())
    # p_net is battery-side already (motor + aux - solar)
    out["i_pack_pred"] = out["p_net"].to_numpy() / out["v_pack_pred"].to_numpy()
    out["v_pack_pred"] = terminal_voltage(
        batt, out["wh_remaining"].to_numpy(), out["i_pack_pred"].to_numpy())

    floor = (1.0 - batt.usable) * capacity_wh(batt)
    hit = bool((out["wh_remaining"] < floor).any())
    out.attrs["soc_floor_hit"] = hit
    out.attrs["wh_start"] = wh_start
    out.attrs["wh_end"] = float(out["wh_remaining"].iloc[-1])
    if hit:
        km = float(out.loc[out["wh_remaining"] < floor, "cum_km"].iloc[0])
        lg.warning(
            "usable floor (%.0f Wh, %.0f %% of %.0f Wh) crossed at km %.1f - "
            "this plan does not fit the battery.",
            floor, 100 * (1 - batt.usable), capacity_wh(batt), km)
    return out


if __name__ == "__main__":
    lg_root = __import__("logging")
    lg_root.basicConfig(level=lg_root.INFO, format="%(levelname)s %(message)s")

    batt = Battery_coeffs()
    print(f"pack {batt.serial_cells}S{batt.parallel_cells}P, "
          f"{batt.cell_capacity_ah} Ah/cell, R_pack = "
          f"{pack_r_ohm(batt)*1000:.0f} mOhm")
    print(f"capacity  {capacity_wh(batt):7.0f} Wh (OCV integral)")
    print(f"  nominal {batt.v_nominal*batt.capacity_ah:7.0f} Wh "
          f"(V_nom * Ah - the old linear basis)")
    print(f"usable    {usable_wh(batt):7.0f} Wh "
          f"({batt.usable:.0%}), floor at "
          f"{(1-batt.usable)*capacity_wh(batt):.0f} Wh")

    print("\nround trip soc -> Wh -> soc:")
    for s in (0.2, 0.5, 0.9):
        wh = wh_from_soc(batt, s)
        lin = s * batt.v_nominal * batt.capacity_ah
        print(f"  soc {s:.0%}: {wh:6.0f} Wh, back to "
              f"{soc_from_wh(batt, wh):.4f}; linear would say {lin:6.0f} Wh "
              f"({100*(lin/wh-1):+.1f} %)")

    print("\nmeasurement, 111.6 V pack:")
    for i in (0.0, 10.0, 25.0):
        st = state_from_measurement(batt, 111.6, i, settled=(i == 0.0))
        print(f"  I = {i:5.1f} A -> sag {st['sag_v']:4.2f} V, "
              f"OCV cell {st['v_ocv_cell']:.3f} V, soc {st['soc_percent']:5.1f} %, "
              f"{st['wh_remaining']:6.1f} Wh, trust={st['trust']}")
    naive = soc_from_ocv_cell(batt, 111.6 / batt.serial_cells)
    print(f"  ignoring the sag at 25 A costs "
          f"{100*(state_from_measurement(batt,111.6,25.)['soc']-naive):.1f} "
          f"percentage points of SoC")
