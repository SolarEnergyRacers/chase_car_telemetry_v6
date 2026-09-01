
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
    forward_windspeed, air_density, RouteWeather)
# NOTE: sun_angles / solar_power_gen are deliberately NOT imported any more.
# The sun elevation, the atmosphere and the cloud cover all sit inside the
# irradiance that Open-Meteo delivers, so re-deriving them here would double
# count. sun_angles.py stays in the tree for plotting and for sanity checks.
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


def solar_p_gen(car: Car_coeffs, irradiance):
    """Electrical power out of the array, in W. Scalar or array.

    Args:
        irradiance: W/m2 in the plane of the panel. Use
            Environment.ghi while driving (flat-lying panel, GHI is by
            definition the horizontal plane) and Environment.gti_tracking
            while parked with the panel aimed at the sun.

    That is the whole model. Everything optical - sun elevation, air mass,
    cloud cover - is already inside the irradiance figure; everything
    electrical - cell efficiency, mismatch, wiring, MPPT, soiling, cell
    temperature - is inside car.panel_eff. See Car_coeffs.panel_eff for the
    derivation chain of the 0.21.
    """
    return irradiance * car.panel_sqm * car.panel_eff


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
    rho_scale = 1.0 if rho is None else np.asarray(rho, dtype=float) / rho_ref
    # broadcast so scalars and arrays both work: returns (6,) for scalar
    # input and (6, n) for arrays of length n, which is what lets
    # total_Ws_for_lap() and the coefficient fit run without a Python loop.
    v, vz, a, sc = np.broadcast_arrays(
        np.asarray(speed,        dtype=float),
        np.asarray(v_speed,      dtype=float),
        np.asarray(fwd_airspeed, dtype=float),
        np.asarray(rho_scale,    dtype=float))
    v_ = np.array([
        v,                    # v1: rolling resistance, force is constant
        v**2,                 # v2: no physical counterpart, coefficient is 0
        # v3: aerodynamic drag. Force ~ airspeed^2, power = force * ground
        # speed, hence airspeed^2 * v and NOT v^3 (which ignored the wind
        # entirely, since airspeed used to appear only in the v4 term).
        # abs() preserves the sign: a tailwind stronger than the ground speed
        # must push the car, not cost energy.
        a * np.abs(a) * v * sc,
        a**4,                 # v4: no physical counterpart, coefficient is 0
        np.maximum(vz, 0.0),  # vu: J per metre climbed
        np.minimum(vz, 0.0),  # vd: J per metre descended (negative here, so a
                              #     positive vd_coeff yields recovery)
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
    return M @ v_   # M first, so (6,) -> scalar and (6,n) -> (n,)


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


def Ws_for_stop(
    car: Car_coeffs,
    weather: RouteWeather,
    start_time: datetime,
    duration: timedelta,
    lat: float,
    lon: float,
    tracked: bool = True,
) -> float:
    """Net energy over a standing phase, in Ws. Negative means charged.

    Distance is zero, so no route segment exists for a stop and
    total_Ws_for_lap() cannot see it. The motor term drops out, the
    auxiliaries keep running, and the panel keeps producing.

    Args:
        tracked: True -> panel aimed at the sun (gti_tracking), which is what
            the team does at the 30-minute control stop. False -> panel left
            flat (ghi).

    Irradiance is sampled once, at the MIDDLE of the stop. That matches the
    team's practice of aiming the panel once, at the sun position halfway
    through, and not tracking afterwards: the sun moves 7.5 deg in 30
    minutes, so +/-3.75 deg off-aim, a cosine loss of 0.21 %. Continuous
    tracking would gain nothing measurable; aiming at all gains a lot
    (roughly 140 Wh over the control stop versus a flat panel).
    """
    mid = start_time + duration / 2
    env = Environment.from_series(
        weather.sample([mid], [lat], [lon]).iloc[0])
    irradiance = env.gti_tracking if tracked else env.ghi
    p_solar = solar_p_gen(car, irradiance)
    return duration.total_seconds() * (car.aux_power - p_solar)


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
    weather: RouteWeather,
    car: Car_coeffs,
    start_time: datetime,
    end_time: datetime = None,
    delta_time: timedelta = None,
    return_detail: bool = False,
):
    """Calculate net energy consumption for a route within specified time.

    BREAKING CHANGE: the second argument used to be an `env` DataFrame of
    Environment.to_tuple() rows indexed by time, i.e. one weather state for
    the entire route. It is now a RouteWeather, which varies along the route
    as well as over time - the reason the weather module resamples the route
    in the first place.

    Args:
        route: df as provided by compile_route() / stitch_routes()
        weather: RouteWeather for the day, from fetch_weather_along_route().
            Looked up by coordinate, so a stitched route with loops works
            without a distance mapping onto the weather sample points.
        car: Car_coeffs performance parameters
        start_time: start of journey, UTC
        end_time: target arrival time (mutually exclusive with delta_time)
        delta_time: journey duration (mutually exclusive with end_time)
        return_detail: if True, return a per-segment DataFrame instead of a
            single number - the basis for an SoC trace over the day, and for
            seeing WHERE the energy goes rather than only how much.
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

    if not isinstance(weather, RouteWeather):
        raise TypeError(
            "weather must be a RouteWeather (from fetch_weather_along_route). "
            "The old `env` DataFrame of Environment rows is no longer "
            "accepted - weather now varies along the route, not only in time.")

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

    # segment mid-times, then one vectorised weather lookup for all of them
    t_mid = (start_time
        + pd.to_timedelta(np.cumsum(dt) - 0.5*dt, unit='s')).round('ms')
    env = weather.sample(t_mid, mid_lat, mid_lon)

    # forward_windspeed() is POSITIVE for a headwind: wind_direction is where
    # the wind comes FROM (meteorological convention, as delivered by
    # Open-Meteo), azimuth is where the car goes TO, so equal angles mean
    # driving into the wind. The airspeed the body sees is therefore the SUM.
    # (This was a minus before, which turned every headwind into a tailwind -
    # harmless while v3 was v**3 and the wind did not enter the physics at
    # all, dangerous now that it does.)
    headwind = forward_windspeed(
        env["wind_speed"].to_numpy() * car.wind_height_factor,
        env["wind_direction"].to_numpy(), azimuth)
    fwd_airspeed = speed + headwind
    v_speed = incline / dt
    rho = air_density(mid_alt, env["temperature"].to_numpy(),
                      env["pressure_msl"].to_numpy())

    p_motor = drive_power(car, speed, v_speed, fwd_airspeed, rho)
    p_solar = solar_p_gen(car, env["ghi"].to_numpy())  # flat panel -> GHI
    Ws_seg  = dt * (p_motor + car.aux_power - p_solar)

    if not return_detail:
        return float(np.sum(Ws_seg))

    cum_m = np.cumsum(distance)
    return pd.DataFrame({
        "time":         t_mid,
        "cum_km":       cum_m / 1e3,
        "dt_s":         dt,
        "speed_kmh":    speed * 3.6,
        "altitude_m":   mid_alt,
        "v_speed":      v_speed,
        "headwind":     headwind,
        "airspeed":     fwd_airspeed,
        "rho":          rho,
        "ghi":          env["ghi"].to_numpy(),
        "p_motor":      p_motor,
        "p_aux":        car.aux_power,
        "p_solar":      p_solar,
        "p_net":        p_motor + car.aux_power - p_solar,
        "Ws":           Ws_seg,
        "Wh_cum":       np.cumsum(Ws_seg) / 3600.0,
    })

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

    start_time = datetime(2026, 9, 10, 9, tzinfo=RACE_TZ)
    end_time   = datetime(2026, 9, 10, 13, tzinfo=RACE_TZ)

    # Weather now comes from Open-Meteo for the actual route and day; this
    # needs network access on the first run and the cache afterwards.
    from ..environment.environment import fetch_weather_along_route
    weather = fetch_weather_along_route(
        fp.with_suffix(""), start_time.date(), spacing_km=5.0)

    detail = total_Ws_for_lap(route, weather, car,
        start_time = start_time,
        end_time   = end_time,
        return_detail = True,
    )
    Ws = detail["Ws"].sum()
    km = detail["cum_km"].iloc[-1]
    print(f"{km:.1f}km in {end_time-start_time}: "
          f"E={Ws/1e3:.0f}kJ ({Ws/3600_000:.3f}kWh, {Ws/3600/km:.2f}Wh/km)")
    print(f"  solar {detail['p_solar'].mul(detail['dt_s']).sum()/3600:.0f}Wh, "
          f"motor {detail['p_motor'].mul(detail['dt_s']).sum()/3600:.0f}Wh, "
          f"aux {detail['p_aux'].mul(detail['dt_s']).sum()/3600:.0f}Wh")