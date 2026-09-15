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

### Panel

| Argument | Bedeutung |
|---|---|
| `--flat-panel` | das Panel wird **nicht** ausgerichtet und bleibt flach liegen |
| `--no-flat-panel` | nimmt einen Default aus `SSC_FLAT_PANEL` für diesen Lauf zurück |
| `--aim-morning` | nur mit `--flat-panel`: im Morgenfenster am Nachtquartier doch ausgerichtet |

`--flat-panel` gilt für **jeden** Halt — Kontrollstopp, Loopstopps, `--stop`,
`--sweep-stop`, Fahrerwechsel und die Restzeit vor der Ziellinie — und für das
Morgenfenster. Die **Standzeiten bleiben unverändert**: die dreissig regulierten
Minuten am Kontrollstopp sind geschuldet, ob dabei geladen wird oder nicht. Es
ändert sich nur die Einstrahlung, mit der der Halt rechnet: GHI statt
nachgeführtes GTI.

Weil das ein Zustand des Autos ist und nicht eine Entscheidung pro Lauf, lässt
sich der Default setzen:

```
export SSC_FLAT_PANEL=1        # Linux/macOS
$env:SSC_FLAT_PANEL = "1"      # PowerShell
```

Das ist unkritisch, weil die **Kopfzeile den Panelzustand ausgibt**, sobald er
nicht der Normalfall ist — ein flacher Plan kann also nicht versehentlich als
ausgerichteter gelesen werden. Zusätzlich stehen `Panel flach` im Plantitel,
über der Optionstabelle, im Plot und im Label der Plandatei, und die Datei
selbst bekommt `_flach` in den Namen sowie `panel_flat: true` im `meta`.

**Das Morgenfenster ist bewusst mitbetroffen.** Morgens zwischen 06:00 und 08:00
steht die Sonne tief, und das Verhältnis nachgeführt/flach geht gegen
1/sin(Elevation) — dort kann die Annahme um einen Faktor zwei falsch liegen. Der
Fehler hätte auch eine unangenehme Richtung: ein zu hohes „angeboten" **senkt**
die Obergrenze für die Ankunftsenergie und würde raten, leerer anzukommen, als
das Morgenfenster wieder auffüllen kann. Wer das Panel am Nachtquartier von Hand
aufbocken kann — was etwas anderes ist als Ausrichten in einer
Fünf-Minuten-Loop-Pause — nimmt das mit `--aim-morning` zurück.

Was mit flachem Panel **nicht** mehr gilt: die Standphase vor der Ziellinie war
energetisch attraktiv, weil ein nachgeführtes Panel dort spätnachmittags rund
+150 % gegenüber flach bringt. Flach ist flach, dieser Gewinn fällt weg. Die
Phase bleibt trotzdem im Plan — ihr Grund ist der 50-km/h-Penalty, nicht der
Ertrag. Die Zeile „Alternative" unter der Penalty-Warnung sagt das jetzt auch so.

### Standladen: Panel pro Halt wählen

`--stop` und `--sweep-stop` nehmen ein optionales drittes Feld, das **nur für
diesen einen Halt** gilt und die Tageseinstellung überstimmt:

| Schreibweise | Bedeutung |
|---|---|
| `--stop 45:30` | folgt dem Tag (flach mit `--flat-panel`, sonst ausgerichtet) |
| `--stop 45:30:aus` | ausgerichtet, auch auf einem `--flat-panel`-Tag |
| `--stop 45:30:flach` | flach, auch ohne `--flat-panel` |
| `--stop 45:45:20` | 20 der 45 Minuten ausgerichtet, den Rest flach |
| `--sweep-stop=-1:aus` | Sweep 1 km vor dem Ziel, ausgerichtet gerechnet |

Statt `aus` gehen auch `ausgerichtet`, `aim`, `tracked`; statt `flach` auch
`flat`, `ghi`, `nein`.

Das ist der Halt, an dem die Wahl überhaupt Sinn ergibt: vierzig Minuten
Standladen reichen, um das Panel von Hand aufzubocken oder das Auto zu drehen,
eine Fünf-Minuten-Loop-Pause nicht. Deshalb überstimmt `aus` hier auch ein
global gesetztes `--flat-panel`, während Kontroll- und Loopstopps davon
unberührt bleiben.

Die Minutenzahl gibt es, weil ein langer Halt oft nur halb betreut ist — das
Panel wird ausgerichtet, solange die Crew ohnehin draussen ist, und liegt vor
dem Losfahren wieder flach. Bei `--sweep-stop` ist eine Minutenzahl abgelehnt:
dort variiert die Haltedauer selbst, eine feste Minutenzahl wäre bei 15 min
Halt alles und bei 120 min ein Achtel, und die Kurve sagt dann über keines von
beidem etwas.

Ein Halt, der vom Tag abweicht, **steht so im Plan**: `Standladen km 45.0
(ausgerichtet)` bzw. `(flach)` bzw. `(20 min ausgerichtet)`. Die Sweep-Tabelle
bekommt es in die Überschrift. Stimmt der Halt mit dem Tag überein, bleibt der
Name wie bisher.

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

### Panel klemmt, Optionstabelle für den Tag

```
python point_strategy.py --day 5 --part to_control --km 0 --soc 57 \
       --time 08:00 --flat-panel
```

Der direkte Vergleich, was die fehlende Ausrichtung kostet — einmal mit und
einmal ohne das Flag laufen lassen. Beide Läufe schreiben ihre Plandateien,
unterscheidbar am `_flach` im Namen.

Und die Mischform: Tag flach, aber am langen Standladen wird von Hand
aufgebockt.

```
python point_strategy.py --day 5 --plan 2 --flat-panel --stop 45:40:aus
```


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

`SSC_FLAT_PANEL` wirkt auf **alle** Läufe in dieser Shell. Wenn eine Zahl nicht
zu einer früheren passt, zuerst die Kopfzeile vergleichen: steht dort `Panel
flach`, ist es das.

Selbsttest für den Panelpfad, ohne Routen und ohne Wettercache:
`python scripts/test_flat_panel.py` (aus `data_analysis/`, mit `src` im
`PYTHONPATH`).

Für Fenster statt PNG braucht matplotlib ein GUI-Backend. Fehlt es, wird das
gemeldet und stattdessen PNG geschrieben — unter Windows genügt meist
`pip install pyqt5`.
