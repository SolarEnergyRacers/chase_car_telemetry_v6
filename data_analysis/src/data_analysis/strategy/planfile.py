"""Plan files: the hand-over from point_strategy.py to the live view.

point_strategy.py answers "what should we drive from here" and prints it.
The live view needs the same answer as DATA, later, on another laptop, while
the car is moving - so every evaluated plan is written to a file, and the
live view picks one of them. Nothing is recomputed on the live side: a plan
file is self-contained (trace with coordinates, standing phases, driver
changes, the pack state it was built on) and needs neither the weather
cache nor strategy-private to be read.

Why a file and not an in-process hand-over: the strategist only knows AFTER
a run which option is the one to drive, and the run happens on whichever
machine has the weather cache. Writing every feasible option costs a few
hundred kB per run and makes "take option 4 from the 12:53 run" a menu
choice instead of a re-run.

The projection helpers live here and not in compile_route.py on purpose:
the live view must work from the plan file alone, without importing the
route toolchain (which needs strategy-private, an SRTM directory and a
localconfig.json on the machine).
"""

from __future__ import annotations

from   dataclasses import dataclass, field
from   datetime import datetime, timedelta, timezone
from   pathlib import Path
import json
import logging as lg
import os

import numpy as np
import pandas as pd

log = lg.getLogger(__name__)

FORMAT_VERSION = 1

# columns of the trace that go into the file, in this order. Everything the
# live view reads is listed here; anything else in the DayOption trace is
# an intermediate of the solver and stays out.
TRACE_COLS = [
    "t_start", "t_end", "dt_s", "km_start", "km_end", "speed_kmh",
    "v_route", "v_limit", "v_limit_est", "lat", "lon", "altitude_m",
    "p_solar", "p_motor", "p_aux", "p_net", "Ws",
    "wh_remaining", "soc", "wh_floor", "v_pack_pred",
    "leg", "kind", "panel", "n_roundabout", "n_traffic_signal",
]

# Labels for the standing phases, matched on the leg name prefix. The
# names are set in dayplan.py; matching on prefix keeps "Loopstopp 2" and
# "Fahrerwechsel km 88.3" in the right bucket.
STOP_KINDS = (
    ("Kontrollstopp",  "control"),
    ("Loopstopp",      "loop"),
    ("Fahrerwechsel",  "driver"),
    ("Standladen",     "charge"),
    ("Restzeit",       "rest"),
)

# The REGULATED minimum halt, which is not the planned halt: dayplan.py
# budgets 35 min at the control stop and 8 at a loop break, the extra
# minutes being the ones spent getting the car ready to leave. What the
# rules demand - and therefore the moment from which driving on is
# allowed - is shorter. Written into every new plan file from
# race_config.json; this is the fallback for files written before that,
# and for a day whose config does not name them.
REGULATED_STOP_MINUTES = {"control": 30.0, "loop": 5.0}


def default_plans_dir() -> Path:
    """Where plan files go: $SSC_PLANS_DIR, else data_analysis/plans/."""
    if env := os.environ.get("SSC_PLANS_DIR"):
        return Path(env).expanduser()
    # .../data_analysis/src/data_analysis/strategy/planfile.py -> data_analysis/
    return Path(__file__).resolve().parents[3] / "plans"


def _utc(t):
    """ISO strings -> UTC timestamps, mixed precision and all.

    `Timestamp.isoformat()` drops the fractional part when it is zero, so
    a column written by _iso() is a MIXTURE: most rows carry nanoseconds,
    a row that happens to land on a whole second does not. pandas infers
    one format from the first element and then applies it strictly, so
    such a column raises

        time data "2026-09-13T15:00:00+00:00" doesn't match format
        "%Y-%m-%dT%H:%M:%S.%f%z"

    and the plan does not load at all. The whole second is not exotic: the
    last leg is clamped to the race deadline, which is a round time, so
    every plan that uses up the day ends on one.

    `format="ISO8601"` is exactly this case - one grammar, per-value
    precision. Not "mixed", which re-sniffs every element and would also
    swallow things that are not ISO at all.
    """
    return pd.to_datetime(t, utc=True, format="ISO8601")


def _secs(t) -> np.ndarray:
    """Timestamps -> Unix seconds, independent of the datetime64 unit.

    An int64 view divided by 1e9 is only right for nanosecond frames;
    pandas 2 happily builds microsecond ones from ISO strings, and then
    every lookup is off by a factor of a thousand without a single error.
    """
    idx = pd.DatetimeIndex(_utc(t))
    return ((idx.tz_convert("UTC").tz_localize(None)
             - pd.Timestamp("1970-01-01")) / pd.Timedelta(seconds=1)).to_numpy()


def _iso(t) -> str:
    t = pd.Timestamp(t)
    t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
    return t.isoformat()


def _trace_for_file(tr: pd.DataFrame) -> pd.DataFrame:
    """Normalise a DayOption trace into the file layout.

    In the solver trace `time` means two different things: the segment
    MID-time for a driving row (total_Ws_for_lap samples the weather
    there) and the START for a standing row. `km_total` is the END of the
    row. The file carries an explicit start and end for both axes, so
    nobody downstream has to know that.
    """
    tr = tr.reset_index(drop=True).copy()
    t = _utc(tr["time"])
    dt = pd.to_timedelta(tr["dt_s"].to_numpy(), unit="s")
    is_drive = tr["kind"].to_numpy() == "drive"
    t_start = pd.Series(np.where(is_drive, t - dt / 2, t), index=tr.index)
    t_start = _utc(t_start)
    tr["t_start"] = t_start
    tr["t_end"] = t_start + dt
    km_end = tr["km_total"].to_numpy(dtype=float)
    tr["km_end"] = km_end
    tr["km_start"] = np.concatenate([[0.0], km_end[:-1]])
    for col in TRACE_COLS:
        if col not in tr.columns:
            tr[col] = np.nan
    return tr[TRACE_COLS]


def save_plan(opt, state, batt, weathers: dict = None, out_dir: Path = None,
              mode: str = "plan", regulated: dict = None) -> Path:
    """Write one evaluated DayOption as a plan file. Returns the path.

    Only feasible options are worth a file; an infeasible one has no trace.
    """
    if not opt.feasible or opt.trace is None:
        raise ValueError("only a feasible option with a trace can be saved")
    from ..simulation.battery import capacity_wh

    out_dir = Path(out_dir or default_plans_dir())
    out_dir.mkdir(parents=True, exist_ok=True)

    tr = _trace_for_file(opt.trace)
    t_end = tr["t_end"].iloc[-1]
    p = state.pack
    created = datetime.now(timezone.utc)

    meta = {
        "format": FORMAT_VERSION,
        "created_utc": _iso(created),
        "mode": mode,
        "day": int(state.day),
        "day_date": state.day_date.isoformat(),
        "t_now": _iso(state.t_now),
        "t_deadline": _iso(state.t_deadline),
        "t_finish": _iso(t_end),
        "part": state.part,
        "km_in_part": float(state.km_in_part),
        "part_km": float(state.part_km),
        "loop_leg": state.loop_leg,
        "loop_done": int(state.loop_done or 0),
        "position_source": state.position_source,
        "cross_track_m": state.cross_track_m,
        "pack": {
            "wh": float(p.wh), "soc": float(p.soc), "source": p.source,
            "trust": p.trust, "v_pack": p.v_pack, "i_batt": p.i_batt,
            "reading_age_s": p.reading_age_s,
            "capacity_wh": float(capacity_wh(batt)),
            "floor_wh": float((1.0 - batt.usable) * capacity_wh(batt)),
        },
        "n_loops": int(opt.n_loops),
        "km": float(opt.km),
        "avg_kmh": float(opt.avg_kmh),
        "drive_time_s": (opt.drive_time.total_seconds()
                         if opt.drive_time else None),
        "stop_time_s": (opt.stop_time.total_seconds()
                        if opt.stop_time else None),
        "reserve_s": opt.reserve.total_seconds() if opt.reserve else None,
        "min_soc": opt.min_soc, "end_soc": opt.end_soc, "end_wh": opt.end_wh,
        "cloud_margin": opt.cloud_margin,
        "wh_spilled": float(opt.wh_spilled),
        "floor_released": bool(opt.floor_released),
        # panel never aimed: every halt in the trace charges at GHI. The
        # live view has to know, because its own standing estimate would
        # otherwise add a tracked bonus the car cannot earn. Older files
        # have no such key, so every reader must default to False.
        "panel_flat": bool(getattr(opt, "panel_flat", False)),
        "driver_changes_km": [float(s.km) for s in opt.driver_changes],
        # the regulated minimum per halt kind, from race_config.json - the
        # live view shows from when driving on is ALLOWED, which is not the
        # planned halt length
        "regulated_stop_minutes": dict(regulated or REGULATED_STOP_MINUTES),
        "weather": {name: (_iso(cw.fetched_at) if cw.fetched_at else None)
                    for name, cw in (weathers or {}).items()},
        "notes": list(state.notes or []),
    }
    meta["label"] = plan_label(meta)

    rows = tr.copy()
    for col in ("t_start", "t_end"):
        rows[col] = [_iso(x) for x in rows[col]]
    # a metre in lat/lon is 1e-5 deg, a Wh is plenty for energy: full
    # double precision would triple the file for nothing
    for col in ("lat", "lon"):
        rows[col] = rows[col].round(6)
    for col in ("dt_s", "km_start", "km_end", "speed_kmh", "v_route",
                "v_limit", "v_limit_est", "altitude_m", "p_solar", "p_motor",
                "p_aux", "p_net", "Ws", "wh_remaining", "wh_floor",
                "v_pack_pred"):
        rows[col] = rows[col].round(3)
    rows["soc"] = rows["soc"].round(5)
    rows = rows.astype(object).where(pd.notna(rows), None)

    t_local = pd.Timestamp(state.t_now).tz_convert("Africa/Johannesburg")
    # "flach" in the name, so that two runs differing ONLY in the panel
    # premise are still tellable apart in a directory listing - the label
    # in the menu says it too, but the file name is what gets copied
    # around and quoted in a debrief
    stem = (f"tag{state.day}_{t_local:%H%M}_{state.part}_"
            f"km{state.km_in_part:.0f}_{opt.n_loops}loops"
            + ("_flach" if meta["panel_flat"] else "")
            + f"_{created:%Y%m%dT%H%M%S}")
    path = out_dir / f"{stem}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump({"meta": meta, "columns": TRACE_COLS,
                   "rows": rows.to_numpy().tolist()}, f)
    log.info("Plan gespeichert: %s", path)
    return path


def plan_label(meta: dict) -> str:
    """One line for a menu: 'Tag 1 · 12:53 vor KS km 84 · 3 Loops · Ende 89 %'."""
    part_de = {"to_control": "vor KS", "loop": "Loop",
               "to_finish": "nach KS"}
    t = pd.Timestamp(meta["t_now"]).tz_convert("Africa/Johannesburg")
    s = (f"Tag {meta['day']} · {t:%H:%M} {part_de.get(meta['part'], meta['part'])} "
         f"km {meta['km_in_part']:.0f} · {meta['n_loops']} Loops · "
         f"{meta['km']:.0f} km · Ende {100*meta['end_soc']:.0f} %")
    if meta.get("panel_flat"):
        s += " · Panel flach"
    if meta.get("mode") == "plan":
        s += " · --plan"
    return s


def list_plans(plans_dir: Path = None) -> list:
    """All plan files, newest first, with their meta (no trace parsed)."""
    plans_dir = Path(plans_dir or default_plans_dir())
    if not plans_dir.is_dir():
        return []
    out = []
    for fp in plans_dir.glob("*.json"):
        try:
            with fp.open("r", encoding="utf-8") as f:
                head = json.load(f)
            meta = head["meta"]
        except (OSError, ValueError, KeyError) as e:
            log.warning("Plandatei %s nicht lesbar: %s", fp.name, e)
            continue
        out.append({"file": fp.name, "path": str(fp),
                    "mtime": fp.stat().st_mtime, **meta})
    out.sort(key=lambda d: d.get("created_utc") or "", reverse=True)
    return out


# ------------------------------------------------------------------- Plan ----

@dataclass
class Plan:
    """A loaded plan file with the lookups the live view needs.

    Everything is indexed by `km` = distance driven since the plan's start
    point, which is the one axis that is unique over a loop day (the route
    km is not - loop 2 and loop 3 share every road km).
    """
    meta: dict
    tr: pd.DataFrame
    path: Path = None

    # arrays, filled in __post_init__
    _km_end: np.ndarray = field(default=None, repr=False)
    _t_end_s: np.ndarray = field(default=None, repr=False)
    _drive: pd.DataFrame = field(default=None, repr=False)

    def __post_init__(self):
        tr = self.tr
        self._km_end = tr["km_end"].to_numpy(dtype=float)
        self._t_end_s = _secs(tr["t_end"])
        self._t_start_s = _secs(tr["t_start"])
        self._drive = tr[tr["kind"] == "drive"]
        d = self._drive
        # polyline of segment START nodes plus the very last end node, for
        # projection. The end node of the last segment is the last node of
        # the route it came from - not stored, so the polyline ends at the
        # last segment start; the missing ~100 m at the finish line do not
        # matter for tracking.
        self._poly_lat = d["lat"].to_numpy(dtype=float)
        self._poly_lon = d["lon"].to_numpy(dtype=float)
        self._poly_km = d["km_start"].to_numpy(dtype=float)
        self._loop_cache = None
        # cumulative planned solar and load, Wh, at km_end - what
        # live_monitor.DayPlan builds too, kept here so a Plan can stand in
        self.dt_h = tr["dt_s"].to_numpy(dtype=float) / 3600.0
        self._wh_solar_cum = np.cumsum(tr["p_solar"].to_numpy(dtype=float)
                                       * self.dt_h)
        self._wh_load_cum = np.cumsum(
            (tr["p_motor"].to_numpy(dtype=float)
             + tr["p_aux"].to_numpy(dtype=float)) * self.dt_h)

    # --- constructors -------------------------------------------------
    @classmethod
    def load(cls, path) -> "Plan":
        path = Path(path)
        with path.open("r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("meta", {}).get("format", 0) > FORMAT_VERSION:
            log.warning("Plandatei %s hat Format %s, gelesen wird %s",
                        path.name, d["meta"]["format"], FORMAT_VERSION)
        tr = pd.DataFrame(d["rows"], columns=d["columns"])
        for col in ("t_start", "t_end"):
            tr[col] = _utc(tr[col])
        num = [c for c in TRACE_COLS if c not in
               ("t_start", "t_end", "leg", "kind", "panel")]
        for c in num:
            tr[c] = pd.to_numeric(tr[c], errors="coerce")
        return cls(meta=d["meta"], tr=tr, path=path)

    @classmethod
    def from_option(cls, opt, state, batt) -> "Plan":
        """Build a Plan in memory from a DayOption, without a file."""
        from ..simulation.battery import capacity_wh
        tr = _trace_for_file(opt.trace)
        meta = {"format": FORMAT_VERSION, "day": state.day,
                "day_date": state.day_date.isoformat(),
                "t_now": _iso(state.t_now), "t_deadline": _iso(state.t_deadline),
                "t_finish": _iso(tr["t_end"].iloc[-1]),
                "part": state.part, "km_in_part": float(state.km_in_part),
                "part_km": float(state.part_km), "loop_leg": state.loop_leg,
                "loop_done": int(state.loop_done or 0),
                "pack": {"wh": float(state.pack.wh), "soc": float(state.pack.soc),
                         "source": state.pack.source, "trust": state.pack.trust,
                         "capacity_wh": float(capacity_wh(batt)),
                         "floor_wh": float((1 - batt.usable) * capacity_wh(batt))},
                "n_loops": int(opt.n_loops), "km": float(opt.km),
                "avg_kmh": float(opt.avg_kmh), "end_soc": opt.end_soc,
                "min_soc": opt.min_soc, "end_wh": opt.end_wh,
                "cloud_margin": opt.cloud_margin, "mode": "memory",
                "driver_changes_km": [float(s.km) for s in opt.driver_changes],
                "reserve_s": opt.reserve.total_seconds() if opt.reserve else None}
        meta["label"] = plan_label(meta)
        return cls(meta=meta, tr=tr)

    # --- basic properties ---------------------------------------------
    @property
    def total_km(self) -> float:
        return float(self._km_end[-1])

    @property
    def t_start(self) -> pd.Timestamp:
        return _utc(self.tr["t_start"].iloc[0])

    @property
    def t_finish(self) -> pd.Timestamp:
        return _utc(self.tr["t_end"].iloc[-1])

    @property
    def wh_start(self) -> float:
        return float(self.meta["pack"]["wh"])

    @property
    def label(self) -> str:
        return self.meta.get("label") or plan_label(self.meta)

    # --- lookups by km -------------------------------------------------
    def _row_at_km(self, km: float) -> int:
        """Index of the DRIVING row that contains km (km_start <= km < km_end).

        Standing rows have zero length and are skipped: at a stop the
        driver wants to know the speed of the road ahead, not "0".
        """
        d = self._drive
        ke = d["km_end"].to_numpy()
        i = int(np.searchsorted(ke, km, side="right"))
        i = min(max(i, 0), len(d) - 1)
        return int(d.index[i])

    def speed_at_km(self, km: float) -> dict:
        """Target, routing cap and limit at a distance along the plan."""
        r = self.tr.loc[self._row_at_km(km)]
        return {"v_soll": float(r["speed_kmh"]), "v_route": float(r["v_route"]),
                "v_limit": (None if pd.isna(r["v_limit"]) else float(r["v_limit"])),
                "v_limit_est": (None if pd.isna(r["v_limit_est"])
                                else float(r["v_limit_est"])),
                "leg": str(r["leg"]),
                "leg_km": float(km - self.leg_start_km(str(r["leg"]))),
                "altitude_m": float(r["altitude_m"]),
                "p_solar_plan": float(r["p_solar"]),
                "p_net_plan": float(r["p_net"])}

    def leg_start_km(self, leg: str) -> float:
        m = self.tr["leg"] == leg
        return float(self.tr.loc[m, "km_start"].iloc[0]) if m.any() else 0.0

    def legs(self) -> list:
        """Driving legs as (name, km_start, km_end) in order."""
        d = self._drive
        blocks = (d["leg"] != d["leg"].shift()).cumsum()
        out = []
        for _, g in d.groupby(blocks, sort=False):
            out.append({"leg": str(g["leg"].iloc[0]),
                        "km_start": float(g["km_start"].iloc[0]),
                        "km_end": float(g["km_end"].iloc[-1]),
                        "t_start": pd.Timestamp(g["t_start"].iloc[0]),
                        "t_end": pd.Timestamp(g["t_end"].iloc[-1])})
        return out

    def loop_passes(self) -> list:
        """Every pass of the loop, with its turnaround.

        A loop is driven out and back over the same road - inputs.py
        builds it as the outbound route followed by itself reversed, with
        the turnaround node dropped - so the two halves share every
        coordinate. A GPS fix alone therefore cannot say which half the
        car is on, nor which pass; only the km axis can, because it is the
        unrolled day. That is precisely why a wrong pass has to be
        correctable by hand: the projection resolves the ambiguity from
        the PREVIOUS fix, so once it has slipped, it stays slipped, and
        every ETA and the whole energy comparison slip with it.

        The turnaround is read off the coordinates rather than assumed to
        be the midpoint: the first loop of a plan computed mid-loop is a
        remainder ("Loop (laufend, Rest)") and may be all return leg. It
        is the node farthest from the leg's start - which for a full loop
        IS the midpoint, and for a remainder is wherever the turn still
        lies ahead, or the start itself if it is already behind.

        Returns, per pass: nr (1-based), leg (the label), km_start,
        km_turn, km_end, out_km/back_km, and the loop stop that closes it.
        """
        if getattr(self, "_loop_cache", None) is not None:
            return self._loop_cache          # called once a second, per fix
        stops = self.stops()
        loop_stops = stops[stops["kind"] == "loop"] if not stops.empty else stops
        out = []
        for lg in self.legs():
            if not str(lg["leg"]).lower().startswith("loop"):
                continue
            a, b = lg["km_start"], lg["km_end"]
            sel = (self._poly_km >= a - 1e-9) & (self._poly_km <= b + 1e-9)
            km_turn = 0.5 * (a + b)          # fallback: no coordinates
            if sel.sum() >= 2:
                lat, lon = self._poly_lat[sel], self._poly_lon[sel]
                km = self._poly_km[sel]
                good = np.isfinite(lat) & np.isfinite(lon)
                if good.sum() >= 2:
                    lat, lon, km = lat[good], lon[good], km[good]
                    # planar is plenty over a loop of a few tens of km
                    phi = np.radians(lat[0])
                    dx = (lon - lon[0]) * 111_412.84 * np.cos(phi)
                    dy = (lat - lat[0]) * 111_132.92
                    km_turn = float(km[int(np.argmax(np.hypot(dx, dy)))])
            nr = len(out) + 1
            stop_name, stop_km = None, None
            if not loop_stops.empty:
                d = (loop_stops["km"] - b).abs()
                if float(d.min()) <= 0.3:
                    r = loop_stops.loc[d.idxmin()]
                    stop_name, stop_km = str(r["name"]), float(r["km"])
            out.append({"nr": nr, "leg": str(lg["leg"]),
                        "km_start": float(a), "km_turn": float(km_turn),
                        "km_end": float(b),
                        "out_km": float(km_turn - a),
                        "back_km": float(b - km_turn),
                        "stop": stop_name, "stop_km": stop_km,
                        "t_start": lg["t_start"], "t_end": lg["t_end"]})
        self._loop_cache = out
        return out

    def loop_at_km(self, km: float) -> dict:
        """Which loop pass and half a kilometre falls in, or None.

        `half` is 'out' or 'back'; a remainder leg with no outbound part
        left reports 'back' throughout, which is what it is.
        """
        if km is None:
            return None
        for p in self.loop_passes():
            if p["km_start"] - 1e-9 <= km <= p["km_end"] + 1e-9:
                half = "out" if (p["out_km"] > 0 and km <= p["km_turn"]) else "back"
                return {**p, "half": half}
        return None

    def loop_window(self, nr: int, half: str = None) -> tuple:
        """(km_lo, km_hi) of one pass, or of one half of it."""
        p = next((x for x in self.loop_passes() if x["nr"] == int(nr)), None)
        if p is None:
            raise ValueError(f"Loop {nr} gibt es in diesem Plan nicht")
        if half == "out" and p["out_km"] > 0:
            return p["km_start"], p["km_turn"]
        if half == "back" and p["back_km"] > 0:
            return p["km_turn"], p["km_end"]
        return p["km_start"], p["km_end"]

    def wh_at_km(self, km: float) -> dict:
        """Planned pack state and cumulatives at km (interpolated)."""
        km = float(np.clip(km, 0.0, self._km_end[-1]))
        ke = self._km_end
        wh = self.tr["wh_remaining"].to_numpy(dtype=float)
        # wh_remaining is the value at the END of each row; prepend start
        x = np.concatenate([[0.0], ke])
        y = np.concatenate([[self.wh_start], wh])
        fl = self.tr["wh_floor"].to_numpy(dtype=float)
        sol = np.concatenate([[0.0], self._wh_solar_cum])
        load = np.concatenate([[0.0], self._wh_load_cum])
        soc = self.tr["soc"].to_numpy(dtype=float)
        # x is monotone non-decreasing (stops add zero length); np.interp
        # copes with repeated x by taking the later value, which is what we
        # want after a stop (the charge is already in the pack)
        return {
            "wh_remaining": float(np.interp(km, x, y)),
            "soc": float(np.interp(km, x, np.concatenate(
                [[self.meta["pack"]["soc"]], soc]))),
            "wh_floor": float(np.interp(km, ke, fl)),
            "wh_solar": float(np.interp(km, x, sol)),
            "wh_load": float(np.interp(km, x, load)),
        }

    def wh_at_time(self, t) -> dict:
        """Planned pack state and cumulatives at a clock time (interpolated).

        The time axis is the right one INSIDE a planned standing phase: at
        km 172.8 the plan holds two values, before and after the control
        stop, and a car that has stood there for 15 of the 35 minutes is
        halfway between them. Over the km axis that is invisible.
        """
        s_ = pd.Timestamp(t).tz_convert("UTC").timestamp()
        x = np.concatenate([[self._t_start_s[0]], self._t_end_s])
        wh = np.concatenate([[self.wh_start],
                             self.tr["wh_remaining"].to_numpy(dtype=float)])
        return {
            "wh_remaining": float(np.interp(s_, x, wh)),
            "soc": float(np.interp(s_, x, np.concatenate(
                [[self.meta["pack"]["soc"]], self.tr["soc"].to_numpy(dtype=float)]))),
            "wh_floor": float(np.interp(s_, self._t_end_s,
                                        self.tr["wh_floor"].to_numpy(dtype=float))),
            "wh_solar": float(np.interp(s_, x, np.concatenate([[0.0], self._wh_solar_cum]))),
            "wh_load": float(np.interp(s_, x, np.concatenate([[0.0], self._wh_load_cum]))),
        }

    def stop_at(self, km: float, t, tol_km: float = 0.15,
                standing: bool = False):
        """The planned standing phase the car is in, if any.

        While DRIVING, a halt counts from two minutes before its planned
        arrival (the car may be early) to its planned departure.

        `standing=True` drops BOTH bounds, because a car that stands at a
        halt's kilometre is at that halt whatever the clock says. The
        upper bound went first: twenty minutes late at the control stop
        and the whole display fell back to "driving" exactly when it was
        most obviously not. The lower bound is the same mistake mirrored,
        and it bites hardest on loops - a loop stop is five minutes long,
        so arriving three minutes early is normal rather than remarkable.
        Measured on the loop plan: six minutes early at Loopstopp 1 and
        `at_stop` was None - no halt box, no "leave at", and a confirmed
        arrival could not even be attributed to a halt. The caller knows
        whether the car stands; the plan does not.

        Returns the stops() row or None. The nearest halt wins when two
        are within tolerance, which only happens on a loop shorter than
        300 m - i.e. never, but ordering by distance costs nothing.
        """
        st = self.stops()
        if st.empty:
            return None
        t = pd.Timestamp(t).tz_convert("UTC")
        best, best_d = None, np.inf
        for _, r in st.iterrows():
            d = abs(float(r["km"]) - km)
            if d > tol_km or d >= best_d:
                continue
            if not standing:
                if t < r["t_arrive"] - pd.Timedelta(minutes=2):
                    continue
                if t > r["t_depart"]:
                    continue
            best, best_d = r, d
        return best

    def ref_at(self, km: float, t, standing: bool = False) -> dict:
        """Plan reference for a live comparison: by km while driving, by
        time while standing in a planned halt. Adds `at_stop` (name|None)."""
        r = self.stop_at(km, t, standing=standing)
        if r is None:
            out = self.wh_at_km(km)
            out["at_stop"] = None
            return out
        by_t = self.wh_at_time(t)
        # clamp between the halt's own start and end values, so an early
        # arrival does not read the driving row before it
        before = self.wh_at_time(r["t_arrive"])
        after = self.wh_at_time(r["t_depart"])
        for k in ("wh_remaining", "soc", "wh_solar", "wh_load"):
            lo, hi = sorted((before[k], after[k]))
            by_t[k] = float(np.clip(by_t[k], lo, hi))
        by_t["at_stop"] = str(r["name"])
        return by_t

    def schedule_min(self, km: float, t) -> float:
        """Minutes behind the plan at km (positive = late).

        Three cases at a planned halt: arrived early -> ahead by that much;
        standing between planned arrival and departure -> on schedule;
        still standing after the planned departure -> behind.
        """
        t = pd.Timestamp(t).tz_convert("UTC")
        t_arr = self.time_at_km_arrival(km)
        t_dep = self.time_at_km(km)
        if t < t_arr:
            return (t - t_arr).total_seconds() / 60.0
        if t <= t_dep:
            return 0.0
        return (t - t_dep).total_seconds() / 60.0

    def time_at_km(self, km: float) -> pd.Timestamp:
        """Planned clock time of reaching km (the END of any stop there)."""
        km = float(np.clip(km, 0.0, self._km_end[-1]))
        x = np.concatenate([[0.0], self._km_end])
        y = np.concatenate([[self._t_start_s[0]], self._t_end_s])
        # at a stop several rows share one km; interp returns the LAST,
        # i.e. the departure - right for "when do I leave here"
        return pd.Timestamp(float(np.interp(km, x, y)), unit="s", tz="UTC")

    def time_at_km_arrival(self, km: float) -> pd.Timestamp:
        """Planned ARRIVAL at km - the end of the driving row before it."""
        d = self._drive
        ke = d["km_end"].to_numpy()
        ks = d["km_start"].to_numpy()
        ts = _secs(d["t_start"])
        te = _secs(d["t_end"])
        i = int(np.clip(np.searchsorted(ke, km, side="left"), 0, len(d) - 1))
        if ke[i] - ks[i] <= 0:
            return pd.Timestamp(te[i], unit="s", tz="UTC")
        f = float(np.clip((km - ks[i]) / (ke[i] - ks[i]), 0.0, 1.0))
        return pd.Timestamp(ts[i] + f * (te[i] - ts[i]), unit="s", tz="UTC")

    def km_at_time(self, t) -> float:
        s = pd.Timestamp(t).tz_convert("UTC").timestamp()
        x = np.concatenate([[self._t_start_s[0]], self._t_end_s])
        y = np.concatenate([[0.0], self._km_end])
        return float(np.interp(s, x, y))

    def travel_time_s(self, km_a: float, km_b: float,
                      include_stops: bool = True) -> float:
        """Planned seconds from km_a to km_b, stops included by default.

        Only the road matters for an ETA at the current pace, so the
        stops between here and there are added separately by the caller
        if it wants them - hence the flag.
        """
        if km_b <= km_a:
            return 0.0
        tr = self.tr
        ks = tr["km_start"].to_numpy(dtype=float)
        ke = self._km_end
        dt = tr["dt_s"].to_numpy(dtype=float)
        drive = tr["kind"].to_numpy() == "drive"
        length = ke - ks
        # overlap fraction of every row with [km_a, km_b]
        lo = np.maximum(ks, km_a)
        hi = np.minimum(ke, km_b)
        frac = np.where(length > 0, np.clip(hi - lo, 0, None) / np.where(
            length > 0, length, 1.0), 0.0)
        t = float(np.sum(dt[drive] * frac[drive]))
        if include_stops:
            # zero-length rows strictly inside the interval: a halt AT km_a
            # is where the car already stands, a halt AT km_b is the one
            # being asked about - neither is time on the way there
            inside = (~drive) & (ks > km_a) & (ks < km_b)
            t += float(np.sum(dt[inside]))
        return t

    def drive_deadline(self, km_from: float, seconds: float,
                       min_break_s: float = 180.0) -> dict:
        """Where the plan's pace uses up `seconds` of CONTINUOUS driving.

        Answers "when is the driver change due at the latest", which is
        not the same as "where does the plan put one": every halt of at
        least `min_break_s` is a break and restarts the two hours, so a
        loop stop on the way can make the question go away. The walk
        therefore stops at the first such halt and says so.

        Returns {km, after_s, reset_by} - `reset_by` is the name of the
        halt that resets the counter first, or None if the driving time
        runs out before any halt. Both are None at the end of the plan
        (the day ends before either happens).
        """
        tr = self.tr
        ks = tr["km_start"].to_numpy(dtype=float)
        ke = self._km_end
        dt = tr["dt_s"].to_numpy(dtype=float)
        kind = tr["kind"].to_numpy()
        legs = tr["leg"].to_numpy()
        acc = 0.0          # driving seconds counted
        elapsed = 0.0      # wall seconds from km_from
        for i in range(len(tr)):
            if ke[i] < km_from:
                continue
            if kind[i] != "drive":
                if ks[i] <= km_from:
                    continue          # the halt the car is standing in
                if dt[i] >= min_break_s:
                    return {"km": float(ks[i]), "after_s": elapsed,
                            "reset_by": str(legs[i])}
                elapsed += dt[i]
                continue
            length = ke[i] - ks[i]
            lo = max(ks[i], km_from)
            avail = dt[i] * ((ke[i] - lo) / length if length > 0 else 0.0)
            if acc + avail >= seconds:
                f = (seconds - acc) / avail if avail > 0 else 0.0
                return {"km": float(lo + f * (ke[i] - lo)),
                        "after_s": elapsed + (seconds - acc), "reset_by": None}
            acc += avail
            elapsed += avail
        return {"km": None, "after_s": None, "reset_by": None}

    # --- events --------------------------------------------------------
    def regulated_s(self, kind: str) -> float:
        """Regulated minimum for a halt kind, seconds. None where none applies.

        A driver change and a charging stop have no regulated length - the
        planned duration is all there is.
        """
        reg = self.meta.get("regulated_stop_minutes") or REGULATED_STOP_MINUTES
        m = reg.get(kind)
        return None if m is None else float(m) * 60.0

    def stops(self) -> pd.DataFrame:
        """Standing phases, one row per halt (tracked+flat merged).

        Columns: name, kind, km, t_arrive, t_depart, dur_s, reg_s, wh_gain.
        `dur_s` is what the plan budgets, `reg_s` what the rules demand
        (None where nothing is regulated) - see REGULATED_STOP_MINUTES.
        """
        s = self.tr[self.tr["kind"] == "stop"]
        cols = ["name", "kind", "km", "t_arrive", "t_depart", "dur_s",
                "reg_s", "wh_gain"]
        if s.empty:
            return pd.DataFrame(columns=cols)
        rows = []
        # merge consecutive rows with the same leg name (tracked + flat)
        blocks = (s["leg"] != s["leg"].shift()).cumsum()
        for _, g in s.groupby(blocks, sort=False):
            name = str(g["leg"].iloc[0])
            kind = next((k for pre, k in STOP_KINDS if name.startswith(pre)),
                        "other")
            dur = float(g["dt_s"].sum())
            reg = self.regulated_s(kind)
            rows.append({
                "name": name, "kind": kind,
                "km": float(g["km_end"].iloc[0]),
                "t_arrive": pd.Timestamp(g["t_start"].iloc[0]),
                "t_depart": pd.Timestamp(g["t_end"].iloc[-1]),
                "dur_s": dur,
                # never longer than the plan itself budgets: a regulated
                # 30 min against a 20 min charging stop would be nonsense
                "reg_s": (None if reg is None else min(reg, dur)),
                "wh_gain": float(-g["Ws"].sum() / 3600.0),
            })
        return pd.DataFrame(rows)[cols]

    def features(self) -> pd.DataFrame:
        """Roundabouts and traffic signals along the plan: kind, km, n."""
        d = self._drive
        rows = []
        for col, kind in (("n_roundabout", "roundabout"),
                          ("n_traffic_signal", "traffic_signal")):
            if col not in d.columns:
                continue
            n = pd.to_numeric(d[col], errors="coerce").fillna(0).to_numpy()
            for i in np.flatnonzero(n > 0):
                rows.append({"kind": kind,
                             "km": float(d["km_start"].iloc[i]),
                             "n": int(n[i])})
        out = pd.DataFrame(rows, columns=["kind", "km", "n"])
        return out.sort_values("km").reset_index(drop=True) if len(out) else out

    # --- geometry ------------------------------------------------------
    def project(self, lat: float, lon: float, after_km: float = None,
                window_km: float = None) -> tuple:
        """Where a coordinate sits along the plan: (km, cross_track_m).

        Same construction as compile_route.project_onto_route(): project
        onto segments, not nodes. `after_km` restricts the search to the
        plan from there on, `window_km` additionally to a stretch beyond it
        - together they resolve the loop ambiguity, because the plan is
        the UNROLLED day and each pass of a loop has its own km.
        """
        plat, plon, pkm = self._poly_lat, self._poly_lon, self._poly_km
        ok = np.isfinite(plat) & np.isfinite(plon)
        if ok.sum() < 2:
            raise ValueError("plan has no coordinates to project onto")
        plat, plon, pkm = plat[ok], plon[ok], pkm[ok]

        phi = np.radians(lat)
        scale_lat = (111_132.92 - 559.82 * np.cos(2 * phi)
                     + 1.175 * np.cos(4 * phi))
        scale_lon = (111_412.84 * np.cos(phi) - 93.5 * np.cos(3 * phi)
                     + 0.118 * np.cos(5 * phi))
        px = (plon - lon) * scale_lon
        py = (plat - lat) * scale_lat
        ax, ay, bx, by = px[:-1], py[:-1], px[1:], py[1:]
        dx, dy = bx - ax, by - ay
        seg_sq = dx * dx + dy * dy
        seg_sq = np.where(seg_sq == 0, np.inf, seg_sq)
        t = np.clip(-(ax * dx + ay * dy) / seg_sq, 0.0, 1.0)
        cross = np.hypot(ax + t * dx, ay + t * dy)

        if after_km is not None:
            cross = np.where(pkm[1:] >= after_km, cross, np.inf)
            if window_km is not None:
                cross = np.where(pkm[:-1] <= after_km + window_km, cross,
                                 np.inf)
            if not np.isfinite(cross).any():
                raise ValueError(f"no plan segment after km {after_km:.1f}")
        i = int(np.argmin(cross))
        km = float(pkm[i] + t[i] * (pkm[i + 1] - pkm[i]))
        return km, float(cross[i])

    def coord_at_km(self, km: float) -> tuple:
        """(lat, lon) on the plan polyline at km."""
        km = float(np.clip(km, self._poly_km[0], self._poly_km[-1]))
        return (float(np.interp(km, self._poly_km, self._poly_lat)),
                float(np.interp(km, self._poly_km, self._poly_lon)))

    # --- adapters ------------------------------------------------------
    def as_dayplan(self):
        """A live_monitor.DayPlan over this trace, for LiveEnergy."""
        from ..simulation.live_monitor import DayPlan
        df = pd.DataFrame({
            "time": _utc(self.tr["t_start"]),
            "cum_km": self._km_end,
            "wh_remaining": self.tr["wh_remaining"].to_numpy(dtype=float),
            "dt_s": self.tr["dt_s"].to_numpy(dtype=float),
            "p_solar": self.tr["p_solar"].to_numpy(dtype=float),
            "p_net": self.tr["p_net"].to_numpy(dtype=float),
        })
        return DayPlan(df)

    def window(self, km_from: float, km_to: float) -> pd.DataFrame:
        """Driving rows overlapping [km_from, km_to] - for the speed strip."""
        d = self._drive
        m = (d["km_end"] >= km_from) & (d["km_start"] <= km_to)
        return d[m]
