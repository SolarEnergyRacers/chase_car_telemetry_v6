"""Fetch and cache Open-Meteo weather for one race day.

    python scripts/cache_weather.py          # today's race day
    python scripts/cache_weather.py 3        # day 3
    python scripts/cache_weather.py all      # all eight days

One day per call by default, because on a race day the weather gets re-checked
several times and refetching the following days every time is wasted effort -
their forecast has not moved meaningfully in the meantime, and the current day
is the only one where a fresh model run changes a decision.

Before the race starts, today maps to day 1; after it ends, to day 8.

One HTTP request per route part, because a "lap" in total_Ws_for_lap() is one
route part, so one RouteWeather per part pairs directly with the call. Each
part's RouteWeather covers the full UTC day, so a single fetch for a loop
serves every repetition of that loop.

The cache keeps `{key}_latest` (overwritten every run, i.e. the current
forecast for planning) plus a timestamped snapshot `{key}_fetched<ts>`. So
repeated runs also build a record of how the forecast for a race day evolved,
which is the only way to find out afterwards how far ahead it was worth
trusting.
"""

from datetime import date, timedelta
from pathlib import Path
import logging as lg
import os
import sys

from data_analysis.environment.environment import (
    fetch_weather_along_route, DEFAULT_HOURLY_VARS)

lg.basicConfig(level=lg.INFO)
log = lg.getLogger("cache_weather")

# Race day 1 is Thursday 10 September 2026, day 8 is 17 September.
# The `day` argument of fetch_weather_along_route() is a UTC calendar date.
# Race time is SAST = UTC+2 with no DST, so a competition day runs 04:00 to
# 15:30 UTC - comfortably inside one UTC day, no date boundary to handle.
# Morning charging from 06:00 SAST = 04:00 UTC is inside it too.
DAY1 = date(2026, 9, 10)
N_DAYS = 8

# Which files make up each day. Day 1 is the odd one out: route1 to route3 and
# day1_route are intermediate stages of the manual route, only manual_day1
# (ToControlStop) and route4 (FromControlStop) are real. Every other day
# follows route1 = ToControlStop, route2 = FromControlStop, loop = the loop at
# the control stop.
#   day 2: loop is a half-blind stage, published on day 1  -> file missing
#   day 3: full blind stage, published on day 2            -> nothing at all
#   day 6: control stop is at the nightly stop             -> no route2
ROUTE_PARTS = {1: ["manual_day1", "day1_loop", "day1_route4"]}
for _n in range(2, N_DAYS + 1):
    ROUTE_PARTS[_n] = [f"day{_n}_route1", f"day{_n}_loop", f"day{_n}_route2"]

SPACING_KM = 5.0
# Open-Meteo's own model grid is ~9-25 km, so 5 km oversamples it - which is
# the point: it makes the result independent of where in a grid cell the route
# happens to run. Payload is irrelevant at this size. NOTE this value goes
# into the cache key (it changes the resampled points), so changing it
# re-fetches everything.


def find_route_dir() -> Path:
    """Locate strategy-private/route_geojson without counting parents[n].

    An explicit SSC_ROUTE_DIR wins; otherwise walk upwards from this file
    until the sibling checkout turns up. Counting parent levels breaks
    silently whenever a file moves or a repo is nested one level deeper -
    which is exactly what happened to the parents[4] in environment.py.
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


def date_for_day(day_n: int) -> date:
    if not 1 <= day_n <= N_DAYS:
        raise ValueError(f"day must be 1..{N_DAYS}, got {day_n}")
    return DAY1 + timedelta(days=day_n - 1)


def day_for_today(today: date = None) -> int:
    """Race day number for a calendar date, clamped into the race window."""
    today = today or date.today()
    day_n = (today - DAY1).days + 1
    if day_n < 1:
        log.info(f"{today} is before day 1 ({DAY1}) - using day 1")
        return 1
    if day_n > N_DAYS:
        log.info(f"{today} is after day {N_DAYS} "
                 f"({date_for_day(N_DAYS)}) - using day {N_DAYS}")
        return N_DAYS
    return day_n


def cache_age(fp: Path, target: date) -> str:
    """How old the cached snapshot for one route part is.

    Read from `_fetched_at` inside the payload rather than from the file's
    mtime: an mtime does not survive a copy, a zip or a git checkout, and
    the age is the one number that stops someone planning on last week's
    clouds.
    """
    from data_analysis.environment.environment import (
        _cache_key, _is_archive_date, cachedir)
    from data_analysis.geojson.read_geojson import resolve_geo_to_coords
    from data_analysis.environment.environment import resample_route
    import json
    from datetime import datetime, timezone

    coords = resolve_geo_to_coords(fp, altitude="drop")
    points, _ = resample_route(coords, SPACING_KM)
    key = _cache_key(points, target, list(DEFAULT_HOURLY_VARS),
                     float("nan"), float("nan"))
    archive = _is_archive_date(target)
    fn = cachedir / (key if archive else f"{key}_latest")
    if not fn.is_file():
        return "nicht im Cache"
    try:
        payload = json.load(fn.open("r"))
        first = payload[0] if isinstance(payload, list) else payload
        ts = datetime.strptime(first["_fetched_at"], "%Y%m%dT%H%M%SZ")
    except (OSError, KeyError, ValueError, TypeError):
        return "im Cache, Abrufzeit unbekannt"
    age = datetime.now(timezone.utc) - ts.replace(tzinfo=timezone.utc)
    hours = age.total_seconds() / 3600
    return f"geholt vor {int(hours)}h{int((hours % 1) * 60):02d}"


def cache_day(day_n: int, route_dir: Path, refresh: bool = False,
              status_only: bool = False) -> None:
    target = date_for_day(day_n)
    ahead = (target - date.today()).days
    log.info(f"=== day {day_n}, {target} ({ahead:+d} days from today), "
             f"spacing {SPACING_KM} km"
             + ("  [REFRESH]" if refresh else ""))

    for stem in ROUTE_PARTS[day_n]:
        fp = route_dir / f"{stem}.geojson"
        if not fp.is_file():
            log.warning(f"  {stem}: no file - blind stage not published yet, "
                        f"or this day has no such part")
            continue
        if status_only:
            log.info(f"  {stem}: {cache_age(fp, target)}")
            continue
        before = cache_age(fp, target)
        try:
            weather = fetch_weather_along_route(
                fp, target, spacing_km=SPACING_KM,
                variables=DEFAULT_HOURLY_VARS, refresh=refresh)
        except Exception as e:
            # A refresh without network must not take the workflow down.
            # The old snapshot is still perfectly usable - it is just old,
            # and saying so is the whole point of the age line.
            if refresh and "nicht im Cache" not in before:
                log.warning(f"  {stem}: Refresh fehlgeschlagen ({type(e).__name__}"
                            f": {str(e)[:80]}) - alter Stand bleibt, {before}")
                continue
            log.error(f"  {stem}: kein Wetter verfuegbar "
                      f"({type(e).__name__}: {str(e)[:80]})")
            continue
        log.info(f"  {stem}: {float(weather.distance_km[-1]):6.1f} km, "
                 f"{len(weather.distance_km):3d} points, "
                 f"{len(weather.df):5d} rows  "
                 f"({before} -> {cache_age(fp, target)})")


USAGE = """usage: cache_weather.py [1..8 | all] [--refresh] [--status]

  (no day)     today's race day
  1..8         that race day
  all          all eight days
  --day N      same as the positional form

  --refresh    fetch from the API even if something is already cached.
               WITHOUT THIS NOTHING IS REFETCHED - a cache hit is returned
               as-is, however old it is.
  --status     only report what is cached and how old it is, no requests
  -v           verbose
"""


def main(argv=None) -> None:
    argv = list(sys.argv if argv is None else argv)
    flags = {a.lower() for a in argv[1:] if a.startswith("-")}
    if flags & {"-h", "--help"}:
        print(USAGE)
        return
    refresh = bool(flags & {"-r", "--refresh", "--force"})
    status_only = bool(flags & {"-s", "--status"})

    # positional day, or --day N
    rest = [a for a in argv[1:] if not a.startswith("-")]
    if "--day" in [a.lower() for a in argv[1:]]:
        i = [a.lower() for a in argv].index("--day")
        if i + 1 < len(argv):
            rest = [argv[i + 1]]
    arg = rest[0].lower() if rest else None

    if arg in ("all", "a"):
        days = list(range(1, N_DAYS + 1))
    elif arg is None:
        days = [day_for_today()]
    else:
        try:
            days = [int(arg)]
        except ValueError:
            raise SystemExit(USAGE)
    if not all(1 <= d <= N_DAYS for d in days):
        raise SystemExit(USAGE)

    route_dir = find_route_dir()
    log.info(f"routes from {route_dir}")
    for day_n in days:
        cache_day(day_n, route_dir, refresh=refresh, status_only=status_only)
    if not refresh and not status_only:
        log.info("nothing was refetched where a snapshot already existed - "
                 "use --refresh for a newer model run")


if __name__ == "__main__":
    main()
