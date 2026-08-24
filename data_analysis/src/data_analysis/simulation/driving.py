
from   datetime import datetime, timedelta, time as daytime
import numpy as np
import pandas as pd
from   pathlib import Path

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
    solar_power_gen, forward_windspeed)
from ..environment.sun_angles   import (
    calculate_SZA_from_datetime, atmospheric_travel_distance, )
from ..ser_dataclasses import (
    Car_coeffs, Environment, ) 


VALID_DATERANGE = (
    datetime(2026, 9,  3),
    datetime(2026, 9, 17, 23, 59, 59),
)
VALID_TIMERANGE = (  # (every day)
    daytime( 5, 0),
    daytime(23, 0),
)   # start, end time of race -> assert that timestamps used are valid.
    # (Sun angles depend on date being correct)


def validate_time(start: datetime, end: datetime):
    errs = []
    if not (VALID_DATERANGE[0] <= start <= VALID_DATERANGE[1]):
        errs.append(f"start time must be between {VALID_DATERANGE[0]} "
            f"and {VALID_DATERANGE[1]} (is {start})")
    if not (VALID_DATERANGE[0] <= end <= VALID_DATERANGE[1]):
        errs.append(f"end time must be between {VALID_DATERANGE[0]} "
            f"and {VALID_DATERANGE[1]} (is {end})")

    if not (end.time() <= VALID_TIMERANGE[1]):
        errs.append(f"end time {end} is after race end.")
    if not (start.time() >= VALID_TIMERANGE[0]):
        errs.append(f"start time {start} is before race start.")
    return errs


def solar_p_gen(car: Car_coeffs, env: Environment, tm: datetime, coord: tuple, 
        panel_angle: float):
    sun_angle = calculate_SZA_from_datetime(tm, *coord[:2])
    return solar_power_gen(env.sun_power, env.sun_visibility, sun_angle,
            car.panel_sqm, car.panel_eff, panel_angle)


def drive_power(car: Car_coeffs, speed: float, v_speed: float, fwd_airspeed: 
        float = None):
    if fwd_airspeed is None:
        fwd_airspeed = speed
    v = speed
    vu = v_speed if v_speed > 0 else 0
    vd = v_speed if v_speed < 0 else 0
    v_ = np.array([v, v**2, v**3, fwd_airspeed**4, vu, vd])
    M  = np.array([car.v1_coeff, car.v2_coeff, car.v3_coeff, 
        car.v4_coeff, car.vu_coeff, car.vd_coeff])
    return v_ @ M


def apply_speed_limit(
    distances: np.array,
    limits: np.array, 
    delta_time: timedelta,
    allow_time_overrun: bool = False
) -> np.ndarray:
    """Calculate road segment speeds based on speed limit and target time.
    Args:
        distances: road segment lengths, in m
        limits: expected max. speed for each segment, in km/h
        delta_time: total time allocated for the road segment array
        allow_time_overrun: if True, return limits if exceeding delta_time
            instead of raising ValueError(). 
            Use with care, no notice is given if this has happened.
    Returns:
        array of speed values corresponding to each road segment
    """
    if not len(distances) == len(limits):
        raise ValueError(
            f"distances and limits must be of the same length, "
            f"but are {len(distances)} and {len(limits)} respectively.")

    speed_lim = limits / 3.6  # km/h -> m/s
    ideal_speed = np.sum(distances) / delta_time.total_seconds()
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
                return limits
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
    # path: pd.DataFrame,  # index = ?
    path: np.ndarray,  # index = ?
    roadinfo: pd.DataFrame,
    env: pd.DataFrame,  # index = time
    car: Car_coeffs,
    start_time: datetime,
    end_time: datetime = None,
    delta_time: timedelta = None,
    fill_speedlimit: float = None
):
    """Calculate net energy consumption for a route within specified time.
    Args:
        path: list of (lon, lat, alt) entries
        roadinfo: list of dict with speed (and more? info for each path entry)
        env: Dataframe with environment info over time
        car: Car_coeffs performance parameters
        start_time: start time for journey (for sun + environment)
        end_time: target return time (mutually exclusive with delta_time)
        delta_time: journey time (mutually exclusive with end_time)
        fill_speedlimit: If given, fill it in for missing speed values
    """
    error_list = []
    # speed limits / different speed parts?
    if delta_time is None:
        if end_time is None:
            raise ValueError(
                "Must specify either end_time or delta_time (neither given)")
        delta_time = end_time - start_time
    else:
        if not(end_time is None):
            raise ValueError("Must specify either end_time or delta_time (both given)")
        end_time = start_time + delta_time

    error_list.extend(validate_time(start_time, end_time))

    apath = path
    rpath = lonlat2angular(path)
    cols  = ["azimuth", "distance", "incline"][:rpath.shape[1]]
    rpath = pd.DataFrame(rpath, columns=cols)
    print(f"path len = {np.sum(rpath["distance"])}")

    tot_distance = np.sum(rpath["distance"])
    ideal_speed = tot_distance / delta_time.total_seconds()  # ideal
    assert ideal_speed > 0.0 and ideal_speed < 50.0, (
        f"nonsensical avg. speed value: {speed*3.6:.2f}km/h")

    if not fill_speedlimit is None:
        roadinfo["speeds"] = roadinfo["speeds"].fillna(fill_speedlimit)
    # speed_lim = roadinfo["speeds"] / 3.6  # km/h -> m/s

    # speeds = np.ones(len(rpath)) * ideal_speed
    # mask = (speeds > speed_lim)
    # while any(mask):
    #     speeds = np.where(mask, speed_lim, speeds)
    #     t_lock = rpath["distance"][~mask] / speeds[~mask]
    #     t_free = delta_time.total_seconds() - t_lock
    #     if t_free < 0:
    #         raise ValueError(
    #             "cannot make journey in time due to speed limits. "
    #             f"({tot_distance/1e3:.1f}km in {delta_time})"
    #         )
    #     d_free = np.sum(np.where(mask, rpath["distance"], 0))
    #     ideal_speed = d_free / t_free
    #     speeds = np.where(mask, ideal_speed, speeds)

    #     mask = np.where(speeds < speed_lim)[0]
    #     # speeds = np.clip(speeds, 0, roadinfo["speed"])

    #     for want, limit in zip(speeds, roadinfo["speed"]):
    #         if want > limit:
    #             speeds_ok = 0
    # rpath["speed"] = speeds

    rpath["speed"] = apply_speed_limit(
        rptath["distance"], 
        roadinfo["speeds"], 
        delta_time, 
    )

    now = start_time
    Ws_total = 0
    for idx, segment in rpath.iterrows():
        # approximate halfway distance as value for segment
        coord = np.mean([apath[idx], apath[idx+1]], axis=0)
        dt = segment["distance"] / speed
        tm = now + timedelta(seconds = 0.5*dt)

        env_now = (
            env.reindex(env.index.union([tm]))
            .sort_index()
            .bfill().ffill()  # extrapolate in zero-order hold
            .interpolate(method="time")
            .loc[tm]
        )
        env_now = Environment.from_tuple(env_now)

        p_solar = solar_p_gen(car, env_now, tm, coord, 0)
        # ^ car cannot climb an incline that would matter in terms of solar angle

        wind_fwd = forward_windspeed(env_now.wind_speed, 
            env_now.wind_direction, segment["azimuth"])
        # ^ only apply forward wind speed
        fwd_airspeed = speed - wind_fwd  # todo: check +/-

        if "incline" in segment:
            v_speed = segment["incline"] / dt
        else:
            v_speed = 0

        p_motor = drive_power(car, speed, v_speed, fwd_airspeed)

        Ws_solar = dt * p_solar
        Ws_motor = dt * p_motor
        Ws_total += Ws_motor - Ws_solar
        #  ---
        now += timedelta(seconds = dt)
    return Ws_total


if __name__ == "__main__":
    import json
    ROOT = Path(__file__).parents[3]
    fp = ROOT / "data/roadinfo/test_segment_sasolburg.geojson"
    with open(fp, 'r') as file:
        j = json.load(file)
    path = get_paths(j)[0]
    # todo: add coordinates, speed limits, expected shadow, etc. to rel. paths
    # todo: remind user of linear interpolation in env[time] -> wind may be weird
    # also averaging -> cannot mix incine and decline

    print(path)
    if len(path[0]) == 2:
        # add altitude placeholder
        path = np.column_stack([path, np.ones_like(path.T[0])*500])
    path[2, 2] = 600.
    print(path)

    # 10kWh/km = 36'000kWs/km = 36J/m ? -> m/s * J/m = J/s = W
    # 72Wh/km = 260kWs/km = 260J/m [twike]
    # 5kWh/700km = 7Wh/km = 25.7J/m [agoria Bluepoint Atlas]
    # E=m*g*h -> 200kg*9.81 = 1962J/m ?
    car = Car_coeffs()
    car.v1_coeff = 36
    car.vu_coeff = 1962
    car.vd_coeff = (- car.vu_coeff) / 3  # get some back ??

    start_time = datetime(2026,  9,  5, 8,  0,  0)
    # env0 = Environment(at_time=start_time)
    env0 = Environment()
    env0.sun_power  = 0.
    env0.wind_speed = 0.
    env = [env0.to_tuple()]
    ts = [datetime(2026, 9, 5)]
    env = pd.DataFrame(env, index=pd.to_datetime(ts))

    roadinfo = []  # todo

    Ws = total_Ws_for_lap(path, roadinfo, env, car, 
        start_time = start_time,
        end_time   = datetime(2026,  9,  5, 10, 15,  0),
    )
    print(f"E={Ws/1e3:.0f}kJ ({Ws/3600_000:.3f}kWh)")
