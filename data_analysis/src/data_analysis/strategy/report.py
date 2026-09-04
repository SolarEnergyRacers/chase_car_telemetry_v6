"""Text output for the point strategy.

Every line here has to survive the same test: it must be checkable on a
scrap of paper in one line. "Loop 3 tot" is not checkable; "Ankunft 17:14
statt 17:00, Loop braucht 32 min" is. So each number carries its own
derivation, and every recommendation states what binds.

Speeds come out as a handful of piecewise constant zones rounded to 5 km/h,
not as a continuous profile - the driver cannot follow a curve, and the
telemetry resolves 1 km/h in 8 bits anyway.
"""

from   datetime import timedelta
import numpy as np
import pandas as pd

from .dayplan import V_FLOOR_APPLIES_AT, V_FLOOR_KMH
from .inputs import RACE_TZ

TRUST_NOTE = {
    "anchor":      "Ruheanker",
    "anchor_weak": "Ruhemessung, aber auf dem OCV-Plateau",
    "check_only":  "unter Last gemessen - Plausibilitaetswert, kein Anker",
    "manual":      "von Hand eingegeben",
}


def _hm(td: timedelta) -> str:
    if td is None:
        return "-"
    s = int(td.total_seconds())
    sign = "-" if s < 0 else ""
    s = abs(s)
    return f"{sign}{s//3600}:{(s%3600)//60:02d}"


def _clock(t) -> str:
    return t.astimezone(RACE_TZ).strftime("%H:%M")


# ------------------------------------------------------------------ header ----

def header(state, weathers: dict, solar_check: dict = None) -> str:
    """Position, time, energy and data provenance - printed before anything.

    The provenance lines are not decoration. A plan on a hand-typed SoC and
    a plan on a settled anchor are different plans, and a seven hour old
    forecast is itself a reason to decide more cautiously.
    """
    L = []
    part_de = {"to_control": "vor dem Kontrollstopp",
               "loop": "auf einem Loop",
               "to_finish": "nach dem Kontrollstopp"}
    pos = f"{part_de.get(state.part, state.part)}"
    if state.part == "loop":
        leg = {"out": "Hinweg", "back": "Rueckweg"}.get(state.loop_leg, "?")
        pos += f", Loop {state.loop_done + 1} {leg}"
    L.append(f"Position  {pos}, km {state.km_in_part:.1f} von "
             f"{state.part_km:.1f}")
    src = state.position_source
    if state.cross_track_m is not None:
        src += f", {state.cross_track_m:.0f} m neben der Route"
    L.append(f"          {src}")

    L.append(f"Zeit      {_clock(state.t_now)} SAST · Deadline "
             f"{_clock(state.t_deadline)} · noch {_hm(state.time_left)}")

    p = state.pack
    line = (f"Energie   SOC {p.soc*100:.0f} % ({p.wh:.0f} Wh), "
            f"{p.wh_above_floor:+.0f} Wh ueber dem Boden")
    L.append(line)
    L.append(f"          {p.source} - {TRUST_NOTE.get(p.trust, p.trust)}")
    if p.reading_age_s is not None and p.reading_age_s > 60:
        L.append(f"          Messung ist {p.reading_age_s/60:.1f} min alt")

    for name, cw in weathers.items():
        age = cw.age
        age_s = "Alter unbekannt" if age is None else f"vor {_hm(age)}"
        L.append(f"Wetter    {name}: geholt {age_s}"
                 + (f" ({cw.fetched_at.astimezone(RACE_TZ):%d.%m %H:%M} SAST)"
                    if cw.fetched_at else "")
                 + f", {len(cw.weather.distance_km)} Punkte")
    L.append("          Modelllauf-Alter liefert die API nicht - der Lauf "
             "kann einige Stunden aelter sein")

    if solar_check:
        L.append(f"Solar     gemessen {solar_check['measured_w']:.0f} W · "
                 f"Prognose {solar_check['forecast_w']:.0f} W · "
                 f"{solar_check['delta_pct']:+.0f} %")

    for n in state.notes:
        L.append(f"Hinweis   {n}")
    return "\n".join(L)


# ------------------------------------------------------------------ options ----

def options_table(opts: list, batt=None) -> str:
    """The decision: one row per remaining loop count."""
    L = ["", f"{'Loops':>5} {'km':>7} {'Ø km/h':>7} {'min SOC':>8} "
             f"{'End-SOC':>8} {'Wh ü.Boden':>11} {'Zeitreserve':>12} "
             f"{'Wechsel':>8}"]
    last_ok = None
    for o in opts:
        if not o.feasible:
            L.append(f"{o.n_loops:>5} {o.km:>7.1f} "
                     f"{'—':>7} {'—':>8} {'—':>8} {'—':>11} {'—':>12} "
                     f"{'—':>8}")
            L.append(f"        nicht machbar: {o.reason}")
            break
        last_ok = o
        L.append(f"{o.n_loops:>5} {o.km:>7.1f} {o.avg_kmh:>7.1f} "
                 f"{o.min_soc*100:>7.0f} % {o.end_soc*100:>7.0f} % "
                 f"{o.wh_above_floor:>+11.0f} {_hm(o.reserve):>12} "
                 f"{len(o.driver_changes):>8}"
                 + (f"   {o.wh_spilled:>5.0f} Wh verworfen"
                    if o.wh_spilled > 5 else ""))

    if last_ok is None:
        L.append("\nKeine Option machbar - siehe Grund oben.")
        return "\n".join(L)

    L.append("")
    L.append(f"Machbar bis {last_ok.n_loops} Loop(s): {last_ok.km:.1f} km, "
             f"Ø {last_ok.avg_kmh:.1f} km/h")
    failed = [o for o in opts if not o.feasible]
    if failed:
        what = failed[0].reason.split(":")[0]
        L.append(f"Loop {failed[0].n_loops} scheitert an: {what}")
    else:
        L.append("Kein Loop in der geprueften Spanne scheitert - n_max "
                 "erhoehen")
    L.append("Die Loop-Zahl wird nicht committet: nach jedem Loop neu "
             "entscheiden.")
    return "\n".join(L)


# --------------------------------------------------------------------- plan ----

def _greedy_edges(dist_m: np.ndarray, dt_s: np.ndarray, min_m: float,
                  max_zones: int, tol: float = 0.12) -> list:
    """Split a run of segments into zones of roughly constant speed.

    Greedy, then merged down to `max_zones`. Deliberately not "split at the
    biggest jumps": single outlier segments are common in routing data (a
    17 km/h node in a village, a 5 km/h artefact at a junction) and
    splitting there produces 100-metre zones that tell the driver nothing.
    A zone has to earn its place by being long enough.

    The average within a zone is the harmonic mean over distance,
    sum(d)/sum(dt) - the only average that reproduces the zone's own
    travel time. An arithmetic mean of speeds would not.
    """
    n = len(dist_m)
    if n == 0:
        return []
    edges = [0]
    d_acc, t_acc = dist_m[0], dt_s[0]
    for i in range(1, n):
        v_zone = d_acc / t_acc
        v_seg = dist_m[i] / dt_s[i] if dt_s[i] > 0 else v_zone
        if d_acc >= min_m and abs(v_seg - v_zone) > tol * v_zone:
            edges.append(i)
            d_acc, t_acc = dist_m[i], dt_s[i]
        else:
            d_acc += dist_m[i]
            t_acc += dt_s[i]
    edges.append(n)

    # merge the most similar neighbours until the count fits
    while len(edges) - 1 > max_zones:
        speeds = [dist_m[a:b].sum() / max(dt_s[a:b].sum(), 1e-9)
                  for a, b in zip(edges[:-1], edges[1:])]
        k = int(np.argmin([abs(speeds[j + 1] - speeds[j])
                           for j in range(len(speeds) - 1)]))
        edges.pop(k + 1)
    return edges


def speed_zones(opt, max_per_leg: int = 3, min_km: float = 3.0):
    """Per-leg piecewise constant target speeds, rounded to 5 km/h.

    Zones never span a leg boundary. That is not a technical constraint but
    the point: the driver experiences the day as "to the control stop, then
    loops, then out to the finish", and a zone straddling a stop would have
    to be read twice with a pause in between.
    """
    if opt.trace is None or "km_total" not in opt.trace.columns:
        return pd.DataFrame()
    d = opt.trace[opt.trace["kind"] == "drive"]
    if d.empty:
        return pd.DataFrame()

    rows = []
    blocks = (d["leg"] != d["leg"].shift()).cumsum()
    for _, g in d.groupby(blocks, sort=False):
        dist = g["speed_kmh"].to_numpy() / 3.6 * g["dt_s"].to_numpy()   # m
        dt = g["dt_s"].to_numpy()
        for a, b in zip(*(lambda e: (e[:-1], e[1:]))(
                _greedy_edges(dist, dt, min_km * 1e3, max_per_leg))):
            dd, tt = dist[a:b].sum(), dt[a:b].sum()
            rows.append({
                "leg":    g["leg"].iloc[0],
                "bis_km": float(g["km_total"].iloc[b - 1]),
                # position within the leg as well as the running total.
                # On a loop the leg value is the useful one - "km 16.6 of
                # 22.6" says where you are, "km 189.4" does not - and at a
                # control stop the running total is what matches the
                # options table.
                "leg_km": float(dist[:b].sum()) / 1e3,
                "leg_len": float(dist.sum()) / 1e3,
                "km":     dd / 1e3,
                "v_kmh":  5.0 * round((dd / max(tt, 1e-9) * 3.6) / 5.0),
                "an":     g["time"].iloc[b - 1],
            })
    out = pd.DataFrame(rows)

    # collapse neighbours that round to the same speed within one leg
    if not out.empty:
        keep = (out["v_kmh"].ne(out["v_kmh"].shift())
                | out["leg"].ne(out["leg"].shift()))
        grp = keep.cumsum()
        out = out.groupby(grp, as_index=False).agg(
            leg=("leg", "first"), bis_km=("bis_km", "last"),
            leg_km=("leg_km", "last"), leg_len=("leg_len", "first"),
            km=("km", "sum"), v_kmh=("v_kmh", "first"), an=("an", "last"))
    return out


def plan_text(opt, state) -> str:
    """Plan mode: target speeds, standing phases, arrival times."""
    if not opt.feasible:
        return f"\nPlan mit {opt.n_loops} Loop(s) nicht machbar: {opt.reason}"

    L = ["", f"Plan mit {opt.n_loops} Loop(s): {opt.km:.1f} km, "
             f"Ø {opt.avg_kmh:.1f} km/h, Fahrzeit {_hm(opt.drive_time)}", ""]

    # one chronological list: driving zones and standing phases interleaved,
    # because that is the order they happen in and the order they get read out
    z = speed_zones(opt)
    items = [(r["an"], "drive", r) for _, r in z.iterrows()]
    stops = opt.trace[opt.trace["kind"] == "stop"]
    items += [(r["time"], "stop", r) for _, r in stops.iterrows()]
    items.sort(key=lambda x: x[0])

    last_leg = None
    for _, kind, r in items:
        if kind == "drive":
            if r["leg"] != last_leg:
                L.append(f"  {r['leg']}  ({r['leg_len']:.1f} km)")
                last_leg = r["leg"]
            L.append(f"    bis km {r['bis_km']:>6.1f}"
                     f"   im Leg {r['leg_km']:>5.1f}"
                     f"   {r['v_kmh']:>3.0f} km/h"
                     f"  ({r['km']:>5.1f} km)   an {_clock(r['an'])}")
        else:
            wh = -r["Ws"] / 3600.0
            L.append(f"  {r['leg']:<22} {r['dt_s']/60:>4.0f} min  "
                     f"{wh:>+6.0f} Wh          ab {_clock(r['time'])}")
            last_leg = None

    L.append("")
    if opt.wh_spilled > 5:
        L.append("")
        L.append(f"WARNUNG   Pack laeuft ueber: {opt.wh_spilled:.0f} Wh "
                 f"Ertrag verworfen"
                 + (f", voll ab {_clock(opt.t_full)}" if opt.t_full is not None
                    else ""))
        L.append(f"          das sind {opt.wh_spilled/14:.0f} km, die heute "
                 f"gefahren werden koennten - mehr Loops oder schneller")
    L.append("")
    L.append(f"SOC       Start {state.pack.soc*100:.0f} % → Ende "
             f"{opt.end_soc*100:.0f} %, Minimum {opt.min_soc*100:.0f} % "
             f"({opt.wh_above_floor:+.0f} Wh ueber dem Boden)")
    # end of the LAST row, not its start: with the regulation floor active
    # the last row is usually the leftover standing phase, and its start is
    # when the driving ends - not when the line is crossed
    t_end = (opt.trace["time"].iloc[-1]
             + timedelta(seconds=float(opt.trace["dt_s"].iloc[-1])))
    L.append(f"Zeit      Ziellinie {_clock(t_end)}, "
             f"Reserve bei Vollgas {_hm(opt.reserve)}")
    n_dc = len(opt.driver_changes)
    L.append(f"Stehen    {_hm(opt.stop_time)} gesamt, davon "
             f"{n_dc} Fahrerwechsel"
             + (f" (bei km "
                + ", ".join(f"{s.km:.0f}" for s in opt.driver_changes) + ")"
                if n_dc else ""))

    if opt.capped_km > 0:
        L.append(f"Modell    Routing-Deckel bindet auf {opt.capped_km:.1f} km"
                 + (f" und kostet {_hm(opt.capped_cost)}"
                    if opt.capped_cost else ""))
        L.append("          das ist eine Schaetzung des Routings, kein "
                 "gesetzliches Limit - pruefen, ob mehr geht")
    if opt.below_floor_km > 0:
        L.append(f"WARNUNG   {opt.below_floor_km:.1f} km unter "
                 f"{V_FLOOR_KMH:.0f} km/h auf Strassen mit Routing "
                 f"≥ {V_FLOOR_APPLIES_AT:.0f} km/h - Penalty-Tatbestand")
        L.append("          Routing-Geschwindigkeit als Ersatz fuer das "
                 "gesetzliche Limit, also nur ein Hinweis")
        hint = floor_alternative(opt)
        if hint:
            L.append(hint)
    return "\n".join(L)


def floor_alternative(opt) -> str:
    """If the plan crawls, say what driving the floor speed would free up.

    The reason to prefer standing is the PENALTY, not the energy. Whether it
    also collects more depends on the time of day and is not a rule of
    thumb: crawling gathers flat irradiance for longer and costs almost
    nothing extra at low speed, so around midday - where tracked sits only
    ~15 % above flat - the two come out level. Late in the day the sun is
    low, tracked/flat grows towards 1/sin(elevation), and standing wins
    clearly (measured on day 1: +285 Wh for a 16:00 stop, -3 Wh for a 13:00
    one). So this line offers the trade and names the --stop call that
    settles it; it does not claim an outcome.
    """
    if opt.seg_m is None or opt.drive_time is None:
        return ""
    v_alt = np.minimum(opt.caps_kmh, np.maximum(opt.speeds_kmh, V_FLOOR_KMH))
    t_alt = timedelta(seconds=float(np.sum(opt.seg_m / (v_alt / 3.6))))
    freed = opt.drive_time - t_alt
    if freed.total_seconds() < 300:
        return ""
    return (f"          Alternative: mindestens {V_FLOOR_KMH:.0f} km/h fahren "
            f"(Fahrzeit {_hm(t_alt)}) und die frei werdenden "
            f"{freed.total_seconds()/60:.0f} min stehend laden. Ob das auch "
            f"energetisch besser ist, haengt an der Tageszeit - mit "
            f"--stop KM:{freed.total_seconds()/60:.0f} nachrechnen")


def sweep_text(rows: list, batt, km: float, wh_ceiling: float = None) -> str:
    """The standing-phase sweep as a table, with the useful row marked.

    "morgen nutzbar" is min(end energy, ceiling): energy above the next
    morning's ceiling is worthless, since it would have arrived free in the
    morning window anyway. Which is why the best row is often NOT the one
    with the highest end SoC.
    """
    from data_analysis.simulation.battery import capacity_wh
    cap = capacity_wh(batt)

    where = (f"{abs(km):.1f} km vor dem Ziel" if km < 0
             else f"km {km:.1f}")
    L = ["", f"Standladen {where}, Dauer variiert"]
    L.append(f"{'Dauer':>7} {'Ø km/h':>7} {'Ende':>7} {'min SOC':>8} "
             f"{'verworfen':>10} {'Ladung':>9} {'morgen nutzbar':>15}")

    best, best_val = None, -1.0
    for minutes, o in rows:
        if not o.feasible:
            L.append(f"{minutes:>5} min  nicht machbar: "
                     f"{o.reason.split(':')[0]}")
            break
        s = o.trace[o.trace["leg"].astype(str).str.startswith("Standladen")]
        wh = -float(s["Ws"].sum()) / 3600.0 if len(s) else 0.0
        usable = min(o.end_wh, wh_ceiling) if wh_ceiling else o.end_wh
        # 30 Wh is about one SoC point. A longer halt means faster
        # driving, so it always costs minimum SoC on the way - and buying
        # a noise-level gain with real margin is the wrong trade. Anything
        # under a point counts as a tie, and ties go to the shorter halt.
        if usable > best_val + 30.0:
            best, best_val = minutes, usable
        L.append(f"{minutes:>5} min {o.avg_kmh:>7.1f} {o.end_soc*100:>6.0f} % "
                 f"{o.min_soc*100:>7.0f} % {o.wh_spilled:>9.0f} "
                 f"{wh:>8.0f} Wh {usable:>12.0f} Wh")

    if best is None:
        return "\n".join(L)

    L.append("")
    if wh_ceiling:
        L.append(f"Obergrenze morgen {wh_ceiling:.0f} Wh "
                 f"({100*wh_ceiling/cap:.0f} %) - darueber ist der Gewinn "
                 f"morgen frueh ohnehin weg")
    L.append(f"Bestes Verhaeltnis: {best} min "
             f"({best_val:.0f} Wh morgen nutzbar; Gewinne unter 30 Wh "
             f"gelten als gleichwertig)")
    L.append("Beachte: laengeres Stehen heisst schneller fahren, also "
             "tieferer minimaler SOC unterwegs - weniger Reserve gegen "
             "Bewoelkung.")
    return "\n".join(L)


def trigger_text(opts: list, state) -> str:
    """The line the strategist actually uses: when does the next loop die.

    A go/no-go threshold is usable at a control stop in a way a curve is
    not. It comes out of the difference between two evaluated options, so
    it needs no extra model - only the reserve of the last feasible one.
    """
    ok = [o for o in opts if o.feasible]
    if not ok:
        return ""
    last = ok[-1]
    nxt = [o for o in opts if not o.feasible]
    L = ["", "Trigger"]
    if nxt and last.reserve is not None:
        cost = (nxt[0].t_min - last.t_min) if nxt[0].t_min else None
        if cost:
            latest = state.t_now + (last.reserve - cost)
            L.append(f"  Loop {nxt[0].n_loops} lebt nur, wenn ab hier "
                     f"{_hm(cost)} frueher als geplant gefahren wird "
                     f"(Aufbruch bis {_clock(latest)})")
    L.append(f"  Loop {last.n_loops} braucht {_hm(last.t_min)} bei Vollgas, "
             f"Fenster {_hm(state.time_left)} → Reserve {_hm(last.reserve)}")
    L.append(f"  und mindestens {last.min_soc*100:.0f} % SOC im Tief "
             f"({last.wh_above_floor:+.0f} Wh ueber dem Boden)")
    return "\n".join(L)
