"""Web-Oberfläche für den Prusa-MK3S+-Stiftplotter-Workflow (nach github.com/brianlow/plotter).

Nimmt eine SVG-Datei entgegen, erzeugt eine vpype-Config aus den Kalibrierwerten,
lässt vpype/vpype-gcode den G-Code erzeugen und schickt ihn an den Browser zurück.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from PIL import Image

import raster

BASE = Path(__file__).parent
VPYPE = str(Path(sys.executable).parent / "vpype")
EXAMPLES = BASE / "examples"
# Profile liegen neben der App oder, z. B. im Docker-Container, im Verzeichnis UPLOT_DATA
DATA = Path(os.environ.get("UPLOT_DATA", BASE))
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
    "no_image": {"de": "Keine Bilddatei übergeben.", "en": "No image file received."},
    "bad_image": {"de": "Das Bild lässt sich nicht lesen (nur PNG und JPG).",
                  "en": "The image could not be read (PNG and JPG only)."},
}


def msg(name, **kw):
    lang = request.accept_languages.best_match(["de", "en"], default="en")
    return MESSAGES[name][lang].format(**kw)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

# Standardwerte = Werte aus plot.sh / vpype.toml / docs/calibrating.md des Originalrepos
DEFAULTS = {
    "machine": "printer",   # "printer" (Marlin/Prusa) oder "axidraw" (AxiDraw/NextDraw über µprint)
    "offset_x": 45.0,       # Düse im Nullpunkt: Druckerkoordinate, bei der der Stift auf der unteren
    "offset_y": 38.0,       # linken Ecke des Betts (0|0) steht (translate in plot.sh)
    "bed_w": 250.0,         # Druckbett MK3S+
    "bed_h": 210.0,
    "safety": 2.0,          # Abstand zum Rand des erreichbaren Bereichs, auf allen vier Seiten gleich ...
    "safety_split": False,  # ... oder je Seite (oben/rechts/unten/links, wie in der Vorschau zu sehen)
    "safety_top": 2.0,
    "safety_right": 2.0,
    "safety_bottom": 2.0,
    "safety_left": 2.0,
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
    "width": 0.0,           # Größe der Grafik in mm (nach dem Drehen), 0 = Originalgröße;
    "height": 0.0,          # gilt nur ohne "fit"
    "keep_ratio": True,     # Proportionen beibehalten: in width × height einpassen statt verzerren
    "merge_layers": True,   # in der Oberfläche ausgeblendet, bis es Stifte pro Ebene gibt
    "linemerge_tol": 0.05,
    "simplify_tol": 0.0,
    "min_length": 0.0,
    "linesort": True,
}

# Umwandlung von PNG/JPG in SVG (raster.py); Schwelle in Prozent Helligkeit
TRACE_DEFAULTS = {"trace_mode": "hatch", "trace_threshold": 50.0, "trace_spacing": 1.0, "trace_levels": 3.0,
                  "trace_centerline": 10.0}

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

# AxiDraw-Modus, Vertrag mit µprint (uplot#1): erste Zeile ist die Formatkennung, Z0 = Stift unten,
# Z1 = Stift oben, keine Referenzfahrt und keine Spindel, am Ende zurück auf den Nullpunkt, weil das
# AxiDraw seine Position nur relativ zur Einschaltposition kennt.
AXIDRAW_TEMPLATE = """[gwrite.plotter]
unit = "mm"
invert_y = false
invert_x = false

document_start = '''; uplot-axidraw 1
G21                             ; units are millimeters
G90                             ; absolute mode
G00 Z1                          ; pen up
'''

layer_start = '''
; -- start layer --
'''

segment_first = '''
G00 X{{x:.4f}} Y{{y:.4f}} F{feed_travel:g}   ; move
G00 Z0                          ; pen down
'''

segment = '''
G01 X{{x:.4f}} Y{{y:.4f}} F{feed_draw:g}     ; draw
'''

segment_last = '''
G01 X{{x:.4f}} Y{{y:.4f}} F{feed_draw:g}     ; draw
G00 Z1                          ; pen up
'''

document_end = '''
; -- shutdown
G00 Z1                          ; pen up
G00 X0 Y0 F{feed_travel:g}      ; back to origin
M84                             ; disable motors
M2                              ; program end
'''
"""

# Zeichenflächen der AxiDraw-/NextDraw-Modelle in mm (x_travel_*/y_travel_* aus axidraw_conf.py von
# Evil Mad Scientist; NextDraw laut Bantam Tools baugleich mit den AxiDraw-Größen)
AXIDRAW_MODELS = [
    {"name": "AxiDraw V3, SE/A4 · NextDraw 8511", "w": 300.0, "h": 218.0},
    {"name": "AxiDraw V3/A3, SE/A3 · NextDraw 1117", "w": 430.0, "h": 297.0},
    {"name": "AxiDraw V3 XLX", "w": 595.0, "h": 218.0},
    {"name": "AxiDraw SE/A2", "w": 594.0, "h": 432.0},
    {"name": "AxiDraw SE/A1 · NextDraw 2234", "w": 864.0, "h": 594.0},
    {"name": "AxiDraw V3/B6", "w": 190.0, "h": 140.0},
    {"name": "AxiDraw MiniKit", "w": 160.0, "h": 101.6},
]

# Im AxiDraw-Modus fest: kein Düsen-Stift-Versatz, Stifthöhen laut Vertrag mit µprint
AXIDRAW_FIXED = {"offset_x": 0.0, "offset_y": 0.0, "z_down": 0.0, "z_up": 1.0, "z_travel": 1.0}


def clean_machine(raw):
    return "axidraw" if raw == "axidraw" else "printer"


def parse_settings(form):
    s = {}
    for key, default in DEFAULTS.items():
        raw = form.get(key)
        if key == "machine":
            s[key] = clean_machine(raw)
        elif isinstance(default, bool):
            s[key] = raw in ("1", "true", "on") if raw is not None else default
        else:
            try:
                s[key] = float(raw) if raw not in (None, "") else default
            except ValueError:
                raise ValueError(msg("invalid_value", key=key, raw=raw))
    if s["machine"] == "axidraw":
        s |= AXIDRAW_FIXED
    s["area_w"], s["area_h"], s["area_x"], s["area_y"] = plot_area(s)
    if s["area_w"] <= 0 or s["area_h"] <= 0:
        raise ValueError(msg("empty_area"))
    return s


def margins(s):
    """Sicherheitsabstände in Stiftkoordinaten: (x am Nullpunkt, x gegenüber, y am Nullpunkt, y gegenüber).
    Oben/unten meint die Vorschau: Beim Drucker liegt der Nullpunkt unten, beim AxiDraw oben."""
    if s["safety_split"]:
        top, right, bottom, left = (s[f"safety_{k}"] for k in ("top", "right", "bottom", "left"))
    else:
        top = right = bottom = left = s["safety"]
    if s["machine"] == "axidraw":
        return left, right, top, bottom
    return left, right, bottom, top


def plot_area(s):
    """Plotbare Fläche: Der Stift steht bei Düse im Nullpunkt auf 0|0 des Betts. Weil er um den
    Nullpunkt versetzt zur Düse sitzt, erreicht er rechts/hinten entsprechend weniger vom Bett;
    davon gehen auf allen Seiten die Sicherheitsabstände ab. Beim AxiDraw ist der Versatz 0, die Fläche
    ist dann direkt die Zeichenfläche. Liefert Breite, Höhe und die Ecke am Nullpunkt (Stiftkoordinaten)."""
    x0, x1, y0, y1 = margins(s)
    return (round(s["bed_w"] - s["offset_x"] - x0 - x1, 3), round(s["bed_h"] - s["offset_y"] - y0 - y1, 3),
            x0, y0)


def build_pipeline(s, svg_path, gcode_path):
    axidraw = s["machine"] == "axidraw"
    cmd = ["read", str(svg_path)]
    if s["merge_layers"]:
        cmd += ["lmove", "all", "1"]
    if s["mirror_x"] or s["mirror_y"]:
        # um die Mitte der Grafik spiegeln
        cmd += ["scale", "--", "-1" if s["mirror_x"] else "1", "-1" if s["mirror_y"] else "1"]
    if s["angle"] % 360:
        cmd += ["rotate", f"{s['angle'] % 360:g}"]
    if not s["fit"] and s["width"] > 0 and s["height"] > 0:
        cmd += ["scaleto"] + ([] if s["keep_ratio"] else ["--fit-dimensions"]) + [f"{s['width']}mm", f"{s['height']}mm"]
    page = [f"{s['area_w']}x{s['area_h']}mm"]
    if s["area_w"] > s["area_h"]:
        page = ["--landscape"] + page
    if s["fit"] or s["center"]:
        # skalieren und/oder mittig auf die plotbare Fläche legen
        cmd += ["layout"] + (["--fit-to-margins", f"{s['margin']}mm"] if s["fit"] else []) + page
    if not s["center"]:
        # Ecke der Grafik am Nullpunkt auf 0|0 legen, danach um die Position verschieben. Drucker: untere
        # linke Ecke (SVG-y zeigt nach unten, "bottom" liegt nach der Umrechnung unten auf dem Drucker).
        # AxiDraw: Nullpunkt oben links, y zeigt wie im SVG nach unten -> obere linke Ecke.
        cmd += ["layout", "--align", "left", "--valign", "top" if axidraw else "bottom"] + page
    if not axidraw:
        # SVG hat y nach unten, der Drucker y nach oben -> an der Flächenmitte umklappen
        cmd += ["scale", "--origin", "0", f"{s['area_h'] / 2}mm", "--", "1", "-1"]
    # auf die Ecke der plotbaren Fläche legen; die Position zählt ab dieser Ecke
    dx, dy = s["offset_x"] + s["area_x"], s["offset_y"] + s["area_y"]
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
    return jsonify(defaults=DEFAULTS | PEN_DEFAULTS | TRACE_DEFAULTS, examples=examples,
                   axidraw_models=AXIDRAW_MODELS)


# ---------- Profile ----------
# Drucker-Profile enthalten nur die maschinenbezogenen Werte (Kalibrierung + Geschwindigkeiten),
# nicht Layout/Optimierung, die zur jeweiligen Zeichnung gehören. Stift-Profile enthalten Farbe und
# Strichbreite; sie wirken nur auf die Vorschau.
MACHINE_KEYS = ["machine", "bed_w", "bed_h", "offset_x", "offset_y", "safety", "safety_split", "safety_top",
                "safety_right", "safety_bottom", "safety_left", "z_down", "z_up", "z_travel",
                "park_x", "park_y", "feed_draw", "feed_travel", "feed_z"]
PEN_DEFAULTS = {"pen_color": "#1d1d1b", "pen_width": 0.4}
COLOR = re.compile(r"#[0-9a-f]{6}")


def clean_printer(data):
    out = {k: float(data[k]) for k in MACHINE_KEYS if k not in ("machine", "safety_split")}
    return {"machine": clean_machine(data.get("machine")), "safety_split": data.get("safety_split") is True} | out


def clean_pen(data):
    color = str(data["pen_color"]).lower()
    width = float(data["pen_width"])
    if not COLOR.fullmatch(color) or not 0 < width <= 20:
        raise ValueError(f"pen_color={color!r}, pen_width={width:g}")
    return {"pen_color": color, "pen_width": width}


KINDS = {
    "printer": {"file": "presets.json", "builtin": BUILTIN_PRESET,
                "defaults": {k: DEFAULTS[k] for k in MACHINE_KEYS},
                "clean": clean_printer},
    "pen": {"file": "pens.json", "builtin": "Fineliner 0.4 mm", "defaults": PEN_DEFAULTS, "clean": clean_pen},
}


def preset_file(kind):
    return DATA / KINDS[kind]["file"]


def load_presets(kind):
    try:
        return json.loads(preset_file(kind).read_text())
    except FileNotFoundError:
        return {}


def store_presets(kind, presets):
    path = preset_file(kind)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(presets, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


@app.get("/api/presets", defaults={"kind": "printer"})
@app.get("/api/pens", defaults={"kind": "pen"})
def list_presets(kind):
    # Das Standardprofil darf überschrieben werden; die Änderung liegt dann unter seinem Namen in der
    # Profildatei, und "Löschen" setzt es auf die Werkseinstellung zurück.
    k = KINDS[kind]
    user = load_presets(kind)
    override = user.pop(k["builtin"], None)
    # ältere Profile ohne neuere Felder (z. B. machine) mit den Standardwerten ergänzen
    builtin = k["defaults"] | (override or {})
    return jsonify(builtin=k["builtin"],
                   presets=[{"name": k["builtin"], "builtin": True, "customized": override is not None,
                             "settings": builtin}] +
                           [{"name": n, "builtin": False, "settings": k["defaults"] | v} for n, v in sorted(user.items())])


@app.put("/api/presets/<path:name>", defaults={"kind": "printer"})
@app.put("/api/pens/<path:name>", defaults={"kind": "pen"})
def save_preset(kind, name):
    name = name.strip()
    if not name or len(name) > 60:
        return jsonify(error=msg("name_length")), 400
    data = request.get_json(silent=True) or {}
    try:
        settings = KINDS[kind]["clean"](data)
    except (KeyError, TypeError, ValueError) as e:
        return jsonify(error=msg("bad_preset", e=e)), 400
    presets = load_presets(kind)
    presets[name] = settings
    store_presets(kind, presets)
    return jsonify(ok=True)


@app.delete("/api/presets/<path:name>", defaults={"kind": "printer"})
@app.delete("/api/pens/<path:name>", defaults={"kind": "pen"})
def delete_preset(kind, name):
    presets = load_presets(kind)
    if presets.pop(name, None) is None:
        return jsonify(error=msg("not_found")), 404
    store_presets(kind, presets)
    return jsonify(ok=True)


@app.get("/api/example/<name>")
def example(name):
    return send_from_directory(EXAMPLES, name, mimetype="image/svg+xml")


@app.post("/api/trace")
def trace():
    upload = request.files.get("image")
    if upload is None:
        return jsonify(error=msg("no_image")), 400
    def num(key, lo, hi):
        raw = request.form.get(key)
        try:
            v = float(raw) if raw not in (None, "") else TRACE_DEFAULTS[key]
        except ValueError:
            raise ValueError(msg("invalid_value", key=key, raw=raw))
        return min(max(v, lo), hi)

    try:
        s = parse_settings(request.form)
        mode = "outline" if request.form.get("trace_mode") == "outline" else "hatch"
        threshold = num("trace_threshold", 0, 100) / 100
        spacing = num("trace_spacing", 0.2, 20)
        levels = int(num("trace_levels", 1, 4))
        centerline = num("trace_centerline", 0, 100)
    except ValueError as e:
        return jsonify(error=str(e)), 400
    try:
        svg = raster.image_to_svg(upload.stream, mode, s["area_w"], s["area_h"], threshold, spacing, levels,
                                  centerline)
    except (OSError, Image.DecompressionBombError):
        return jsonify(error=msg("bad_image")), 400
    return jsonify(svg=svg)


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
        template = AXIDRAW_TEMPLATE if s["machine"] == "axidraw" else CONFIG_TEMPLATE
        cfg_path.write_text(template.format(**s))
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
