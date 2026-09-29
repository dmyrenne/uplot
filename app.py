"""Web-Oberfläche für den Prusa-MK3S+-Stiftplotter-Workflow (nach github.com/brianlow/plotter).

Nimmt eine SVG-Datei entgegen, erzeugt eine vpype-Config aus den Kalibrierwerten,
lässt vpype/vpype-gcode den G-Code erzeugen und schickt ihn an den Browser zurück.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

BASE = Path(__file__).parent
VPYPE = str(Path(sys.executable).parent / "vpype")
EXAMPLES = BASE / "examples"
# Profile liegen neben der App oder, z. B. im Docker-Container, im Verzeichnis UPLOT_DATA
PRESETS_FILE = Path(os.environ.get("UPLOT_DATA", BASE)) / "presets.json"
BUILTIN_PRESET = "Prusa MK3S+"

app = Flask(__name__, static_folder=None)

# Fehlermeldungen in der Sprache der Oberfläche (Accept-Language); Texte der Oberfläche: static/i18n.js
MESSAGES = {
    "invalid_value": {"de": "Ungültiger Wert für {key}: {raw!r}", "en": "Invalid value for {key}: {raw!r}"},
    "empty_area": {"de": "Die plotbare Fläche ist leer. Nullpunkt und Sicherheitsabstand prüfen.",
                   "en": "The plot area is empty. Check the origin and safety margin."},
    "no_svg": {"de": "Keine SVG-Datei übergeben.", "en": "No SVG file received."},
    "vpype_failed": {"de": "vpype ist fehlgeschlagen:\n{out}", "en": "vpype failed:\n{out}"},
    "name_length": {"de": "Der Name muss 1–60 Zeichen lang sein.", "en": "The name must be 1–60 characters long."},
    "bad_preset": {"de": "Ungültiger oder fehlender Wert: {e}", "en": "Invalid or missing value: {e}"},
    "not_found": {"de": "Profil nicht gefunden.", "en": "Profile not found."},
}


def msg(key, **kw):
    lang = request.accept_languages.best_match(["de", "en"], default="en")
    return MESSAGES[key][lang].format(**kw)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

# Standardwerte = Werte aus plot.sh / vpype.toml / docs/calibrating.md des Originalrepos
DEFAULTS = {
    "offset_x": 45.0,       # Düse im Nullpunkt: Druckerkoordinate, bei der der Stift auf der unteren
    "offset_y": 38.0,       # linken Ecke des Betts (0|0) steht (translate in plot.sh)
    "bed_w": 250.0,         # Druckbett MK3S+
    "bed_h": 210.0,
    "safety": 2.0,          # Abstand zum rechten/hinteren Rand des erreichbaren Bereichs
    "z_down": 7.0,
    "z_up": 10.0,
    "z_travel": 30.0,
    "feed_z": 750.0,
    "feed_travel": 10000.0,
    "feed_draw": 10000.0,
    "park_x": 85.0,
    "park_y": 175.0,
    "mirror_x": False,      # Grafik spiegeln
    "mirror_y": False,
    "fit": False,           # auf plotbare Fläche skalieren
    "center": False,        # auf plotbarer Fläche zentrieren
    "pos_x": 0.0,           # untere linke Ecke der Grafik auf dem Bett (Stiftkoordinaten),
    "pos_y": 0.0,           # gilt, wenn nicht zentriert wird
    "margin": 0.0,
    "angle": 0.0,           # Drehung in Grad, im Uhrzeigersinn
    "merge_layers": True,
    "linemerge_tol": 0.05,
    "simplify_tol": 0.0,
    "min_length": 0.0,
    "linesort": True,
}

CONFIG_TEMPLATE = """[gwrite.plotter]
unit = "mm"
invert_y = false
invert_x = false

document_start = '''
; -- setup
M3                              ; start spindle
G21                             ; units are millimeters
G90                             ; absolute mode
G28 W                           ; home all without mesh bed level
G00 Z{z_travel:g} F{feed_z:g}   ; raise pen for travel
'''

layer_start = '''
; -- start layer --
'''

segment_first = '''
G00 X{{x:.4f}} Y{{y:.4f}} F{feed_travel:g}   ; move
G00 Z{z_down:g} F{feed_z:g}     ; pen down
'''

segment = '''
G01 X{{x:.4f}} Y{{y:.4f}} F{feed_draw:g}     ; draw
'''

segment_last = '''
G01 X{{x:.4f}} Y{{y:.4f}} F{feed_draw:g}     ; draw
G00 Z{z_up:g} F{feed_z:g}       ; pen up
'''

document_end = '''
; -- shutdown
G00 Z{z_travel:g} F{feed_z:g}   ; raise pen for travel
G00 X{park_x:g} Y{park_y:g} F{feed_travel:g}   ; move head out of the way
M5                              ; stop spindle
M84                             ; disable motors
M2                              ; program end
'''
"""


def parse_settings(form):
    s = {}
    for key, default in DEFAULTS.items():
        raw = form.get(key)
        if isinstance(default, bool):
            s[key] = raw in ("1", "true", "on") if raw is not None else default
        else:
            try:
                s[key] = float(raw) if raw not in (None, "") else default
            except ValueError:
                raise ValueError(msg("invalid_value", key=key, raw=raw))
    s["area_w"], s["area_h"] = plot_area(s)
    if s["area_w"] <= 0 or s["area_h"] <= 0:
        raise ValueError(msg("empty_area"))
    return s


def plot_area(s):
    """Plotbare Fläche: Der Stift steht bei Düse im Nullpunkt auf 0|0 des Betts. Weil er um den
    Nullpunkt versetzt zur Düse sitzt, erreicht er rechts/hinten entsprechend weniger vom Bett;
    davon geht noch der Sicherheitsabstand ab."""
    return (round(s["bed_w"] - s["offset_x"] - s["safety"], 3),
            round(s["bed_h"] - s["offset_y"] - s["safety"], 3))


def build_pipeline(s, svg_path, gcode_path):
    cmd = ["read", str(svg_path)]
    if s["merge_layers"]:
        cmd += ["lmove", "all", "1"]
    if s["mirror_x"] or s["mirror_y"]:
        # um die Mitte der Grafik spiegeln
        cmd += ["scale", "--", "-1" if s["mirror_x"] else "1", "-1" if s["mirror_y"] else "1"]
    if s["angle"] % 360:
        cmd += ["rotate", f"{s['angle'] % 360:g}"]
    page = [f"{s['area_w']}x{s['area_h']}mm"]
    if s["area_w"] > s["area_h"]:
        page = ["--landscape"] + page
    if s["fit"] or s["center"]:
        # skalieren und/oder mittig auf die plotbare Fläche legen
        cmd += ["layout"] + (["--fit-to-margins", f"{s['margin']}mm"] if s["fit"] else []) + page
    if not s["center"]:
        # untere linke Ecke der Grafik auf 0|0 legen, danach um die Position verschieben.
        # SVG-y zeigt nach unten, "bottom" liegt nach der Umrechnung unten auf dem Drucker.
        cmd += ["layout", "--align", "left", "--valign", "bottom"] + page
    # SVG hat y nach unten, der Drucker y nach oben -> an der Flächenmitte umklappen
    cmd += ["scale", "--origin", "0", f"{s['area_h'] / 2}mm", "--", "1", "-1"]
    dx, dy = s["offset_x"], s["offset_y"]
    if not s["center"]:
        dx, dy = dx + s["pos_x"], dy + s["pos_y"]
    cmd += ["translate", f"{dx:g}mm", f"{dy:g}mm"]
    if s["simplify_tol"] > 0:
        cmd += ["linesimplify", "--tolerance", f"{s['simplify_tol']}mm"]
    if s["min_length"] > 0:
        cmd += ["filter", "--min-length", f"{s['min_length']}mm"]
    cmd += ["linemerge", "--tolerance", f"{s['linemerge_tol']}mm"]
    if s["linesort"]:
        cmd += ["linesort"]
    cmd += ["gwrite", "--profile", "plotter", str(gcode_path)]
    return cmd


@app.get("/")
def index():
    return send_from_directory(BASE / "static", "index.html")


@app.get("/static/<path:name>")
def static_file(name):
    return send_from_directory(BASE / "static", name)


@app.get("/api/defaults")
def defaults():
    examples = sorted(p.name for p in EXAMPLES.glob("*.svg"))
    return jsonify(defaults=DEFAULTS, examples=examples)


# ---------- Drucker-Profile ----------
# Ein Profil enthält nur die maschinenbezogenen Werte (Kalibrierung + Geschwindigkeiten),
# nicht Layout/Optimierung, die zur jeweiligen Zeichnung gehören.
MACHINE_KEYS = ["bed_w", "bed_h", "offset_x", "offset_y", "safety", "z_down", "z_up", "z_travel",
                "park_x", "park_y", "feed_draw", "feed_travel", "feed_z"]


def load_presets():
    try:
        return json.loads(PRESETS_FILE.read_text())
    except FileNotFoundError:
        return {}


def store_presets(presets):
    tmp = PRESETS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(presets, indent=2, ensure_ascii=False))
    os.replace(tmp, PRESETS_FILE)


@app.get("/api/presets")
def list_presets():
    # Das Standardprofil darf überschrieben werden; die Änderung liegt dann unter seinem Namen in
    # presets.json, und "Löschen" setzt es auf die Werkseinstellung zurück.
    user = load_presets()
    override = user.pop(BUILTIN_PRESET, None)
    builtin = {k: DEFAULTS[k] for k in MACHINE_KEYS} | (override or {})
    return jsonify(builtin=BUILTIN_PRESET,
                   presets=[{"name": BUILTIN_PRESET, "builtin": True, "customized": override is not None,
                             "settings": builtin}] +
                           [{"name": n, "builtin": False, "settings": v} for n, v in sorted(user.items())])


@app.put("/api/presets/<path:name>")
def save_preset(name):
    name = name.strip()
    if not name or len(name) > 60:
        return jsonify(error=msg("name_length")), 400
    data = request.get_json(silent=True) or {}
    try:
        settings = {k: float(data[k]) for k in MACHINE_KEYS}
    except (KeyError, TypeError, ValueError) as e:
        return jsonify(error=msg("bad_preset", e=e)), 400
    presets = load_presets()
    presets[name] = settings
    store_presets(presets)
    return jsonify(ok=True)


@app.delete("/api/presets/<path:name>")
def delete_preset(name):
    presets = load_presets()
    if presets.pop(name, None) is None:
        return jsonify(error=msg("not_found")), 404
    store_presets(presets)
    return jsonify(ok=True)


@app.get("/api/example/<name>")
def example(name):
    return send_from_directory(EXAMPLES, name, mimetype="image/svg+xml")


@app.post("/api/convert")
def convert():
    upload = request.files.get("svg")
    if upload is None:
        return jsonify(error=msg("no_svg")), 400
    try:
        s = parse_settings(request.form)
    except ValueError as e:
        return jsonify(error=str(e)), 400

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        svg_path, gcode_path, cfg_path = tmp / "in.svg", tmp / "out.gcode", tmp / "vpype.toml"
        upload.save(svg_path)
        cfg_path.write_text(CONFIG_TEMPLATE.format(**s))
        pipeline = build_pipeline(s, svg_path, gcode_path)
        proc = subprocess.run(
            [VPYPE, "--config", str(cfg_path), *pipeline],
            capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0 or not gcode_path.exists():
            return jsonify(error=msg("vpype_failed", out=(proc.stderr or proc.stdout)[-3000:])), 500
        # Befehlszeile zur Anzeige, mit neutralen Dateinamen statt Temp-Pfaden
        shown = " ".join(
            "input.svg" if a == str(svg_path) else "output.gcode" if a == str(gcode_path) else a
            for a in pipeline
        )
        return jsonify(gcode=gcode_path.read_text(), command="vpype --config vpype.toml " + shown,
                       settings=s)


if __name__ == "__main__":
    import argparse
    import webbrowser

    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5055)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    if not args.no_browser:
        webbrowser.open(f"http://127.0.0.1:{args.port}")
    app.run(host=args.host, port=args.port)
