"""Offline checks for the live view: plan file, projection, ETA, tracker.

    python scripts/test_live_offline.py

No network, no weather cache, no strategy-private: the plan is built here
from a synthetic straight road, so every number has a known answer. The
checks that matter most are the ones that once failed silently - the
datetime unit in the km lookup, the loop-pass projection, the out-of-order
backfill - because those produce plausible numbers, not errors.
"""

from __future__ import annotations

from   pathlib import Path
import sys
import tempfile

import numpy as np
import pandas as pd

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from data_analysis.ser_dataclasses import Battery_coeffs                  # noqa
from data_analysis.simulation.battery import capacity_wh, terminal_voltage  # noqa
from data_analysis.strategy import planfile, live                          # noqa
from data_analysis.telemetry import ser_client                             # noqa

FAILS = []


def check(name, cond, detail=""):
    print(f"  [{'OK  ' if cond else 'FEHL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAILS.append(name)


def synthetic_plan(batt) -> planfile.Plan:
    """30 km due north at 60 km/h, a 10-min stop at km 10, a loop-like
    out-and-back over km 20..30 so the same road appears twice."""
    t0 = pd.Timestamp("2026-09-10T07:00:00Z")
    rows, t, km, wh = [], t0, 0.0, 2000.0
    lat0, lon0 = -26.0, 28.0
    dlat = 0.5 / 111.0    # 0.5 km per node

    def node(k):        # out-and-back: km 20..25 north, 25..30 back south
        if k <= 25:
            return lat0 + k * dlat, lon0
        return lat0 + (50 - k) * dlat, lon0

    def add_drive(n, v):
        nonlocal t, km, wh
        for _ in range(n):
            dt = 0.5 / v * 3600.0
            lat, lon = node(km)
            p_net = 800.0
            rows.append(dict(t_start=t, t_end=t + pd.Timedelta(seconds=dt), dt_s=dt,
                             km_start=km, km_end=km + 0.5, speed_kmh=v,
                             v_route=80.0, v_limit=100.0 if km < 15 else np.nan,
                             v_limit_est=100.0, lat=lat, lon=lon, altitude_m=1500.0,
                             p_solar=600.0, p_motor=1300.0, p_aux=100.0, p_net=p_net,
                             Ws=p_net * dt, wh_remaining=wh - p_net * dt / 3600.0,
                             soc=0.6, wh_floor=291.0, v_pack_pred=115.0,
                             leg="Loop 1" if km >= 20 else "ToControlStop",
                             kind="drive", panel="flat",
                             n_roundabout=1 if abs(km - 12.0) < 0.01 else 0,
                             n_traffic_signal=0))
            wh -= p_net * dt / 3600.0
            t = t + pd.Timedelta(seconds=dt)
            km += 0.5

    def add_stop(name, minutes, p_solar):
        nonlocal t, wh
        secs = minutes * 60.0
        lat, lon = node(km)
        rows.append(dict(t_start=t, t_end=t + pd.Timedelta(seconds=secs), dt_s=secs,
                         km_start=km, km_end=km, speed_kmh=0.0, v_route=np.nan,
                         v_limit=np.nan, v_limit_est=np.nan, lat=lat, lon=lon,
                         altitude_m=1500.0, p_solar=p_solar, p_motor=0.0, p_aux=100.0,
                         p_net=100.0 - p_solar, Ws=(100.0 - p_solar) * secs,
                         wh_remaining=wh + (p_solar - 100.0) * secs / 3600.0,
                         soc=0.6, wh_floor=291.0, v_pack_pred=118.0, leg=name,
                         kind="stop", panel="tracked", n_roundabout=0,
                         n_traffic_signal=0))
        wh += (p_solar - 100.0) * secs / 3600.0
        t = t + pd.Timedelta(seconds=secs)

    add_drive(20, 60.0)                 # km 0..10
    add_stop("Fahrerwechsel km 10.0", 5, 600.0)
    add_drive(20, 60.0)                 # km 10..20
    add_stop("Kontrollstopp", 30, 900.0)
    add_drive(20, 50.0)                 # km 20..30 (out and back)
    tr = pd.DataFrame(rows)[planfile.TRACE_COLS]
    meta = {"format": 1, "day": 1, "day_date": "2026-09-10", "t_now": t0.isoformat(),
            "t_deadline": "2026-09-10T15:00:00+00:00",
            "t_finish": t.isoformat(), "part": "to_control", "km_in_part": 0.0,
            "part_km": 20.0, "loop_leg": None, "loop_done": 0,
            "pack": {"wh": 2000.0, "soc": 0.68, "source": "test", "trust": "manual",
                     "capacity_wh": capacity_wh(batt), "floor_wh": 291.0},
            "n_loops": 1, "km": 30.0, "avg_kmh": 56.0, "end_soc": 0.6, "min_soc": 0.6,
            "end_wh": wh, "cloud_margin": 0.9, "mode": "test",
            "driver_changes_km": [10.0], "reserve_s": 3600.0}
    meta["label"] = planfile.plan_label(meta)
    return planfile.Plan(meta=meta, tr=tr)


def main() -> int:
    batt = Battery_coeffs()
    plan = synthetic_plan(batt)

    print("Plan-Datei")
    with tempfile.TemporaryDirectory() as d:
        fp = Path(d) / "p.json"
        rows = plan.tr.copy()
        for c in ("t_start", "t_end"):
            rows[c] = [planfile._iso(x) for x in rows[c]]
        rows = rows.astype(object).where(pd.notna(rows), None)
        import json
        fp.write_text(json.dumps({"meta": plan.meta, "columns": planfile.TRACE_COLS,
                                  "rows": rows.to_numpy().tolist()}))
        p2 = planfile.Plan.load(fp)
        check("Roundtrip total_km", abs(p2.total_km - 30.0) < 1e-6, f"{p2.total_km}")
        check("Roundtrip Zeiten (Einheit datetime64!)",
              abs((p2.time_at_km(10.0) - plan.time_at_km(10.0)).total_seconds()) < 1,
              f"{p2.time_at_km(10.0)}")
        lst = planfile.list_plans(Path(d))
        check("list_plans findet die Datei", len(lst) == 1 and lst[0]["label"] == plan.label)

    print("Lookups")
    check("time_at_km(10) = Abfahrt nach dem Wechsel (07:15)",
          plan.time_at_km(10.0) == pd.Timestamp("2026-09-10T07:15:00Z"),
          str(plan.time_at_km(10.0)))
    check("time_at_km_arrival(10) = Ankunft (07:10)",
          plan.time_at_km_arrival(10.0) == pd.Timestamp("2026-09-10T07:10:00Z"),
          str(plan.time_at_km_arrival(10.0)))
    tt = plan.travel_time_s(5.0, 15.0, include_stops=True)
    check("travel_time 5->15 km = 10 min Fahrt + 5 min Wechsel", abs(tt - 900.0) < 1, f"{tt:.0f} s")
    tt = plan.travel_time_s(5.0, 20.0, include_stops=True)
    check("travel_time bis zum Kontrollstopp zaehlt den Stopp dort NICHT",
          abs(tt - (15 * 60 + 300)) < 1, f"{tt:.0f} s")
    st = plan.stops()
    check("3 Standphasen erkannt", len(st) == 2 and list(st["kind"]) == ["driver", "control"],
          str(list(st["kind"])))
    check("Kreisel bei km 12", len(plan.features()) == 1 and abs(plan.features()["km"][0] - 12.0) < 0.01)
    sp = plan.speed_at_km(20.0)
    check("speed_at_km an einer Stoppgrenze nimmt die Fahrzeile danach", sp["v_soll"] == 50.0, str(sp["v_soll"]))
    check("km_at_time(07:20) = 20 km, steht am Kontrollstopp",
          abs(plan.km_at_time(pd.Timestamp("2026-09-10T07:40:00Z")) - 20.0) < 1e-6)

    print("Projektion")
    lat, lon = plan.coord_at_km(7.25)
    km, cross = plan.project(lat, lon)
    check("Projektion auf gerader Strasse", abs(km - 7.25) < 0.01 and cross < 1, f"{km:.3f} km, {cross:.1f} m")
    lat, lon = plan.coord_at_km(22.0)          # same place as km 28
    km_a, _ = plan.project(lat, lon)
    km_b, _ = plan.project(lat, lon, after_km=25.0, window_km=10.0)
    check("Hin-/Rueckweg: global und mit after_km verschieden",
          abs(km_a - 22.0) < 0.01 and abs(km_b - 28.0) < 0.01, f"{km_a:.2f} / {km_b:.2f}")

    print("Tracker")
    tr = live.LiveTracker(plan, batt)
    t0 = plan.t_start
    rows, fixes = [], []
    for s in range(0, 40 * 60):
        t = t0 + pd.Timedelta(seconds=s)
        km = plan.km_at_time(t)
        here = plan.speed_at_km(km)
        standing = plan.time_at_km(km) > t and km > 0.01
        v_kmh = 0.0 if standing else here["v_soll"]
        wh = plan.wh_at_time(t)["wh_remaining"]      # time-true, also in halts
        p_sol = 900.0 if (standing and km > 15) else 600.0
        i = (100.0 - p_sol) / 115.0 if standing else 800.0 / 115.0
        p_now = p_sol
        rows.append({"time": t, "speed": v_kmh, "mppt1_power": p_now / 3, "mppt2_power": p_now / 3,
                     "mppt3_power": p_now / 3, "mppt4_power": 0.0,
                     "battery_voltage": terminal_voltage(batt, wh, i),
                     "battery_current": -i, "gap": False})
        if s % 5 == 0:
            la, lo = plan.coord_at_km(km)
            fixes.append({"id": s, "time": t, "lat": la, "lon": lo, "speed_kmh": v_kmh})
    df = pd.DataFrame(rows).set_index("time")
    fi = 0
    for k in range(0, len(df), 10):
        chunk = df.iloc[k:k + 10]
        while fi < len(fixes) and fixes[fi]["time"] <= chunk.index[-1]:
            tr.update_gps(fixes[fi]); fi += 1
        tr.ingest(chunk)
    now = df.index[-1]
    st = tr.status(now=now)
    check("Position nach 40 min: km 20 (am Kontrollstopp)", abs(st["position"]["km"] - 20.0) < 0.3,
          f"{st['position']['km']}")
    check("Zeitplan-Abweichung ~0 (steht planmaessig am Kontrollstopp)",
          abs(st["position"]["schedule_min"]) < 1.0 and st["position"]["at_stop"] == "Kontrollstopp",
          f"{st['position']['schedule_min']} min, {st['position']['at_stop']}")
    check("Energie: Ist gegen Plan < 40 Wh (Vergleich zeitbasiert im Halt)",
          abs(st["energy"]["delta_wh"]) < 40 and st["energy"]["compared_at"] == "stop",
          f"{st['energy']['delta_wh']} Wh, {st['energy']['compared_at']}")
    # mid-drive snapshot at 07:20 (km 15): compare by km, driver counter running
    tr2 = live.LiveTracker(plan, batt)
    cut = df.index <= t0 + pd.Timedelta(minutes=20)
    fi2 = 0
    for k in range(0, int(cut.sum()), 10):
        chunk = df[cut].iloc[k:k + 10]
        while fi2 < len(fixes) and fixes[fi2]["time"] <= chunk.index[-1]:
            tr2.update_gps(fixes[fi2]); fi2 += 1
        tr2.ingest(chunk)
    st2 = tr2.status(now=t0 + pd.Timedelta(minutes=20))
    check("07:20: km 15, Vergleich per km, Zeitplan ~0",
          abs(st2["position"]["km"] - 15.0) < 0.3 and st2["energy"]["compared_at"] == "km"
          and abs(st2["position"]["schedule_min"]) < 1.0,
          f"km {st2['position']['km']}, {st2['energy']['compared_at']}, {st2['position']['schedule_min']} min")
    check("07:20: Energie Ist gegen Plan < 40 Wh", abs(st2["energy"]["delta_wh"]) < 40, f"{st2['energy']['delta_wh']} Wh")
    check("07:20: Fahrzeit seit dem Wechsel bei km 10 ~5 min",
          st2["driver"]["drive_s"] is not None and 240 < st2["driver"]["drive_s"] < 420,
          f"{st2['driver']['drive_hm']} ({st2['driver']['drive_s']} s)")
    check("07:20: naechste Ankunft = Kontrollstopp 07:25",
          st2["next"]["kind"] == "control" and st2["next"]["t_plan"].startswith("09:25")
          and st2["next"]["t_live"].startswith("09:25"), f"{st2['next']}")
    check("SOC aus Spannung: Band umschliesst Ist", st["energy"]["live_in_band"],
          f"{st['energy']['soc_v_lo']}..{st['energy']['soc_v_hi']} vs {st['energy']['soc_live']}")
    check("Naechste Ankunft = Ziel (Kontrollstopp ist erreicht)",
          st["next"]["type"] == "finish" or st["next"]["kind"] == "control", str(st["next"]["name"]))
    dr = st["driver"]
    check("Fahrzeit-Zaehler steht auf 0, waehrend das Auto > 3 min steht",
          dr["drive_s"] is not None and dr["drive_s"] == 0, f"{dr['drive_hm']}")
    check("Finish hat Deadline-Feld", st["finish"] is not None and st["finish"]["late_min"] is not None)
    import json
    json.dumps(st)
    check("Status ist JSON-serialisierbar", True)

    print("Geschwindigkeitsquelle")
    v, s = tr.current_speed(now)
    check("Tempo kommt aus der Telemetrie, nicht aus dem GPS", s == "can", f"{v} km/h aus {s}")
    later = now + pd.Timedelta(seconds=30)      # CAN row is 30 s old ...
    tr.pos.gps_speed_kmh, tr.pos.t_fix = 77.0, later      # ... GPS is fresh
    v, s = tr.current_speed(later)
    check("CAN veraltet -> GPS springt ein", s == "gps" and v == 77.0, f"{v} km/h aus {s}")
    tr2b = live.LiveTracker(plan, batt)
    gapped = df.iloc[:60].copy()
    gapped.loc[:, ["speed", "battery_voltage", "battery_current"]] = np.nan
    tr2b.ingest(gapped)
    v, s = tr2b.current_speed(gapped.index[-1])
    check("Zeile aus einer Luecke liefert kein Tempo (nicht 0 km/h)", v is None, f"{v} / {s}")

    print("Fahrerwechsel")
    tr3 = live.LiveTracker(plan, batt)
    idx = df.index[:1500]
    tr3.ingest(df.iloc[:1500],
               pd.Series([plan.km_at_time(t) for t in idx], index=idx))
    tr3.pos.km = plan.km_at_time(idx[-1])
    t_now = idx[-1]
    tr3.log_driver_change(t_now)
    check("Wechsel jetzt -> Zaehler bei 0", abs(tr3.drive_time_s(t_now)) < 2,
          f"{tr3.drive_time_s(t_now)} s")
    tr3.log_driver_change(t_now, at=t_now - pd.Timedelta(minutes=10))
    check("nachgetragener aelterer Wechsel setzt den Zaehler NICHT zurueck",
          abs(tr3.drive_time_s(t_now)) < 2, f"{tr3.drive_time_s(t_now)} s")
    check("Log chronologisch, 2 Eintraege",
          len(tr3.driver_log) == 2 and tr3.driver_log[0]["time"] < tr3.driver_log[1]["time"])
    km_back = tr3.driver_log[0]["km"]
    check("nachgetragener Wechsel bekommt den km von DAMALS, nicht von jetzt",
          km_back is not None and km_back < tr3.pos.km - 1.0,
          f"km {km_back} gegen jetzt {tr3.pos.km:.1f}")
    tr3.log_driver_change(t_now, at=t_now + pd.Timedelta(hours=1))
    check("Wechsel in der Zukunft wird auf jetzt geklemmt",
          tr3.driver_log[-1]["time"] <= t_now)
    tr4 = live.LiveTracker(plan, batt)
    tr4.adopt_driver_log(tr3.driver_log)
    check("Planwechsel: Zeiten uebernommen, km verworfen",
          len(tr4.driver_log) == 3 and all(e["km"] is None for e in tr4.driver_log)
          and tr4._drive_start == tr3.driver_log[-1]["time"])

    print("Wechselgrenze aus dem Plan")
    d = plan.drive_deadline(0.0, 3600.0)      # 1 h fahren ab km 0
    check("Halt bei km 10 nimmt die Frage weg", d["reset_by"] is not None
          and abs(d["km"] - 10.0) < 0.01, str(d))
    d = plan.drive_deadline(10.5, 3600.0)     # nach dem Wechsel
    check("Kontrollstopp bei km 20 setzt zurueck", d["reset_by"] == "Kontrollstopp"
          and abs(d["km"] - 20.0) < 0.01, str(d))
    d = plan.drive_deadline(20.5, 600.0)      # 10 min ab km 20.5, keine Halte mehr
    check("ohne Halt: Grenze nach der gefahrenen Zeit", d["reset_by"] is None
          and abs(d["km"] - (20.5 + 600 / 3600 * 50)) < 0.2, str(d))
    d = plan.drive_deadline(20.5, 10 * 3600.0)
    check("Zeit reicht bis zum Tagesende", d["km"] is None, str(d))

    print("Halt: kein Toggeln am Knoten")
    # das Auto steht seit 07:25 am Kontrollstopp (km 20)
    t_halt = pd.Timestamp("2026-09-10T07:40:00Z")
    trh = live.LiveTracker(plan, batt)
    cut = df.index <= t_halt
    idx_h = df.index[cut]
    trh.ingest(df[cut], pd.Series([plan.km_at_time(t) for t in idx_h], index=idx_h))
    seen = set()
    for d_km in (-0.02, -0.005, 0.0, +0.005, +0.02):
        trh.pos.km = 20.0 + d_km
        s = trh.status(now=t_halt)
        seen.add((s["position"]["leg"], s["position"]["schedule_min"],
                  s["next"]["t_live"], s["position"]["halt_leave"]))
    check("km-Zappeln um den Halt aendert nichts mehr", len(seen) == 1,
          f"{len(seen)} verschiedene Zustaende: {sorted(seen)[:2]}")
    st_h = trh.status(now=t_halt)["position"]
    check("Halt erkannt, Ankunft aus der Geschwindigkeit",
          st_h["at_stop"] == "Kontrollstopp" and st_h["halt_source"] == "speed"
          and st_h["halt_arrived"].startswith("09:25"), str(st_h["halt_arrived"]))
    check("Zeitplan im Halt = 0 (planmaessig angekommen)",
          abs(st_h["schedule_min"]) < 0.5, f"{st_h['schedule_min']}")
    # Position einfrieren, solange das Auto steht
    lat, lon = plan.coord_at_km(20.05)
    trh.update_gps({"id": 1, "time": t_halt + pd.Timedelta(seconds=1),
                    "lat": lat, "lon": lon, "speed_kmh": 0.0})
    check("stehendes Auto: GPS-Rauschen verschiebt den km nicht",
          abs(trh.pos.km - 20.02) < 1e-9 and trh.pos.frozen, f"{trh.pos.km}")
    lat, lon = plan.coord_at_km(21.0)          # 1 km weiter: echte Bewegung
    trh.update_gps({"id": 2, "time": t_halt + pd.Timedelta(seconds=2),
                    "lat": lat, "lon": lon, "speed_kmh": 0.0})
    check("echte Bewegung wird trotz 'steht' geglaubt",
          abs(trh.pos.km - 21.0) < 0.05, f"{trh.pos.km}")

    # losfahren: der Halt muss sauber loslassen
    t_go = t_halt + pd.Timedelta(minutes=31)
    go = pd.DataFrame([{"time": t_go + pd.Timedelta(seconds=k), "speed": 50.0,
                        "mppt1_power": 200, "mppt2_power": 200,
                        "mppt3_power": 200, "mppt4_power": 0,
                        "battery_voltage": 116.0, "battery_current": -7.0,
                        "gap": False} for k in range(30)]).set_index("time")
    trh.ingest(go)
    lat, lon = plan.coord_at_km(21.5)
    trh.update_gps({"id": 3, "time": t_go + pd.Timedelta(seconds=31),
                    "lat": lat, "lon": lon, "speed_kmh": 50.0})
    s_go = trh.status(now=t_go + pd.Timedelta(seconds=31))["position"]
    check("nach der Abfahrt laesst der Halt los",
          s_go["at_stop"] is None and not s_go["frozen"]
          and abs(s_go["km"] - 21.5) < 0.1,
          f"km {s_go['km']}, Halt {s_go['at_stop']}, frozen {s_go['frozen']}")

    print("Halt: verspaetete Ankunft und Reglement")
    reg = plan.stops()
    check("regulierte Dauer im Plan: Kontrollstopp 30 min",
          float(reg[reg["kind"] == "control"]["reg_s"].iloc[0]) == 1800.0,
          str(reg[["name", "dur_s", "reg_s"]].to_dict("records")))
    check("Fahrerwechsel hat keine regulierte Dauer",
          pd.isna(reg[reg["kind"] == "driver"]["reg_s"].iloc[0]))
    late = pd.Timestamp("2026-09-10T08:05:00Z")     # 10 min nach Planabfahrt
    check("Halt wird auch nach der Planabfahrt erkannt (stehend)",
          plan.stop_at(20.0, late, standing=True) is not None
          and plan.stop_at(20.0, late) is None)

    print("Stopp bestaetigen")
    trc = live.LiveTracker(plan, batt)
    # Plan startet erst 07:34, das Auto steht aber schon seit 07:25
    late_start = df.index >= pd.Timestamp("2026-09-10T07:34:00Z")
    idx_l = df.index[late_start & cut]
    trc.ingest(df[late_start & cut],
               pd.Series([plan.km_at_time(t) for t in idx_l], index=idx_l))
    trc.pos.km = 20.0
    st_l = trc.status(now=t_halt)["position"]
    check("ohne Vorlauf ist die erkannte Ankunft zu spaet",
          st_l["halt_arrived"].startswith("09:34"), str(st_l["halt_arrived"]))
    pre = df[(df.index >= pd.Timestamp("2026-09-10T07:20:00Z"))
             & (df.index < pd.Timestamp("2026-09-10T07:34:00Z"))][["speed"]]
    trc2 = live.LiveTracker(plan, batt)
    trc2.seed_standstill(pre)
    trc2.ingest(df[late_start & cut],
                pd.Series([plan.km_at_time(t) for t in idx_l], index=idx_l))
    trc2.pos.km = 20.0
    st_s = trc2.status(now=t_halt)["position"]
    check("Vorlauf aus der Telemetrie findet die wahre Ankunft",
          st_s["halt_arrived"].startswith("09:25"), str(st_s["halt_arrived"]))
    trc.log_stop(t_halt, at=pd.Timestamp("2026-09-10T07:25:00Z"))
    st_c = trc.status(now=t_halt)["position"]
    check("bestaetigte Ankunft gewinnt ueber die erkannte",
          st_c["halt_source"] == "confirmed" and st_c["halt_arrived"].startswith("09:25"),
          f"{st_c['halt_source']} {st_c['halt_arrived']}")
    check("weiter ab = Ankunft + 30 min Reglement, Plan-Abfahrt daneben",
          st_c["halt_free_at"].startswith("09:55") and st_c["halt_leave"].startswith("09:55"),
          f"frei {st_c['halt_free_at']} / Plan {st_c['halt_leave']}")
    trc.log_stop(t_halt, at=pd.Timestamp("2026-09-10T07:27:00Z"))
    check("zweite Bestaetigung korrigiert, statt zu doppeln",
          len(trc.stop_log) == 1 and trc.stop_log[0]["time"].minute == 27,
          str(trc.stop_log))

    print("Manuelle Position")
    # after `now`, because the speed check above moved the fix clock forward
    t_a = now + pd.Timedelta(seconds=60)
    tr.set_position_km(5.0)
    tr.update_gps({"id": 9999, "time": t_a, "lat": fixes[-1]["lat"],
                   "lon": fixes[-1]["lon"], "speed_kmh": 0.0})
    check("Hold: GPS-Fix aendert die Handposition nicht", abs(tr.pos.km - 5.0) < 1e-9, f"{tr.pos.km}")
    tr.release_position()
    tr.update_gps({"id": 10000, "time": t_a + pd.Timedelta(seconds=5), "lat": fixes[-1]["lat"],
                   "lon": fixes[-1]["lon"], "speed_kmh": 0.0})
    check("Release: naechster Fix setzt die Position wieder", abs(tr.pos.km - 20.0) < 0.3, f"{tr.pos.km}")

    print("Telemetrie-Client")
    payload = {"series": ["speed", "battery_voltage", "battery_current", "mppt3_power"],
               "points": [{"timestamp": "2026-09-10T07:00:00Z", "values": [50, 118.0, -5.0, 200.0]},
                          {"timestamp": "2026-09-10T07:00:01Z", "values": [0, 0.0, 0.0, 0.0]},
                          {"timestamp": "2026-09-10T07:00:02Z", "values": [50, None, -5.0, 210.0]}]}
    d = ser_client.clean(ser_client.parse_range_json(payload))
    check("0-V-Zeile wird als Luecke maskiert", bool(d["gap"].iloc[1]) and pd.isna(d["battery_voltage"].iloc[1]))
    check("Luecke maskiert AUCH speed und MPPT (sonst liest sie als 'steht')",
          all(pd.isna(d[c].iloc[1]) for c in ("speed", "battery_current", "mppt3_power")),
          str(d.iloc[1].to_dict()))
    check("None wird NaN und Luecke", bool(d["gap"].iloc[2]))
    check("gueltige Zeile bleibt unangetastet",
          d["speed"].iloc[0] == 50 and d["battery_voltage"].iloc[0] == 118.0)
    check("Zeitstempel UTC", str(d.index.tz) == "UTC")
    g = ser_client._parse_gps({"id": 1, "deviceName": "honor", "timestamp": "2026-09-10T09:00:00+02:00",
                               "latitude": -26.0, "longitude": 28.0, "speedKmh": 55.0})
    check("GPS-Zeit mit Offset -> 07:00 UTC", g["time"] == pd.Timestamp("2026-09-10T07:00:00Z"), str(g["time"]))
    g = ser_client._parse_gps({"timestamp": "2026-09-10T09:00:00", "latitude": -26.0, "longitude": 28.0})
    check("GPS-Zeit ohne Zone gilt als SAST", g["time"] == pd.Timestamp("2026-09-10T07:00:00Z"), str(g["time"]))

    print("Backfill km-Zuordnung")
    gps = pd.DataFrame([{"lat": f["lat"], "lon": f["lon"], "speed_kmh": f["speed_kmh"]} for f in fixes],
                       index=pd.DatetimeIndex([f["time"] for f in fixes]).as_unit("us"))
    ks = live.km_series_from_gps(plan, gps, df.index.as_unit("us"))
    check("km_series ohne NaN im Inneren (Mikrosekunden-Index)",
          ks is not None and ks.iloc[10:-10].notna().all(), f"NaN: {None if ks is None else ks.isna().sum()}")
    check("km_series monoton", ks is not None and (ks.dropna().diff().dropna() >= -1e-6).all())

    print()
    if FAILS:
        print(f"{len(FAILS)} Pruefung(en) fehlgeschlagen: {FAILS}")
        return 1
    print("alle Pruefungen bestanden")
    return 0


if __name__ == "__main__":
    sys.exit(main())
