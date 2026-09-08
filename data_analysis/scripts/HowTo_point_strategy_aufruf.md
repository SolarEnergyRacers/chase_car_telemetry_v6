# `point_strategy.py` — Aufruf und Argumente

Ausgeführt aus `data_analysis/scripts/`. Ohne Argumente wird alles abgefragt,
mit Argumenten läuft es durch.

Das Wetter kommt **ausschliesslich aus dem Cache**. Ein neuer Abruf ist eine
eigene, bewusste Aktion (`cache_weather.py`), damit der Solver an keinem Punkt
in einen Netz-Timeout laufen kann und zwei Läufe am selben Kontrollstopp
dieselben Zahlen geben.

---

## Argumente

### Tag und Verbindung

| Argument | Bedeutung |
|---|---|
| `--day 1..8` | Renntag. Ohne Angabe: heutiges Datum gegen `race_config.json`, sonst Tag 1 mit Warnung |
| `--host HOST:PORT` | live_monitoring, Default `localhost:5240` |

### Position

Genau eine Variante, sonst wird interaktiv gefragt.

| Argument | Bedeutung |
|---|---|
| `--part to_control \| loop \| to_finish` | welcher Streckenteil |
| `--km 84.2` | Kilometer innerhalb dieses Teils |
| `--at "-26.81,27.83"` | Koordinaten, werden auf den Teil projiziert |
| `--leg out \| back` | Hin- oder Rückweg, nur bei `--part loop` |
| `--loops-done 1` | schon fertige Loops, nur bei `--part loop` |

Interaktiv gibt es zusätzlich „GPS aus der App holen" (`GET /api/gps/latest`).

Auf einem Loop ist die Projektion nicht eindeutig, weil Hin- und Rückweg
übereinanderliegen. Deshalb wird `--leg` gefragt und die Folgerung
zurückgespiegelt — eine falsche Antwort verschiebt die Restpflichtstrecke und
damit den ganzen Plan.

### Batterie

Genau eines, sonst wird gefragt.

| Argument | Bedeutung |
|---|---|
| `--soc 62` | SOC in Prozent |
| `--wh 1716` | verbleibende Wh |
| `--full` | Pack voll — der Tag-1-Startfall |

Interaktiv zusätzlich „aus der Telemetrie holen", mit der Rückfrage, ob der
Pack in Ruhe stand. Das entscheidet zwischen Anker und blossem
Plausibilitätswert, und der Unterschied steht in der Kopfzeile.

### Zeit

| Argument | Bedeutung |
|---|---|
| `--time now` | jetzt |
| `--time 12:40` | Uhrzeit SAST auf dem Renntag |
| `--time 2026-09-10T12:40` | vollständiger Zeitstempel |

Naive Eingaben gelten als SAST, nie als UTC.

### Modus und Rechnung

| Argument | Bedeutung |
|---|---|
| *(nichts)* | Optionstabelle über 0..n Loops |
| `--plan 5` | Fahrplan für fünf verbleibende Loops |
| `--sweep-stop KM` | eine Standphase dort in 15-Minuten-Schritten durchrechnen |
| `--stop KM:MIN` | Standladen, mehrfach erlaubt |
| `--driver-change KM` oder `@HH:MM` | Fahrerwechsel erzwingen, mehrfach erlaubt |
| `--no-auto-driver-change` | die automatischen Wechsel alle 2 h weglassen |
| `--n-max 10` | wie viele Loop-Zahlen die Tabelle durchrechnet |
| `--plot` | beide Anzeigen in Fenstern öffnen |
| `--plot-png [PREFIX]` | zusätzlich als PNG schreiben |
| `--no-save` | keine Plandateien schreiben (Default: jede machbare Option landet in `plans/`, siehe unten) |
| `--plans-dir DIR` | Zielordner der Plandateien (Default `data_analysis/plans`, oder `$SSC_PLANS_DIR`) |
| `--spacing-km 5.0` | muss zu `cache_weather.py` passen |
| `-v` | Info-Logging |

**Negative Kilometer zählen vom Ziel zurück und brauchen ein
Gleichheitszeichen:** `--stop=-5:30`, `--sweep-stop=-1`. Ohne das hält argparse
das Minus für den Anfang einer Option. Aufgelöst wird der Wert dort, wo die
Gesamtstrecke bekannt ist, er bleibt also für jede Loop-Zahl richtig.

`--n-max` greift nur, wenn **alle** geprüften Zahlen machbar sind — die Tabelle
bricht ohnehin eine Zeile nach der ersten nicht machbaren ab. Bei `--part loop`
zählt die Zahl die Loops **nach** dem laufenden.

---

## Beispiele

### Abends im Quartier, Überblick für morgen

```
python cache_weather.py 5 --refresh
python point_strategy.py --day 5 --part to_control --km 0 --soc 57 --time 08:00
```

Der einzige Moment, in dem Netz gebraucht wird. `cache_weather.py` holt dabei
auch den Nachtquartierpunkt für das Morgenfenster von Tag 6.

### Morgens am Start von Tag 1

```
python point_strategy.py --day 1 --part to_control --km 0 --full --time 09:00
```

`--full`, weil der Pack per Regelwerk voll startet und vorher nicht geladen
werden darf.

### Zwanzig Minuten vor dem Kontrollstopp

```
python point_strategy.py --day 5
```

Der Normalfall im Begleitfahrzeug: fragt Position, Batterie und Zeit ab. Vor
dem Anhalten aufrufen, nicht danach — dann liegt die Entscheidung schon vor,
wenn die dreissig Minuten beginnen.

### Am Kontrollstopp, Fahrplan für die gewählte Loop-Zahl

```
python point_strategy.py --day 5 --plan 3 --plot
```

### Nach dem zweiten Loop, auf dem Rückweg

```
python point_strategy.py --day 5 --part loop --leg back --loops-done 2
```

### Ladestopp fünf Kilometer vor dem Ziel prüfen

```
python point_strategy.py --day 1 --part to_control --km 0 --full --time 09:00 \
       --plan 5 --stop=-5:30
```

### Und wie lange sich dieser Halt lohnt

```
python point_strategy.py --day 1 --part to_control --km 0 --full --time 09:00 \
       --plan 4 --sweep-stop=-1
```

Entscheidend ist die Spalte **„morgen nutzbar"** = `min(End-Energie,
Obergrenze)`. Energie über der Obergrenze des Morgenfensters ist wertlos, weil
sie am nächsten Morgen ohnehin gratis gekommen wäre — die beste Zeile ist
deshalb oft **nicht** die mit dem höchsten End-SOC. Gewinne unter 30 Wh gelten
als gleichwertig, und der kürzere Halt gewinnt: längeres Stehen heisst
schneller fahren und damit tieferen minimalen SOC unterwegs.

### Fahrerwechsel selbst setzen

```
python point_strategy.py --day 5 --plan 3 --driver-change 60 \
       --driver-change @13:30 --no-auto-driver-change
```

Ohne `--no-auto-driver-change` kommen die automatischen dazu, wo nach dem
erzwungenen Wechsel noch über zwei Stunden am Stück gefahren wird.

---

## Was die Ausgabe zeigt

**Kopfzeile** mit Position, Zeit, Energie samt Herkunft und Vertrauensgrad,
Alter der Wetterdaten. Ein Plan auf einer Lastspannung ist ein anderer Plan als
einer auf einem Ruheanker, und ein sieben Stunden alter Forecast ist selbst ein
Grund, vorsichtiger zu entscheiden.

**Optionstabelle** mit km, Ø, min SOC, End-SOC, Wh über dem Boden,
Zeitreserve bei Vollgas und Anzahl Fahrerwechsel — plus einer Zeile, woran der
erste nicht machbare Loop scheitert. Nach dem Kontrollstopp entfällt sie, weil
dort keine Loops mehr möglich sind.

**Fahrplan** mit Sollgeschwindigkeiten als 5-km/h-Zonen je Leg, laufendem
Kilometer **und** Position im Leg, Standphasen mit Wh und Ankunftszeiten.

**Trigger** — die Zeile, die man dem Fahrer sagt: bis wann aufbrechen und mit
wie viel SOC, damit der nächste Loop lebt.

**Morgenfenster** für den Folgetag, mit der Obergrenze für die Ankunftsenergie.
Fehlt es, steht der Grund da.

### Zonengeschwindigkeit lesen

Die Zonenwerte sind das harmonische Mittel über die Distanz — nur das
reproduziert die Fahrzeit. Ob eine Zahl eine **Anweisung** oder eine
**Prognose** ist, hängt aber davon ab, ob der Routing-Deckel dort bindet: in
einer Ortsdurchfahrt ist die Geschwindigkeit vorgegeben, nicht gewählt. Die
verlässliche Information ist dort die **Sollzeit** („bei km 98.6 um 11:14").

---

## Plandateien für die Live-Anzeige

Jeder Lauf schreibt seine machbaren Optionen als Plandateien nach
`data_analysis/plans/` (im Plan-Modus die eine, in der Optionstabelle jede
machbare Zeile). Die Dateien sind selbständig - Trace mit Koordinaten,
Standphasen, Fahrerwechsel, Startzustand des Packs - und werden von
`live_strategy.py` im Menü „Plan“ angeboten. Dort wird erst entschieden,
welcher gefahren wird; deshalb schreibt der Lauf alle und nicht nur die
beste. Aufrufe, die man nicht behalten will, bekommen `--no-save`.

Siehe `HowTo_live_strategy.md`.

## Stolperstellen

`--spacing-km` muss dem Wert in `cache_weather.py` entsprechen, sonst gibt es
garantiert einen Cache-Miss — der Wert geht in den Schlüssel ein. Default ist
bei beiden 5.0.

`SSC_ROUTE_DIR` setzen, falls `strategy-private` nicht als Geschwisterordner
liegt. Sonst wird der Pfad selbst gefunden.

Nach jedem Routenabruf `cache_weather.py N` laufen lassen, damit der
Nachtquartierpunkt für Tag N+1 mitkommt. Ohne ihn greift der Rückfall über die
Route des Folgetags, und der scheitert an den Blind Stages.

Für Fenster statt PNG braucht matplotlib ein GUI-Backend. Fehlt es, wird das
gemeldet und stattdessen PNG geschrieben — unter Windows genügt meist
`pip install pyqt5`.
