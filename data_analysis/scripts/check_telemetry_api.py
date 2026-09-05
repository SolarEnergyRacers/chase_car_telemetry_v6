#!/usr/bin/env python3
"""Prueft die Telemetrie-API der SER-Live-Monitoring-App.

Rolle im Repo: dieses Skript sagt, was die API TATSAECHLICH liefert - Rohantwort,
Spalteninventar, Zeitstempel-Kontinuitaet, Wertebereiche, GPS-Spur. Es filtert
nichts, korrigiert nichts und maskiert nichts. Widerspricht es
src/data_analysis/telemetry/ser_client.py, gilt dieses Skript, und der Client
wird korrigiert.

Deshalb bewusst nur Standardbibliothek: laeuft auf einem frischen Laptop ohne
venv und ohne pip install, also auch dann, wenn sonst nichts laeuft.
Keine Importe aus data_analysis - nie welche hinzufuegen.

Abgedeckte Endpoints:
    GET /api/timeseries?start&end             CSV, Unix-Sekunden
    GET /api/timeseries/range?from&to&series  JSON, ISO 8601
    GET /api/gps/latest?deviceName
    GET /api/gps/range?from&to&deviceName

Alle Zeiten werden als UTC ausgegeben und mit explizitem "Z" abgefragt.

Beispiele:
    # letzte 10 Minuten: CSV-Bericht + GPS
    python scripts/check_telemetry_api.py --host 192.168.1.50:5240

    # denselben Bereich ueber den JSON-Endpoint
    python scripts/check_telemetry_api.py --host 192.168.1.50 --json

    # fester UTC-Bereich, jede Zeile ausgeben
    python scripts/check_telemetry_api.py --host 192.168.1.50 \\
        --start 2026-09-03T08:00 --end 2026-09-03T08:30 --print-rows

    # Livebetrieb: Cursor-Polling wie im Rennen (from ohne to)
    python scripts/check_telemetry_api.py --host 192.168.1.50 --watch

    # Konformitaetspruefungen (Cursor, Zeitzone, Reihenfolge, Fehlerfaelle)
    python scripts/check_telemetry_api.py --host 192.168.1.50 --selftest
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

DEFAULT_PORT = 5240
DEFAULT_DEVICE = "honor"          # GPS-Logger, der immer im Auto ist

# Die sieben Reihen, mit denen die Strategie rechnet. motor_current/voltage/power
# fehlen absichtlich: der Motorcontroller ist nicht am Bus, sie sind rechnerisch
# aus Batterie und MPPT gebildet und tragen keine eigene Information.
STRATEGY_SERIES = [
    "speed",
    "mppt1_power", "mppt2_power", "mppt3_power", "mppt4_power",
    "battery_voltage", "battery_current",
]

# Nur zur Einordnung im Bericht - das Skript verwirft nichts deswegen.
PLAUSIBLE = {
    "speed": (0.0, 160.0, "km/h"),
    "battery_voltage": (70.0, 135.0, "V"),
    "battery_current": (-120.0, 120.0, "A"),
    "battery_power": (-15000.0, 15000.0, "W"),
    "motor_voltage": (0.0, 140.0, "V"),
    "motor_current": (-200.0, 200.0, "A"),
    "motor_power": (-20000.0, 20000.0, "W"),
    "mppt1_power": (0.0, 500.0, "W"),
    "mppt2_power": (0.0, 500.0, "W"),
    "mppt3_power": (0.0, 500.0, "W"),
    "mppt4_power": (0.0, 500.0, "W"),
}


# ---------------------------------------------------------------- HTTP

def normalize_host(host: str) -> str:
    """Ergaenzt den Standardport, wenn nur eine IP angegeben wurde."""
    host = host.strip().rstrip("/")
    for prefix in ("http://", "https://"):
        if host.startswith(prefix):
            host = host[len(prefix):]
    return host if ":" in host else f"{host}:{DEFAULT_PORT}"


def http_get(url: str, timeout: float) -> tuple[int, str, float]:
    """Gibt (Status, Body, Dauer). Status 0 heisst: gar keine Antwort.

    Netzfehler werden nicht geworfen, sondern zurueckgegeben - im Rennen faellt
    das WLAN aus, und ein Traceback beendet dann die Ueberwachung.
    """
    started = time.monotonic()
    request = urllib.request.Request(url, headers={"Accept": "text/csv, application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, body, time.monotonic() - started
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        return error.code, body, time.monotonic() - started
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        reason = getattr(error, "reason", error)
        return 0, f"kein Kontakt: {reason}", time.monotonic() - started


def build_url(host: str, path: str, params: dict) -> str:
    clean = {k: v for k, v in params.items() if v is not None}
    return f"http://{host}{path}?{urllib.parse.urlencode(clean)}"


def get(host: str, path: str, params: dict, timeout: float,
        verbose: bool = True) -> tuple[str, int, str]:
    url = build_url(host, path, params)
    if verbose:
        print(f"GET {url}")
    status, body, elapsed = http_get(url, timeout)
    if verbose:
        print(f"  -> HTTP {status}, {len(body)} Bytes, {elapsed * 1000:.0f} ms")
    return url, status, body


def get_or_die(host: str, path: str, params: dict, timeout: float,
               verbose: bool = True) -> str:
    url, status, body = get(host, path, params, timeout, verbose)
    if status == 200:
        return body
    if status == 0:
        raise SystemExit(
            f"\nAbbruch: {body}\n"
            f"  Laeuft die App auf dem anderen Laptop? Firewall offen fuer Port "
            f"{host.split(':')[-1]}?\n  Gegenprobe im Browser: {url}")
    raise SystemExit(f"\nAbbruch: HTTP {status} von {url}\nAntwort: {body[:400]}")


# ---------------------------------------------------------------- Zeit

def z(moment: datetime) -> str:
    """ISO 8601 mit explizitem Z.

    Ohne Offset faellt UtcRangeTimestampParser auf die Zeitzone des SERVERS
    zurueck - dann haengt das Ergebnis daran, wie der Telemetrie-Laptop gestellt
    ist. Aus Code also nie ohne Z und nie gekuerzt (gekuerzte Angaben werden auf
    den Anfang der Einheit abgerundet, auch bei "to").
    """
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(raw: str) -> datetime | None:
    """Toleranter ISO-Leser fuer die Antworten der App.

    /api/timeseries/range liefert Kind=Utc, also "...Z".
    /api/gps/* liefert Kind=Local, also "...+02:00" auf einem SAST-Laptop.
    Fehlt beides, ist es Serverzeit - dann kann dieses Skript nicht wissen, wie
    viel das in UTC ist, und meldet None statt zu raten.
    """
    text = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def iso(unix_seconds: float) -> str:
    return datetime.fromtimestamp(unix_seconds, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- Parsen

def parse_csv(text: str):
    """CSV -> (Datenspalten, [(unix_s, {spalte: wert|None})]).

    Leere Zellen werden zu None. Die App laesst eine Zelle leer, wenn eine
    Reihe zu diesem Zeitpunkt keinen Wert hat - das ist NICHT dasselbe wie 0.
    """
    reader = csv.reader(io.StringIO(text))
    try:
        header = [h.strip() for h in next(reader)]
    except StopIteration:
        return [], []

    if not header or header[0] != "timestamp":
        raise SystemExit(f"Unerwarteter Header, erwartet 'timestamp' zuerst: {header}")

    columns = header[1:]
    rows = []

    for line_no, raw in enumerate(reader, start=2):
        if not raw or not raw[0].strip():
            continue
        try:
            timestamp = int(raw[0])
        except ValueError:
            print(f"  Zeile {line_no}: Zeitstempel '{raw[0]}' nicht lesbar", file=sys.stderr)
            continue
        values = {}
        for index, column in enumerate(columns, start=1):
            cell = raw[index].strip() if index < len(raw) else ""
            try:
                values[column] = float(cell) if cell else None
            except ValueError:
                values[column] = None
        rows.append((timestamp, values))

    return columns, rows


def parse_range_json(text: str):
    """JSON von /api/timeseries/range -> dieselbe Form wie parse_csv.

    Die Werte werden ueber das ZURUECKGEGEBENE series-Array zugeordnet, nicht
    ueber die angefragte Reihenfolge: TimeseriesJsonBuilder baut die Spalten aus
    den Keys von DataManager._series, nicht aus dem series-Parameter.
    """
    payload = json.loads(text)
    columns = list(payload.get("series") or [])
    rows = []

    for point in payload.get("points") or []:
        moment = parse_iso(str(point.get("timestamp", "")))
        if moment is None:
            continue
        values = point.get("values") or []
        if len(values) != len(columns):
            raise SystemExit(
                f"Antwort inkonsistent: {len(columns)} Reihen, aber {len(values)} Werte "
                f"bei {point.get('timestamp')}")
        rows.append((int(moment.timestamp()),
                     {c: (float(v) if v is not None else None)
                      for c, v in zip(columns, values)}))

    return columns, rows


def parse_gps_json(text: str):
    payload = json.loads(text)
    points = payload if isinstance(payload, list) else [payload]
    out = []
    for raw in points:
        if not isinstance(raw, dict):
            continue
        lower = {k.lower(): v for k, v in raw.items()}
        out.append({
            "id": lower.get("id"),
            "time": parse_iso(str(lower.get("timestamp", ""))),
            "time_raw": lower.get("timestamp"),
            "lat": lower.get("latitude"),
            "lon": lower.get("longitude"),
            "speed": lower.get("speedkmh"),
            "hdop": lower.get("accuracymeters"),
            "device": lower.get("devicename"),
        })
    return out


# ---------------------------------------------------------------- Bericht

def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def report_raw(text: str, lines: int = 4) -> None:
    section("Rohantwort (Kopf)")
    for line in text.splitlines()[:lines]:
        print(f"  {line[:200]}")
    total = len(text.splitlines())
    if total > lines:
        print(f"  ... ({total} Zeilen insgesamt)")


def report_range(rows, start: datetime, end) -> None:
    section("Bereich")
    end_text = end.strftime("%Y-%m-%d %H:%M:%S") if end else "offen (bis jetzt)"
    print(f"  angefragt   {start.strftime('%Y-%m-%d %H:%M:%S')} .. {end_text} UTC")
    if not rows:
        print("  geliefert   keine Zeilen")
        return

    first, last = rows[0][0], rows[-1][0]
    print(f"  geliefert   {iso(first)} .. {iso(last)} UTC  ({last - first + 1} s Spanne)")
    print(f"  Zeilen      {len(rows)}")

    # Die App kann mehr liefern als angefragt (SliceRange klemmt den Startindex,
    # nicht die Laenge). Wer clientseitig nicht nachschneidet, rechnet mit
    # Sekunden, die er nicht bestellt hat.
    before = sum(1 for ts, _ in rows if ts < int(start.timestamp()))
    after = sum(1 for ts, _ in rows if end and ts > int(end.timestamp()))
    if before or after:
        print(f"  ACHTUNG     {before} Zeilen vor 'start', {after} nach 'end' - nachschneiden")

    if any(rows[i][0] <= rows[i - 1][0] for i in range(1, len(rows))):
        print("  ACHTUNG     Zeitstempel nicht streng monoton steigend")


def report_columns(columns, rows):
    section("Spalten")
    if not rows:
        print("  " + (", ".join(columns) if columns else "keine"))
        return [], []

    width = max(len(c) for c in columns)
    header = (f"  {'Spalte'.ljust(width)}  {'Werte':>6} {'leer':>6} {'null':>6}   "
              f"{'min':>10} {'max':>10} {'Mittel':>10}   Status")
    print(header)
    print("  " + "-" * (len(header) - 2))

    empty_columns, zero_columns, active = [], [], []

    for column in columns:
        series = [v for _, values in rows if values.get(column) is not None
                  for v in [values[column]]]
        n_empty = len(rows) - len(series)
        n_zero = sum(1 for v in series if v == 0.0)

        if not series:
            empty_columns.append(column)
            print(f"  {column.ljust(width)}  {0:>6} {n_empty:>6} {0:>6}   "
                  f"{'-':>10} {'-':>10} {'-':>10}   LEER - Kanal nie gesehen")
            continue

        active.append(column)
        low, high, unit = PLAUSIBLE.get(column, (None, None, ""))
        minimum, maximum, mean = min(series), max(series), statistics.fmean(series)

        if all(v == 0.0 for v in series):
            status = "NUR NULL"
            zero_columns.append(column)
        elif low is not None and (minimum < low or maximum > high):
            status = f"ausserhalb {low:g}..{high:g} {unit}"
        else:
            status = f"ok {unit}".strip()

        print(f"  {column.ljust(width)}  {len(series):>6} {n_empty:>6} {n_zero:>6}   "
              f"{minimum:>10.3f} {maximum:>10.3f} {mean:>10.3f}   {status}")

    if empty_columns:
        print("\n  LEER bedeutet: kein einziges Sample. Kein Sensor, Geraet aus, oder")
        print("  CAN-Adresse passt nicht zu den CanAddressSettings in /settings.")
        print(f"  Betroffen: {', '.join(empty_columns)}")
    if zero_columns:
        print("\n  NUR NULL bedeutet: Samples kommen an, sind aber alle 0.0. Anderer")
        print("  Fehler als LEER - der Kanal wird dekodiert, liefert aber nichts.")
        print(f"  Betroffen: {', '.join(zero_columns)}")
    return active, empty_columns


def report_continuity(rows, max_listed: int = 5) -> None:
    section("Zeitstempel-Kontinuitaet")
    if len(rows) < 2:
        print("  zu wenige Zeilen fuer eine Aussage")
        return

    gaps = [(rows[i - 1][0], rows[i][0], rows[i][0] - rows[i - 1][0] - 1)
            for i in range(1, len(rows)) if rows[i][0] - rows[i - 1][0] > 1]

    expected = rows[-1][0] - rows[0][0] + 1
    print(f"  erwartet {expected} Sekunden, vorhanden {len(rows)}, "
          f"fehlend {expected - len(rows)}")

    if not gaps:
        print("  keine Luecken - jede Sekunde im gelieferten Bereich ist vertreten")
        return

    print(f"  {len(gaps)} Luecken, groesste {max(g[2] for g in gaps)} s")
    for begin, finish, length in gaps[:max_listed]:
        print(f"    {iso(begin)} -> {iso(finish)}   {length} s fehlen")
    if len(gaps) > max_listed:
        print(f"    ... {len(gaps) - max_listed} weitere")


def report_fill_suspicion(active, rows, min_run: int = 6) -> None:
    """Sucht Laeufe, in denen alle vorhandenen Kanaele gleichzeitig genau 0.0 sind.

    Die App fuellt Luecken ueber 5 s mit 0.0 statt NaN (TimeSeries.AddAndInterpolate,
    _fillValue ist fuer alle elf Reihen 0.0). Solche Sekunden sehen wie Messwerte
    aus, sind aber keine. Das "null" im JSON hilft dabei nicht - es markiert nur
    Reihen ohne Sample zu einem Zeitpunkt, den eine andere Reihe hat.

    Sauberster Indikator waere battery_voltage == 0 (mit geschlossenen Schuetzen
    unmoeglich). Fehlt die Batterie im Aufbau, greift nur die schwaechere
    Variante ueber alle nicht-leeren Kanaele.
    """
    section("Verdacht auf aufgefuellte Luecken")
    if not active or not rows:
        print("  keine auswertbaren Kanaele")
        return

    runs, run_start, run_length = [], None, 0
    for timestamp, values in rows:
        present = [values[c] for c in active if values.get(c) is not None]
        if present and all(v == 0.0 for v in present):
            run_start = timestamp if run_length == 0 else run_start
            run_length += 1
        else:
            if run_length >= min_run:
                runs.append((run_start, run_length))
            run_length = 0
    if run_length >= min_run:
        runs.append((run_start, run_length))

    print(f"  geprueft auf: {', '.join(active)}")
    if "battery_voltage" not in active:
        print("  Hinweis: battery_voltage fehlt, deshalb nur die schwache Pruefung.")
    if not runs:
        print(f"  keine Laeufe >= {min_run} s mit durchgehend 0.0 auf allen Kanaelen")
        return

    word = "Lauf" if len(runs) == 1 else "Laeufe"
    print(f"  {len(runs)} {word} >= {min_run} s mit durchgehend 0.0 auf allen Kanaelen:")
    for begin, length in runs[:5]:
        print(f"    {iso(begin)}   {length} s")
    if len(runs) > 5:
        print(f"    ... {len(runs) - 5} weitere")
    print("  Steht das Fahrzeug wirklich still, ist das echt. Sonst sind es")
    print("  aufgefuellte Funkloecher, und ein Energieintegral zaehlt sie mit.")


def print_rows(columns, rows, limit) -> None:
    section("Zeilen")
    if not columns:
        print("  keine Spalten")
        return
    width = max(11, max(len(c) for c in columns))
    print("  " + "ZeitUTC".ljust(19) + "".join(c.rjust(width + 1) for c in columns))
    shown = rows if limit is None else rows[:limit]
    for timestamp, values in shown:
        cells = "".join(
            ("-" if values.get(c) is None else f"{values[c]:.3f}").rjust(width + 1)
            for c in columns)
        print("  " + iso(timestamp).ljust(19) + cells)
    if limit is not None and len(rows) > limit:
        print(f"  ... {len(rows) - limit} weitere Zeilen (--print-rows 0 zeigt alle)")


# ---------------------------------------------------------------- GPS

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def report_gps(host: str, start: datetime, end, device: str, timeout: float) -> None:
    section(f"GPS - Geraet '{device}'")

    _, status, body = get(host, "/api/gps/latest", {"deviceName": device}, timeout)
    if status == 404:
        print(f"  Kein Punkt fuer Geraet '{device}'.")
        # Ohne deviceName liefert die App den letzten Punkt UNABHAENGIG vom Geraet.
        # Damit lassen sich zwei sonst gleich aussehende Fehler trennen: kommt hier
        # etwas, ist GPS grundsaetzlich da und nur der Name passt nicht; kommt auch
        # hier nichts, erreicht kein einziger Punkt die App.
        _, any_status, any_body = get(host, "/api/gps/latest", {}, timeout, verbose=False)
        if any_status == 200:
            other = parse_gps_json(any_body)
            name = other[0]["device"] if other else None
            print(f"  Ungefiltert kommt aber ein Punkt zurueck, von Geraet {name!r}.")
            print(f"  Also erreicht GPS die App - nur '{device}' meldet nicht (oder")
            print("  meldet unter anderem Namen). Namen in OsmAnd pruefen.")
        else:
            print("  Auch ungefiltert kein Punkt - es erreicht kein GPS die App.")
            print("  Haeufigste Ursache seit dem API-Update: 'device' ist im Endpoint")
            print("  Pflicht, fehlt aber in der dokumentierten OsmAnd-URL - dann wird")
            print("  jeder Punkt mit HTTP 400 abgewiesen und nur auf der App-Konsole")
            print(f"  protokolliert. URL um '&device={device}' ergaenzen.")
    elif status != 200:
        print(f"  HTTP {status}: {body[:200]}")
    else:
        latest = parse_gps_json(body)
        if not latest:
            print("  Antwort ohne verwertbaren Punkt")
        else:
            point = latest[0]
            if point["time"] is None:
                print(f"  Zeitstempel ohne Zone: {point['time_raw']!r} - Serverzeit, "
                      "nicht umrechenbar")
            else:
                age = (datetime.now(timezone.utc) - point["time"]).total_seconds()
                stamp = point["time"].astimezone(timezone.utc)
                print(f"  letzter Fix   {stamp:%Y-%m-%d %H:%M:%S} UTC   Alter {age:.0f} s")
            print(f"  Position      {point['lat']}, {point['lon']}")
            print(f"  Geraetename   {point['device']!r}")
            print(f"  SpeedKmh      {point['speed']}   AccuracyMeters {point['hdop']} "
                  "(bei OsmAnd der HDOP, keine Meter)")

    params = {"from": z(start), "deviceName": device}
    if end is not None:
        params["to"] = z(end)
    _, status, body = get(host, "/api/gps/range", params, timeout)
    if status != 200:
        print(f"  /range: HTTP {status}: {body[:200]}")
        return

    points = [p for p in parse_gps_json(body) if p["time"] and p["lat"] is not None]
    print(f"  Punkte im Fenster: {len(points)}")
    if len(points) >= 2:
        points.sort(key=lambda p: p["time"])
        deltas = [(points[i]["time"] - points[i - 1]["time"]).total_seconds()
                  for i in range(1, len(points))]
        distance = sum(haversine_km(points[i - 1]["lat"], points[i - 1]["lon"],
                                    points[i]["lat"], points[i]["lon"])
                       for i in range(1, len(points)))
        print(f"  Abstaende     Median {statistics.median(deltas):.1f} s, "
              f"max {max(deltas):.0f} s, min {min(deltas):.1f} s")
        print(f"  Strecke       {distance:.3f} km (Haversine, ohne Hoehe)")
        if max(deltas) > 60:
            print("  ACHTUNG       Luecke > 60 s - fuer den Odometer relevant, weil")
            print("                die Sehne die Strecke in Kurven unterschaetzt.")

    # Ohne deviceName-Filter, um zu sehen, welche Geraete ueberhaupt melden.
    params.pop("deviceName")
    _, status, body = get(host, "/api/gps/range", params, timeout, verbose=False)
    if status == 200:
        names = sorted({str(p["device"]) for p in parse_gps_json(body)})
        print(f"  Geraete im Fenster: {', '.join(repr(n) for n in names) or '-'}")
        if "" in names:
            print("  ACHTUNG       Ein Geraet meldet ohne Namen - alte Punkte oder ein")
            print("                Reporter ohne 'device'. Spuren nicht trennbar.")
        if len([n for n in names if n]) > 1:
            print("  ACHTUNG       Mehr als ein Geraet meldet. Ungefiltert liefern")
            print("                /latest und /range alle Geraete gemischt - fuer den")
            print("                Odometer immer den Namen des Autos setzen.")


def poll_gps(host: str, device: str, timeout: float):
    """Holt den letzten Fix fuer ein Geraet. Gibt (Status, Punkt|None) zurueck."""
    _, status, body = get(host, "/api/gps/latest", {"deviceName": device},
                          timeout, verbose=False)
    if status != 200:
        return status, None
    try:
        points = parse_gps_json(body)
    except json.JSONDecodeError:
        return status, None
    return status, points[0] if points else None


# ---------------------------------------------------------------- Selbsttest

class Checks:
    def __init__(self) -> None:
        self.results = []

    def add(self, name: str, ok, detail: str) -> None:
        self.results.append((name, ok, detail))
        mark = {True: "OK  ", False: "FEHL", None: "INFO"}[ok]
        print(f"  [{mark}] {name}")
        for line in detail.splitlines():
            print(f"         {line}")

    def summary(self) -> int:
        failed = sum(1 for _, ok, _ in self.results if ok is False)
        info = sum(1 for _, ok, _ in self.results if ok is None)
        passed = len(self.results) - failed - info
        section("Ergebnis")
        print(f"  {passed} ok, {failed} fehlgeschlagen, {info} nur informativ")
        return 1 if failed else 0


def run_selftest(host: str, timeout: float) -> int:
    """Prueft die Zusagen der API, die der Client spaeter voraussetzt."""
    section("Konformitaetspruefungen")
    checks = Checks()
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=5)
    all_series = ",".join(STRATEGY_SERIES)

    # 1 - Unbekannte Reihe muss 400 mit Auflistung geben. Der Client nutzt das
    #     beim Start, um eine geaenderte Kanalmenge zu bemerken.
    _, status, body = get(host, "/api/timeseries/range",
                          {"from": z(window_start), "series": "gibt_es_nicht"},
                          timeout, verbose=False)
    checks.add("Unbekannte Reihe -> 400 mit Liste",
               status == 400 and "vailable series" in body,
               f"HTTP {status}: {body[:160]}")

    # 2 - from ohne to muss offen bis zum neuesten Datensatz gehen.
    _, status, body = get(host, "/api/timeseries/range",
                          {"from": z(window_start), "series": all_series},
                          timeout, verbose=False)
    open_rows = []
    if status != 200:
        checks.add("from ohne to (offenes Ende)", False, f"HTTP {status}: {body[:160]}")
    else:
        _, open_rows = parse_range_json(body)
        checks.add("from ohne to (offenes Ende)", bool(open_rows),
                   f"{len(open_rows)} Punkte" if open_rows
                   else "keine Daten - laeuft die Telemetrie gerade?")

    # 3 - Cursor: ab dem letzten Zeitstempel darf nichts Aelteres mehr kommen.
    if open_rows:
        cursor = datetime.fromtimestamp(open_rows[-1][0], timezone.utc) + timedelta(seconds=1)
        _, status, body = get(host, "/api/timeseries/range",
                              {"from": z(cursor), "series": all_series},
                              timeout, verbose=False)
        if status != 200:
            checks.add("Cursor (from = letzter Stand + 1 s)", False, f"HTTP {status}")
        else:
            _, fresh = parse_range_json(body)
            stale = [ts for ts, _ in fresh if ts < int(cursor.timestamp())]
            detail = f"{len(fresh)} neue Punkte, {len(stale)} davon aelter als der Cursor"
            if not fresh:
                detail += ("\n0 neu ist hier erwartbar - der Cursor liegt eine Sekunde"
                           "\nhinter dem letzten Stand. Geprueft ist, dass nichts"
                           "\nAelteres mitkommt.")
            checks.add("Cursor (from = letzter Stand + 1 s)", not stale, detail)
    else:
        checks.add("Cursor (from = letzter Stand + 1 s)", None, "uebersprungen, keine Daten")

    # 4 - Reihenfolge in values: wie angefragt oder wie in DataManager._series?
    reversed_series = list(reversed(STRATEGY_SERIES))
    _, status, body = get(host, "/api/timeseries/range",
                          {"from": z(window_start), "series": ",".join(reversed_series)},
                          timeout, verbose=False)
    if status != 200:
        checks.add("Reihenfolge in 'values'", None, f"HTTP {status}")
    else:
        columns, _ = parse_range_json(body)
        detail = ("folgt der Anfrage" if columns == reversed_series
                  else "folgt NICHT der Anfrage, sondern DataManager._series")
        checks.add("Reihenfolge in 'values'", None,
                   f"{detail}\nangefragt:  {','.join(reversed_series)}"
                   f"\ngeliefert:  {','.join(columns)}"
                   "\nDer Client muss ueber das series-Array zuordnen.")

    # 5 - Zeitzone: dieselbe Grenze mit Z und ohne. Weichen die Ergebnisse ab,
    #     laeuft der Server nicht auf UTC, und eine Abfrage ohne Z waere um den
    #     Offset verschoben.
    naive = window_start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    _, status_a, body_a = get(host, "/api/timeseries/range",
                              {"from": z(window_start), "series": "speed"},
                              timeout, verbose=False)
    _, status_b, body_b = get(host, "/api/timeseries/range",
                              {"from": naive, "series": "speed"},
                              timeout, verbose=False)
    if status_a != 200 or status_b != 200:
        checks.add("Zeitzone: mit Z vs. ohne Z", None, f"HTTP {status_a}/{status_b}")
    else:
        _, rows_a = parse_range_json(body_a)
        _, rows_b = parse_range_json(body_b)
        detail = f"mit Z: {len(rows_a)} Punkte, ohne Z: {len(rows_b)} Punkte"
        if len(rows_a) != len(rows_b):
            detail += ("\nServer laeuft nicht auf UTC. Immer mit Z abfragen - ohne Z"
                       "\ngilt die Zeitzone des Telemetrie-Laptops.")
        checks.add("Zeitzone: mit Z vs. ohne Z", None, detail)

    # 6 - Gekuerztes 'to' wird auf Mitternacht abgerundet, nicht auf Tagesende.
    today = now.strftime("%Y-%m-%d")
    midnight = datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    _, status, body = get(host, "/api/timeseries/range",
                          {"from": z(now - timedelta(days=1)), "to": today, "series": "speed"},
                          timeout, verbose=False)
    if status != 200:
        checks.add("Gekuerztes 'to' wird abgerundet", None, f"HTTP {status}")
    else:
        _, rows = parse_range_json(body)
        after = [ts for ts, _ in rows if ts >= int(midnight.timestamp())]
        checks.add("Gekuerztes 'to' wird abgerundet", None,
                   f"to={today} liefert {len(rows)} Punkte, davon {len(after)} nach "
                   "Mitternacht UTC\nAus Code nie kuerzen - immer volles ISO mit Z.")

    # 7 - CSV und JSON muessen fuer dasselbe Fenster uebereinstimmen.
    _, status_c, body_c = get(host, "/api/timeseries",
                              {"start": int(window_start.timestamp()),
                               "end": int(now.timestamp())}, timeout, verbose=False)
    _, status_j, body_j = get(host, "/api/timeseries/range",
                              {"from": z(window_start), "to": z(now)}, timeout, verbose=False)
    if status_c != 200 or status_j != 200:
        checks.add("CSV und JSON stimmen ueberein", None,
                   f"HTTP {status_c}/{status_j} - einer der Endpoints antwortet nicht")
    else:
        _, csv_rows = parse_csv(body_c)
        _, json_rows = parse_range_json(body_j)
        csv_map, json_map = dict(csv_rows), dict(json_rows)
        shared = sorted(set(csv_map) & set(json_map))
        diffs = [ts for ts in shared
                 if any(csv_map[ts].get(k) != json_map[ts].get(k) for k in json_map[ts])]
        checks.add("CSV und JSON stimmen ueberein", not diffs,
                   f"CSV {len(csv_rows)} Zeilen, JSON {len(json_rows)} Punkte, "
                   f"{len(shared)} gemeinsame Sekunden, {len(diffs)} mit Abweichung")

    return checks.summary()


# ---------------------------------------------------------------- Betriebsarten

def run_once(args, start: datetime, end: datetime) -> None:
    if args.json:
        params = {"from": z(start), "to": z(end)}
        if args.series:
            params["series"] = args.series
        text = get_or_die(args.host, "/api/timeseries/range", params, args.timeout)
        columns, rows = parse_range_json(text)
    else:
        text = get_or_die(args.host, "/api/timeseries",
                          {"start": int(start.timestamp()), "end": int(end.timestamp())},
                          args.timeout)
        columns, rows = parse_csv(text)

    if args.save:
        with open(args.save, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(f"  Rohantwort gespeichert: {args.save}")

    report_raw(text)
    report_range(rows, start, end)
    active, _ = report_columns(columns, rows)
    report_continuity(rows)
    report_fill_suspicion(active, rows)

    if args.print_rows is not None:
        print_rows(columns, rows, None if args.print_rows == 0 else args.print_rows)

    if not args.no_gps:
        report_gps(args.host, start, end, args.device, args.timeout)

    section("Fazit")
    if not rows:
        print("  Keine Daten im Fenster. Laeuft die App, ist der Serial-Port auf der")
        print("  Home-Seite verbunden, und stimmt die Uhr auf beiden Rechnern?")
    else:
        print(f"  {len(rows)} Zeilen, {len(active)} von {len(columns)} Kanaelen mit Daten.")
        print(f"  Mit Daten:  {', '.join(active) or '-'}")
        print(f"  Ohne Daten: {', '.join(c for c in columns if c not in active) or '-'}")


def run_watch(args) -> None:
    """Cursor-Polling, wie es im Rennen laufen wuerde.

    from = letzter gesehener Zeitstempel + 1 s, kein to. Das ueberlappende
    Fenster frueherer Fassungen ist damit unnoetig; die Deduplizierung bleibt
    trotzdem drin, weil from inklusiv ist und ein Abruf zweimal losgehen kann.

    GPS wird pro Durchgang einmal ueber /api/gps/latest geholt und auf einer
    eigenen Zeile ausgegeben - Position, Geschwindigkeit, Alter des Fix und die
    seit Beginn aufsummierte Strecke. Letztere ist die Groesse, an der spaeter
    der Odometer haengt, also lohnt es, sie schon im Test zu sehen.
    """
    series = args.series or ",".join(STRATEGY_SERIES)
    print(f"Livebetrieb: alle {args.interval:g} s ab dem letzten Stand, kein 'to'.")
    print(f"Reihen: {series}")
    if not args.no_gps:
        print(f"GPS: /api/gps/latest?deviceName={args.device}")
    print("Abbruch mit Strg+C.\n")

    cursor = datetime.now(timezone.utc) - timedelta(seconds=args.backfill)
    seen_until = 0
    total = 0
    gps_last = None          # letzter Fix mit anderer Id
    gps_km = 0.0
    gps_quiet = False        # 404 nur einmal ausfuehrlich melden

    try:
        while True:
            now = datetime.now(timezone.utc)
            _, status, text = get(args.host, "/api/timeseries/range",
                                  {"from": z(cursor), "series": series},
                                  args.timeout, verbose=False)
            if status != 200:
                detail = text.strip().splitlines()[0] if text.strip() else ""
                print(f"{now:%H:%M:%S}  Abruf fehlgeschlagen (HTTP {status}) {detail[:80]}")
                time.sleep(args.interval)
                continue

            try:
                columns, rows = parse_range_json(text)
            except (json.JSONDecodeError, SystemExit) as error:
                print(f"{now:%H:%M:%S}  Antwort nicht lesbar: {error}")
                time.sleep(args.interval)
                continue

            can_speed = None
            fresh = [(ts, values) for ts, values in rows if ts > seen_until]
            if fresh:
                seen_until = fresh[-1][0]
                cursor = datetime.fromtimestamp(seen_until, timezone.utc) + timedelta(seconds=1)
                total += len(fresh)
                timestamp, values = fresh[-1]
                can_speed = values.get("speed")
                shown = [c for c in columns if any(v.get(c) is not None for _, v in rows)]
                snapshot = "  ".join(
                    f"{c}={'-' if values.get(c) is None else format(values[c], '.1f')}"
                    for c in shown)
                age = int(now.timestamp()) - timestamp
                print(f"{now:%H:%M:%S}  +{len(fresh):>3} s  Alter {age:>2} s  "
                      f"gesamt {total:>5}  |  {snapshot}")
            else:
                print(f"{now:%H:%M:%S}  keine neuen Sekunden (gesamt {total})")

            if not args.no_gps:
                gps_status, point = poll_gps(args.host, args.device, args.timeout)
                if point is None:
                    if not gps_quiet:
                        print(f"          GPS  kein Fix fuer '{args.device}' "
                              f"(HTTP {gps_status}) - ungefiltert pruefen, ob ueberhaupt")
                        print("               Punkte ankommen; sonst fehlt 'device' in der")
                        print("               OsmAnd-URL und jeder Punkt wird abgewiesen.")
                        gps_quiet = True
                    else:
                        print(f"          GPS  weiterhin kein Fix (HTTP {gps_status})")
                else:
                    gps_quiet = False
                    # Neuer Fix? Bevorzugt ueber die Id, ersatzweise ueber den
                    # Zeitstempel - falls eine kuenftige Antwort ohne Id kommt.
                    def fix_key(p):
                        return p.get("id") if p.get("id") is not None else p.get("time_raw")

                    is_new = gps_last is None or fix_key(point) != fix_key(gps_last)
                    if (is_new and gps_last is not None
                            and point["lat"] is not None and gps_last["lat"] is not None):
                        gps_km += haversine_km(gps_last["lat"], gps_last["lon"],
                                               point["lat"], point["lon"])
                    if is_new:
                        gps_last = point

                    if point["time"] is None:
                        age_text = "Fix ohne Zone"
                        fix_age = None
                    else:
                        fix_age = (now - point["time"]).total_seconds()
                        age_text = f"Fix {fix_age:.0f} s alt"
                        if fix_age > args.gps_stale:
                            age_text += "  ACHTUNG"

                    speed_text = ("-" if point["speed"] is None
                                  else f"{float(point['speed']):.1f} km/h")
                    # Der Vergleich mit der CAN-Geschwindigkeit lohnt nur, wenn der
                    # Fix frisch ist - sonst vergleicht man zwei Zeitpunkte.
                    if (can_speed is not None and point["speed"] is not None
                            and fix_age is not None and fix_age <= 5):
                        delta = float(point["speed"]) - can_speed
                        speed_text += f" (CAN {can_speed:.0f}, delta {delta:+.1f})"

                    print(f"          GPS  {point['lat']:.6f}, {point['lon']:.6f}   "
                          f"{speed_text}   {age_text}   +{gps_km:.3f} km seit Start")

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(f"\nBeendet. {total} Sekunden empfangen"
              + (f", {gps_km:.3f} km aus GPS-Fixes." if not args.no_gps else "."))


# ---------------------------------------------------------------- CLI

def parse_utc(text: str) -> datetime:
    """ISO ohne Zone wird als UTC gelesen - die API wird immer mit Z abgefragt."""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"'{text}' ist kein ISO-Zeitstempel (z. B. 2026-09-03T08:00)")
    return (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None
            else parsed.astimezone(timezone.utc))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prueft die Telemetrie-API der SER-Live-Monitoring-App (alle Zeiten UTC).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Beispiele:")[-1])
    parser.add_argument("--host", required=True,
                        help=f"IP oder Host der App, Port optional (Default {DEFAULT_PORT})")
    parser.add_argument("--minutes", type=float, default=10.0,
                        help="Minuten rueckwaerts ab jetzt (Default 10)")
    parser.add_argument("--start", type=parse_utc, help="fester Beginn, ISO UTC")
    parser.add_argument("--end", type=parse_utc, help="festes Ende, ISO UTC")
    parser.add_argument("--json", action="store_true",
                        help="/api/timeseries/range (JSON) statt CSV abfragen")
    parser.add_argument("--series",
                        help="kommagetrennte Reihen (Default: alle; im Watch-Modus "
                             "die sieben genutzten)")
    parser.add_argument("--device", default=DEFAULT_DEVICE,
                        help=f"GPS-Geraetename (Default '{DEFAULT_DEVICE}')")
    parser.add_argument("--no-gps", action="store_true", help="GPS-Abschnitt ueberspringen")
    parser.add_argument("--print-rows", type=int, nargs="?", const=20, default=None,
                        metavar="N", help="N Zeilen ausgeben (0 = alle, ohne Wert = 20)")
    parser.add_argument("--save", metavar="DATEI", help="Rohantwort unveraendert speichern")
    parser.add_argument("--watch", action="store_true", help="Livebetrieb, Cursor-Polling")
    parser.add_argument("--interval", type=float, default=10.0,
                        help="Watch: Abstand in s (Default 10)")
    parser.add_argument("--backfill", type=float, default=120.0,
                        help="Watch: Sekunden, die beim Start nachgeladen werden (Default 120)")
    parser.add_argument("--gps-stale", type=float, default=30.0, metavar="S",
                        help="Watch: ab diesem Alter gilt ein Fix als veraltet (Default 30 s)")
    parser.add_argument("--selftest", action="store_true",
                        help="Konformitaetspruefungen der API")
    parser.add_argument("--timeout", type=float, default=30.0,
                        help="HTTP-Timeout in s (Default 30)")

    args = parser.parse_args()
    args.host = normalize_host(args.host)

    if (args.start is None) != (args.end is None):
        parser.error("--start und --end nur gemeinsam")
    if args.start and args.end and args.end <= args.start:
        parser.error("--end muss nach --start liegen (die API antwortet sonst mit HTTP 400)")
    return args


def main() -> int:
    args = parse_args()
    print(f"Ziel: http://{args.host}   Jetzt: {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC")

    if args.selftest:
        return run_selftest(args.host, args.timeout)
    if args.watch:
        run_watch(args)
        return 0

    if args.start:
        start, end = args.start, args.end
    else:
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=args.minutes)

    run_once(args, start, end)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
