# `live_strategy.py` — Live-Anzeige im Begleitfahrzeug

Zeigt im Browser, wo das Auto auf dem **geladenen Plan** steht und wie weit
es davon abweicht: Sollgeschwindigkeit hier und voraus, nächste Ankunft,
Fahrerwechsel, Energie Ist gegen Plan, Sonne Ist gegen Prognose. Die Seite
rechnet **nichts neu** — ein neuer Plan entsteht mit `point_strategy.py` und
wird dann bewusst ausgewählt.

Ausgeführt aus `data_analysis/scripts/`.

---

## Ablauf im Rennen

1. `point_strategy.py` laufen lassen (Optionstabelle oder `--plan N`). Jede
   machbare Option landet als Datei in `plans/`.
2. `live_strategy.py` läuft im Begleitfahrzeug durch (einmal starten):
   ```
   python live_strategy.py --host 192.168.1.50:5240 --device honor
   ```
3. Browser auf `http://<Laptop>:8765` — auch vom Tablet oder Handy im selben
   WLAN. Oben im Menü **Plan** die gewünschte Datei wählen, **Plan
   übernehmen**.
4. Neue Einschätzung (Kontrollstopp, Wetter gekippt): `point_strategy.py`
   erneut, dann in der Seite den neuen Plan wählen. Nichts passiert
   automatisch — ein Plan, der sich unter dem Fahrer ändert, ist schlechter
   als ein alter.

Beim Laden eines Plans **mitten am Tag** holt die Seite die Telemetrie und die
GPS-Spur seit dem Startzeitpunkt des Plans nach (`/api/timeseries/range`,
`/api/gps/range`) und integriert sie. Die Energie-Ist-Kurve beginnt damit
beim Packzustand, mit dem der Plan gerechnet wurde. Solange das läuft, steht
„Historie wird nachgeladen“ in der Anzeige.

---

## Argumente

| Argument | Bedeutung |
|---|---|
| `--host HOST:PORT` | live_monitoring, Default `localhost:5240` |
| `--device NAME` | GPS-Gerätename des Autos in der App, Default `honor`. Ohne den richtigen Namen mischen sich Auto und Begleitfahrzeug. Auf der Seite jederzeit umstellbar |
| `--plan DATEI` | Plandatei beim Start laden (sonst im Browser wählen) |
| `--plans-dir DIR` | Ordner der Plandateien (Default `data_analysis/plans`, oder `$SSC_PLANS_DIR`) |
| `--port 8765` | Port der Webseite |
| `--bind 0.0.0.0` | Adresse; `127.0.0.1`, wenn nur der Laptop selbst zugreifen soll |
| `--interval 1.0` | Abfrageintervall in Sekunden |
| `--discharge-positive` | `battery_current` ist beim Entladen positiv (Default: negativ, wie SER-5) |
| `--simulate` | ohne Auto: den zuletzt gerechneten Plan abfahren |
| `--sim-speed 10` | Zeitraffer der Simulation beim Start (auf der Seite änderbar, inkl. Pause) |
| `--sim-slow 0.9` / `--sim-cloud 0.7` | Simulation langsamer bzw. mit weniger Sonne |
| `--time-shift S` | nur Test/Wiedergabe: eigene Uhr um S Sekunden verschieben (siehe `mock_live_monitoring.py`) |
| `-v` | Info-Logging |

---

## Was die Seite zeigt

**Kopfzeile.** Uhr (SAST), geladener Plan, Ziel laut Plan, **erwartete
Ankunft am Ziel** im aktuellen Rückstand, Deadline, Wolkenmarge des Plans.
Zwei Punkte rechts: Alter der Telemetrie, Alter des GPS-Fix und Abstand
zur Route.

**Geschwindigkeit.** Ist gegen Soll an dieser Stelle, darunter `v_route`
und Limit. Der Ist-Wert kommt aus der **Telemetrie** (`speed`, CAN) — die
Messung des Autos, jede Sekunde, 1 km/h Auflösung. Das GPS springt nur ein,
wenn die Telemetrie älter als 5 s ist oder in einer Lücke steckt; die Zeile
darunter sagt immer, welche Quelle gerade gilt, und die Zahl wird gelb,
sobald es nicht der CAN ist. Steht „Soll am Routing-Deckel“, ist die Zahl
eine Prognose des Routings, keine Anweisung — dort gilt die Sollzeit. Die
Kästchen darunter sind die nächsten Zonen: „35 in 4.6 km · 7.9 km“ heisst
in 4.6 km beginnt eine 7.9 km lange Zone mit 35 km/h.

**Position.** km ab Planstart und Rest, Leg und km im Leg (auf dem Loop die
brauchbare Angabe), Abweichung vom Zeitplan.

Steht das Auto an einem geplanten Halt, kommt darunter der Kasten mit der
Zahl, auf die es ankommt: **weiter ab HH:MM** — Ankunft plus die
**regulierte** Haltezeit (30 min Kontrollstopp, 5 min Loopstopp). Ab da darf
weitergefahren werden, also ab da wird vorbereitet. Daneben steht, wie lange
der Plan mit dem Halt rechnet (35 bzw. 8 min); die Differenz ist die Zeit
fürs Aus- und Einsteigen, und auf ihr beruhen die Ankunftszeiten. Darunter,
woher die Ankunft stammt — erkannt oder bestätigt.

Eine verspätete Ankunft bleibt als Rückstand stehen, so lange man dort
steht: die Haltezeit läuft ab Ankunft, nicht ab Plan.

**Stopp jetzt / nachtragen.** Die Ankunft wird sonst aus der Geschwindigkeit
gelesen, und die kennt der Tracker erst ab der ersten integrierten Sekunde.
Wird der Plan **am Kontrollstopp** gerechnet (`--time now`, der Normalfall),
beginnt der Halt scheinbar erst beim Laden — gemessen 9 Minuten zu spät, und
„weiter ab“ wandert mit. Zwei Gegenmittel, beide eingebaut:

- automatisch: beim Nachladen wird zusätzlich die Stunde **vor** dem
  Planstart abgefragt (nur `speed`) und daraus die echte Ankunft bestimmt.
  Steht in den Meldungen als „Auto stand schon vor dem Planstart, seit …“.
- von Hand: **Stopp jetzt** oder `HH:MM` + **nachtragen**. Die bestätigte
  Zeit gewinnt über die erkannte, eine zweite Bestätigung korrigiert die
  erste (es gibt nur eine Ankunft pro Halt). Das ist auch der richtige Weg,
  wenn der Offizielle die Zeit anders nimmt als das Auto zum Stehen kam.

**Nächste Ankunft.** Kontrollstopp, Loopstopp oder Ziel — was zuerst kommt.
Erwartete Zeit = jetzt + geplante Fahrzeit ab hier (plus Rest eines
laufenden Halts), Plan-Zeit daneben, Differenz farbig.

**Fahrer.** Fahrzeit seit dem letzten Halt von mindestens 3 Minuten und Rest
bis zur 2-Stunden-Grenze. Der Zähler kommt aus der Geschwindigkeit; ein Halt
unter 3 Minuten (Ampel, Kreisel) setzt ihn nicht zurück.

Darunter die Zeile, die zählt: entweder **„kein Wechsel nötig: <Halt> bei km
X ist die Pause“** — der nächste geplante Halt kommt, bevor die zwei Stunden
um sind, und setzt sie ohnehin zurück — oder **„spätestens HH:MM bei km X“**,
wenn bis dahin kein Halt vorgesehen ist. Beides ist aus dem Plan gerechnet,
nicht aus den geplanten Wechselpunkten abgelesen.

**Fahrerwechsel jetzt** bestätigt einen Wechsel: die zwei Stunden starten
neu. Nötig, weil der Zähler nur die Geschwindigkeit sieht und ein Halt für
ihn gleich aussieht, egal ob gewechselt wurde. Wird früher gewechselt als
geplant — etwa an einem Halt, der aus einem anderen Grund entstand —, ist das
der Weg, es dem Werkzeug zu sagen. Nachträglich geht es über das Feld
`HH:MM` + **nachtragen**; der Eintrag bekommt dann den Kilometer von damals
(aus der eigenen Positionshistorie), nicht den von jetzt. Ein nachgetragener
Wechsel, der **älter** ist als ein schon bestätigter, verschiebt den Zähler
nicht zurück. Die letzte Zeile listet die bestätigten Wechsel.

Beim Planwechsel wandern die bestätigten Wechsel mit — der Fahrer hat
gewechselt, unabhängig davon, welcher Plan geladen ist. Die Kilometer wandern
**nicht** mit: jeder Plan zählt km ab seinem eigenen Startpunkt.

**Sollgeschwindigkeit voraus.** Ausschnitt 2 km zurück, 15 km voraus: Soll
(weiss), `v_route` (blau), `v_limit` (grün, gepunktet wo aus der
Strassenklasse geschätzt), Höhenprofil, Halte (gelb), Kreisel und Ampeln
(rosa Punkte), Leg-Grenzen. Der rote Strich ist das Auto, der Punkt seine
Geschwindigkeit.

**Energie im Pack über den Tag.** Plan (blau) über die ganze Reststrecke,
Ist aus V·I integriert (gelb), SOC aus der Spannung (rosa gepunktet),
Boden-Kurve (rot gestrichelt: darunter ist das Ziel nicht mehr erreichbar).
Am aktuellen Punkt der rosa Balken: das Band der Spannungsmethode.

**Energie am aktuellen Punkt.** Ist gegen Plan **am gleichen Kilometer**, in
einer Standphase **zum gleichen Zeitpunkt**. Die Differenz aufgeteilt in
Sonne und Verbrauch (positiv = besser als Plan). Darunter der SOC aus der
Spannung mit Band und ob der integrierte Wert darin liegt; weichen sie
dauerhaft ab, driftet einer von beiden — im Stand mit **Anker (Stand)** auf
die Ruhespannung setzen. Über Boden: Abstand zur Boden-Kurve. Tagesende:
Hochrechnung mit den bisher beobachteten Verhältnissen Sonne und Verbrauch.

**Sonne.** MPPT-Leistung Ist gegen Plan-Prognose an dieser Stelle, Kanäle
einzeln, Ertrag seit Planstart gemessen gegen Prognose.

**Voraus.** Tabelle der nächsten Halte, Kreisel/Ampeln (bis 15 km) und das
Ziel mit Plan- und erwarteter Zeit. Das Ziel wird rot, wenn die erwartete
Ankunft nach der Deadline liegt.

**Loop wählen (nur bei Plänen mit Loops).** Unter der Haltebox: welcher
Durchgang gerade gefahren wird und ob **Hinweg** oder **Rückweg**. Die Zeile
darüber zeigt, was die Erkennung meint; die Auswahl folgt ihr automatisch,
solange niemand das Feld angefasst hat. Siehe „Loops“ weiter unten.

**Eingriffe.** Position von Hand setzen (GPS tot oder Umweg): hält die
Position fest, bis **wieder GPS** gedrückt wird. Der nächste Fix wird dann
in einem Fenster von ±10 km um die Handposition gesucht. Für einen falsch
erkannten Loop ist die Loop-Auswahl der bessere Weg — sie hält nichts fest.
**Anker
(Stand)**: Energiezähler auf die Ruhespannung setzen, nur nach einigen
Minuten Stillstand — unter Last wird er verweigert. Dazu die
**GPS-Geräteauswahl** (siehe unten) und, nur im Simulationsmodus, der
**Zeitraffer** samt Pause.

---

## GPS-Gerät wählen — und was die App hergibt

Das Auswahlfeld unter „Eingriffe“ listet die Geräte, die in den letzten
30 Minuten gemeldet haben, mit Anzahl Fixe und Alter des letzten. Auswahl
wirkt sofort auf `/api/gps/latest` und auf das Nachladen der Spur.

Was die App dafür anbietet, und was nicht:

| | |
|---|---|
| `GET /api/gps/latest?deviceName=` | letzter Fix, optional je Gerät |
| `GET /api/gps/range?from&to&deviceName=` | Spur im Zeitfenster, optional je Gerät |
| **Geräteliste** | **gibt es nicht** als Endpoint |

`DataManager` kennt die Namen (`GetGpsDeviceNames()`), aber nichts stellt
sie über HTTP bereit. Die Liste wird deshalb aus einem ungefilterten
`/api/gps/range` über die letzten 30 Minuten abgeleitet — ein Gerät, das
länger stumm ist, erscheint nicht (was für eine Auswahl richtig ist: ein
Handy, das heute Morgen gemeldet hat und seither aus ist, ist nicht das
Auto). Ein per `--device` gesetzter, aber gerade stummer Name bleibt
trotzdem wählbar und wird als „meldet gerade nicht“ angezeigt. Ein
Endpoint `GET /api/gps/devices` wären in der App ~10 Zeilen und würde
diesen Umweg ersetzen.

**(alle Geräte)** ist auch wählbar, taugt aber nur zur Diagnose: ohne
Filter liefert die App den zuletzt geschriebenen Punkt **irgendeines**
Geräts, und sobald das Begleitfahrzeug mitmeldet, springt die Position
zwischen beiden hin und her. Im Test mit zwei Meldern (2 km Abstand) zeigt
das Werkzeug dann abwechselnd beide Positionen — auf der Route, mit
kleinem Abstand zur Linie, also **ohne** Warnung. Deshalb im Rennen immer
den Namen des Autos setzen.

Wichtig dazu: der Endpoint `/api/gps/report` verlangt `device` als
Pflichtfeld, die im README dokumentierte OsmAnd-URL enthält es aber nicht.
Ohne `&device=<name>` wird jeder Punkt mit HTTP 400 abgewiesen und nur auf
der App-Konsole protokolliert — dann ist die Geräteliste leer und die Seite
sagt das auch.

---

## Was beim Planwechsel mit der Energie passiert

Die Ist-Kurve ist **kein** eigener Messwert des Packs, sondern ein Integral
mit einem Startwert:

```
Ist(t) = Startenergie des Plans  −  ∫ V·I dt   ab Planstartzeit
```

Beim Laden eines Plans wird beides neu gesetzt:

1. **Startenergie** = der Packzustand, mit dem `point_strategy.py` gerechnet
   hat (`--soc`, `--wh`, `--full` oder aus der Telemetrie geholt). Er steht
   in der Plandatei und in der Anzeige unter „Herkunft“.
2. **Startzeitpunkt** = die `--time` des Plans. Liegt sie in der
   Vergangenheit, holt die Seite Telemetrie und GPS-Spur von dort bis jetzt
   (`/api/timeseries/range`, `/api/gps/range`) und integriert sie durch;
   solange steht „Historie wird nachgeladen“. Liegt sie in der Zukunft, wird
   nichts nachgeladen und die Integration beginnt mit der ersten
   Live-Sekunde.

Ein Planwechsel ist also ein **Neustart der Integration**: gesammelte
Abweichungen, gesetzte Anker und die Lückenzählung sind weg, und die Ist-Kurve
beginnt wieder exakt beim Startwert des neuen Plans. Das ist gewollt — die
Abweichung soll gegen genau den Plan gemessen werden, der gefahren wird, und
mit dem Zustand, aus dem er gerechnet wurde.

Zwei praktische Folgen:

- Wird der neue Plan mit einer **frischen Messung** gerechnet (am
  Kontrollstopp mit `--soc` aus der Telemetrie, idealerweise nach ein paar
  Minuten Ruhe), ist der Neustart zugleich ein Anker: die Ist-Kurve wird auf
  die Realität zurückgesetzt. Das ist der saubere Weg.
- Wird er mit einem **alten** oder geschätzten Wert gerechnet, übernimmt die
  Live-Anzeige diesen Fehler. Die Zeile „Herkunft“ nennt deshalb immer den
  Startwert und woher er stammt, und der SOC aus der Spannung steht als
  unabhängige Gegenprobe daneben.

Der einzige Eingriff, der die Ist-Kurve sonst verschiebt, ist **Anker
(Stand)**.

---

## Das SOC-Band aus der Spannung

Aus Klemmenspannung und Strom wird über den Packwiderstand die Ruhespannung
geschätzt und mit der OCV-Kurve in SOC übersetzt (`battery.state_from_measurement`,
dieselbe Rechnung wie `calculate_soc_wh_under_load` aus SER_strategy_sosol_2026).
Das Band deckt zwei Unsicherheiten ab, beides **Annahmen** bis zum Standversuch:

| Quelle | Spanne |
|---|---|
| Packwiderstand | 0.6 × bis 1.4 × des angenommenen Werts (Zelle 0.010–0.016 Ω gegen 0.015 angenommen, plus 10 mΩ Platzhalter) |
| OCV-Kurve | ± 20 mV pro Zelle (generische Kurve, auf dem Plateau 1.3 % SOC je 10 mV) |

Konstanten `R_BAND` und `OCV_TOL_V` in `strategy/live.py`. Unter Last ist
das Band mehrere Prozent breit — genau deshalb ist der integrierte Wert die
Hauptzahl und die Spannung die Plausibilitätsprüfung.

---

## Loops: Durchgang und Hin-/Rückweg

Ein Loop wird hin und zurück über dieselbe Strasse gefahren — `inputs.py`
baut ihn als Hinweg plus denselben Hinweg rückwärts, ohne den Wendepunkt
doppelt zu zählen. Damit teilen sich **Hin- und Rückweg jede Koordinate**,
und jeder Durchgang teilt sie mit jedem anderen. Ein GPS-Fix allein kann
also weder sagen, in welchem Durchgang das Auto ist, noch in welcher
Hälfte. Nur die km-Achse des Plans kann das, weil sie der **abgewickelte
Tag** ist; die Projektion trägt die Antwort vom vorherigen Fix weiter.

Genau deshalb braucht es die Korrektur von Hand: ist die Fortschreibung
einmal verrutscht — Plan mitten im Loop geladen, GPS-Loch an der Wende,
ein Umweg —, bleibt sie es für den Rest des Tages, und Zeitplan, ETAs und
der ganze Energievergleich verrutschen mit. Einmal gemessen: km 90.7 statt
22.8 und damit −138 Minuten Zeitplan.

**Bedienung:** Durchgang im Auswahlfeld wählen, dann **Hinweg** oder
**Rückweg** drücken. Unter den Knöpfen steht, wo die Wende liegt und
welcher Loopstopp den Durchgang abschliesst.

Was dabei passiert: der aktuelle GPS-Fix wird **in das genannte
Halbstück** projiziert (`after_km` = Anfang, `window_km` = Länge der
Hälfte), und das Ergebnis ist die neue Position. Die Position wird dabei
**nicht festgehalten** — anders als „Position von Hand“. Das GPS übernimmt
sofort wieder und sucht ab dem korrigierten Kilometer weiter, das Auto
fährt von selbst über die Wende in den Rückweg.

Liegt der Fix mehr als 1.5 km neben dem gewählten Halbstück, wird die
Eingabe trotzdem ausgeführt — der Grund für die Korrektur kann ja gerade
ein totes GPS sein —, aber die Seite fragt nach, ob der Loop stimmt, und
die Notiz hält den Abstand fest.

Ein Rest-Loop („Loop (laufend, Rest)“, wenn der Plan mitten im Loop
gerechnet wurde) kann ganz aus Rückweg bestehen. Die Wende wird deshalb
nicht als Mitte angenommen, sondern aus den Koordinaten gelesen: der vom
Anfang des Abschnitts am weitesten entfernte Knoten. Wo eine Hälfte nicht
existiert, ist ihr Knopf gesperrt.

---

## Halte: zu früh angekommen

Ein Halt gilt **im Stand ohne Zeitfenster**. Während der Fahrt zählt er ab
zwei Minuten vor der Planankunft bis zur Planabfahrt; steht das Auto aber
am Kilometer eines Halts, dann ist es dort — unabhängig von der Uhr.

Das war zuerst nur nach oben so (zwanzig Minuten zu spät am Kontrollstopp,
und die Anzeige fiel auf „fährt“ zurück). Nach unten fehlte es, und dort
trifft es vor allem die Loops: ein Loopstopp dauert fünf Minuten, drei
Minuten zu früh sind normal. Gemessen am Testplan: sechs Minuten zu früh am
Loopstopp 1 — `at_stop` war `None`, also keine Haltebox, kein „weiter ab“,
und eine bestätigte Ankunft liess sich nicht einmal einem Halt zuordnen.

Jetzt gilt am Loopstopp dasselbe wie am Kontrollstopp: Ankunft aus dem
Geschwindigkeitssignal oder **bestätigt** (`Stopp jetzt` / `nachtragen`),
**weiter ab** = Ankunft + 5 min Reglement, und die ETAs rechnen weiter mit
den 8 Minuten, die der Plan für Ein- und Aussteigen budgetiert.

---

## Vorzeichen und Plausibilität der Leistung

**Vorzeichen — an den Daten vom 12.09. geprüft, beide stimmen:**

| Frage | Messung | Ergebnis |
|---|---|---|
| Ist Entladen negativ? | Fahrt > 30 km/h: `batteryCurrent` Mittel **−5.83 A** | ja, `i_batt_discharge_positive=False` ✓ |
| Sieht der Shunt schon den Netto-Strom? | Stillstand mit Sonne (21 Zeilen): `batteryCurrent` **+4.06 A** gegen `pvCurrent` **+4.37 A**, Verhältnis 0.93 | ja, `i_batt_is_net=True` ✓ (die fehlenden 7 % sind der Aux-Verbrauch, der nie in den Pack geht) |

Damit steht `net_verified=True` in `TelemetrySigns` — der Standversuch ist
gelaufen, die Warnung beim Start ist weg.

**Was tatsächlich falsch war:** `v_pack` und `i_batt` hatten seit jeher ein
Plausibilitätsfenster, `p_solar` nicht. Ein einziges Schrott-Sample aus dem
MPPT-Frame (im Log vom 12.09. **−357 W auf MPPT 4**, in einem Kanal, der
sonst 0…256 W liefert) landet damit ungeprüft im Integral, und das Integral
wird von nichts später neu verankert. Mit einem künstlichen 1.1e31-W-Sample
lässt sich die Anzeige aus den Screenshots exakt reproduzieren: Sonne
6.111e+27 Wh, Tagesende −1.719e+26 Wh. Nicht das Vorzeichen — der fehlende
Filter.

Jetzt gilt pro MPPT-Kanal:

| Gate | Wert | Warum |
|---|---|---|
| `p_mppt_max` | 600 W je Kanal | Array ≈ 1.2 kW auf vier MPPTs, gemessenes Maximum 256 W je Kanal — 600 W ist schon doppelt so viel, wie ein Kanal kann |
| `p_mppt_min` | −100 W je Kanal | ein Wandler, der senkt, ist physikalisch klein; −357 W ist ein kaputter Frame |
| `max_bridge_s` | 1800 s | ein Trapez über ein Loch von Stunden ist keine Überbrückung, sondern eine Erfindung |

Ein unplausibler Kanal wird **verworfen, nicht das ganze Sample**: die
Batterieseite dieses Samples ist eine gültige Messung, und weil der Shunt
den Nettostrom sieht, geht `p_solar` ohnehin nur in die Aufteilung
Sonne/Verbrauch ein, nicht in den Packverbrauch. Eine Lücke über
`max_bridge_s` wird gar nicht integriert — die Strecke fehlt dann ehrlich
in der Zählung, statt sie zu erfinden. Beides steht auf der Seite in der
Zeile **Daten** (`… MPPT-Werte unplausibel`, `… Abbruch (n min NICHT
gezählt)`); nach einem Abbruch im Stand neu verankern.

Bei einer Lücke über `max_gap_s` (30 s) wird weiterhin überbrückt und die
Menge als *Wh überbrückt* ausgewiesen — das ist die Grauzone dazwischen.

---

## Testen ohne Auto

Zwei Wege.

**Simulation im Prozess:**
```
python live_strategy.py --simulate --sim-speed 30 --sim-slow 0.95 --sim-cloud 0.85
```
lädt die neueste Plandatei und fährt sie im Zeitraffer ab. Der Faktor lässt
sich auf der Seite unter „Eingriffe“ zwischen Pause und 120× umstellen — ohne
Sprung in der simulierten Zeit, die Uhr läuft an derselben Stelle weiter.

**Attrappe der App**, prüft den echten Pfad (Poller, Nachladen, GPS-Spur,
Lücken mit 0.0):
```
python mock_live_monitoring.py --plan ../plans/<datei>.json --backlog-min 40 \
       --slow 0.95 --cloud 0.85 --gap 600 720 --extra-device begleitfahrzeug
python live_strategy.py --host localhost:5240 --device honor \
       --plan ../plans/<datei>.json --time-shift <vom Mock ausgegeben>
```
`--time-shift` ist nötig, weil der Plan auf einem Renntag liegt und die
Attrappe in Planzeit sendet. Derselbe Mechanismus taugt später für die
Wiedergabe eines aufgezeichneten Renntags gegen seinen Plan.

**Offline-Prüfungen** (kein Netz, kein Wettercache, keine strategy-private):
```
python test_live_offline.py
```

---

## Stolperstellen

- Ohne den richtigen Gerätenamen liefert `/api/gps/latest` den letzten Punkt
  **irgendeines** Geräts (siehe oben). Steht gar nichts, prüft
  `check_telemetry_api.py`, ob überhaupt Punkte ankommen.
- Die Ist-Geschwindigkeit ist die des CAN. Zeigt sie dauerhaft 0, während das
  Auto fährt, stimmt die CAN-Adresse für `speed` nicht — dann greift die
  Anzeige aufs GPS zurück und sagt es in der Zeile darunter.
- Ein Fix mehr als 250 m neben dem Plan wird als „neben der Route“ markiert,
  mehr als 1.5 km wird verworfen (Position bleibt stehen). Häufigste
  Ursache: der falsche Plan ist geladen (anderer Tag, anderer Startpunkt).
- Im Stand ist der Kilometer **eingefroren** (GPS-Rauschen ist keine
  Bewegung). Springt ein Fix weiter als 150 m, wird er trotzdem geglaubt —
  ein bei 0 hängendes Geschwindigkeitssignal soll die Position nicht
  festnageln.
- Am Kontrollstopp und an jedem Loopstopp liegen mehrere Loop-Durchgänge auf
  **demselben Punkt**, und Hin- und Rückweg eines Durchgangs liegen auf
  derselben Strasse. Die Zuordnung kommt deshalb aus der Historie
  (sequenzielle Projektion), nicht aus einer Einzelmessung. Steht sie
  trotzdem falsch: **Loop wählen** unter der Haltebox (Durchgang +
  Hinweg/Rückweg) — siehe Abschnitt „Loops“.
- **Behoben (13.09.):** `Timestamp.isoformat()` lässt den Bruchteil weg,
  wenn er null ist. Das letzte Leg wird auf die Deadline geklemmt — eine
  runde Zeit —, also enthält jeder Plan, der den Tag ausnutzt, genau eine
  solche Zeile, während alle anderen Nanosekunden tragen. pandas rät das
  Format aus dem **ersten** Element und wendet es strikt an, und der Plan
  liess sich gar nicht mehr laden (`time data "…T15:00:00+00:00" doesn't
  match format "%Y-%m-%dT%H:%M:%S.%f%z"`). Alles Einlesen von ISO-Strings
  läuft jetzt über `planfile._utc()` mit `format="ISO8601"`; dasselbe in
  `ser_client.parse_range_json()`, wo eine Telemetrie-Sekunde ohne
  Bruchteil den ganzen Abruf werfen liess.
- Die Seite braucht kein Internet — kein CDN, alles in einer Datei.
- Die Plandatei enthält die Uhrzeit, mit der `point_strategy.py` gerechnet
  hat (`--time`). Liegt sie in der Zukunft, wird nichts nachgeladen und die
  Integration beginnt mit der ersten Live-Sekunde.
- `battery_voltage == 0` heisst in der App „Lücke“, nicht Messwert; solche
  Sekunden werden übersprungen und als Lücke gezählt (Anzeige „Lücken … Wh
  überbrückt“).
- Vorzeichen `battery_current` und ob der Strom netto ist (Topologie) sind
  weiterhin die zwei Punkte aus `telemetrie-anbindung-live-strategie.md`,
  Abschnitt 9. Bis zum Standversuch ist die Solar/Last-Aufteilung ein
  Hinweis, keine Diagnose.
