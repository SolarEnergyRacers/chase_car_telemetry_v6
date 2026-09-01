
from   datetime import datetime, timedelta, timezone, time as daytime
import numpy as np
import pandas as pd
from   pathlib import Path
from   zoneinfo import ZoneInfo

# ~~~ < include dir hack > ~~~
# normalize include dir root
py_root = Path(__file__).resolve().parents[1]  # data_analysis/src/
if __package__ is None:
    import sys
    sys.path.insert(0, str(py_root.parent))
    __package__ = py_root.name + ".driving"
elif __package__ == "":
    __package__ = py_root.name + ".driving"
# ~~~ </include dir hack > ~~~

from ..geojson.read_geojson import (
    lonlat2angular, get_paths)
from ..environment.environment   import (
    solar_power_gen, forward_windspeed, air_density)
from ..environment.sun_angles   import (
    calculate_SZA_from_datetime, atmospheric_travel_distance, )
from ..ser_dataclasses import (
    Car_coeffs, Environment, ) 


RACE_TZ = ZoneInfo("Africa/Johannesburg")  # SAST, UTC+2, no DST

VALID_DATERANGE = (
    datetime(2026, 9,  3,             tzinfo=RACE_TZ),
    datetime(2026, 9, 17, 23, 59, 59, tzinfo=RACE_TZ),
)
VALID_TIMERANGE = (  # (every day, local race time)
    daytime( 5, 0),
    daytime(23, 0),
)   # start, end time of race -> assert that timestamps used are valid.
    # (Sun angles depend on date being correct)


def validate_time(start: datetime, end: datetime):
    errs = []
    for name, t in (("start", start), ("end", end)):
        if t.tzinfo is None or t.utcoffset() is None:
            errs.append(f"{name} time must be timezone-aware (is {t})")
    if errs:
        return errs  # further comparisons would raise TypeError

    if not (VALID_DATERANGE[0] <= start <= VALID_DATERANGE[1]):
        errs.append(f"start time must be between {VALID_DATERANGE[0]} "
            f"and {VALID_DATERANGE[1]} (is {start})")
    if not (VALID_DATERANGE[0] <= end <= VALID_DATERANGE[1]):
        errs.append(f"end time must be between {VALID_DATERANGE[0]} "
            f"and {VALID_DATERANGE[1]} (is {end})")

    # compare wall clock time in race local timezone, not in the given one
    start_local = start.astimezone(RACE_TZ)
    end_local   = end.astimezone(RACE_TZ)
    if not (end_local.time() <= VALID_TIMERANGE[1]):
        errs.append(f"end time {end_local} is after race end.")
    if not (start_local.time() >= VALID_TIMERANGE[0]):
        errs.append(f"start time {start_local} is before race start.")
    return errs


def solar_p_gen(car: Car_coeffs, env: Environment, tm: datetime,
        lat: float, lon: float, panel_angle: float):
    if tm.tzinfo is None or tm.utcoffset() is None:
        raise ValueError(f"tm must be timezone-aware (is {tm})")
    sun_angle = calculate_SZA_from_datetime(
        tm.astimezone(timezone.utc), lat, lon)
    return solar_power_gen(env.sun_power, env.sun_visibility, sun_angle,
            car.panel_sqm, car.panel_eff, panel_angle)


def drive_basis(speed: float, v_speed: float, fwd_airspeed: float = None,
        rho: float = None, rho_ref: float = 1.02) -> np.ndarray:
    """Basis functions of the drive power series, in coefficient order
    (v1, v2, v3, v4, vu, vd). Units: W per unit of the respective coefficient.

    Shared on purpose by drive_power() and the day-1 coefficient fit. The fit
    solves `P_batt - aux_power + P_solar = drive_basis(...) @ coeffs` by least
    squares, which only returns meaningful coefficients if the model and the
    design matrix use the identical basis. One function is what stops the two
    from drifting apart unnoticed.

    Args:
        speed: ground speed, m/s
        v_speed: vertical speed, m/s (positive climbing, negative descending)
        fwd_airspeed: airspeed along the driving direction, m/s.
            None -> assume still air, i.e. equal to `speed`.
        rho: local air density, kg/m^3. None -> no density scaling, the
            coefficients are used exactly as anchored/fitted.
        rho_ref: the density `v3_coeff` was anchored/fitted at. Must be
            car.rho_ref; only defaulted here so the function stays usable
            without a Car_coeffs at hand.
    """
    if fwd_airspeed is None:
        fwd_airspeed = speed
    v = speed
    vu = v_speed if v_speed > 0 else 0
    vd = v_speed if v_speed < 0 else 0
    rho_scale = 1.0 if rho is None else rho / rho_ref
    v_ = np.array([
        v,                    # v1: rolling resistance, force is constant
        v**2,                 # v2: no physical counterpart, coefficient is 0
        # v3: aerodynamic drag. Force ~ airspeed^2, power = force * ground
        # speed, hence airspeed^2 * v and NOT v^3 (which ignored the wind
        # entirely, since airspeed used to appear only in the v4 term).
        # abs() preserves the sign: a tailwind stronger than the ground speed
        # must push the car, not cost energy.
        fwd_airspeed * abs(fwd_airspeed) * v * rho_scale,
        fwd_airspeed**4,      # v4: no physical counterpart, coefficient is 0
        vu,                   # vu: J per metre climbed
        vd,                   # vd: J per metre descended (vd is negative here,
                              #     so a positive vd_coeff yields recovery)
    ])
    return v_


def drive_power(car: Car_coeffs, speed: float, v_speed: float, fwd_airspeed:
        float = None, rho: float = None):
    """Battery-side drive power in W. Excludes car.aux_power, which is
    constant over time rather than distance and belongs in the energy
    integral (see total_Ws_for_lap())."""
    v_ = drive_basis(speed, v_speed, fwd_airspeed, rho, car.rho_ref)
    M  = np.array([car.v1_coeff, car.v2_coeff, car.v3_coeff, 
        car.v4_coeff, car.vu_coeff, car.vd_coeff])
    return v_ @ M


def Ws_for_stop_start(car: Car_coeffs, speed: float) -> float:
    """Net energy of one stop-start cycle in Ws: braking to a halt on
    arrival and accelerating back to `speed` on departure.

    Needs no extra parameter. vu_coeff = m*g/eta gives m/eta = vu_coeff/g,
    and vd_coeff = m*g*eta_regen gives m*eta_regen = vd_coeff/g, so
        E_net = 0.5 * (vu_coeff - vd_coeff) / g * speed^2
    Assumes braking is always regenerative (team's stated practice).
        at 72.5 km/h: 0.5 * (2507-1692) / 9.81 * 20.139^2 = 16.85 kJ = 4.68 Wh
    About 7 stops a day (1 control stop + 6 loop stops) -> ~33 Wh.
    Kinetic energy is otherwise absent from the model, which is correct for
    the constant-speed segments but not across a stop.
    """
    return 0.5 * (car.vu_coeff - car.vd_coeff) / 9.81 * speed**2


def apply_speed_limit(
    distances: np.array,
    max_speeds: np.array,
    delta_time: timedelta,
    allow_time_overrun: bool = False
) -> np.ndarray:
    """Calculate road segment speeds based on speed limit and target time.
    Args:
        distances: road segment lengths, in m
        max_speeds: expected achievable speed for each segment, in km/h.
            Use routing speed (speed_route) rather than the legal limit:
            it is available for nearly every segment and reflects what
            can actually be driven.
        delta_time: total time allocated for the road segment array
        allow_time_overrun: if True, return limits if exceeding max_speeds
            instead of raising ValueError(). 
            Use with care, no notice is given if this has happened.
    Returns:
        array of speed values corresponding to each road segment
    """
    if not len(distances) == len(max_speeds):
        raise ValueError(
            f"distances and max_speeds must be of the same length, "
            f"but are {len(distances)} and {len(max_speeds)} respectively.")

    if np.isnan(distances).any() or np.isnan(max_speeds).any():
        raise ValueError(
            f"distances and max_speeds must not contain NaN "
            f"({np.isnan(distances).sum()} / {np.isnan(max_speeds).sum()} found). "
            f"Note the last row of a compiled route holds no segment data.")
    
    speed_lim = max_speeds / 3.6  # km/h -> m/s
    tot_distance = np.sum(distances)
    ideal_speed = tot_distance / delta_time.total_seconds()
    speeds = np.ones(len(distances), dtype=float) * ideal_speed

    maxiter = 0
    while any(speeds > speed_lim):
        mask = (speeds >= speed_lim)
        # assignment only -> '>=' on float is needed to keep previously fixed
        # in mask; but break criterium with '>' requires not including these
        speeds = np.where(mask, speed_lim, speeds)
        t_lock = np.sum(distances[mask] / speeds[mask])
        t_free = delta_time.total_seconds() - t_lock
        if t_free < 0:
            if allow_time_overrun:
                return speed_lim
            raise ValueError(
                "cannot make journey in time due to speed limits. "
                f"({tot_distance/1e3:.1f}km in {delta_time})")
        # d_free = np.sum(np.where(~mask, distances, 0))
        d_free = np.sum(distances[~mask])
        ideal_speed = d_free / t_free
        # speeds = np.where(mask, speeds, ideal_speed)
        speeds[~mask] = ideal_speed

        if (maxiter := maxiter+1) > 1000:
            # at most one per speed limit steps, but routing speed does not 
            # follow standard grid of k*10 values for integer k
            raise RuntimeError(
                f"Maximum iteration: Could not conclusively apply speed "
                f"limits within {maxiter} steps")
    return speeds

def total_Ws_for_lap(
    route: pd.DataFrame,
    env: pd.DataFrame,
    car: Car_coeffs,
    start_time: datetime,
    end_time: datetime = None,
    delta_time: timedelta = None,
) -> float:
    """Calculate net energy consumption for a route within specified time.
    Args:
        route: df as provided by compile_route() / stitch_routes()
        env: df with environment info over time, index = time (UTC).
            Columns must match Environment.to_tuple() order.
        car: Car_coeffs performance parameters
        start_time: start of journey, UTC (sun angles depend on it)
        end_time: target arrival time (mutually exclusive with delta_time)
        delta_time: journey duration (mutually exclusive with end_time)
    Returns:
        net energy in Ws. Positive means drawn from the battery.
        Includes car.aux_power over the journey time and, via air_density(),
        the altitude/temperature dependence of the aerodynamic term.
        Does NOT include stop-start losses -> add Ws_for_stop_start() per
        control stop / loop stop, nor standing phases (no distance, so no
        segment exists for them here).
    """
    if delta_time is None:
        if end_time is None:
            raise ValueError(
                "Must specify either end_time or delta_time (neither given)")
        delta_time = end_time - start_time
    else:
        if not(end_time is None):
            raise ValueError(
                "Must specify either end_time or delta_time (both given)")
        end_time = start_time + delta_time

    if errs := validate_time(start_time, end_time):
        raise ValueError("; ".join(errs))

    n_env = len(Environment().to_tuple())
    if env.shape[1] != n_env:
        raise ValueError(
            f"env must have exactly {n_env} columns matching "
            f"Environment.to_tuple() order, but has {env.shape[1]}: "
            f"{list(env.columns)}")
    if env.index.tz is None:
        raise ValueError("env.index must be timezone-aware")
    
    # n nodes -> n-1 segments. Last row of a compiled route holds no segment.
    distance   = route["distance"   ].to_numpy()[:-1]
    azimuth    = route["azimuth"    ].to_numpy()[:-1]
    max_speeds = route["speed_route"].to_numpy()[:-1]
    lon        = route["longitude"  ].to_numpy()
    lat        = route["latitude"   ].to_numpy()
    altitude   = route["altitude"   ].to_numpy()
    incline    = np.diff(altitude)                      # m, filtered SRTM
    mid_lon    = 0.5 * (lon[:-1] + lon[1:])
    mid_lat    = 0.5 * (lat[:-1] + lat[1:])
    mid_alt    = 0.5 * (altitude[:-1] + altitude[1:])   # m, for air density

    speed = apply_speed_limit(distance, max_speeds, delta_time)  # m/s
    dt    = distance / speed                                     # s

    # segment mid-times, then one single interpolation pass over env
    t_mid = (start_time
        + pd.to_timedelta(np.cumsum(dt) - 0.5*dt, unit='s')).round('ms')
    env_seg = (
        env.reindex(env.index.union(t_mid))
        .sort_index()
        .bfill().ffill()  # extrapolate in zero-order hold
        .interpolate(method="time")
        .loc[t_mid]
    )

    Ws_total = 0.0
    for i in range(len(distance)):
        env_now = Environment.from_tuple(env_seg.iloc[i])
        tm      = t_mid[i].to_pydatetime()

        p_solar = solar_p_gen(car, env_now, tm, mid_lat[i], mid_lon[i], 0)
        # ^ car cannot climb an incline that would matter for solar angle

        # forward_windspeed() is POSITIVE for a headwind: wind_direction is
        # where the wind comes FROM (meteorological convention, as delivered
        # by Open-Meteo), azimuth is where the car goes TO, so equal angles
        # mean driving into the wind. The airspeed the body sees is therefore
        # the SUM. (This was a minus before, which turned every headwind into
        # a tailwind - harmless while v3 was v**3 and the wind did not enter
        # the physics at all, dangerous now that it does.)
        headwind = forward_windspeed(
            env_now.wind_speed, env_now.wind_direction, azimuth[i])
        fwd_airspeed = speed[i] + headwind
        v_speed = incline[i] / dt[i]
        rho = air_density(
            mid_alt[i], env_now.temperature, env_now.pressure_msl)

        p_motor = drive_power(car, speed[i], v_speed, fwd_airspeed, rho)
        Ws_total += dt[i] * (p_motor + car.aux_power - p_solar)

    return Ws_total

if __name__ == "__main__":
    ROOT = Path(__file__).parents[4]
    fp = ROOT / "data/roadinfo/test_segment_sasolburg.geojson.pkl"
    route = pd.read_pickle(fp)

    # Coefficients now live in Car_coeffs with their derivation in the
    # comments there. Do NOT override them here: the old placeholders
    # (v1_coeff = 36, vd_coeff = vu_coeff/3) silently replaced the whole
    # calibrated set for anyone running this file directly.
    car = Car_coeffs()

    # sanity check: does the set still reproduce the anchor it was built on?
    # 14 Wh/km at 72.5 km/h, flat, still air, aux included.
    v_ref = 72.5 / 3.6
    Jm = (drive_power(car, v_ref, 0.0) + car.aux_power) / v_ref
    print(f"anchor check: {Jm:6.2f} J/m = {Jm/3.6:5.2f} Wh/km "
          f"at 72.5 km/h (expected 50.40 / 14.00)")

    start_time = datetime(2026, 9, 5, 8, tzinfo=RACE_TZ)
    end_time   = datetime(2026, 9, 5, 12, tzinfo=RACE_TZ)

    ts  = pd.date_range(start_time, end_time, freq="1h")
    env = pd.DataFrame(
        [Environment().to_tuple()] * len(ts), index=ts)

    Ws = total_Ws_for_lap(route, env, car,
        start_time = start_time,
        end_time   = end_time,
    )
    print(f"{route.index[-1]/1e3:.1f}km in {end_time-start_time}: "
          f"E={Ws/1e3:.0f}kJ ({Ws/3600_000:.3f}kWh)")