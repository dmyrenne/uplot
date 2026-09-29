# μplot

Weboberfläche zum Stiftplotten mit einem Prusa MK3S+ (oder einem anderen 3D-Drucker mit Stifthalter).
Sie baut auf dem Workflow aus [brianlow/plotter](https://github.com/brianlow/plotter) von Brian Low auf und
ersetzt dessen `plot.sh`, `plot-draft.sh` und `vpype.toml` durch eine grafische Oberfläche.
Der G-Code entsteht mit [vpype](https://github.com/abey79/vpype) und [vpype-gcode](https://github.com/plottertools/vpype-gcode).

## Installation

Benötigt Python 3.12 (vpype startet unter Python 3.14 nicht). Mit [uv](https://github.com/astral-sh/uv):

    uv venv --python 3.12 .venv
    uv pip install --python .venv/bin/python -r requirements.txt
    ./fetch-examples.sh     # optional: Beispiel-SVGs aus brianlow/plotter laden
    ./start.sh              # öffnet http://127.0.0.1:5055

`./start.sh --no-browser` startet den Server, ohne einen Browser zu öffnen; `--port` wählt einen anderen Port.

## Funktionen

- SVG hineinziehen, Vorschau auf dem Druckbett prüfen und G-Code herunterladen. Den G-Code druckst du von SD-Karte
  oder schickst ihn mit einem Programm deiner Wahl (z. B. OctoPrint, Pronterface) per USB an den Drucker.
- Vorschau im Maßstab des Druckbetts, Nullpunkt unten links. Düse (Kreis) und Stift (Fadenkreuz) im Nullpunkt,
  nicht erreichbare Bereiche grau, Sicherheitsabstand pink schraffiert, Leerfahrten und Plot-Fortschritt zum Scrubben.
- Layout: „An Plotfläche anpassen“ skaliert proportional, „Zentrieren“ richtet mittig aus, freier Drehwinkel,
  X/Y spiegeln. Position X/Y ist die untere linke Ecke der Grafik auf dem Bett; die Grafik lässt sich in der
  Vorschau mit der Maus verschieben.
- Optimierung: Ebenen zusammenführen, Linien sortieren und zusammenfügen, vereinfachen, kurze Linien entfernen.
- Kalibrierung: Bettgröße, Düse im Nullpunkt, Sicherheitsabstand, Z-Höhen und Parkposition; die (i)-Symbole erklären
  die Felder. Der Stift steht bei Düse im Nullpunkt immer auf 0|0 des Betts.
  Plotbare Fläche = Bett − Nullpunkt − Sicherheitsabstand. Anleitung zum Ermitteln der Werte:
  [docs/calibrating.md](https://github.com/brianlow/plotter/blob/main/docs/calibrating.md) im Originalrepo.
- Drucker-Profile: Kalibrierung und Geschwindigkeiten als Profil speichern. „Prusa MK3S+“ ist das Standardprofil;
  es lässt sich überschreiben und mit „Zurücksetzen“ wiederherstellen. Profile liegen in `presets.json`,
  die übrigen Einstellungen im Browser.

## Hinweise

- Beim Streamen über USB kann der Drucker bei vielen kurzen Segmenten und hoher Geschwindigkeit ruckeln.
  Dann „Vereinfachen“ auf etwa 0,1 mm stellen oder die Zeichengeschwindigkeit senken.
- Der Server lauscht nur auf `127.0.0.1`.

## Credits

μplot by Daniel Myrenne. Idee, G-Code-Aufbau und Kalibrierverfahren stammen aus
[brianlow/plotter](https://github.com/brianlow/plotter) von Brian Low.
