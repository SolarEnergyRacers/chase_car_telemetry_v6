"""Strategy from one point: what can still be driven from here, today.

    python scripts/point_strategy.py                    # asks everything
    python scripts/point_strategy.py --day 1 --part to_control --km 84.2 \
        --soc 62 --time 12:40                           # nothing asked
    python scripts/point_strategy.py --plan 2            # plan mode, 2 loops
    python scripts/point_strategy.py --plan 2 --stop 45:20   # +20 min at km 45

Three inputs can each come from a measurement or from the keyboard, and the
script asks per input rather than per mode - at a control stop the pack may
be readable while the GPS is not, or the other way round.

    position   GPS from the app, or part + km by hand
    pack       telemetry from the app, or SOC/Wh by hand
    time       now, or a given time

Weather comes from the cache and is never fetched here. A fresh forecast is
a deliberate act (scripts/cache_weather.py) so that the same inputs always
give the same numbers - two people at one control stop must not get two
different answers, and the evening review has to be able to reproduce the
day's plan.
"""

from   datetime import date, datetime, timedelta
import argparse
import logging as lg
import sys

import numpy as np

from data_analysis.simulation.battery import capacity_wh
from data_analysis.ser_dataclasses import Battery_coeffs, Car_coeffs
from data_analysis.strategy import dayplan, inputs, report
from data_analysis.strategy.inputs import RACE_TZ

log = lg.getLogger("point_strategy")

DEFAULT_HOST = "localhost:5240"
PART_CHOICES = ("to_control", "loop", "to_finish")

# Which geojson stem belongs to which part, per day. Mirrors
# scripts/cache_weather.py: day 1 is the odd one out because route1..3 are
# intermediate stages of the manual route.
def route_stems(day: int) -> dict:
    if day == 1:
        return {"to_control": "manual_day1", "loop": "day1_loop",
                "to_finish": "day1_route4"}
    return {"to_control": f"day{day}_route1", "loop": f"day{day}_loop",
            "to_finish": f"day{day}_route2"}


# ------------------------------------------------------------------- input ----

# Warnings that are true but say nothing actionable on every single run.
# The climb/descent one names a known, quantified gap (15-45 Wh per stage,
# measured against the Valhalla edge elevations); repeating it before every
# plan only trains people to skip the header.
_MUTED = ("route has no climb/descent columns",)


class _MuteFilter(lg.Filter):
    """Drop known, already-decided warnings."""

    def filter(self, record):
        msg = record.getMessage()
        return not any(m in msg for m in _MUTED)


class _OnceFilter(lg.Filter):
    """Let each distinct log message through once.

    total_Ws_for_lap() warns per call, and one options table calls it a few
    dozen times. Ten identical warnings do not carry ten times the
    information - they bury the one line that matters.
    """

    def __init__(self):
        super().__init__()
        self._seen = set()

    def filter(self, record):
        key = (record.name, record.levelno, record.getMessage()[:120])
        if key in self._seen:
            return False
        self._seen.add(key)
        return True


def ask(prompt: str, choices: dict, default: str = None) -> str:
    """Small numbered menu. Keeps the interactive path honest: every option
    is spelled out, so nobody has to remember flag names in a hurry."""
    keys = list(choices)
    print(f"\n{prompt}")
    for i, k in enumerate(keys, 1):
        mark = " (Enter)" if k == default else ""
        print(f"  {i}) {choices[k]}{mark}")
    while True:
        raw = input("> ").strip()
        if not raw and default:
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(keys):
            return keys[int(raw) - 1]
        if raw in keys:
            return raw
        print(f"bitte 1..{len(keys)}")


def ask_float(prompt: str, lo: float = None, hi: float = None) -> float:
    while True:
        raw = input(f"{prompt} > ").strip().replace(",", ".")
        try:
            v = float(raw)
        except ValueError:
            print("bitte eine Zahl")
            continue
        if lo is not None and v < lo or hi is not None and v > hi:
            print(f"bitte zwischen {lo} und {hi}")
            continue
        return v


def resolve_day(args, rc) -> int:
    if args.day:
        return args.day
    today = date.today()
    for k in sorted(rc.config["days"], key=int):
        if rc.config["days"][k]["date"] == today.isoformat():
            return int(k)
    log.warning("%s is not a race day - assuming day 1. Use --day to "
                "override.", today)
    return 1


def resolve_position(args, rc, day: int, routes: dict, state_notes: list):
    """Which part, and how far into it.

    On a loop the projection is ambiguous by construction: an out-and-back
    route passes every place twice. project_onto_route() can resolve that
    with `after` or `heading`, but a wrong guess shifts the mandatory
    remaining distance and with it the whole plan - so the leg is asked, not
    inferred, and the consequence is echoed back for checking.
    """
    part = args.part
    if part is None:
        part = ask("Wo befindet sich das Auto?", {
            "to_control": "noch vor dem Kontrollstopp",
            "loop":       "auf einem Loop",
            "to_finish":  "nach dem Kontrollstopp, auf dem Weg zum Ziel",
        }, default="to_control")
    if part not in routes:
        raise SystemExit(f"Tag {day} hat keinen Streckenteil "
                         f"'{part}' (vorhanden: {sorted(routes)})")

    route = routes[part]
    part_km = float(route.index[-1]) / 1e3

    loop_leg, loop_done = None, args.loops_done
    if part == "loop":
        if args.leg:
            loop_leg = args.leg
        else:
            loop_leg = ask("Hin- oder Rueckweg des laufenden Loops?",
                           {"out": "Hinweg", "back": "Rueckweg"})
        if loop_done is None:
            loop_done = int(ask_float("Wie viele Loops sind fertig", 0, 20))

    # --- how far in
    source, cross = "Handeingabe", None
    if args.km is not None:
        km = args.km
    elif args.at:
        lat, lon = (float(x) for x in args.at.split(","))
        km, cross = _project(route, part, loop_leg, lat, lon)
        source = f"Handeingabe {lat:.5f},{lon:.5f}"
    else:
        how = ask("Position bestimmen?", {
            "gps":  f"GPS von der App holen ({args.host})",
            "km":   "Kilometer im Streckenteil von Hand",
            "coord": "Koordinaten von Hand",
        }, default="km")
        if how == "gps":
            fix = inputs.fetch_gps(args.host)
            km, cross = _project(route, part, loop_leg, fix["lat"], fix["lon"])
            source = f"GPS {fix['lat']:.5f},{fix['lon']:.5f}"
            if fix.get("speed_kmh") is not None:
                source += f", {fix['speed_kmh']:.0f} km/h"
        elif how == "coord":
            lat = ask_float("Breite (Grad)", -90, 90)
            lon = ask_float("Laenge (Grad)", -180, 180)
            km, cross = _project(route, part, loop_leg, lat, lon)
            source = f"Handeingabe {lat:.5f},{lon:.5f}"
        else:
            km = ask_float(f"km im Teil '{part}' (0..{part_km:.1f})",
                           0, part_km)

    if cross is not None and cross > 500:
        state_notes.append(
            f"Position liegt {cross:.0f} m neben der Route - falscher "
            f"Streckenteil oder falscher Loop-Durchgang?")
    if km >= part_km - 0.05:
        raise SystemExit(
            f"km {km:.2f} liegt am Ende von '{part}' ({part_km:.2f} km) - "
            f"wahrscheinlich ist der naechste Streckenteil gemeint")

    return part, km, part_km, loop_leg, (loop_done or 0), source, cross


def _project(route, part: str, loop_leg: str, lat: float, lon: float):
    """Project a coordinate onto a part, resolving the loop ambiguity."""
    rc_mod = inputs.load_raceconfig()
    from compile_route import project_onto_route          # noqa: E402

    after = None
    if part == "loop" and loop_leg == "back":
        # the return leg is the second half of the stitched out-and-back
        after = float(route.index[-1]) / 2.0
    d, cross = project_onto_route(route, lat, lon, after=after)
    return d / 1e3, cross


def resolve_pack(args, batt) -> inputs.PackState:
    if args.soc is not None:
        return inputs.pack_state_manual(batt, soc_percent=args.soc)
    if args.wh is not None:
        return inputs.pack_state_manual(batt, wh=args.wh)
    if args.full:
        return inputs.pack_state_full(batt)

    how = ask("Batteriezustand?", {
        "api":  f"aus der Telemetrie holen ({args.host})",
        "soc":  "SOC in Prozent von Hand",
        "wh":   "verbleibende Wh von Hand",
        "full": "Pack voll (Tag-1-Start)",
    }, default="api")
    if how == "api":
        reading = inputs.fetch_pack_reading(args.host)
        settled = ask("Stand der Pack in Ruhe (Motor aus, kein Laden)?",
                      {"no": "nein - Wert nur als Plausibilitaetspruefung",
                       "yes": "ja, mehrere Minuten Ruhe - als Anker nutzen"},
                      default="no") == "yes"
        return inputs.pack_state_from_reading(batt, reading, settled=settled)
    if how == "soc":
        return inputs.pack_state_manual(
            batt, soc_percent=ask_float("SOC in Prozent", 0, 100))
    if how == "wh":
        return inputs.pack_state_manual(batt, wh=ask_float("Wh", 0, 4000))
    return inputs.pack_state_full(batt)


def resolve_start_time(args, day_date: date) -> datetime:
    spec = args.time
    if spec is None:
        spec = {"now": "now", "manual": None}[ask(
            "Startzeitpunkt der Rechnung?",
            {"now": "jetzt", "manual": "Uhrzeit eingeben"}, default="now")]
        if spec is None:
            raw = input("Uhrzeit HH:MM (SAST) > ").strip()
            spec = raw
    t = inputs.resolve_time(spec)
    if spec not in (None, "now", "jetzt") and len(str(spec)) <= 5:
        t = inputs.attach_date(t, day_date)
    return t


# ------------------------------------------------------------------- loader ----

def load_routes(day: int):
    """Compiled routes of a day - no weather, no network, no cache."""
    rc = inputs.load_raceconfig()
    routes = {}
    for which in ("to_control", "to_finish"):
        try:
            routes[which] = rc.stage(day, which)
        except (KeyError, FileNotFoundError) as e:
            log.info("Tag %d ohne '%s': %s", day, which, e)

    # road_class and the roundabout positions come from the cached Valhalla
    # answers, not from a compiled column - see attach_roadinfo(). The parts
    # have to be listed in the order raceconfig stitches them, otherwise the
    # mapping is shifted.
    route_dir = inputs.find_route_dir()

    def geos_for(which):
        spec = rc.config["days"][str(day)]["stages"].get(which)
        if not spec:
            return []
        out = []
        for n in spec["routes"]:
            g = route_dir / n.replace(".geojson.pkl", ".geojson")
            if not g.is_file():
                return []
            out.append(g)
        return out

    for which in list(routes):
        g = geos_for(which)
        if g:
            routes[which] = inputs.attach_roadinfo(routes[which], g)

    if rc.loops_pending(day):
        log.warning("Tag %d: Loop noch nicht veroeffentlicht - 0 Loops ist "
                    "hier keine Aussage ueber den Tag", day)
    else:
        try:
            day_loops = rc.loops(day)
        except FileNotFoundError as e:
            log.warning("Tag %d: Loop-Route fehlt: %s", day, e)
            day_loops = {}
        if day_loops:
            name = next(iter(day_loops))
            loop_fp = route_dir / f"{route_stems(day)['loop']}.geojson"
            routes["loop"] = (
                inputs.attach_roadinfo(day_loops[name], loop_fp,
                                       mirror_tail=True)
                if loop_fp.is_file() else day_loops[name])
            if len(day_loops) > 1:
                log.info("Tag %d hat %d Loop-Varianten, benutzt wird %r",
                         day, len(day_loops), name)
    return rc, routes


def parts_needed(part: str) -> tuple:
    """Which route parts still lie ahead, given where the car is.

    Loading weather for a stage already driven would be wasted work - and
    worse, under cache-only it turns a perfectly answerable question into a
    cache miss. Standing at km 20 of the run to the finish must not fail
    because nobody cached this morning's stage.
    """
    return {
        "to_control": ("to_control", "loop", "to_finish"),
        "loop":       ("loop", "to_finish"),
        "to_finish":  ("to_finish",),
    }[part]


def overnight_point(routes: dict):
    """Where the car spends the night: the last node of the day's route.

    Same place as the NEXT day's first route point, but known a day
    earlier - which is the whole reason to take it from here. On a blind
    stage the next day's route does not exist yet, and deriving the
    overnight stop from it made the morning window disappear exactly on
    the days where it is hardest to plan.
    """
    for which in ("to_finish", "to_control"):
        if which in routes:
            last = routes[which].iloc[-1]
            return float(last["latitude"]), float(last["longitude"])
    return None


def load_morning(day: int, rc, routes: dict, spacing_km: float, car, batt,
                 wh_end):
    """The next morning's charge window at the overnight stop.

    Location from TODAY's route end; weather for TOMORROW's date at that
    point. Falls back to the next day's first route point where the point
    entry has not been cached, so older caches keep working.

    Returns (MorningCharge | None, note). Never raises: no morning window
    is "cannot say", not "the plan is wrong". But it says WHY - a window
    that vanishes without a word is the same failure mode as a weather
    cache that silently refuses to refresh.
    """
    from data_analysis.strategy import dayplan

    nxt = day + 1
    if str(nxt) not in rc.config["days"]:
        return None, f"Tag {day} ist der letzte Renntag"
    nxt_date = date.fromisoformat(rc.config["days"][str(nxt)]["date"])
    t_start_next, _ = rc.day_window(nxt)
    release = t_start_next.replace(hour=6, minute=0, second=0, microsecond=0)
    if release >= t_start_next:
        return None, (f"Tag {nxt} startet um "
                      f"{t_start_next.strftime('%H:%M')}, kein Fenster davor")

    here = overnight_point(routes)
    weather, why = None, ""
    if here is not None:
        try:
            weather = inputs.load_point_weather(here[0], here[1],
                                                nxt_date).weather
        except SystemExit:
            why = (f"Nachtquartier {here[0]:.4f},{here[1]:.4f} fuer "
                   f"{nxt_date} nicht im Cache "
                   f"(python scripts/cache_weather.py {day})")
        except Exception as e:
            # Anything else - a stale environment.py without
            # fetch_weather_at_point, a corrupt cache entry - must not take
            # the plan down with it. The morning window is extra
            # information; without it the day is still planned.
            why = (f"Nachtquartier nicht auswertbar "
                   f"({type(e).__name__}: {str(e)[:70]})")

    if weather is None:
        # older caches: take the next day's first route point instead
        try:
            _, nxt_routes = load_routes(nxt)
            if "to_control" not in nxt_routes:
                return None, (why or f"Tag {nxt}: Route noch nicht "
                                     f"veroeffentlicht")
            w = load_weathers(nxt, nxt_date, ("to_control",), spacing_km)
            if "to_control" not in w:
                return None, why or f"Tag {nxt}: kein Wetter im Cache"
            weather = w["to_control"].weather
            first = nxt_routes["to_control"].iloc[0]
            here = (float(first["latitude"]), float(first["longitude"]))
        except SystemExit as e:
            return None, why or str(e).splitlines()[0]
        except Exception as e:
            return None, why or f"{type(e).__name__}: {str(e)[:70]}"

    try:
        return dayplan.morning_charge(weather, here[0], here[1], release,
                                      t_start_next, car, batt, wh_end), why
    except Exception as e:
        return None, f"nicht berechenbar ({type(e).__name__}: {str(e)[:70]})"


def load_weathers(day: int, day_date, which: tuple, spacing_km: float):
    """Cached weather for the named route parts. Cache-only, see inputs."""
    route_dir = inputs.find_route_dir()
    stems = route_stems(day)
    out = {}
    for w in which:
        fp = route_dir / f"{stems[w]}.geojson"
        if not fp.is_file():
            log.info("keine Routendatei %s - Teil '%s' wird uebersprungen",
                     fp.name, w)
            continue
        out[w] = inputs.load_weather(fp, day_date, spacing_km=spacing_km,
                                     hint=f"--day {day}")
    return out


# --------------------------------------------------------------------- main ----

def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--day", type=int, help="Renntag 1..8 (Default: heute)")
    p.add_argument("--host", default=DEFAULT_HOST,
                   help=f"live_monitoring host:port (Default {DEFAULT_HOST})")

    g = p.add_argument_group("Position")
    g.add_argument("--part", choices=PART_CHOICES)
    g.add_argument("--km", type=float, help="km im Streckenteil")
    g.add_argument("--at", help="'lat,lon' statt km")
    g.add_argument("--leg", choices=("out", "back"),
                   help="Hin- oder Rueckweg, nur fuer --part loop")
    g.add_argument("--loops-done", type=int, dest="loops_done",
                   help="bereits gefahrene Loops")

    g = p.add_argument_group("Batterie")
    g.add_argument("--soc", type=float, help="SOC in Prozent")
    g.add_argument("--wh", type=float, help="verbleibende Wh")
    g.add_argument("--full", action="store_true", help="Pack voll")

    g = p.add_argument_group("Zeit")
    g.add_argument("--time", help="'now', 'HH:MM' oder ISO-Zeitstempel")

    g = p.add_argument_group("Modus")
    g.add_argument("--plan", type=int, metavar="N",
                   help="Fahrplan fuer N verbleibende Loops statt der "
                        "Optionstabelle")
    g.add_argument("--stop", action="append", default=[], metavar="KM:MIN",
                   help="Standladen: km ab hier und Dauer in Minuten, "
                        "mehrfach erlaubt. Negative km zaehlen vom Ziel "
                        "zurueck, dann mit Gleichheitszeichen schreiben: "
                        "--stop=-5:30 sind 30 min ab 5 km vor dem Ziel "
                        "(ohne = haelt argparse das Minus fuer eine Option)")
    g.add_argument("--driver-change", action="append", default=[],
                   dest="driver_change", metavar="KM|@HH:MM",
                   help="Fahrerwechsel erzwingen, bei km oder zu einer "
                        "Uhrzeit (@14:30). Mehrfach erlaubt")
    g.add_argument("--no-auto-driver-change", action="store_true",
                   dest="no_auto_dc",
                   help="die automatischen Wechsel alle 2 h weglassen")
    g.add_argument("--sweep-stop", type=float, dest="sweep_stop",
                   metavar="KM",
                   help="Standladen an dieser Stelle in 15-min-Schritten "
                        "durchrechnen und die Ausbeute vergleichen. "
                        "Negative km zaehlen vom Ziel zurueck, dann mit "
                        "Gleichheitszeichen: --sweep-stop=-1")
    g.add_argument("--n-max", type=int, default=10, dest="n_max",
                   help="wie viele Loop-Zahlen die Optionstabelle "
                        "durchrechnet. Sie bricht ohnehin eine Zeile nach "
                        "der ersten nicht machbaren ab, der Wert greift also "
                        "nur, wenn alle machbar sind")
    g.add_argument("--plot", action="store_true",
                   help="beide Anzeigen in einem Fenster oeffnen (zoomen, "
                        "verschieben, speichern). Ohne --plan wird die beste "
                        "machbare Option geplottet")
    g.add_argument("--plot-png", nargs="?", const="strategie",
                   metavar="PREFIX", dest="plot_png",
                   help="Anzeigen zusaetzlich als PNG schreiben")
    g.add_argument("--no-save", action="store_true", dest="no_save",
                   help="keine Plandateien schreiben. Sonst landet jede "
                        "machbare Option als Datei in plans/, wo "
                        "live_strategy.py sie zur Auswahl anbietet")
    g.add_argument("--plans-dir", dest="plans_dir", metavar="DIR",
                   help="Zielordner der Plandateien (Default: "
                        "data_analysis/plans, oder $SSC_PLANS_DIR)")
    g.add_argument("--spacing-km", type=float, default=5.0,
                   dest="spacing_km",
                   help="muss dem Wert in cache_weather.py entsprechen, "
                        "sonst Cache-Miss")
    g.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    lg.basicConfig(level=lg.INFO if args.verbose else lg.WARNING,
                   format="%(levelname)-7s %(message)s")
    for h in lg.getLogger().handlers:
        h.addFilter(_OnceFilter())
        h.addFilter(_MuteFilter())

    car, batt = Car_coeffs(), Battery_coeffs()
    rc = inputs.load_raceconfig()
    day = resolve_day(args, rc)
    day_date = date.fromisoformat(rc.config["days"][str(day)]["date"])
    t_start, t_deadline = rc.day_window(day)

    rc, routes = load_routes(day)
    if not routes:
        raise SystemExit(f"Tag {day}: keine Route verfuegbar - Blind Stage "
                         f"noch nicht veroeffentlicht?")

    notes = []
    # position first, weather second: only the parts still ahead need
    # weather, and asking for the rest would fail on a cache miss for a
    # stage that has already been driven
    part, km, part_km, leg, loops_done, pos_src, cross = resolve_position(
        args, rc, day, routes, notes)

    which = tuple(w for w in parts_needed(part) if w in routes)
    weathers = load_weathers(day, day_date, which, args.spacing_km)
    parts = {w: (routes[w], weathers[w].weather) for w in which
             if w in weathers}
    if part not in parts:
        raise SystemExit(
            f"Tag {day}: fuer '{part}' fehlt Route oder Wetter - "
            f"vorhanden: {sorted(parts)}")

    pack = resolve_pack(args, batt)
    t_now = resolve_start_time(args, day_date)

    if t_now >= t_deadline:
        raise SystemExit(f"{t_now:%H:%M} liegt nach der Deadline "
                         f"{t_deadline:%H:%M} - nichts mehr zu planen")
    if t_now < t_start:
        notes.append(f"Startzeit {t_now:%H:%M} liegt vor dem offiziellen "
                     f"Start {t_start:%H:%M}")

    state = inputs.PointState(
        day=day, day_date=day_date, t_now=t_now, t_deadline=t_deadline,
        part=part, km_in_part=km, part_km=part_km, pack=pack,
        loop_leg=leg, loop_done=loops_done, position_source=pos_src,
        cross_track_m=cross, notes=notes)

    # --stop KM:MIN, where a negative KM counts back from the end of the
    # day. rsplit, because "-5:30" has the minus in front of the km.
    extra_stops = []
    for s in args.stop:
        try:
            a, b = s.rsplit(":", 1)
            extra_stops.append(dayplan.StopSpec(km=float(a),
                                                minutes=float(b)))
        except ValueError:
            raise SystemExit(f"--stop erwartet KM:MIN, bekommen {s!r}")

    forced_dc = []
    for s in args.driver_change:
        try:
            if s.startswith("@"):
                tt = inputs.attach_date(inputs.resolve_time(s[1:]), day_date)
                forced_dc.append(dayplan.StopSpec(
                    at_time=tt,
                    minutes=dayplan.DRIVER_CHANGE.total_seconds() / 60,
                    label=dayplan.DRIVER_CHANGE_LABEL, tracked_min=0.0))
            else:
                forced_dc.append(dayplan.StopSpec(
                    km=float(s),
                    minutes=dayplan.DRIVER_CHANGE.total_seconds() / 60,
                    label=dayplan.DRIVER_CHANGE_LABEL, tracked_min=0.0))
        except ValueError:
            raise SystemExit(f"--driver-change erwartet km oder @HH:MM, "
                             f"bekommen {s!r}")

    ev = dict(extra_stops=extra_stops, driver_changes=forced_dc,
              auto_driver_change=not args.no_auto_dc)

    print()
    print(f"=== Tag {day}, {day_date:%d.%m.%Y}, Fenster "
          f"{t_start:%H:%M}-{t_deadline:%H:%M} SAST ===")
    print(report.header(state, weathers))

    plotted = None
    to_save = []        # (DayOption, mode) pairs written as plan files

    if args.sweep_stop is not None:
        # which loop count to sweep at: the one asked for, otherwise the
        # best feasible one - sweeping a count that cannot be driven
        # anyway would only produce a column of "nicht machbar"
        n = args.plan
        if n is None:
            feas = [o for o in dayplan.options(state, parts, car, batt,
                                               n_max=args.n_max, **ev)
                    if o.feasible]
            if not feas:
                raise SystemExit("keine machbare Option - nichts zu sweepen")
            n = feas[-1].n_loops
            print(f"\nkeine --plan angegeben, gesweept wird die beste "
                  f"machbare Option: {n} Loop(s)")
        rows = dayplan.sweep_stop(state, parts, n, car, batt,
                                  args.sweep_stop, **ev)
        ceiling = None
        first_ok = next((o for _, o in rows if o.feasible), None)
        if first_ok is not None:
            mc, _ = load_morning(day, rc, routes, args.spacing_km, car,
                                 batt, first_ok.end_wh)
            if mc is not None:
                ceiling = mc.wh_max_arrival
        print(report.sweep_text(rows, batt, args.sweep_stop, ceiling))
        return 0

    if part == "to_finish" and args.plan is None:
        # the loops are driven at the control stop; past it the only
        # remaining question is how to pace the run to the finish. An
        # options table would print the same row n+1 times.
        print("\nLoops sind nach dem Kontrollstopp nicht mehr moeglich - "
              "es bleibt der Weg zum Ziel.")
        opt = dayplan.evaluate(state, parts, 0, car, batt, **ev)
        print(report.plan_text(opt, state))
        plotted = opt
        to_save = [(opt, "plan")]
    elif args.plan is not None:
        opt = dayplan.evaluate(state, parts, args.plan, car, batt, **ev)
        print(report.plan_text(opt, state))
        plotted = opt
        to_save = [(opt, "plan")]
    else:
        opts = dayplan.options(state, parts, car, batt, n_max=args.n_max,
                               **ev)
        if part == "loop":
            print("\nLoops = noch zu fahrende Loops NACH dem laufenden. "
                  "0 heisst: diesen beenden und zum Ziel.")
        print(report.options_table(opts))
        print(report.trigger_text(opts, state))
        ok = [o for o in opts if o.feasible]
        plotted = ok[-1] if ok else None      # die beste machbare Option
        # every feasible row is a candidate the live view may be asked to
        # follow - which one is only known after reading the table
        to_save = [(o, "table") for o in ok]

    if to_save and not args.no_save:
        from data_analysis.strategy import planfile
        # the REGULATED halt lengths, which are not the planned ones
        # (dayplan budgets 35/8 min, the rules ask 30/5). The live view
        # shows from when driving on is allowed and cannot read
        # race_config.json itself, so the numbers travel in the plan file.
        regulated = {"control": float(rc.config.get("control_stop_minutes",
                                                    30)),
                     "loop": float(rc.config.get("loop_stop_minutes", 5))}
        written = []
        for o, mode in to_save:
            if o.feasible and o.trace is not None:
                try:
                    written.append(planfile.save_plan(
                        o, state, batt, weathers, out_dir=args.plans_dir,
                        mode=mode, regulated=regulated))
                except Exception as e:      # a plan file is a convenience,
                    log.warning("Plandatei fuer %d Loops nicht geschrieben: "
                                "%s", o.n_loops, e)   # never the result
        if written:
            print(f"\nPlandateien ({written[0].parent}):")
            for fp in written:
                print(f"  {fp.name}")
            print("  -> in live_strategy.py unter 'Plan' auswaehlbar")

    # The morning window is text, and useful whether or not anything is
    # plotted - it used to be computed inside the plotting branch, so a
    # plain run silently lost it.
    mc = None
    if plotted is not None and plotted.feasible:
        mc, why = load_morning(day, rc, routes, args.spacing_km, car, batt,
                               plotted.end_wh)
        if mc is None:
            print(f"\nKein Morgenfenster fuer Tag {day+1}: {why}")
        else:
            if why:
                print(f"\nHinweis  {why}")
            print(f"\nMorgenfenster Tag {day+1}: angeboten "
                  f"{mc.offered:.0f} Wh, aufgenommen {mc.absorbed:.0f} Wh"
                  + (f", verworfen {mc.spilled:.0f} Wh"
                     if mc.spilled > 1 else "")
                  + f"\n                     Ankunft ohne Verlust bis "
                    f"{mc.wh_max_arrival:.0f} Wh "
                    f"({100*mc.wh_max_arrival/capacity_wh(batt):.0f} %) "
                    f"- tiefer ist nicht schlechter, nur mehr gefahren")

    if (args.plot or args.plot_png):
        if plotted is None or not plotted.feasible:
            print("\nKein Plot: die gewaehlte Option ist nicht machbar.")
        else:
            from data_analysis.strategy import plots
            files = plots.render(plotted, state, batt,
                                 png_prefix=args.plot_png, show=args.plot,
                                 morning=mc)
            if files:
                print("\nPNG: " + ", ".join(files))
            elif args.plot:
                print("\nPlotfenster geschlossen.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
