"""Input gathering for the point strategy: time, position, pack state, weather.

Everything the strategy needs to know about "where are we and with what"
lands in a `PointState`. Each field can come from a measurement or from the
keyboard, and the state records WHICH - a plan built on a hand-typed SoC is
not the same plan as one built on a settled anchor, and the output has to be
able to say so.

Weather is read from the cache only. It is never fetched here. Two reasons:
the solver must not be able to block on a network timeout at a control stop,
and the same inputs must give the same numbers - otherwise two people running
the script five minutes apart get different advice, and the evening review
cannot reproduce a day's plan at all. A new fetch is a deliberate, separate
action (scripts/cache_weather.py).
"""

from   dataclasses import dataclass, field
from   datetime import date, datetime, timedelta, timezone
from   pathlib import Path
from   zoneinfo import ZoneInfo
import io
import json
import logging as lg
import os
import sys

import numpy as np
import pandas as pd

from ..environment.environment import (
    DEFAULT_HOURLY_VARS, RouteWeather, cachedir, resample_route,
    _cache_key, _get_cache, _is_archive_date)
from ..geojson.read_geojson import resolve_geo_to_coords
from ..simulation.battery import (
    capacity_wh, soc_from_wh, state_from_measurement, usable_wh, wh_from_soc)
from ..ser_dataclasses import Battery_coeffs

log = lg.getLogger(__name__)

RACE_TZ = ZoneInfo("Africa/Johannesburg")   # SAST, UTC+2, no DST


# --------------------------------------------------------------- routes ----

def find_route_dir() -> Path:
    """Locate strategy-private/route_geojson.

    Same approach as scripts/cache_weather.py: an explicit SSC_ROUTE_DIR
    wins, otherwise walk upwards until the sibling checkout turns up.
    Counting parents[n] breaks silently as soon as anything moves.
    """
    if env := os.environ.get("SSC_ROUTE_DIR"):
        return Path(env).expanduser().resolve()
    for base in Path(__file__).resolve().parents:
        cand = base / "strategy-private" / "route_geojson"
        if cand.is_dir():
            return cand
    raise FileNotFoundError(
        "strategy-private/route_geojson not found in any parent of "
        f"{Path(__file__).resolve()} - set SSC_ROUTE_DIR to point at it")


def load_raceconfig():
    """Import strategy-private's raceconfig module.

    It lives in a separate checkout and imports its siblings by bare name
    (`from compile_route import ...`), so its directory has to be on
    sys.path. Done here, once, rather than in every caller.
    """
    route_dir = find_route_dir()
    if str(route_dir) not in sys.path:
        sys.path.insert(0, str(route_dir))
    import raceconfig                      # noqa: E402  (path set above)
    return raceconfig


# ----------------------------------------------------------------- time ----

def resolve_time(spec: str = None, now: datetime = None) -> datetime:
    """Start time for the calculation, race-local and timezone aware.

    Args:
        spec: None or "now" -> current clock. "HH:MM" -> that time on the
            race day being planned (filled in by the caller via
            `attach_date`). A full ISO timestamp is also accepted.

    Naive input is interpreted as race-local, never as UTC. Everyone on the
    team thinks in SAST; a plan silently shifted by two hours would look
    entirely plausible and be entirely wrong.
    """
    if spec in (None, "", "now", "jetzt"):
        return (now or datetime.now(timezone.utc)).astimezone(RACE_TZ)
    try:
        t = datetime.fromisoformat(spec)
    except ValueError:
        # datetime.fromisoformat() rejects a bare "HH:MM", which is the most
        # natural thing to type at a control stop. Parse it as a time of day
        # and let attach_date() put it on the race day.
        try:
            hh, mm = (int(x) for x in spec.split(":")[:2])
            t = datetime(2000, 1, 1, hh, mm)
        except (ValueError, TypeError):
            raise SystemExit(
                f"cannot read time {spec!r} - use 'now', 'HH:MM' or an ISO "
                f"timestamp like 2026-09-10T13:40")
    return t if t.tzinfo else t.replace(tzinfo=RACE_TZ)


def attach_date(t: datetime, day_date: date) -> datetime:
    """Move a time-of-day onto the race day's calendar date.

    "HH:MM" is the normal way to type a time at a control stop, and it
    carries no date. Rather than defaulting to today - which is wrong the
    moment anyone plans tomorrow evening - the date comes from the race day.
    """
    return t.replace(year=day_date.year, month=day_date.month,
                     day=day_date.day)


# ------------------------------------------------------------- telemetry ----

def fetch_pack_reading(host: str, window_min: float = 3.0) -> dict:
    """Latest usable battery voltage/current from GET /api/timeseries.

    Returns a dict with v_pack, i_batt, p_mppt_w, timestamp and age_s.

    The newest row is not automatically the best one: the series are
    interpolated per second and a dropout writes battery_voltage == 0
    (documented in the telemetry notes). So the last row with a plausible
    voltage wins, and how old it is gets reported rather than hidden - a
    four-minute-old reading is still usable, it just has to be visible.
    """
    import requests                        # optional dependency, only here

    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=window_min)
    r = requests.get(f"http://{host}/api/timeseries",
                     params={"start": int(start.timestamp()),
                             "end": int(end.timestamp())}, timeout=10)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    if df.empty:
        raise SystemExit(f"{host}/api/timeseries returned no rows for the "
                         f"last {window_min:.0f} min")
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)

    # same plausibility window as the 2024 evaluation and TelemetrySigns
    ok = df["battery_voltage"].between(60.0, 140.0)
    if "battery_current" in df:
        ok &= df["battery_current"].abs() < 120.0
    if not ok.any():
        raise SystemExit(
            f"no plausible battery voltage in the last {window_min:.0f} min "
            f"({len(df)} rows, all outside 60-140 V). Telemetry down, or the "
            f"car is off - enter the pack state by hand instead.")
    row = df[ok].iloc[-1]

    mppt = [c for c in ("mppt1_power", "mppt2_power", "mppt3_power",
                        "mppt4_power") if c in df.columns]
    return {
        "v_pack":    float(row["battery_voltage"]),
        "i_batt":    float(row.get("battery_current", 0.0)),
        "p_mppt_w":  float(sum(float(row[c]) for c in mppt)) if mppt else None,
        "timestamp": row["timestamp"].to_pydatetime(),
        "age_s":     float((end - row["timestamp"].to_pydatetime())
                           .total_seconds()),
        "n_rows":    int(len(df)),
        "n_rejected": int((~ok).sum()),
    }


def fetch_gps(host: str) -> dict:
    """Latest GPS fix from GET /api/gps/latest.

    The endpoint returns a GpsPoint; field casing depends on the serializer,
    so both spellings are accepted rather than guessed.
    """
    import requests

    r = requests.get(f"http://{host}/api/gps/latest", timeout=10)
    if r.status_code == 404:
        raise SystemExit(f"{host} has no GPS fix yet - enter the position "
                         f"by hand (--at) instead")
    r.raise_for_status()
    d = r.json()

    def pick(*names):
        for n in names:
            if n in d and d[n] is not None:
                return d[n]
        return None

    lat = pick("latitude", "Latitude")
    lon = pick("longitude", "Longitude")
    if lat is None or lon is None:
        raise SystemExit(f"GPS response has no coordinates: {sorted(d)}")
    return {
        "lat": float(lat),
        "lon": float(lon),
        "speed_kmh": (lambda v: float(v) if v is not None else None)(
            pick("speedKmh", "SpeedKmh")),
        "bearing": (lambda v: float(v) if v is not None else None)(
            pick("bearing", "Bearing")),
        "raw": d,
    }


# ------------------------------------------------------------ pack state ----

@dataclass
class PackState:
    """Energy in the pack, plus where the number came from."""
    wh: float
    soc: float
    source: str                 # human readable, goes into the output header
    trust: str                  # 'anchor' | 'anchor_weak' | 'check_only' | 'manual'
    v_pack: float = None
    i_batt: float = None
    reading_age_s: float = None

    @property
    def wh_above_floor(self) -> float:
        return self.wh - self._floor

    _floor: float = 0.0


def pack_state_from_reading(batt: Battery_coeffs, reading: dict,
                            settled: bool = False) -> PackState:
    """Pack state from a telemetry reading.

    `settled` defaults to False on purpose. At a control stop the pack is
    charging at ~10-12 A, so the terminal voltage is not the OCV, and the
    correction runs through pack_r_extra_ohm, which is still an estimate.
    Such a reading is a plausibility check, not an anchor - and the
    difference decides whether the SoC band around the plan is narrow or
    wide, so it must reach the output rather than being flattened here.
    """
    st = state_from_measurement(batt, reading["v_pack"], reading["i_batt"],
                                settled=settled)
    src = (f"Telemetrie {reading['v_pack']:.1f} V / "
           f"{reading['i_batt']:+.1f} A")
    return PackState(
        wh=st["wh_remaining"], soc=st["soc"], source=src, trust=st["trust"],
        v_pack=reading["v_pack"], i_batt=reading["i_batt"],
        reading_age_s=reading.get("age_s"),
        _floor=(1.0 - batt.usable) * capacity_wh(batt))


def pack_state_manual(batt: Battery_coeffs, soc_percent: float = None,
                      wh: float = None) -> PackState:
    """Pack state from a hand-entered SoC or Wh."""
    if (soc_percent is None) == (wh is None):
        raise SystemExit("give exactly one of --soc / --wh")
    if wh is None:
        wh = wh_from_soc(batt, soc_percent / 100.0)
        src = f"Handeingabe SOC {soc_percent:.0f} %"
    else:
        src = f"Handeingabe {wh:.0f} Wh"
    return PackState(wh=float(wh), soc=float(soc_from_wh(batt, wh)),
                     source=src, trust="manual",
                     _floor=(1.0 - batt.usable) * capacity_wh(batt))


def pack_state_full(batt: Battery_coeffs) -> PackState:
    """Full pack - the exactly known boundary condition on day 1 at 09:00."""
    wh = capacity_wh(batt)
    return PackState(wh=wh, soc=1.0, source="Pack voll (100 %)",
                     trust="anchor",
                     _floor=(1.0 - batt.usable) * capacity_wh(batt))


# ---------------------------------------------------------------- weather ----

@dataclass
class CachedWeather:
    """A RouteWeather plus the provenance the output has to show."""
    weather: RouteWeather
    fetched_at: datetime = None       # UTC, from the cached payload
    cache_file: str = None
    stem: str = None

    @property
    def age(self) -> timedelta:
        if self.fetched_at is None:
            return None
        return datetime.now(timezone.utc) - self.fetched_at


def _fetched_at_from_cache(key: str, archive: bool):
    """Read `_fetched_at` back out of the cached payload.

    environment._set_cache() tags every forecast entry with the fetch time,
    which is exactly what the age line needs. Deliberately read from inside
    the file rather than from its mtime or its name: an mtime does not
    survive a copy, a zip or a git checkout, and the age is the one number
    that stops someone planning on yesterday's clouds.
    """
    fn = cachedir / (key if archive else f"{key}_latest")
    if not fn.is_file():
        return None, None
    try:
        with fn.open("r") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None, fn.name
    first = payload[0] if isinstance(payload, list) else payload
    ts = first.get("_fetched_at") if isinstance(first, dict) else None
    if not ts:
        return None, fn.name
    try:
        return datetime.strptime(ts, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc), fn.name
    except ValueError:
        return None, fn.name


def load_weather(geo, day: date, spacing_km: float = 5.0,
                 variables: list = None, tilt: float = float("nan"),
                 azimuth: float = float("nan"),
                 hint: str = "") -> CachedWeather:
    """Load one route part's weather FROM CACHE ONLY.

    Raises SystemExit with an actionable message on a miss instead of
    reaching for the network. The message has to say which part of the cache
    key failed to match: the key is a hash over route file, variable set and
    resampling spacing, so adding one variable invalidates everything at
    once. That used to mean "the first run refetches"; under cache-only it
    means "the script does not work at the control stop", and then nobody
    should have to guess why.
    """
    variables = list(variables or DEFAULT_HOURLY_VARS)
    stem = Path(str(geo)).stem if not isinstance(geo, np.ndarray) else "coords"

    coords = resolve_geo_to_coords(geo, altitude="drop")
    points, distance_km = resample_route(coords, spacing_km)
    key = _cache_key(points, day, variables, tilt, azimuth)
    archive = _is_archive_date(day)

    payload = _get_cache(key, archive)
    if payload is None:
        raise SystemExit(
            f"Cache-Miss: {stem} fuer {day} nicht im Cache.\n"
            f"  Key {key[:12]}... aus: Route {stem}, {len(points)} Punkte @ "
            f"{spacing_km} km, Variablen {sorted(variables)}\n"
            f"  Pruefen ob Variablensatz oder spacing_km geaendert wurden - "
            f"beide gehen in den Key ein.\n"
            f"  Abrufen:     python scripts/cache_weather.py "
            f"{hint.replace('--day ', '')}\n"
            f"  Cache-Stand: ls {cachedir}")

    # cache hit -> the call below cannot go to the network
    from ..environment.environment import fetch_weather_along_route
    weather = fetch_weather_along_route(
        geo, day, spacing_km=spacing_km, variables=variables,
        tilt=tilt, azimuth=azimuth)

    fetched_at, fn = _fetched_at_from_cache(key, archive)
    return CachedWeather(weather=weather, fetched_at=fetched_at,
                         cache_file=fn, stem=stem)


# ------------------------------------------------------------------ state ----

@dataclass
class PointState:
    """Where we are, when, and with how much energy."""
    day: int
    day_date: date
    t_now: datetime
    t_deadline: datetime
    part: str                    # 'to_control' | 'loop' | 'to_finish'
    km_in_part: float            # distance already covered within that part
    part_km: float               # total length of that part
    pack: PackState
    loop_name: str = None        # which loop variant, if part == 'loop'
    loop_done: int = 0           # completed loops before the current one
    loop_leg: str = None         # 'out' | 'back', only for part == 'loop'
    position_source: str = "Handeingabe"
    cross_track_m: float = None
    notes: list = field(default_factory=list)

    @property
    def time_left(self) -> timedelta:
        return self.t_deadline - self.t_now
