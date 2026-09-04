

import hashlib
import json
import logging as lg
import time
from   datetime import date, datetime, timezone
from   pathlib import Path

import numpy as np
import pandas as pd
import requests

# ~~~ < include dir hack > ~~~
# normalize include dir root (same pattern as roadinfo/roadinfo_api.py)
py_root = Path(__file__).resolve().parents[1]  # data_analysis/src/
if __package__ is None:
    import sys
    sys.path.insert(0, str(py_root.parent))
    __package__ = py_root.name + ".environment"
elif __package__ == "":
    __package__ = py_root.name + ".environment"
# ~~~ </include dir hack > ~~~

from .sun_angles import *
from ..geojson.read_geojson import resolve_geo_to_coords, lonlat2angular

if __name__ == "__main__":
    # demo run, see below
    lg.basicConfig(level=lg.INFO, handlers=[lg.StreamHandler()])
lg = lg.getLogger(__name__)


# -----------------------------------------------------------------------------
# physics (unchanged)

def solar_power_gen(
    sun_power,
    sun_visibility,
    sun_angle,
    panel_sqm,
    panel_eff,
    panel_angle,
):
    """DEPRECATED clear-sky panel model. Superseded by measured/forecast
    irradiance from Open-Meteo: GHI already contains the atmosphere, the
    cloud cover and the sun elevation, so neither the extinction term nor
    the cosine term below is needed any more.

    Kept only for reference and for offline experiments without weather
    data. The energy path in driving.py uses solar_p_gen(car, irradiance),
    i.e. `irradiance * panel_sqm * panel_eff`, with GHI while driving and
    sun-tracking GTI while parked.
    """
    sun_percentage = (  # negligible?
        atmospheric_travel_distance(sun_angle) /
        atmospheric_travel_distance(0))
    sun_power = sun_power * sun_visibility**sun_percentage
    panel_cosine = np.cos((sun_angle-panel_angle)/180*np.pi)
    return sun_power * panel_cosine * panel_sqm * panel_eff


def forward_windspeed(
    wind_speed,
    wind_direction,
    car_direction
):
    """Component of the wind along the driving direction, in m/s.

    wind_direction follows the meteorological convention used by Open-Meteo:
    the direction the wind comes FROM. car_direction is the azimuth the car
    travels TO. Equal angles therefore mean the car drives into the wind, and
    the return value is POSITIVE for a headwind, negative for a tailwind.

    Airspeed seen by the body is `ground_speed + forward_windspeed(...)`.
    """
    angle = (car_direction - wind_direction)
    return np.cos(angle/180*np.pi)*wind_speed


def air_density(altitude_m, temp_c, pressure_msl_pa=101325.0):
    """Air density in kg/m^3 from route altitude and weather temperature.

    Barometric formula on the ISA profile for the altitude, then ideal gas
    with the actual temperature. Humidity is ignored (< 1 %).

    Contributions over this race, for scale:
        altitude  1480 m (Sasolburg) -> 120 m (Paarl):   18 %
        daily temperature swing 5 °C -> 28 °C:            8 %
        synoptic pressure (highs/lows):                 1-2 %
    The v3 (aero) term scales linearly with this, so at 80 km/h the altitude
    span alone is worth ~2.3 Wh/km.
    """
    p = pressure_msl_pa * (1.0 - 2.25577e-5 * altitude_m) ** 5.2559
    return p / (287.05 * (temp_c + 273.15))


# -----------------------------------------------------------------------------
# weather (Open-Meteo) - setup

cachedir = Path(__file__).parent / "weather_cache"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL  = "https://archive-api.open-meteo.com/v1/archive"

# Open-Meteo serves a rolling past/future window (recent past + up to ~16
# days forecast) from FORECAST_URL; anything older than that only exists in
# the immutable historical archive at ARCHIVE_URL, which lags
# ~5 days behind "now". This distinction also drives cache behaviour below:
# archive answers never change for a given query and are cached forever,
# forecast answers can change as newer model runs come in and are refreshed
# every time (see _get_cache()/_set_cache()).
FORECAST_PAST_DAYS_MAX = 92
FORECAST_FUTURE_DAYS_MAX = 16

DEFAULT_HOURLY_VARS = [
    "shortwave_radiation",       # GHI, W/m^2, horizontal - use while driving
    "direct_normal_irradiance",  # DNI, W/m^2, perpendicular to sun
    "diffuse_radiation",         # DHI, W/m^2, horizontal
    "global_tilted_irradiance",  # GTI, W/m^2, per `tilt`/`azimuth` below -
                                  # with tilt=azimuth=nan (the default here)
                                  # this is a 2-axis sun tracker, i.e. what a
                                  # parked car aligning its panel to the sun
                                  # at a control stop / loop break sees.
    "wind_speed_10m",            # m/s (see wind_speed_unit in _get_api())
    "wind_direction_10m",        # deg, direction the wind comes FROM
    "temperature_2m",            # deg C  - for air_density()
    "pressure_msl",              # hPa    - for air_density(), needs *100 -> Pa
]

# Open-Meteo also offers "*_instant" radiation variables - values AT the
# timestamp, which it derives from the backward averages via an analytical
# solution over the solar zenith angle. They are NOT used, and the reason
# is measured, not assumed.
#
# An instant value is a point sample; a backward mean is the integral over
# the hour. Checked against a two-variable API pull for 26.1S 27.9E on
# 2026-09-05:
#
#   hourly energy from the means (exact by definition)  5182 Wh/m2
#   trapezoid of the instant values                     5323 Wh/m2  +2.7 %
#   means re-timed to interval centres, integrated      5182 Wh/m2  +-0.0 %
#
# The worst single hour was 15:00: the mean says 433 Wh/m2, the two instant
# values at the hour boundaries (741 and 386 W/m2) give 564. A cloud passed
# through inside that hour. The mean saw it; the point samples at the edges
# did not. Sub-hourly cloud is exactly what matters for a solar car, so the
# means are the better datum.
#
# The centre re-timing conserving energy exactly is an identity, not a
# coincidence: with linear interpolation between interval centres, the
# integral over an hour IS the mean at that hour's centre.
#
# The one thing the instant values are better at is the shape at sunrise
# and sunset - see the note in _centre_backward_means() on the residual
# artefact there. Not enough to carry a second variable set for, so they
# are not requested at all.
#
# Not an option at all: minutely_15. Outside Central Europe and North
# America Open-Meteo interpolates it from the hourly values, so in South
# Africa it adds sample points but no information.

# NOTE: changing this list changes the cache key (see _cache_key()), so the
# first run after an edit re-fetches instead of using the existing cache.

if not cachedir.is_dir():
    if cachedir.exists():
        raise RuntimeError(
            f"weather cache dir {cachedir} exists but is not a directory")
    lg.info(f"creating weather cache '{cachedir}'...")
    cachedir.mkdir()
else:
    lg.info(f"using weather cache '{cachedir}'...")


# -----------------------------------------------------------------------------
# weather - route resampling

def resample_route(coords: np.ndarray, spacing_km: float):
    """Resample a route to (approximately) even spacing along its length.

    Uses linear interpolation of lon/lat between the original vertices,
    weighted by arc-length (via lonlat2angular's haversine distances) - a
    fine approximation at the vertex spacing typical of GPS-derived routes
    and the km-scale spacing used here;

    Args:
        coords: (N,2) or (N,3) array of [lon, lat, (alt)], e.g. straight
            from read_geojson.resolve_geo_to_coords().
        spacing_km: target distance between resampled points, in km.

    Returns:
        (points, distance_km):
            points: (M,2) array of [lat, lon] (note the order - lat first,
                matching fetch_weather()'s argument order).
            distance_km: (M,) cumulative distance from the route start, km.
    """
    deltas = lonlat2angular(coords)  # [compass, distance_m, (rise)] per seg.
    seg_dist_m = deltas[:, 1]
    cum_dist_m = np.concatenate([[0.0], np.cumsum(seg_dist_m)])
    total_m = cum_dist_m[-1]

    n_points = max(2, int(np.floor(total_m / (spacing_km * 1e3))) + 1)
    target_m = np.linspace(0.0, total_m, n_points)

    lons = coords[:, 0]
    lats = coords[:, 1]
    out_lat = np.interp(target_m, cum_dist_m, lats)
    out_lon = np.interp(target_m, cum_dist_m, lons)
    points = np.stack([out_lat, out_lon], axis=1)
    return points, target_m / 1e3


# -----------------------------------------------------------------------------
# weather - canonical names

# Maps our field names onto Open-Meteo variable names plus a unit factor.
# This dict is the ONLY place in the code base that knows Open-Meteo's
# spelling; everything downstream (Environment, driving.py, the coefficient
# fit) uses the keys on the left.
CANONICAL_VARS = {
    "ghi":            ("shortwave_radiation",        1.0),    # W/m2 horizontal
    "gti_tracking":   ("global_tilted_irradiance",   1.0),    # W/m2, 2-axis
    "dni":            ("direct_normal_irradiance",   1.0),    # W/m2
    "dhi":            ("diffuse_radiation",          1.0),    # W/m2
    "wind_speed":     ("wind_speed_10m",             1.0),    # m/s at 10 m
    "wind_direction": ("wind_direction_10m",         1.0),    # deg, FROM
    "temperature":    ("temperature_2m",             1.0),    # deg C
    "pressure_msl":   ("pressure_msl",             100.0),    # hPa -> Pa
}

# Fallbacks used when a variable was not fetched (e.g. an older cache entry
# without temperature/pressure). Missing variables are logged, not raised:
# a warning is easier to act on than a KeyError deep in a simulation loop.
CANONICAL_DEFAULTS = {
    "ghi": 0.0, "gti_tracking": 0.0, "dni": 0.0, "dhi": 0.0,
    "wind_speed": 0.0, "wind_direction": 0.0,
    "temperature": 20.0, "pressure_msl": 101325.0,
}

# Open-Meteo's radiation variables are BACKWARD AVERAGES over the preceding
# hour, and the timestamp marks the END of that hour: the value at 06:00 is
# the mean between 05:00 and 06:00. Their own documentation lists
# shortwave_radiation, diffuse_radiation, direct_normal_irradiance and
# global_tilted_irradiance as "preceding hour mean", and their blog spells
# it out - the instantaneous value at 06:00 is much higher.
#
# Everything here interpolates linearly between samples, which treats them
# as instantaneous values AT the timestamp. Uncorrected that shifts the
# whole solar curve half an hour too late: sunrise arrives late and sunset
# lingers. Measured on the day-2 cache, the 06:00-08:00 window came out at
# 645 Wh instead of 1046 Wh - a 38 % underestimate of the free morning
# charge - while an evening window was overestimated by 6 %.
#
# The fix is to attribute each mean to the MIDDLE of its interval. Applied
# once, when the frame is assembled, so that at() and sample() and any
# direct consumer of .df all see the same corrected series.
BACKWARD_MEAN_VARS = frozenset({
    "shortwave_radiation", "diffuse_radiation", "direct_normal_irradiance",
    "global_tilted_irradiance", "direct_radiation", "terrestrial_radiation",
})
BACKWARD_MEAN_WINDOW = pd.Timedelta(hours=1)

_EPOCH = pd.Timestamp("1970-01-01", tz="UTC")


def _epoch_seconds(idx) -> np.ndarray:
    """Timestamps -> float seconds since the epoch, for use with np.interp.

    Deliberately NOT `.astype("int64")`: pandas >= 3 keeps the unit of a
    DatetimeIndex (us for date_range(), ns for a Timestamp plus a
    to_timedelta()), and astype returns the raw integer in THAT unit. Mixing
    the two silently scales one side by 1000, np.interp then clamps every
    query to the end of the series, and the simulation quietly runs on the
    weather of 23:00 - i.e. no sun at all. Subtracting a Timestamp and
    taking total_seconds() is resolution independent.
    """
    idx = pd.DatetimeIndex(pd.to_datetime(idx, utc=True))
    return np.asarray((idx - _EPOCH).total_seconds(), dtype=float)


# -----------------------------------------------------------------------------
# weather - output structure

class RouteWeather:
    """Weather data sampled at points along a route, indexed by distance
    along the route and by time. Backed by a pandas.DataFrame with a
    (distance_km, time) MultiIndex; use .at() rather than indexing the
    DataFrame directly, it handles nearest-point lookup and time
    interpolation.
    """

    def __init__(self, df: pd.DataFrame, points: np.ndarray,
                 distance_km: np.ndarray):
        """
        Args:
            df: rows indexed by MultiIndex (distance_km, time), one column
                per fetched weather variable.
            points: (M,2) array of [lat, lon], aligned with distance_km.
            distance_km: (M,) cumulative route distance, aligned with
                points - i.e. points[i] was sampled at distance_km[i].
        """
        self.df = df
        self.points = points
        self.distance_km = distance_km

    def _nearest_distance_for_coord(self, lat: float, lon: float) -> float:
        """crude nearest-neighbour in degrees - fine given the sampled
        points are only ~spacing_km apart along a single known route;
        not a substitute for a real distance calc over long distances."""
        d2 = (self.points[:, 0] - lat)**2 + (self.points[:, 1] - lon)**2
        return float(self.distance_km[int(np.argmin(d2))])

    def at(self, time_utc, *, distance_km: float = None,
           lat: float = None, lon: float = None) -> pd.Series:
        """Look up weather nearest to a route position, interpolated in
        time between the (hourly) samples.

        Args:
            time_utc: timestamp to interpolate to. Naive datetimes are
                assumed to already be UTC.
            distance_km: distance along the route (same convention as
                resample_route()) - the natural key if the caller (e.g.
                the simulation loop in driving.py) already tracks distance
                travelled.
            lat, lon: alternative to distance_km - nearest sampled point
                is found by raw coordinate distance instead. Give either
                distance_km, or both lat and lon - not both forms.

        Returns:
            pd.Series of variable values at the nearest sampled route
            point, linearly interpolated in time to time_utc.
        """
        if distance_km is None:
            if lat is None or lon is None:
                raise ValueError(
                    "give either distance_km, or both lat and lon")
            distance_km = self._nearest_distance_for_coord(lat, lon)
        elif lat is not None or lon is not None:
            raise ValueError(
                "give either distance_km, or lat/lon - not both")

        idx = int(np.argmin(np.abs(self.distance_km - distance_km)))
        nearest = self.distance_km[idx]
        sub = self.df.xs(nearest, level="distance_km").sort_index()

        t = pd.Timestamp(time_utc)
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        if t <= sub.index[0]:
            return sub.iloc[0]
        if t >= sub.index[-1]:
            return sub.iloc[-1]
        i1 = sub.index.searchsorted(t)
        i0 = i1 - 1
        t0, t1 = sub.index[i0], sub.index[i1]
        frac = (t - t0) / (t1 - t0)
        return sub.iloc[i0] + (sub.iloc[i1] - sub.iloc[i0]) * frac

    def sample(self, times, lats, lons) -> pd.DataFrame:
        """Vectorised lookup for a whole simulated journey at once.

        This is the path the simulation should use; .at() is the
        single-point convenience form. Differences that matter:

        * returns CANONICAL_VARS keys, not Open-Meteo spellings, with units
          already converted (pressure in Pa, not hPa);
        * interpolates wind as a VECTOR (u/v components) instead of
          interpolating the direction in degrees. Linear interpolation of
          350 deg and 10 deg gives 180 deg, i.e. exactly the opposite wind -
          which, now that the wind actually enters drive_power(), would turn
          a headwind into a tailwind for one hour around midnight-ish
          direction changes;
        * one pass per weather point instead of one DataFrame .xs() per
          route segment, which matters because a strategy optimiser calls
          total_Ws_for_lap() many times over a 5000-segment route.

        Args:
            times: array-like of timestamps. Naive values are read as UTC.
            lats, lons: arrays of the same length - the position each
                timestamp belongs to. Nearest sampled weather point wins
                (see _nearest_distance_for_coord()); coordinates are used
                rather than distance along the route so that stitched
                routes and loops work without a distance mapping.

        Returns:
            pd.DataFrame with one row per input timestamp, indexed by that
            timestamp, columns = CANONICAL_VARS keys.
        """
        t_idx = pd.DatetimeIndex(pd.to_datetime(times, utc=True))
        tq = _epoch_seconds(t_idx)
        lats = np.asarray(lats, dtype=float)
        lons = np.asarray(lons, dtype=float)
        if not (len(tq) == len(lats) == len(lons)):
            raise ValueError(
                f"times, lats and lons must be the same length, are "
                f"{len(tq)}, {len(lats)}, {len(lons)}")

        # nearest sampled weather point per query position
        d2 = ((lats[:, None] - self.points[None, :, 0])**2
            + (lons[:, None] - self.points[None, :, 1])**2)
        pidx = np.argmin(d2, axis=1)

        have = set(self.df.columns)
        missing = [k for k, (col, _) in CANONICAL_VARS.items()
                   if col not in have]
        if missing:
            lg.warning(
                f"weather data has no {[CANONICAL_VARS[k][0] for k in missing]}"
                f" - falling back to defaults for {missing}. Re-fetch with "
                f"DEFAULT_HOURLY_VARS to get real values.")

        out = {k: np.full(len(tq), CANONICAL_DEFAULTS[k], dtype=float)
               for k in CANONICAL_VARS}

        wind_ok = ("wind_speed_10m" in have) and ("wind_direction_10m" in have)

        for pi in np.unique(pidx):
            sub = self.df.xs(self.distance_km[pi], level="distance_km")
            sub = sub.sort_index()
            tp = _epoch_seconds(sub.index)
            m = (pidx == pi)
            tm = tq[m]

            for key, (col, scale) in CANONICAL_VARS.items():
                if col not in have:
                    continue
                if wind_ok and key in ("wind_speed", "wind_direction"):
                    continue  # handled as a vector below
                out[key][m] = np.interp(
                    tm, tp, sub[col].to_numpy(dtype=float)) * scale

            if wind_ok:
                sp = sub["wind_speed_10m"].to_numpy(dtype=float)
                dr = np.deg2rad(
                    sub["wind_direction_10m"].to_numpy(dtype=float))
                # meteorological "from" direction -> flow vector
                u = -sp * np.sin(dr)
                v = -sp * np.cos(dr)
                ui = np.interp(tm, tp, u)
                vi = np.interp(tm, tp, v)
                out["wind_speed"][m] = np.hypot(ui, vi)
                out["wind_direction"][m] = np.rad2deg(
                    np.arctan2(-ui, -vi)) % 360.0

        return pd.DataFrame(out, index=t_idx)


# -----------------------------------------------------------------------------
# weather - public

def fetch_weather(
    lat: float,
    lon: float,
    day: date,
    variables: list = None,
    tilt: float = float("nan"),
    azimuth: float = float("nan"),
    refresh: bool = False,
) -> pd.DataFrame:
    """Get one day of hourly weather at a single point.

    Args:
        lat, lon: coordinates in degrees.
        day: UTC calendar date (a datetime is also accepted, only its date
            part is used). The full day (00:00-23:00 UTC) is always
            returned.
        variables: hourly Open-Meteo variable names to fetch. Defaults to
            DEFAULT_HOURLY_VARS (GHI, DNI, DHI, GTI, wind speed/direction).
        tilt, azimuth: panel orientation in degrees for
            'global_tilted_irradiance' (ignored if that variable isn't
            requested). Default is float('nan') for both, i.e. a 2-axis
            sun tracker - per Open-Meteo's convention, "nan" requests
            bi-axial tracking.

    Returns:
        pd.DataFrame indexed by UTC time (tz-aware), one column per
        variable in `variables`.
    """
    points = np.array([[lat, lon]])
    return _fetch_weather_batch(points, day, variables, tilt, azimuth,
                                refresh=refresh)[0]


def fetch_weather_along_route(
    geo,
    day: date,
    spacing_km: float = 5.0,
    variables: list = None,
    tilt: float = float("nan"),
    azimuth: float = float("nan"),
    refresh: bool = False,
) -> RouteWeather:
    """Get one day of hourly weather along a route, resampled to
    approximately even spacing.

    Args:
        geo: anything accepted by read_geojson.resolve_geo_to_coords() - a
            path to a .geojson file, a geojson dict, or a coordinate array.
        day, variables, tilt, azimuth: see fetch_weather(). The defaults
            request the full variable set (incl. sun-tracking GTI) at
            all points, including ones on the open road.
        spacing_km: target distance between weather query points along the
            route. Open-Meteo's own model grid is roughly 9-25km depending
            on model/region. All points for one day go into a single
            batched HTTP call (Open-Meteo accepts comma-separated
            lat/lon lists).

    Returns:
        RouteWeather wrapping all fetched points - see that class'
        docstring for the lookup API.
    """
    coords = resolve_geo_to_coords(geo, altitude="drop")  # (N,2) lon/lat
    points, distance_km = resample_route(coords, spacing_km)  # (M,2) lat/lon

    dfs = _fetch_weather_batch(points, day, variables, tilt, azimuth,
                               refresh=refresh)

    frames = []
    for dist, df in zip(distance_km, dfs):
        df = _centre_backward_means(df.copy())
        df.index.name = "time"
        df["distance_km"] = dist
        df = df.set_index("distance_km", append=True)
        df = df.reorder_levels(["distance_km", "time"])
        frames.append(df)
    combined = pd.concat(frames).sort_index()

    return RouteWeather(combined, points, distance_km)


# -----------------------------------------------------------------------------
# weather - private

def _centre_backward_means(df: pd.DataFrame) -> pd.DataFrame:
    """Re-time the backward-averaged radiation columns to interval centres.

    See BACKWARD_MEAN_VARS. Each value is the mean over the hour ENDING at
    its timestamp, so it describes the middle of that hour. Every consumer
    interpolates linearly, i.e. treats samples as instantaneous values at
    the label - so the series is resampled onto the original timestamps
    from a base shifted half a window earlier.

    Resampling rather than shifting the index, because the frame shares one
    index across all columns and temperature, wind and pressure genuinely
    ARE instantaneous at the label. Shifting the whole index would fix the
    sun and break the wind.

    Energy is conserved exactly, not approximately: with linear
    interpolation between interval centres, the integral over one hour is
    the mean at that hour's centre. Verified on a full day against the raw
    hourly means - the totals agree to the last watt-hour.

    RESIDUAL ARTEFACT, known and left in: in the hour containing sunrise
    the mean covers a partly dark hour, so the interpolated curve reports
    irradiance before the sun is up - about 100 W/m2 at 06:00 where sunrise
    is 06:21. Worth roughly 40 Wh of a 1000 Wh morning window, always in
    the optimistic direction. Removing it properly needs sub-hourly nodes
    (a node at sunrise, value zero), which means a finer frame; not done,
    because the energy total is already exact and the error sits at one
    shoulder of the day.
    """
    cols = [c for c in df.columns if c in BACKWARD_MEAN_VARS]
    if not cols or len(df) < 2:
        return df
    t = _epoch_seconds(df.index)
    t_centre = t - BACKWARD_MEAN_WINDOW.total_seconds() / 2.0
    for c in cols:
        v = df[c].to_numpy(dtype=float)
        df[c] = np.interp(t, t_centre, v)
    return df


def _coerce_date(day) -> date:
    if isinstance(day, datetime):
        return day.date()
    return day


def _is_archive_date(day: date) -> bool:
    """True once a date is old enough to only be servable by the
    historical archive; False while it's still inside the
    forecast endpoint's rolling past/future window, where the answer can
    still change as newer model runs come in (see _set_cache())."""
    return (date.today() - day).days > FORECAST_PAST_DAYS_MAX


def _check_forecast_horizon(day: date) -> None:
    days_ahead = (day - date.today()).days
    if days_ahead > FORECAST_FUTURE_DAYS_MAX:
        lg.warning(
            f"{day} is {days_ahead} days out - beyond Open-Meteo's "
            f"~{FORECAST_FUTURE_DAYS_MAX}-day forecast horizon; the "
            f"request may fail or return low-confidence data")


def _cache_key(points: np.ndarray, day: date, variables: list,
               tilt: float, azimuth: float) -> str:
    """deterministic hash identifying one (points, day, variables, panel
    orientation) query - NOT a hash of the response, see _get_cache()/
    _set_cache() for why the response itself still needs different
    treatment for archive vs. forecast dates."""
    payload = {
        "points": [[round(float(lat), 5), round(float(lon), 5)]
                   for lat, lon in points],
        "date": day.isoformat(),
        "variables": sorted(variables),
        "tilt": tilt,
        "azimuth": azimuth,
    }
    s = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _get_cache(key: str, archive: bool) -> dict | list | None:
    """Read a cached response, if any.

    Archive (historical) dates: reads the single immutable cache file.
    Forecast dates: reads the '_latest' file, i.e. the most recently
        fetched snapshot for this key. This is NOT guaranteed to be the
        same data an earlier run used - see _set_cache().
    """
    fn = cachedir / (key if archive else f"{key}_latest")
    if fn.is_file():
        with fn.open('r') as file:
            lg.info(f"reading {fn.name} from cache")
            return json.load(file)
    return None


def _set_cache(key: str, archive: bool, response) -> None:
    """Write a response to cache.

    Archive dates are written once - a second write for the same key is
    a no-op, since the data cannot change anymore.

    Forecast dates are always refreshed: a timestamped snapshot is kept.
    The file _get_cache() reads for actual planning.
    """
    if archive:
        fn = cachedir / key
        if fn.exists():
            return  # immutable, nothing to do
        with fn.open('w') as file:
            json.dump(response, file)
        lg.info(f"wrote {fn.name} to cache ({int(fn.stat().st_size/1e3)}kB)")
        return

    fetched_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot_fn = cachedir / f"{key}_fetched{fetched_at}"
    latest_fn = cachedir / f"{key}_latest"
    entries = response if isinstance(response, list) else [response]
    tagged = [{**e, "_fetched_at": fetched_at} for e in entries]
    tagged = tagged if isinstance(response, list) else tagged[0]
    with snapshot_fn.open('w') as file:
        json.dump(tagged, file)
    with latest_fn.open('w') as file:
        json.dump(tagged, file)
    lg.info(f"wrote {snapshot_fn.name} and updated {latest_fn.name} "
             f"({int(latest_fn.stat().st_size/1e3)}kB)")


def _get_api(points: np.ndarray, day: date, variables: list,
             tilt: float, azimuth: float):
    """Query Open-Meteo for one or more points on a single UTC day.

    Args:
        points: (N,2) array of (lat, lon).
        day: date to query, interpreted and returned in UTC.
        variables: hourly variable names, e.g. DEFAULT_HOURLY_VARS.
        tilt, azimuth: panel orientation for 'global_tilted_irradiance',
            in degrees, or float('nan') for sun-tracking (see
            fetch_weather()). Required if 'global_tilted_irradiance' is in
            `variables`, ignored otherwise.

    Returns:
        dict (single point) or list of dict (multiple points), as returned
        by Open-Meteo - one 'hourly' block per point.
    """
    archive = _is_archive_date(day)
    if archive:
        url = ARCHIVE_URL
    else:
        url = FORECAST_URL
        _check_forecast_horizon(day)

    params = {
        "latitude": ",".join(f"{lat:.5f}" for lat, lon in points),
        "longitude": ",".join(f"{lon:.5f}" for lat, lon in points),
        "start_date": day.isoformat(),
        "end_date": day.isoformat(),
        "hourly": ",".join(variables),
        "timezone": "UTC",
        "wind_speed_unit": "ms",
    }
    if "global_tilted_irradiance" in variables:
        if tilt is None or azimuth is None:
            raise ValueError(
                "requesting 'global_tilted_irradiance' needs tilt and "
                "azimuth (use float('nan') for both for sun-tracking)")
        params["tilt"] = tilt
        params["azimuth"] = azimuth

    t0 = time.monotonic()
    r = requests.get(url, params=params)
    dt = time.monotonic() - t0
    if r.status_code >= 300:
        lg.error(f"HTTP {r.status_code}: {r.text} (after {dt:.3f}sec)")
    lg.info(f"HTTP {r.status_code} in {dt:.3f}sec ({url})")
    r.raise_for_status()
    return r.json()


def _fetch_weather_batch(
    points: np.ndarray,
    day: date,
    variables: list = None,
    tilt: float = float("nan"),
    azimuth: float = float("nan"),
    refresh: bool = False,
) -> list:
    """Fetch (or read from cache) one hourly weather DataFrame per point.

    Args:
        points: (N,2) array of (lat, lon).
        day: see fetch_weather().
        variables, tilt, azimuth: see fetch_weather().
        refresh: fetch from the API even when a cached snapshot exists.
            Without this there is NO way to get a newer model run once a
            key has been cached once, which matters as soon as anything
            reads the cache exclusively. Ignored for archive dates - that
            data cannot change.

    Returns:
        list of pd.DataFrame, one per point in `points` (same order),
        each indexed by UTC time (tz-aware) with one column per variable.
    """
    day = _coerce_date(day)
    if variables is None:
        variables = DEFAULT_HOURLY_VARS

    archive = _is_archive_date(day)
    key = _cache_key(points, day, variables, tilt, azimuth)

    if refresh and archive:
        lg.info("refresh ignored: %s is an archive date and immutable", day)
        refresh = False

    response = None if refresh else _get_cache(key, archive)
    if response is None:
        response = _get_api(points, day, variables, tilt, azimuth)
        _set_cache(key, archive, response)

    entries = response if isinstance(response, list) else [response]
    if len(entries) != len(points):
        raise RuntimeError(
            f"Open-Meteo returned {len(entries)} locations for "
            f"{len(points)} requested points")

    dfs = []
    for entry in entries:
        hourly = entry["hourly"]
        idx = pd.to_datetime(hourly["time"], utc=True)
        cols = {v: hourly[v] for v in variables}
        dfs.append(pd.DataFrame(cols, index=idx))
    return dfs

if __name__ == "__main__":
    # demo: full-day weather along day N's ToControlStop + loop
    GITHUB_ROOT = Path(__file__).parents[5]  # .../Documents/GitHub
    day_n = 1
    target_date = date.today()
    fp = GITHUB_ROOT / "strategy-private" / "route_geojson" / f"day{day_n}_route1.geojson"
    if fp.is_file():
        rw = fetch_weather_along_route(fp, target_date, spacing_km=5.0)
        print(f"{len(rw.distance_km)} points, "
              f"{rw.distance_km[-1]:.1f}km total")
        sample_time = datetime(
            target_date.year, target_date.month, target_date.day, 10,
            tzinfo=timezone.utc)
        print(rw.at(sample_time, distance_km=rw.distance_km[len(rw.distance_km)//2]))
    else:
        print(f"file not found: {fp}")