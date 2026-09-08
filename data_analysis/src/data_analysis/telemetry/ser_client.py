"""HTTP client for the SER live_monitoring app.

The ONE place that knows the app's URLs, its column names and its quirks.
Everything downstream gets pandas frames with UTC timestamps and NaN where
there is no data. If scripts/check_telemetry_api.py (stdlib only, prints
the raw truth) and this module disagree, the check script is right and this
module gets fixed.

Quirks handled here, all documented in telemetrie-anbindung-live-strategie.md:

* gaps longer than 5 s are filled with 0.0 in the app's series, not NaN.
  A pack voltage of 0 V is impossible with the contactors closed, so rows
  with battery_voltage == 0 are treated as missing (`gap` column).
* /api/timeseries has no cursor; /api/timeseries/range takes `from` without
  `to` and returns everything since - that is the polling endpoint.
* GPS timestamps come with an offset (server local); series timestamps
  come as UTC. Both are normalised to UTC here.
* `motor_*` columns are a rearrangement of battery and MPPT numbers (the
  controller is not on the bus) and are dropped so nobody treats them as
  an independent measurement.
"""

from __future__ import annotations

from   dataclasses import dataclass, field
from   datetime import datetime, timedelta, timezone
import logging as lg
import time

import numpy as np
import pandas as pd

log = lg.getLogger(__name__)

DEFAULT_HOST = "localhost:5240"
DEFAULT_DEVICE = "honor"           # the GPS logger that rides in the car

# the seven series the strategy uses
STRATEGY_SERIES = ["speed", "mppt1_power", "mppt2_power", "mppt3_power",
                   "mppt4_power", "battery_voltage", "battery_current"]
MPPT_COLS = ["mppt1_power", "mppt2_power", "mppt3_power", "mppt4_power"]
DROP_COLS = ("motor_current", "motor_voltage", "motor_power", "battery_power")

RACE_TZ = "Africa/Johannesburg"


def normalize_host(host: str) -> str:
    host = host.strip()
    for pre in ("http://", "https://"):
        if host.startswith(pre):
            host = host[len(pre):]
    host = host.rstrip("/")
    if ":" not in host:
        host += ":5240"
    return host


def _utc(t) -> pd.Timestamp:
    t = pd.Timestamp(t)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _z(t) -> str:
    """ISO 8601 with explicit Z - the API assumes server-local otherwise."""
    return _utc(t).strftime("%Y-%m-%dT%H:%M:%SZ")


def _get(host: str, path: str, params: dict, timeout: float):
    import requests
    r = requests.get(f"http://{normalize_host(host)}{path}", params=params,
                     timeout=timeout)
    return r


# ------------------------------------------------------------- timeseries ----

def parse_range_json(payload: dict) -> pd.DataFrame:
    """{series, points} -> DataFrame indexed by UTC time, one column per series.

    Columns follow the RETURNED `series` array, not the requested order:
    the app builds them from its own dictionary keys.
    """
    cols = list(payload.get("series") or [])
    pts = payload.get("points") or []
    if not pts:
        return pd.DataFrame(columns=cols, index=pd.DatetimeIndex([], tz="UTC"))
    ts = pd.to_datetime([p["timestamp"] for p in pts], utc=True)
    vals = np.array([[np.nan if v is None else float(v)
                      for v in p["values"]] for p in pts], dtype=float)
    df = pd.DataFrame(vals, columns=cols, index=ts)
    df.index.name = "time"
    return df


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Apply the app's quirks: drop derived columns, mark fill-zeros as gaps.

    A gap fills EVERY series with 0.0, not just the battery ones, so the
    whole row is blanked - `speed` above all. A dropout would otherwise
    read as "the car is standing", which is the one reading the driver
    must not be shown while doing 80 km/h through a radio hole.

    The gap is recognised on `battery_voltage`, because 0 V is impossible
    with the contactors closed while 0 km/h is perfectly normal.
    """
    df = df.drop(columns=[c for c in DROP_COLS if c in df.columns])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    gap = pd.Series(False, index=df.index)
    if "battery_voltage" in df.columns:
        v = df["battery_voltage"]
        gap = v.isna() | (v == 0.0)
        blank = [c for c in (["battery_voltage", "battery_current", "speed"]
                             + MPPT_COLS) if c in df.columns]
        # Column by column with where(), NOT `df.loc[gap, blank] = np.nan`:
        # on pandas 3.0.2 that form silently wrote only the LAST column of
        # the list on a frame with a DatetimeIndex - no error, no warning,
        # and the speed of a dropout would have stayed 0 km/h.
        for c in blank:
            df[c] = df[c].where(~gap)
    df["gap"] = gap.to_numpy()
    return df


def fetch_range(host: str, t_from, t_to=None, series: list = None,
                timeout: float = 10.0) -> pd.DataFrame:
    """GET /api/timeseries/range as a cleaned frame. `t_to=None` = up to now."""
    params = {"from": _z(t_from),
              "series": ",".join(series or STRATEGY_SERIES)}
    if t_to is not None:
        params["to"] = _z(t_to)
    r = _get(host, "/api/timeseries/range", params, timeout)
    r.raise_for_status()
    df = clean(parse_range_json(r.json()))
    # the CSV endpoint can return more than asked; be safe here as well
    df = df[df.index >= _utc(t_from)]
    if t_to is not None:
        df = df[df.index <= _utc(t_to)]
    return df


def fetch_window_csv(host: str, t_from, t_to, timeout: float = 10.0
                     ) -> pd.DataFrame:
    """GET /api/timeseries (CSV, unix seconds). The older endpoint; kept
    because a day's worth of data is smaller as CSV than as JSON."""
    import io
    r = _get(host, "/api/timeseries",
             {"start": int(_utc(t_from).timestamp()),
              "end": int(_utc(t_to).timestamp())}, timeout)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    if df.empty:
        return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC"))
    df["time"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df = df.drop(columns=["timestamp"]).set_index("time")
    df = clean(df)
    return df[(df.index >= _utc(t_from)) & (df.index <= _utc(t_to))]


# -------------------------------------------------------------------- gps ----

def _parse_gps(raw: dict) -> dict:
    low = {k.lower(): v for k, v in raw.items()}
    t = low.get("timestamp")
    ts = None
    if t is not None:
        ts = pd.Timestamp(str(t))
        # a naive stamp is the server's local time (SAST on the race laptop)
        ts = ts.tz_localize(RACE_TZ) if ts.tzinfo is None else ts
        ts = ts.tz_convert("UTC")
    return {
        "id": low.get("id"),
        "time": ts,
        "lat": (None if low.get("latitude") is None else float(low["latitude"])),
        "lon": (None if low.get("longitude") is None else float(low["longitude"])),
        "speed_kmh": (None if low.get("speedkmh") is None
                      else float(low["speedkmh"])),
        "hdop": low.get("accuracymeters"),     # OsmAnd puts the HDOP here
        "device": low.get("devicename"),
    }


def fetch_gps_latest(host: str, device: str = None, timeout: float = 5.0):
    """Latest fix for `device` (or any device). None if the app has none."""
    params = {"deviceName": device} if device else {}
    r = _get(host, "/api/gps/latest", params, timeout)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    d = r.json()
    if isinstance(d, list):
        d = d[-1] if d else None
    return _parse_gps(d) if d else None


def fetch_gps_range(host: str, t_from, t_to=None, device: str = None,
                    timeout: float = 10.0) -> pd.DataFrame:
    """GPS fixes in a window as a frame: time (UTC index), lat, lon,
    speed_kmh, hdop, device, id."""
    params = {"from": _z(t_from)}
    if t_to is not None:
        params["to"] = _z(t_to)
    if device:
        params["deviceName"] = device
    r = _get(host, "/api/gps/range", params, timeout)
    r.raise_for_status()
    pts = [_parse_gps(p) for p in r.json()]
    pts = [p for p in pts if p["time"] is not None and p["lat"] is not None]
    if not pts:
        return pd.DataFrame(columns=["lat", "lon", "speed_kmh", "hdop",
                                     "device", "id"],
                            index=pd.DatetimeIndex([], tz="UTC"))
    df = pd.DataFrame(pts).set_index("time").sort_index()
    return df


def list_gps_devices(host: str, minutes: float = 30.0,
                     timeout: float = 10.0, now=None) -> list:
    """Which GPS devices have reported lately, newest fix first.

    The app has no endpoint that lists devices - `DataManager` knows them
    (`GetGpsDeviceNames`) but nothing exposes it. So the names are read out
    of an unfiltered `/api/gps/range` over the last `minutes`, which is
    what check_telemetry_api.py does too. A device that has been silent
    for longer than the window is therefore invisible; that is the right
    behaviour for a picker (a phone that reported this morning and has
    been off since is not the car), but it means the list can be empty
    while the app still holds old points.

    Returns [{device, n, last_time, age_s, lat, lon, speed_kmh}].
    """
    # `now` is the caller's clock, not the wall clock: during a replay the
    # session runs on the race day, and asking for "the last 30 minutes"
    # of real time would look at an empty window.
    now = pd.Timestamp.now("UTC") if now is None else _utc(now)
    df = fetch_gps_range(host, now - pd.Timedelta(minutes=minutes), None,
                         device=None, timeout=timeout)
    out = []
    if df.empty:
        return out
    for name, g in df.groupby(df["device"].astype(str), sort=False):
        last = g.iloc[-1]
        t = g.index[-1]
        out.append({"device": name, "n": int(len(g)),
                    "last_time": t.isoformat(),
                    "age_s": float((now - t).total_seconds()),
                    "lat": float(last["lat"]), "lon": float(last["lon"]),
                    "speed_kmh": (None if pd.isna(last.get("speed_kmh"))
                                  else float(last["speed_kmh"]))})
    out.sort(key=lambda d: d["age_s"])
    return out


# ---------------------------------------------------------------- poller ----

@dataclass
class TelemetryPoller:
    """Cursor polling of series + latest GPS fix, with dedup and backfill.

    `poll()` returns only rows not seen before. The cursor is the last seen
    timestamp + 1 s; `from` is inclusive, so a repeated request cannot
    duplicate a row, and the dedup stays in anyway - a request can go out
    twice.
    """
    host: str = DEFAULT_HOST
    device: str = DEFAULT_DEVICE
    series: list = field(default_factory=lambda: list(STRATEGY_SERIES))
    timeout: float = 8.0
    cursor: pd.Timestamp = None
    last_seen: pd.Timestamp = None
    last_gps_id: object = None
    n_polls: int = 0
    n_errors: int = 0
    last_error: str = None
    last_ok: float = None          # time.time() of the last successful poll

    def backfill(self, t_from, t_to=None, chunk: timedelta = timedelta(hours=2)
                 ) -> pd.DataFrame:
        """Fetch everything since `t_from` in chunks and set the cursor.

        Chunked because the JSON endpoint is verbose: ten hours of seven
        series is ~250 000 numbers, and one request for all of it can
        time out on a laptop that is also decoding CAN frames.
        """
        t_from = _utc(t_from)
        t_end = _utc(t_to) if t_to is not None else pd.Timestamp.now("UTC")
        frames = []
        t = t_from
        while t < t_end:
            t2 = min(t + chunk, t_end)
            frames.append(fetch_range(self.host, t, t2, self.series,
                                      timeout=max(self.timeout, 30.0)))
            t = t2 + pd.Timedelta(seconds=1)
        df = (pd.concat(frames).sort_index() if frames
              else pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC")))
        df = df[~df.index.duplicated(keep="last")]
        if len(df):
            self.last_seen = df.index[-1]
            self.cursor = self.last_seen + pd.Timedelta(seconds=1)
        else:
            self.cursor = t_from
        return df

    def poll(self, lookback: timedelta = timedelta(seconds=120), now=None):
        """One round: (new_rows DataFrame, gps dict or None).

        On the first call without a backfill the cursor starts `lookback`
        before `now` (the caller's clock, so a replay can run on a shifted
        one). Errors are counted and returned as empty results - the live
        loop must keep running through a dropped WLAN, not die on it.
        """
        self.n_polls += 1
        now = pd.Timestamp.now("UTC") if now is None else pd.Timestamp(now)
        if self.cursor is None:
            self.cursor = now - lookback
        new = pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC"))
        gps = None
        try:
            df = fetch_range(self.host, self.cursor, None, self.series,
                             timeout=self.timeout)
            if self.last_seen is not None:
                df = df[df.index > self.last_seen]
            if len(df):
                self.last_seen = df.index[-1]
                self.cursor = self.last_seen + pd.Timedelta(seconds=1)
            new = df
            self.last_ok = time.time()
            self.last_error = None
        except Exception as e:                         # network, JSON, 5xx
            self.n_errors += 1
            self.last_error = f"{type(e).__name__}: {str(e)[:80]}"
            log.warning("Telemetrie-Abruf fehlgeschlagen: %s", self.last_error)
        try:
            gps = fetch_gps_latest(self.host, self.device, timeout=self.timeout)
            if gps is not None:
                gps["new"] = gps.get("id") != self.last_gps_id
                self.last_gps_id = gps.get("id")
        except Exception as e:
            self.n_errors += 1
            self.last_error = f"GPS {type(e).__name__}: {str(e)[:80]}"
            log.warning("GPS-Abruf fehlgeschlagen: %s", self.last_error)
        return new, gps

    @property
    def age_s(self) -> float:
        """Seconds since the newest series row, or None."""
        if self.last_seen is None:
            return None
        return (pd.Timestamp.now("UTC") - self.last_seen).total_seconds()
