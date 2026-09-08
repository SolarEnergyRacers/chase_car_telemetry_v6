"""Stand-in for the live_monitoring app, for testing live_strategy.py.

    python scripts/mock_live_monitoring.py --plan plans/tag1_..._3loops.json \
        --backlog-min 40 --slow 0.95 --cloud 0.85
    python scripts/live_strategy.py --host localhost:5240 --device honor \
        --plan plans/tag1_..._3loops.json --time-shift <printed value>

Serves the three endpoints live_strategy uses, with the SAME shapes and
quirks as the real app (see telemetrie-anbindung-live-strategie.md):

    GET /api/timeseries/range?from&to&series   -> {series, points}
    GET /api/gps/latest?deviceName             -> GpsPoint (camelCase)
    GET /api/gps/range?from&to&deviceName      -> [GpsPoint]

The car drives the given plan in PLAN TIME at real-time pace: at start the
mock has already recorded `--backlog-min` minutes (so the backfill path is
exercised), and its clock then advances one second per second. Because the
plan's clock is a race day and not today, live_strategy has to be told the
offset (`--time-shift`); the mock prints the value to use.

Quirks reproduced on purpose: a `--gap` window fills battery/MPPT/speed
with 0.0 rather than NaN, as TimeSeries.AddAndInterpolate() does; GPS
timestamps carry the +02:00 offset; series timestamps are UTC with Z.
"""

from __future__ import annotations

from   http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from   pathlib import Path
import argparse
import json
import sys
import threading
import time
import urllib.parse

import numpy as np
import pandas as pd

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from data_analysis.ser_dataclasses import Battery_coeffs             # noqa: E402
from data_analysis.simulation.battery import terminal_voltage       # noqa: E402
from data_analysis.strategy import planfile                         # noqa: E402

SERIES = ["speed", "mppt1_power", "mppt2_power", "mppt3_power", "mppt4_power",
          "motor_current", "motor_voltage", "motor_power",
          "battery_voltage", "battery_current", "battery_power"]


class World:
    """One second of car per second of wall clock, along the plan."""

    def __init__(self, plan, batt, backlog_s: int, slow: float, cloud: float,
                 gap: tuple, device: str, gps_every_s: int = 2,
                 extra_device: str = None):
        self.plan, self.batt = plan, batt
        self.slow, self.cloud, self.gap = slow, cloud, gap
        self.device, self.gps_every_s = device, gps_every_s
        self.extra_device = extra_device
        self.rng = np.random.default_rng(3)
        self.lock = threading.Lock()
        self.rows = {}            # unix s -> dict of series values
        self.gps = []             # list of GpsPoint dicts
        self.km = 0.0
        self.t = plan.t_start     # plan-time clock
        self.t0 = plan.t_start
        self._wall0 = time.time()
        self._wall_backlog = backlog_s
        self._gps_id = 0
        self.wh = plan.wh_start           # the car's OWN energy state
        self.cap = __import__("data_analysis.simulation.battery",
                              fromlist=["capacity_wh"]).capacity_wh(batt)
        for _ in range(backlog_s):
            self._step()

    def now(self) -> pd.Timestamp:
        return self.t0 + pd.Timedelta(seconds=self._wall_backlog
                                      + int(time.time() - self._wall0))

    def advance(self):
        with self.lock:
            while self.t < self.now():
                self._step()

    def _step(self):
        p = self.plan
        self.t = self.t + pd.Timedelta(seconds=1)
        t_dep = p.time_at_km(self.km)
        standing = self.t < t_dep and self.km > 0.01
        here = p.speed_at_km(self.km)
        v_kmh = 0.0 if standing else here["v_soll"] * self.slow
        self.km = min(self.km + v_kmh / 3600.0, p.total_km)
        p_sol = here["p_solar_plan"] * self.cloud
        if standing:
            p_sol *= 1.3
            p_net = -(p_sol - 60.0)
        else:
            p_net = here["p_net_plan"] + here["p_solar_plan"] - p_sol
        # integrate the car's own pack, so the voltage the mock reports is
        # consistent with the energy it actually has (not with the plan)
        self.wh = min(self.wh - p_net / 3600.0, self.cap)
        i = p_net / 115.0 + self.rng.normal(0, 0.3)
        v = terminal_voltage(self.batt, self.wh, i) + self.rng.normal(0, 0.05)
        unix = int(self.t.timestamp())
        elapsed = (self.t - self.t0).total_seconds()
        in_gap = self.gap and self.gap[0] <= elapsed <= self.gap[1]
        if in_gap:
            row = {s: 0.0 for s in SERIES}
        else:
            row = {"speed": float(round(v_kmh)),
                   "mppt1_power": p_sol / 3, "mppt2_power": p_sol / 3,
                   "mppt3_power": p_sol / 3, "mppt4_power": 0.0,
                   "battery_voltage": v, "battery_current": -i,
                   "battery_power": -v * i,
                   "motor_current": -i + p_sol / v, "motor_voltage": None,
                   "motor_power": (-i + p_sol / v) * v}
        self.rows[unix] = row
        if unix % self.gps_every_s == 0 and not in_gap:
            lat, lon = p.coord_at_km(self.km)
            self._gps_id += 1
            self.gps.append({
                "id": self._gps_id, "deviceName": self.device,
                "timestamp": self.t.tz_convert("Africa/Johannesburg").isoformat(),
                "_unix": unix,
                "latitude": lat + self.rng.normal(0, 2e-5),
                "longitude": lon + self.rng.normal(0, 2e-5),
                "speedKmh": v_kmh, "accuracyMeters": 1.2})
            if self.extra_device:
                # a second reporter - the chase car, 2 km behind. Both
                # land in the same table, and unfiltered /latest returns
                # whichever wrote last, so this is what picking the wrong
                # device looks like.
                lat2, lon2 = p.coord_at_km(max(self.km - 2.0, 0.0))
                self._gps_id += 1
                self.gps.append({
                    "id": self._gps_id, "deviceName": self.extra_device,
                    "timestamp": self.t.tz_convert("Africa/Johannesburg").isoformat(),
                    "_unix": unix,
                    "latitude": lat2 + self.rng.normal(0, 3e-5),
                    "longitude": lon2 + self.rng.normal(0, 3e-5),
                    "speedKmh": v_kmh, "accuracyMeters": 2.0})

    # --- queries
    def series_range(self, t_from: int, t_to, names: list) -> dict:
        with self.lock:
            keys = sorted(k for k in self.rows
                          if k >= t_from and (t_to is None or k <= t_to))
            pts = []
            for k in keys:
                r = self.rows[k]
                pts.append({"timestamp": pd.Timestamp(k, unit="s", tz="UTC")
                            .strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "values": [r.get(n) for n in names]})
        return {"series": names, "points": pts}

    def gps_latest(self, device):
        with self.lock:
            for g in reversed(self.gps):
                if device is None or g["deviceName"] == device:
                    return {k: v for k, v in g.items() if k != "_unix"}
        return None

    def gps_range(self, t_from: int, t_to, device):
        with self.lock:
            return [{k: v for k, v in g.items() if k != "_unix"}
                    for g in self.gps
                    if g["_unix"] >= t_from and (t_to is None or g["_unix"] <= t_to)
                    and (device is None or g["deviceName"] == device)]


def parse_ts(s: str):
    if not s:
        return None
    t = pd.Timestamp(s)
    t = t.tz_localize("Africa/Johannesburg") if t.tzinfo is None else t
    return int(t.tz_convert("UTC").timestamp())


def make_handler(world: World):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            world.advance()
            u = urllib.parse.urlparse(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            if u.path == "/api/timeseries/range":
                if "from" not in q:
                    return self._json("from is required.", 400)
                names = ([n.strip() for n in q["series"].split(",")]
                         if q.get("series") else SERIES)
                bad = [n for n in names if n not in SERIES]
                if bad:
                    return self._json(f"Unknown series: {bad}", 400)
                self._json(world.series_range(parse_ts(q["from"]),
                                              parse_ts(q.get("to")), names))
            elif u.path == "/api/gps/latest":
                g = world.gps_latest(q.get("deviceName"))
                self._json(g) if g else self._json(None, 404)
            elif u.path == "/api/gps/range":
                if "from" not in q:
                    return self._json("from is required.", 400)
                self._json(world.gps_range(parse_ts(q["from"]),
                                           parse_ts(q.get("to")),
                                           q.get("deviceName")))
            else:
                self._json({"error": "not found"}, 404)
    return H


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", required=True)
    ap.add_argument("--port", type=int, default=5240)
    ap.add_argument("--device", default="honor")
    ap.add_argument("--backlog-min", type=float, default=40.0, dest="backlog_min")
    ap.add_argument("--slow", type=float, default=1.0)
    ap.add_argument("--cloud", type=float, default=1.0)
    ap.add_argument("--extra-device", dest="extra_device", metavar="NAME",
                    help="zweites meldendes Geraet (z. B. das Begleitfahrzeug, "
                         "2 km zurueck) - fuer die Geraeteauswahl")
    ap.add_argument("--gap", nargs=2, type=float, metavar=("FROM_S", "TO_S"),
                    help="Funkloch: Sekunden nach Planstart, gefuellt mit 0.0")
    args = ap.parse_args(argv)

    plan = planfile.Plan.load(args.plan)
    world = World(plan, Battery_coeffs(), int(args.backlog_min * 60),
                  args.slow, args.cloud, tuple(args.gap) if args.gap else None,
                  args.device, extra_device=args.extra_device)
    shift = (world.now() - pd.Timestamp.now("UTC")).total_seconds()
    print(f"mock live_monitoring auf :{args.port}, Plan {plan.label}")
    print(f"Planzeit jetzt {world.now().tz_convert('Africa/Johannesburg'):%H:%M:%S} "
          f"SAST, {len(world.rows)} s Rueckstand aufgezeichnet")
    print(f"--> live_strategy.py --host localhost:{args.port} --device "
          f"{args.device} --time-shift {shift:.0f}")
    httpd = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(world))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
