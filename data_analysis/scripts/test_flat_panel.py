"""Selbsttest fuer --flat-panel, ohne Routen und ohne Wettercache.

Die echten Routen liegen in `strategy-private` und der Wettercache haengt am
Tagesdatum, also baut dieser Test beides synthetisch: eine gerade Strecke auf
konstanter Hoehe und ein RouteWeather mit von Hand gesetztem GHI und GTI. Das
reicht fuer die Frage, die hier zu pruefen ist - naemlich ob `panel_flat` an
JEDEM Halt ankommt und nicht nur an denen, an die man beim Einbau gedacht hat.

    python scripts/test_flat_panel.py

Bewusst kein pytest: das Skript soll im Begleitfahrzeug auf einem Laptop ohne
Testumgebung laufen koennen.
"""

from   dataclasses import dataclass
from   datetime import datetime, timedelta
from   zoneinfo import ZoneInfo
import sys

import logging as lg

import numpy as np
import pandas as pd

from data_analysis.environment.environment import RouteWeather
from data_analysis.ser_dataclasses import Battery_coeffs, Car_coeffs
from data_analysis.simulation.battery import capacity_wh, wh_from_soc
from data_analysis.strategy import dayplan

SAST = ZoneInfo("Africa/Johannesburg")

# Constant irradiance, with a tracking bonus at the upper end of what is
# real (the measured spread is ~15 % at midday and grows towards 2x when
# the sun is low). Large enough that a missed halt shows up as a clear
# energy difference, small enough that the pack does not run into its
# capacity - a capped pack makes both variants end at 100 % and the
# comparison says nothing.
GHI = 260.0
GTI = 420.0


def make_weather(lat0: float, lon0: float, n_points: int = 3) -> RouteWeather:
    """RouteWeather with constant irradiance over a whole UTC day."""
    times = pd.date_range("2026-09-11 00:00", periods=25, freq="1h", tz="UTC")
    dist_km = np.linspace(0.0, 100.0, n_points)
    points = np.stack([np.full(n_points, lat0),
                       lon0 + dist_km / 100.0], axis=1)
    idx = pd.MultiIndex.from_product([dist_km, times],
                                     names=["distance_km", "time"])
    df = pd.DataFrame({
        "shortwave_radiation":      GHI,
        "global_tilted_irradiance": GTI,
        "direct_normal_irradiance": GHI,
        "diffuse_radiation":        100.0,
        "wind_speed_10m":           0.0,
        "wind_direction_10m":       0.0,
        "temperature_2m":           25.0,
        "pressure_msl":             1013.25,
    }, index=idx)
    return RouteWeather(df, points, dist_km)


def make_route(km: float, n: int = 40, lat0: float = -30.0,
               lon0: float = 25.0) -> pd.DataFrame:
    """Straight, flat route: one variable less between cause and effect."""
    d = np.linspace(0.0, km * 1e3, n)
    seg = np.diff(d, append=d[-1])          # last row carries no segment
    seg[-1] = np.nan
    return pd.DataFrame({
        "longitude": lon0 + d / 1e5,
        "latitude":  np.full(n, lat0),
        "altitude":  np.full(n, 1200.0),
        "azimuth":   np.full(n, 90.0),
        "distance":  seg,
        "speed_route": np.full(n, 80.0),
    }, index=d)


@dataclass
class FakePack:
    wh: float
    soc: float = 0.0
    source: str = "Test"
    trust: str = "manual"
    wh_above_floor: float = 0.0
    v_pack: float = None
    i_batt: float = None
    reading_age_s: float = None


@dataclass
class FakeState:
    """Only the fields dayplan actually reads off a PointState."""
    day: int
    day_date: object
    t_now: datetime
    t_deadline: datetime
    part: str
    km_in_part: float
    part_km: float
    pack: FakePack
    loop_leg: str = None
    loop_done: int = 0
    position_source: str = "Test"
    cross_track_m: float = None
    notes: list = None

    @property
    def time_left(self):
        return self.t_deadline - self.t_now


def build(batt) -> tuple:
    route_c = make_route(60.0)
    route_l = make_route(30.0)
    route_f = make_route(80.0)
    w = make_weather(-30.0, 25.0)
    parts = {"to_control": (route_c, w), "loop": (route_l, w),
             "to_finish": (route_f, w)}
    state = FakeState(
        day=1, day_date=datetime(2026, 9, 11).date(),
        t_now=datetime(2026, 9, 11, 9, 0, tzinfo=SAST),
        t_deadline=datetime(2026, 9, 11, 17, 0, tzinfo=SAST),
        part="to_control", km_in_part=0.0, part_km=60.0,
        pack=FakePack(wh=wh_from_soc(batt, 0.75)), notes=[])
    return state, parts


def stop_rows(opt) -> pd.DataFrame:
    return opt.trace[opt.trace["kind"] == "stop"]


def main() -> int:
    # The synthetic route has no climb/descent columns and total_Ws_for_lap()
    # says so once per call - a few dozen times per options table. The
    # warning is right and irrelevant here.
    lg.basicConfig(level=lg.ERROR)
    car, batt = Car_coeffs(), Battery_coeffs()
    state, parts = build(batt)
    fails = []

    def check(name: str, cond: bool, detail: str = ""):
        print(f"  {'ok  ' if cond else 'FAIL'}  {name}"
              + (f"   {detail}" if detail else ""))
        if not cond:
            fails.append(name)

    # ---------------------------------------------------------------- 1 ----
    print("\n1) jeder Halt kennt seinen Panelzustand")
    for flat in (False, True):
        legs = dayplan.build_legs(state, parts, 2,
                                  [dayplan.StopSpec(km=100.0, minutes=30.0)],
                                  panel_flat=flat)
        stops = [l for l in legs if l.kind == "stop"]
        names = [l.name for l in stops]
        tracked = {l.name: l.tracked_for.total_seconds() / 60.0
                   for l in stops}
        check(f"panel_flat={flat}: Halte gefunden",
              len(stops) >= 4, ", ".join(names))
        if flat:
            check("panel_flat=True: kein Halt richtet aus",
                  all(v == 0.0 for v in tracked.values()), str(tracked))
        else:
            check("panel_flat=False: Kontrollstopp weiter 28 min",
                  tracked.get("Kontrollstopp") == 28.0, str(tracked))
        check(f"panel_flat={flat}: Standzeiten unveraendert",
              sum(l.duration.total_seconds() for l in stops) > 0,
              f"{sum(l.duration.total_seconds() for l in stops)/60:.0f} min")

    # ---------------------------------------------------------------- 2 ----
    print("\n2) evaluate(): Trace, Energie, Flag")
    ev = dict(extra_stops=[dayplan.StopSpec(km=100.0, minutes=30.0)],
              auto_driver_change=True)
    o_tr = dayplan.evaluate(state, parts, 2, car, batt,
                            panel_flat=False, **ev)
    o_fl = dayplan.evaluate(state, parts, 2, car, batt,
                            panel_flat=True, **ev)
    check("beide Optionen machbar", o_tr.feasible and o_fl.feasible,
          f"{o_tr.reason or 'ok'} / {o_fl.reason or 'ok'}")
    check("DayOption.panel_flat gesetzt",
          o_tr.panel_flat is False and o_fl.panel_flat is True)

    p_tr = set(o_tr.trace["panel"].unique())
    p_fl = set(o_fl.trace["panel"].unique())
    check("flach: keine 'tracked'-Zeile im Trace", "tracked" not in p_fl,
          str(sorted(p_fl)))
    check("ausgerichtet: 'tracked' kommt vor", "tracked" in p_tr,
          str(sorted(p_tr)))

    # Same halt lengths, less energy. Both matter: the rules do not get
    # shorter because the panel is broken.
    t_tr = stop_rows(o_tr)["dt_s"].sum()
    t_fl = stop_rows(o_fl)["dt_s"].sum()
    check("Standzeit identisch", abs(t_tr - t_fl) < 1.0,
          f"{t_tr/60:.1f} vs {t_fl/60:.1f} min")

    wh_tr = -stop_rows(o_tr)["Ws"].sum() / 3600.0
    wh_fl = -stop_rows(o_fl)["Ws"].sum() / 3600.0
    check("flach laedt im Stand weniger", wh_fl < wh_tr,
          f"{wh_tr:.0f} Wh vs {wh_fl:.0f} Wh")
    check("End-SOC flach tiefer", o_fl.end_soc < o_tr.end_soc,
          f"{100*o_tr.end_soc:.1f} % vs {100*o_fl.end_soc:.1f} %")

    # ---------------------------------------------------------------- 3 ----
    print("\n3) die Wege, die das Flag leicht verpassen")
    # every halt kind in one plan: control, loop, charge stop, driver
    # change, and the leftover standing phase before the finish line
    legs = dayplan.build_legs(state, parts, 1,
                              [dayplan.StopSpec(km=50.0, minutes=45.0)],
                              panel_flat=True)
    check("--stop flach gespleisst",
          all(l.tracked_for.total_seconds() == 0
              for l in legs if l.kind == "stop" and "Standladen" in l.name))

    o_rest = dayplan.evaluate(state, parts, 0, car, batt, panel_flat=True)
    rest = o_rest.trace[o_rest.trace["leg"].str.startswith("Restzeit")]
    check("Restzeit vor der Ziellinie existiert und ist flach",
          len(rest) > 0 and set(rest["panel"]) == {"flat"},
          f"{len(rest)} Zeile(n)")

    rows = dayplan.sweep_stop(state, parts, 1, car, batt, km=50.0,
                              step_min=30, max_min=60, panel_flat=True)
    sweep_ok = all("tracked" not in set(o.trace["panel"].unique())
                   for _, o in rows if o.feasible and o.trace is not None)
    check("sweep_stop reicht panel_flat durch", sweep_ok,
          f"{len(rows)} Zeile(n)")

    opts = dayplan.options(state, parts, car, batt, n_max=2, panel_flat=True)
    check("options() reicht panel_flat durch",
          all(o.panel_flat for o in opts), f"{len(opts)} Zeile(n)")

    # ---------------------------------------------------------------- 4 ----
    print("\n4) Standladen: Panel pro Halt")

    def charge_wh(opt):
        s = opt.trace[opt.trace["leg"].astype(str).str.startswith(
            "Standladen")]
        return -float(s["Ws"].sum()) / 3600.0, s

    # aim=True on a flat day, and aim=False on a normal one: the halt has
    # to disagree with the day in BOTH directions, or the override is only
    # a second way of writing the global flag
    spec_aim  = dayplan.StopSpec(km=100.0, minutes=45.0, aim=True)
    spec_flat = dayplan.StopSpec(km=100.0, minutes=45.0, aim=False)
    spec_auto = dayplan.StopSpec(km=100.0, minutes=45.0)

    o_day_flat = dayplan.evaluate(state, parts, 1, car, batt,
                                  extra_stops=[spec_auto], panel_flat=True)
    o_aim_on_flat = dayplan.evaluate(state, parts, 1, car, batt,
                                     extra_stops=[spec_aim], panel_flat=True)
    o_day_aim = dayplan.evaluate(state, parts, 1, car, batt,
                                 extra_stops=[spec_auto], panel_flat=False)
    o_flat_on_aim = dayplan.evaluate(state, parts, 1, car, batt,
                                     extra_stops=[spec_flat],
                                     panel_flat=False)

    wh_day_flat, _ = charge_wh(o_day_flat)
    wh_aim_on_flat, rows_aim = charge_wh(o_aim_on_flat)
    wh_day_aim, _ = charge_wh(o_day_aim)
    wh_flat_on_aim, _ = charge_wh(o_flat_on_aim)

    check("aim=True hebt --flat-panel fuer diesen Halt auf",
          wh_aim_on_flat > wh_day_flat,
          f"{wh_day_flat:.0f} Wh -> {wh_aim_on_flat:.0f} Wh")
    check("aim=False macht den Halt ohne --flat-panel flach",
          wh_flat_on_aim < wh_day_aim,
          f"{wh_day_aim:.0f} Wh -> {wh_flat_on_aim:.0f} Wh")
    check("der ausgerichtete Halt ist der einzige tracked-Eintrag",
          set(o_aim_on_flat.trace["panel"]) == {"flat", "tracked"}
          and set(rows_aim["panel"]) == {"tracked"})
    check("uebrige Halte bleiben flach",
          set(o_aim_on_flat.trace[
              o_aim_on_flat.trace["leg"].astype(str).str.startswith(
                  "Kontrollstopp")]["panel"]) == {"flat"})

    # tracked_min splits one halt
    spec_half = dayplan.StopSpec(km=100.0, minutes=40.0, aim=True,
                                 tracked_min=10.0)
    o_half = dayplan.evaluate(state, parts, 1, car, batt,
                              extra_stops=[spec_half], panel_flat=True)
    _, rows_half = charge_wh(o_half)
    mins_tracked = float(rows_half[rows_half["panel"] == "tracked"]
                         ["dt_s"].sum()) / 60.0
    check("tracked_min teilt den Halt", abs(mins_tracked - 10.0) < 0.1,
          f"{mins_tracked:.1f} von 40 min")

    names = [l.name for l in dayplan.build_legs(
        state, parts, 1, [spec_aim], panel_flat=True) if l.kind == "stop"]
    check("abweichender Halt ist am Namen erkennbar",
          any("(ausgerichtet)" in n for n in names),
          ", ".join(n for n in names if "Standladen" in n))
    names2 = [l.name for l in dayplan.build_legs(
        state, parts, 1, [spec_auto], panel_flat=True) if l.kind == "stop"]
    check("ohne Abweichung kein Zusatz im Namen",
          not any("(" in n for n in names2 if "Standladen" in n))
    # the prefix that report.py and planfile.py match on must survive
    check("Praefix 'Standladen' bleibt erhalten",
          all(n.startswith("Standladen") for n in names + names2
              if "Standladen" in n))

    # sweep_stop with its own panel choice
    rows_fl = dayplan.sweep_stop(state, parts, 1, car, batt, km=100.0,
                                 step_min=30, max_min=60, panel_flat=True)
    rows_ai = dayplan.sweep_stop(state, parts, 1, car, batt, km=100.0,
                                 step_min=30, max_min=60, panel_flat=True,
                                 aim=True)
    last_fl = [o for _, o in rows_fl if o.feasible][-1]
    last_ai = [o for _, o in rows_ai if o.feasible][-1]
    check("sweep_stop(aim=True) laedt mehr als der flache Tag",
          charge_wh(last_ai)[0] > charge_wh(last_fl)[0],
          f"{charge_wh(last_fl)[0]:.0f} Wh vs {charge_wh(last_ai)[0]:.0f} Wh")

    # CLI parsing
    sys.path.insert(0, "scripts")
    import point_strategy as ps
    for word, want in (("aus", (True, None)), ("ausgerichtet", (True, None)),
                       ("flach", (False, None)), ("flat", (False, None)),
                       ("20", (True, 20.0)), ("0", (False, None)),
                       ("", (None, None))):
        check(f"PANEL {word!r} -> {want}",
              ps._parse_panel_mode(word, "test") == want)
    a = ps.parse_args(["--stop", "45:30:aus", "--stop=-5:20:flach"])
    check("--stop nimmt drei Felder", a.stop == ["45:30:aus", "-5:20:flach"])
    bad = False
    try:
        ps._parse_panel_mode("quatsch", "test")
    except SystemExit:
        bad = True
    check("unsinniges PANEL wird abgelehnt", bad)

    # ---------------------------------------------------------------- 5 ----
    print("\n5) Morgenfenster")
    w = make_weather(-30.0, 25.0)
    rel = datetime(2026, 9, 11, 6, 0, tzinfo=SAST)
    st = datetime(2026, 9, 11, 8, 0, tzinfo=SAST)
    wh0 = wh_from_soc(batt, 0.5)
    m_tr = dayplan.morning_charge(w, -30.0, 25.0, rel, st, car, batt, wh0,
                                  tracked=True)
    m_fl = dayplan.morning_charge(w, -30.0, 25.0, rel, st, car, batt, wh0,
                                  tracked=False)
    check("flach bietet weniger an", m_fl.offered < m_tr.offered,
          f"{m_tr.offered:.0f} Wh vs {m_fl.offered:.0f} Wh")
    # the direction that matters: a flat morning RAISES the sensible
    # arrival energy, because less arrives for free
    check("Obergrenze Ankunft steigt",
          m_fl.wh_max_arrival > m_tr.wh_max_arrival,
          f"{m_tr.wh_max_arrival:.0f} Wh vs {m_fl.wh_max_arrival:.0f} Wh "
          f"(Kapazitaet {capacity_wh(batt):.0f} Wh)")
    check("MorningCharge.tracked gesetzt",
          m_tr.tracked is True and m_fl.tracked is False)

    # ---------------------------------------------------------------- 5 ----
    print("\n6) Report und Plandatei")
    from data_analysis.strategy import planfile, report
    h = report.header(state, {}, panel_flat=True, aim_morning=False)
    check("Header nennt das flache Panel", "Panel" in h and "flach" in h)
    check("Header ohne Flag schweigt",
          "Panel     flach" not in report.header(state, {}))
    check("Plantext markiert es",
          "Panel flach" in report.plan_text(o_fl, state))
    check("Optionstabelle markiert es",
          "Panel flach" in report.options_table([o_fl]))

    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        fp = planfile.save_plan(o_fl, state, batt, {}, out_dir=Path(tmp))
        import json
        meta = json.loads(fp.read_text())["meta"]
        check("Plandatei traegt panel_flat", meta["panel_flat"] is True)
        check("Dateiname markiert es", "_flach" in fp.name, fp.name)
        check("Label markiert es", "Panel flach" in meta["label"],
              meta["label"])
        fp2 = planfile.save_plan(o_tr, state, batt, {}, out_dir=Path(tmp))
        meta2 = json.loads(fp2.read_text())["meta"]
        check("normaler Plan unveraendert",
              meta2["panel_flat"] is False and "_flach" not in fp2.name)

    print()
    if fails:
        print(f"{len(fails)} Pruefung(en) fehlgeschlagen: "
              + ", ".join(fails))
        return 1
    print("alle Pruefungen bestanden")
    return 0


if __name__ == "__main__":
    sys.exit(main())
