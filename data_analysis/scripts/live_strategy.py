"""Live view in the chase car: where are we on the plan, and how far off.

    python scripts/live_strategy.py --host 192.168.1.50:5240 --device honor
    python scripts/live_strategy.py --plan plans/tag1_0900_..._3loops.json
    python scripts/live_strategy.py --simulate            # no car needed

Then open  http://localhost:8765  (or the laptop's LAN address from a
tablet in the car). The page refreshes once a second.

What it does, and does not do:

  * polls the live_monitoring app once a second (series + latest GPS fix),
    projects the fix onto the LOADED PLAN, integrates V*I into an energy
    state and compares everything with the plan AT THE SAME KILOMETRE.
  * never re-plans. When the plan no longer fits, run point_strategy.py
    again - it writes its options to plans/ - and pick the new file in the
    page's plan menu. Loading is a deliberate click, not a file watcher: a
    plan that changes under the driver's feet is worse than an old one.
  * on loading a plan mid-day it catches up: GPS history and telemetry
    since the plan's start time are fetched and integrated, so the energy
    comparison starts from the pack state the plan was built on.

--simulate drives the loaded plan at planned pace with the plan's own
energy numbers (plus a bit of noise and optional --sim-slow/--sim-cloud
factors) so the page can be tried without a car. The simulation clock
starts at the plan's start time and runs --sim-speed times faster.
"""

from __future__ import annotations

from   datetime import timedelta
from   http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from   pathlib import Path
import argparse
import json
import logging as lg
import sys
import threading
import time
import urllib.parse

import numpy as np
import pandas as pd

# scripts/ is run directly; make src importable like point_strategy does
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from data_analysis.ser_dataclasses import Battery_coeffs             # noqa: E402
from data_analysis.simulation.battery import terminal_voltage       # noqa: E402
from data_analysis.simulation.live_monitor import TelemetrySigns    # noqa: E402
from data_analysis.strategy import live, planfile                   # noqa: E402
from data_analysis.telemetry import ser_client                      # noqa: E402

log = lg.getLogger("live_strategy")
PAGE = Path(__file__).with_name("live_strategy_page.html")

# How far back before a plan's start the standstill counters are seeded.
# Long enough to cover a control stop that began before the plan was
# computed (35 min) with room to spare, short enough to stay one small
# request.
PRE_ROLL_MIN = 60.0


# ------------------------------------------------------------- simulator ----

class SimSource:
    """Fake telemetry that drives the loaded plan.

    Rows come from the plan itself: speed = target speed, pack current from
    the planned net power, voltage from the battery model, MPPT power from
    the planned solar split over three channels. `slow` scales the speed
    (0.9 = 10 % slower than planned), `cloud` the sun (0.7 = 30 % less),
    so the deviation display has something to show.
    """

    def __init__(self, batt, speed: float = 10.0, slow: float = 1.0,
                 cloud: float = 1.0, gps_every_s: int = 3, start_km: float = 0.0):
        self.batt = batt
        self.speed = speed
        self.slow = slow
        self.cloud = cloud
        self.gps_every_s = gps_every_s
        self.plan = None
        self.km = start_km
        self.t = None
        self._wall = None
        self._gps_id = 0
        self._rng = np.random.default_rng(7)
        self._last_gps = None
        self.n_polls = 0
        self.n_errors = 0
        self.last_error = None
        self.last_seen = None

    def attach(self, plan: planfile.Plan) -> None:
        self.plan = plan
        self.t = plan.t_start
        self._wall = time.time()
        self.km = min(self.km, plan.total_km)

    def now(self) -> pd.Timestamp:
        if self.t is None:
            return pd.Timestamp.now("UTC")
        return self.t

    def set_speed(self, speed: float) -> float:
        """Change the time lapse without a jump in simulated time.

        The clock is `_wall` plus elapsed x speed, so the anchor has to be
        moved to now before the factor changes - otherwise the seconds
        already waited get recounted at the new rate and the car teleports.
        0 pauses.
        """
        self._wall = time.time()
        self.speed = max(0.0, float(speed))
        return self.speed

    def poll(self):
        self.n_polls += 1
        if self.plan is None:
            return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC")), None
        wall = time.time()
        n = int((wall - self._wall) * self.speed)
        if n <= 0:
            return pd.DataFrame(index=pd.DatetimeIndex([], tz="UTC")), self._last_gps
        n = min(n, 600)
        # keep the remainder: at 0.5x a poll every second would otherwise
        # always truncate to zero and the clock would never move
        self._wall += n / self.speed if self.speed > 0 else 0.0
        rows = []
        p = self.plan
        for _ in range(n):
            self.t = self.t + pd.Timedelta(seconds=1)
            # standing where the plan stands
            t_plan_here = p.time_at_km(self.km)          # departure time
            standing = self.t < t_plan_here and self.km > 0.01
            here = p.speed_at_km(self.km)
            v_kmh = 0.0 if standing else here["v_soll"] * self.slow
            self.km = min(self.km + v_kmh / 3600.0, p.total_km)
            wh = p.wh_at_km(self.km)["wh_remaining"]
            p_sol = here["p_solar_plan"] * self.cloud
            p_net = (here["p_net_plan"] + here["p_solar_plan"] - p_sol
                     if not standing else -(p_sol - 60.0))
            if standing:
                # tracked panel charges a bit more than the flat estimate
                p_sol *= 1.3
                p_net = -(p_sol - 60.0)
            i = p_net / 115.0 + self._rng.normal(0, 0.3)
            v = terminal_voltage(self.batt, wh, i) + self._rng.normal(0, 0.05)
            rows.append({"time": self.t, "speed": float(round(v_kmh)),
                         "mppt1_power": p_sol / 3, "mppt2_power": p_sol / 3,
                         "mppt3_power": p_sol / 3, "mppt4_power": 0.0,
                         "battery_voltage": v, "battery_current": -i,
                         "gap": False})
            if int(self.t.timestamp()) % self.gps_every_s == 0:
                lat, lon = p.coord_at_km(self.km)
                self._gps_id += 1
                self._last_gps = {"id": self._gps_id, "time": self.t,
                                  "lat": lat + self._rng.normal(0, 2e-5),
                                  "lon": lon + self._rng.normal(0, 2e-5),
                                  "speed_kmh": v_kmh, "new": True,
                                  "device": "sim"}
        df = pd.DataFrame(rows).set_index("time")
        self.last_seen = df.index[-1]
        return df, self._last_gps

    @property
    def age_s(self):
        return 0.0


# ------------------------------------------------------------- the state ----

class LiveServer:
    def __init__(self, args):
        self.args = args
        self.batt = Battery_coeffs()
        self.signs = TelemetrySigns(
            i_batt_discharge_positive=args.discharge_positive)
        self.plans_dir = Path(args.plans_dir) if args.plans_dir \
            else planfile.default_plans_dir()
        self.lock = threading.Lock()
        self.plan = None
        self.tracker = None
        self.status = {"plan": None, "message": "kein Plan geladen",
                       "telemetry": {}}
        self.messages = []
        self.sim = args.simulate
        self.device = args.device
        if self.sim:
            self.source = SimSource(self.batt, speed=args.sim_speed,
                                    slow=args.sim_slow, cloud=args.sim_cloud,
                                    start_km=args.sim_start_km)
        else:
            self.source = ser_client.TelemetryPoller(
                host=args.host, device=self.device, timeout=args.timeout)
        self._stop = threading.Event()
        self.backfilling = False
        # test/replay only: the app's clock against the plan's clock
        self.time_shift = pd.Timedelta(seconds=args.time_shift or 0)

    def now(self) -> pd.Timestamp:
        if self.sim:
            return self.source.now()
        return pd.Timestamp.now("UTC") + self.time_shift

    def say(self, msg: str) -> None:
        log.info(msg)
        self.messages.append(f"{self.now().tz_convert(live.RACE_TZ):%H:%M:%S} {msg}")
        self.messages = self.messages[-20:]

    # --- gps device ----------------------------------------------------
    def devices(self, minutes: float = 30.0) -> dict:
        """Which GPS devices are reporting, and which one is selected."""
        if self.sim:
            return {"current": "sim", "devices": [
                {"device": "sim", "n": 1, "age_s": 0.0,
                 "last_time": None, "lat": None, "lon": None,
                 "speed_kmh": None}], "note": "Simulation"}
        try:
            found = ser_client.list_gps_devices(self.args.host, minutes,
                                                self.args.timeout,
                                                now=self.now())
            note = ""
        except Exception as e:
            found, note = [], f"{type(e).__name__}: {str(e)[:70]}"
        return {"current": self.device, "devices": found, "note": note,
                "window_min": minutes}

    def set_device(self, name) -> str:
        """Switch the GPS device the position is taken from.

        The new device's first fix is projected in a window around the
        current km rather than globally - on a loop day a global search
        would land on whichever pass happens to be nearest. If that window
        does not fit, the search widens by itself (see update_gps).
        """
        name = (name or "").strip() or None
        with self.lock:
            self.device = name
            if not self.sim:
                self.source.device = name
                self.source.last_gps_id = None
            if self.tracker is not None:
                self.tracker.release_position()
        self.say(f"GPS-Geraet: {name or 'alle Geraete'}")
        return name or ""

    # --- plan handling -------------------------------------------------
    def list_plans(self) -> list:
        out = planfile.list_plans(self.plans_dir)
        for d in out:
            d["active"] = bool(self.plan is not None and self.plan.path
                               and Path(d["path"]) == self.plan.path)
        return out

    def load_plan(self, path) -> str:
        path = Path(path)
        if not path.is_absolute():
            path = self.plans_dir / path
        plan = planfile.Plan.load(path)
        tracker = live.LiveTracker(plan, self.batt, signs=self.signs)
        with self.lock:
            old = self.tracker
            if old is not None and old.driver_log:
                # a confirmed driver change is a fact about the crew, not
                # about the plan, so it survives a plan change. The km does
                # NOT: every plan counts km from its own start point, and
                # carrying the number over would quietly mean a different
                # place. Only the times come along.
                tracker.adopt_driver_log(old.driver_log)
            self.plan, self.tracker = plan, tracker
            if self.sim:
                self.source.attach(plan)
        self.say(f"Plan geladen: {plan.label}")
        if not self.sim:
            # The loop must not feed live rows before the history is in:
            # LiveEnergy integrates in time order and rejects anything
            # older than its last sample, so a single live second ahead of
            # the backfill would silently discard the whole backfill.
            self.backfilling = True
            threading.Thread(target=self._backfill, args=(tracker,),
                             daemon=True).start()
        return plan.label

    def _backfill(self, tracker: live.LiveTracker) -> None:
        """Catch up from the plan's start to now, with km from GPS history."""
        plan = tracker.plan
        t0 = plan.t_start
        now = self.now()
        try:
            if t0 > now:
                self.say(f"Plan beginnt erst {t0.tz_convert(live.RACE_TZ):%H:%M}"
                         f" - kein Nachladen")
                with self.lock:
                    self.source.cursor = None
                    self.source.last_seen = None
                return
            try:
                gps = ser_client.fetch_gps_range(self.args.host, t0, now,
                                                 device=self.device,
                                                 timeout=30)
                df = self.source.backfill(t0, now)
            except Exception as e:
                self.say(f"Nachladen fehlgeschlagen ({type(e).__name__}: "
                         f"{str(e)[:60]}) - Integration startet jetzt")
                with self.lock:
                    self.source.cursor = None
                    self.source.last_seen = None
                return
            # Speed only, from BEFORE the plan starts: a plan computed at
            # the control stop (`--time now`, the normal case) begins while
            # the car already stands, and without this the halt looks as if
            # it started when the plan was loaded - measured 9 min late,
            # and "weiter ab" moves with it. Not integrated, only counted:
            # the energy has to start at the plan's own start value.
            pre = pd.DataFrame()
            try:
                pre = ser_client.fetch_range(
                    self.args.host, t0 - pd.Timedelta(minutes=PRE_ROLL_MIN),
                    t0, series=["speed", "battery_voltage"], timeout=30)
            except Exception as e:
                log.info("Vorlauf nicht abrufbar: %s", e)
            km_series = (live.km_series_from_gps(plan, gps, df.index)
                         if len(df) and len(gps) else None)
            with self.lock:
                if self.tracker is not tracker:
                    return                      # another plan was loaded
                stood = tracker.seed_standstill(pre)
                n = tracker.ingest(df, km_series)
                if len(gps):
                    last = gps.iloc[-1]
                    fix = {"id": last.get("id"), "time": gps.index[-1],
                           "lat": last["lat"], "lon": last["lon"],
                           "speed_kmh": last.get("speed_kmh")}
                    # hand over WHICH pass of the loop the history ended on
                    km_last = (float(km_series.dropna().iloc[-1])
                               if km_series is not None
                               and km_series.notna().any() else None)
                    if km_last is not None:
                        tracker.seed_position(km_last, fix)
                    else:
                        tracker.update_gps(fix)
            self.say(f"Nachgeladen: {len(df)} s Telemetrie, {len(gps)} GPS-Fixe "
                     f"seit {t0.tz_convert(live.RACE_TZ):%H:%M}, {n} Samples "
                     f"integriert"
                     + (", km aus GPS-Historie" if km_series is not None
                        else ", ohne km-Zuordnung"))
            if stood and tracker._still_since is not None:
                self.say(f"Auto stand schon vor dem Planstart, seit "
                         f"{live._to_local(tracker._still_since)} "
                         f"({len(pre)} s Vorlauf)")
        finally:
            self.backfilling = False

    # --- loop ----------------------------------------------------------
    def run(self) -> None:
        interval = self.args.interval
        while not self._stop.is_set():
            t_loop = time.time()
            try:
                if self.backfilling:
                    new, gps = None, None
                else:
                    new, gps = (self.source.poll() if self.sim
                                else self.source.poll(now=self.now()))
                with self.lock:
                    tracker = self.tracker
                    if tracker is not None and self.backfilling:
                        st = tracker.status(now=self.now())
                        st["message"] = "Historie wird nachgeladen ..."
                    elif tracker is not None:
                        if gps is not None:
                            tracker.update_gps(gps)
                        tracker.ingest(new)
                        st = tracker.status(now=self.now())
                    else:
                        st = {"plan": None, "message": "kein Plan geladen",
                              "now": live._to_local(self.now()),
                              "telemetry": {"rows": 0}}
                    st["source"] = {
                        "kind": "simulation" if self.sim else "live_monitoring",
                        "host": None if self.sim else self.args.host,
                        "device": None if self.sim else self.device,
                        "polls": self.source.n_polls,
                        "errors": self.source.n_errors,
                        "last_error": self.source.last_error,
                        "series_age_s": (
                            None if getattr(self.source, "last_seen", None) is None
                            else round((self.now() - self.source.last_seen)
                                       .total_seconds())),
                        "sim_speed": (self.source.speed if self.sim else None),
                        "sim_paused": bool(self.sim and self.source.speed == 0),
                    }
                    st["messages"] = list(reversed(self.messages[-8:]))
                    self.status = st
            except Exception as e:                      # keep the loop alive
                log.exception("Tick fehlgeschlagen")
                self.say(f"Fehler im Tick: {type(e).__name__}: {str(e)[:80]}")
            dt = time.time() - t_loop
            self._stop.wait(max(0.05, interval - dt))

    def stop(self):
        self._stop.set()


# ------------------------------------------------------------------- http ----

def _hhmm_on(raw, now) -> pd.Timestamp:
    """'HH:MM' typed in the car -> a timestamp on the day the session is in.

    Deliberately built from `now` rather than parsed as a date: `now` is
    the session's clock, which during a replay is the race day and not the
    laptop's calendar. Empty input means "now" and returns None.
    """
    raw = (str(raw or "")).strip()
    if not raw:
        return None
    parts = raw.replace(".", ":").split(":")
    try:
        hh, mm = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        raise ValueError(f"Uhrzeit {raw!r} nicht lesbar - HH:MM erwartet")
    if not (0 <= hh < 24 and 0 <= mm < 60):
        raise ValueError(f"Uhrzeit {raw!r} liegt ausserhalb 00:00-23:59")
    local = pd.Timestamp(now).tz_convert(live.RACE_TZ)
    return local.replace(hour=hh, minute=mm, second=0, microsecond=0)

def make_handler(server: LiveServer):
    page_html = PAGE.read_text(encoding="utf-8")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):       # quiet
            if self.path.startswith("/api/status"):
                return
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path in ("/", "/index.html"):
                html = (PAGE.read_text(encoding="utf-8") if server.args.dev
                        else page_html)
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/api/status":
                with server.lock:
                    st = server.status
                self._json(st)
            elif path == "/api/plans":
                self._json({"plans_dir": str(server.plans_dir),
                            "plans": server.list_plans()})
            elif path == "/api/devices":
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                minutes = float(q.get("minutes", ["30"])[0])
                self._json(server.devices(minutes))
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            path = urllib.parse.urlparse(self.path).path
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return self._json({"error": "bad json"}, 400)
            try:
                if path == "/api/plan":
                    label = server.load_plan(body["file"])
                    self._json({"ok": True, "label": label})
                elif path == "/api/position":
                    with server.lock:
                        if server.tracker is None:
                            raise ValueError("kein Plan geladen")
                        server.tracker.set_position_km(float(body["km"]))
                    server.say(f"Position von Hand: km {float(body['km']):.1f}")
                    self._json({"ok": True})
                elif path == "/api/position/gps":
                    with server.lock:
                        if server.tracker is None:
                            raise ValueError("kein Plan geladen")
                        server.tracker.release_position()
                    server.say("Position wieder aus GPS")
                    self._json({"ok": True})
                elif path == "/api/device":
                    self._json({"ok": True,
                                "device": server.set_device(body.get("device"))})
                elif path == "/api/driver-change":
                    with server.lock:
                        if server.tracker is None:
                            raise ValueError("kein Plan geladen")
                        e = server.tracker.log_driver_change(
                            server.now(), at=_hhmm_on(body.get("at"),
                                                      server.now()),
                            note=str(body.get("note") or "")[:60])
                    server.say(f"Fahrerwechsel bestaetigt "
                               f"({live._to_local(e['time'])})")
                    self._json({"ok": True, "time": live._to_local(e["time"]),
                                "km": e["km"]})
                elif path == "/api/stop-confirm":
                    with server.lock:
                        if server.tracker is None:
                            raise ValueError("kein Plan geladen")
                        e = server.tracker.log_stop(
                            server.now(), at=_hhmm_on(body.get("at"),
                                                      server.now()),
                            note=str(body.get("note") or "")[:60])
                    server.say(f"Stopp bestaetigt: {e['stop'] or 'ohne Halt'} "
                               f"angekommen {live._to_local(e['time'])}")
                    self._json({"ok": True, "time": live._to_local(e["time"]),
                                "stop": e["stop"]})
                elif path == "/api/sim":
                    if not server.sim:
                        raise ValueError("laeuft nicht in der Simulation")
                    with server.lock:
                        sp = server.source.set_speed(body.get("speed", 1.0))
                    server.say(f"Simulation {'pausiert' if sp == 0 else f'{sp:g}x'}")
                    self._json({"ok": True, "speed": sp})
                elif path == "/api/anchor":
                    with server.lock:
                        if server.tracker is None:
                            raise ValueError("kein Plan geladen")
                        res = server.tracker.anchor()
                    server.say("Anker gesetzt" if res.get("applied")
                               else f"Anker verweigert: {res.get('reason', 'unter Last')}")
                    self._json({"ok": bool(res.get("applied")),
                                "result": {k: (float(v) if isinstance(v, (int, float, np.floating)) else v)
                                           for k, v in res.items()}})
                else:
                    self._json({"error": "not found"}, 404)
            except Exception as e:
                log.exception("POST %s", path)
                self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 400)

    return Handler


# -------------------------------------------------------------------- main ----

def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default=ser_client.DEFAULT_HOST,
                   help=f"live_monitoring host:port (Default {ser_client.DEFAULT_HOST})")
    p.add_argument("--device", default=ser_client.DEFAULT_DEVICE,
                   help="GPS-Geraetename des Autos in der App "
                        f"(Default {ser_client.DEFAULT_DEVICE!r})")
    p.add_argument("--plan", help="Plandatei beim Start laden")
    p.add_argument("--plans-dir", dest="plans_dir",
                   help="Ordner der Plandateien (Default: data_analysis/plans "
                        "oder $SSC_PLANS_DIR)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--bind", default="0.0.0.0",
                   help="Adresse des Webservers (0.0.0.0 = im ganzen LAN)")
    p.add_argument("--interval", type=float, default=1.0,
                   help="Abfrageintervall in Sekunden")
    p.add_argument("--timeout", type=float, default=8.0)
    p.add_argument("--discharge-positive", action="store_true",
                   dest="discharge_positive",
                   help="battery_current ist beim Entladen positiv (Default: "
                        "negativ, wie SER-5)")
    p.add_argument("--simulate", action="store_true",
                   help="ohne Auto: den geladenen Plan abfahren")
    p.add_argument("--sim-speed", type=float, default=10.0, dest="sim_speed",
                   help="Zeitraffer der Simulation (Default 10x)")
    p.add_argument("--sim-slow", type=float, default=1.0, dest="sim_slow",
                   help="Faktor auf die Geschwindigkeit (0.9 = langsamer)")
    p.add_argument("--sim-cloud", type=float, default=1.0, dest="sim_cloud",
                   help="Faktor auf die Sonne (0.7 = 30 %% weniger)")
    p.add_argument("--sim-start-km", type=float, default=0.0, dest="sim_start_km")
    p.add_argument("--time-shift", type=float, default=0.0, dest="time_shift",
                   metavar="S",
                   help="nur fuer Tests/Wiedergabe: eigene Uhr um S Sekunden "
                        "verschieben, damit ein aufgezeichneter Tag gegen "
                        "seinen Plan laeuft (mock_live_monitoring.py)")
    p.add_argument("--dev", action="store_true",
                   help="HTML-Seite bei jedem Aufruf neu von Platte lesen")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    lg.basicConfig(level=lg.INFO if args.verbose else lg.WARNING,
                   format="%(asctime)s %(levelname)-7s %(message)s")
    lg.getLogger("live_strategy").setLevel(lg.INFO)
    # LiveEnergy warns once about the unverified topology - that is
    # deliberate and belongs in the log, not on every page load
    srv = LiveServer(args)

    if args.plan:
        srv.load_plan(args.plan)
    elif args.simulate:
        plans = srv.list_plans()
        if plans:
            srv.load_plan(plans[0]["path"])
        else:
            print("keine Plandatei in", srv.plans_dir, "- zuerst "
                  "point_strategy.py laufen lassen")
            return 1

    handler = make_handler(srv)
    httpd = ThreadingHTTPServer((args.bind, args.port), handler)
    httpd.daemon_threads = True
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    print(f"live_strategy: http://localhost:{args.port}  "
          f"({'SIMULATION' if args.simulate else args.host}), Plaene aus "
          f"{srv.plans_dir}")
    print("Beenden mit Strg+C")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
