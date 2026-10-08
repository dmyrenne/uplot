"""Wandelt PNG/JPG in ein plotbares SVG um.

Zwei Verfahren:
- Umriss: Schwellwert, dann zeichnet potrace die Konturen der dunklen Flächen nach (für Logos, Strichzeichnungen).
  Dünne, langgestreckte Flächen (Schrift, Linien) bekommen stattdessen einen Pfad durch die Mitte.
- Schraffur: Graustufen werden zu parallelen Linien; je dunkler, desto mehr Lagen in anderen Winkeln (für Fotos).
"""

import math

import numpy as np
import potrace
from PIL import Image, ImageFilter, ImageOps
from scipy import ndimage
from shapely.geometry import LineString

MAX_PX = 1000                     # längere Bildseite wird darauf verkleinert (potrace ist reines Python)
HATCH_ANGLES = [45, 135, 0, 90]   # Lage 1 (hellste Stufe) zuerst
STROKE_RATIO = 12                 # Fläche / (halbe Strichstärke)² ab der eine Fläche als Strich gilt


def load_gray(stream):
    """Bild als Graustufen-Array 0 (schwarz) … 1 (weiß); Transparenz wird weiß."""
    img = ImageOps.exif_transpose(Image.open(stream))
    if img.mode in ("RGBA", "LA", "P", "PA"):
        img = img.convert("RGBA")
        img = Image.alpha_composite(Image.new("RGBA", img.size, "white"), img)
    img = img.convert("L")
    img.thumbnail((MAX_PX, MAX_PX))
    return img


def to_svg(w, h, scale, paths):
    """SVG in Bildpixeln (viewBox); scale = mm pro Pixel."""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w * scale:.3f}mm" height="{h * scale:.3f}mm" '
            f'viewBox="0 0 {w} {h}">\n'
            f'<path fill="none" stroke="black" d="{" ".join(paths)}"/>\n</svg>\n')


def outline(img, threshold, centerline_px=0):
    """Konturen aller Flächen, die dunkler als threshold (0…1) sind. Striche bis centerline_px Breite
    (Schrift, Linien) werden stattdessen als Mittellinie gezeichnet."""
    dark = np.asarray(img) < threshold * 255
    paths = []
    if centerline_px > 0:
        strokes = find_strokes(dark, centerline_px)
        paths = centerlines(strokes)
        dark &= ~strokes
    curves = potrace.Bitmap(~dark).trace(turdsize=4)   # Bitmap invertiert: verfolgt wird das Dunkle
    p = lambda pt: f"{pt.x:.2f},{pt.y:.2f}"
    for c in curves:
        d = [f"M{p(c.start_point)}"]
        for s in c.segments:
            d.append(f"L{p(s.c)} {p(s.end_point)}" if s.is_corner
                     else f"C{p(s.c1)} {p(s.c2)} {p(s.end_point)}")
        paths.append("".join(d) + "Z")
    return paths


def find_strokes(dark, max_width):
    """Zusammenhängende dunkle Flächen, die schmal (bis max_width Pixel) und langgestreckt sind.
    Ein Punkt oder ein breiter Block hat ein kleines Verhältnis Fläche / Strichstärke², ein Buchstabe ein großes."""
    labels, n = ndimage.label(dark, structure=np.ones((3, 3)))
    if not n:
        return np.zeros_like(dark)
    ids = np.arange(1, n + 1)
    half = np.asarray(ndimage.maximum(ndimage.distance_transform_edt(dark), labels, ids))   # halbe Strichstärke
    area = np.asarray(ndimage.sum(dark, labels, ids))
    keep = ids[(2 * half - 1 <= max_width) & (area >= STROKE_RATIO * half ** 2)]
    return np.isin(labels, keep)


def skeletonize(mask):
    """Ausdünnen auf 1 Pixel breite Linien (Zhang-Suen)."""
    img = np.pad(mask, 1).astype(np.uint8)
    changed = True
    while changed:
        changed = False
        for step in (0, 1):
            p2, p3, p4 = img[:-2, 1:-1], img[:-2, 2:], img[1:-1, 2:]
            p5, p6, p7 = img[2:, 2:], img[2:, 1:-1], img[2:, :-2]
            p8, p9 = img[1:-1, :-2], img[:-2, :-2]
            ring = [p2, p3, p4, p5, p6, p7, p8, p9, p2]
            b = sum(ring[:8])
            a = sum((ring[i] == 0) & (ring[i + 1] == 1) for i in range(8))
            c = ((p2 * p4 * p6 == 0) & (p4 * p6 * p8 == 0) if step == 0
                 else (p2 * p4 * p8 == 0) & (p2 * p6 * p8 == 0))
            m = (img[1:-1, 1:-1] == 1) & (b >= 2) & (b <= 6) & (a == 1) & c
            if m.any():
                img[1:-1, 1:-1][m] = 0
                changed = True
    return img[1:-1, 1:-1].astype(bool)


def centerlines(strokes):
    """Skelett der Striche als Linienzüge; kurze Sporne an Ecken werden verworfen."""
    pix = set(zip(*np.nonzero(skeletonize(strokes))))
    dist = ndimage.distance_transform_edt(strokes)   # halbe Strichstärke je Pixel

    def nbrs(p):
        # m-Nachbarschaft: diagonal nur, wenn kein gemeinsamer gerader Nachbar existiert (keine Dreiecke)
        y, x = p
        out = [q for q in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)) if q in pix]
        for dy in (-1, 1):
            for dx in (-1, 1):
                if (y + dy, x + dx) in pix and (y + dy, x) not in pix and (y, x + dx) not in pix:
                    out.append((y + dy, x + dx))
        return out

    adj = {p: nbrs(p) for p in pix}
    seen, lines = set(), []

    def walk(start, nxt):
        line, prev, cur = [start], start, nxt
        seen.add(frozenset((start, nxt)))
        while True:
            line.append(cur)
            if len(adj[cur]) != 2 or cur == start:
                return line
            step = adj[cur][0] if adj[cur][1] == prev else adj[cur][1]
            if frozenset((cur, step)) in seen:
                return line
            seen.add(frozenset((cur, step)))
            prev, cur = cur, step

    for p in pix:   # von Enden und Kreuzungen aus
        if len(adj[p]) != 2:
            for q in adj[p]:
                if frozenset((p, q)) not in seen:
                    line = walk(p, q)
                    # Sporn: läuft von einer Kreuzung ins Leere und ist kaum länger als der Strich dort breit
                    spur = len(adj[line[0]]) > 2 and len(adj[line[-1]]) == 1 and len(line) < dist[line[0]]
                    if not spur:
                        lines.append(line)
    for p in pix:   # geschlossene Ringe ohne Kreuzung, z. B. „o“
        for q in adj[p]:
            if frozenset((p, q)) not in seen:
                lines.append(walk(p, q))

    paths = []
    for line in lines:
        pts = np.array([(x + 0.5, y + 0.5) for y, x in line], dtype=float)
        if len(pts) > 2:   # Pixeltreppen glätten, Enden bleiben
            pts[1:-1] = (pts[:-2] + pts[1:-1] + pts[2:]) / 3
        pts = LineString(pts).simplify(0.4).coords if len(pts) > 1 else pts
        paths.append("M" + "L".join(f"{x:.2f},{y:.2f}" for x, y in pts))
    return paths


def hatch(img, spacing_px, levels):
    """Schraffur: Stufe i bedeckt alle Pixel, deren Dunkelheit über (i+1)/(levels+1) liegt."""
    img = img.filter(ImageFilter.GaussianBlur(max(spacing_px / 3, 0.5)))   # Rauschen nicht als Strichel plotten
    dark = 1 - np.asarray(img, dtype=float) / 255
    w, h = img.size
    paths = []
    for i in range(levels):
        mask = Image.fromarray(((dark > (i + 1) / (levels + 1)) * 255).astype(np.uint8))
        angle = HATCH_ANGLES[i % len(HATCH_ANGLES)]
        # Maske drehen, waagrecht abtasten und die Strecken zurückdrehen
        rot = np.asarray(mask.rotate(angle, resample=Image.NEAREST, expand=True)) > 0
        rh, rw = rot.shape
        a = math.radians(angle)
        cos, sin = math.cos(a), math.sin(a)

        def back(x, y):
            # PIL dreht gegen den Uhrzeigersinn um die Bildmitte; hier die Umkehrung
            x, y = x - rw / 2, y - rh / 2
            return f"{x * cos - y * sin + w / 2:.2f},{x * sin + y * cos + h / 2:.2f}"

        for n, y in enumerate(np.arange(spacing_px / 2, rh, spacing_px)):
            row = np.concatenate(([False], rot[int(y)], [False]))
            edges = np.flatnonzero(row[1:] != row[:-1]).reshape(-1, 2)
            runs = [(x0, x1) for x0, x1 in edges if x1 - x0 >= 2]
            if n % 2:   # Zickzack: jede zweite Zeile rückwärts, spart Leerfahrten
                runs = [(x1, x0) for x0, x1 in reversed(runs)]
            paths += [f"M{back(x0, y)}L{back(x1, y)}" for x0, x1 in runs]
    return paths


def image_to_svg(stream, mode, area_w, area_h, threshold=0.5, spacing=1.0, levels=3, centerline=10.0):
    """SVG des Bilds, eingepasst in die plotbare Fläche area_w × area_h mm."""
    img = load_gray(stream)
    w, h = img.size
    scale = min(area_w / w, area_h / h)   # mm pro Pixel
    paths = (outline(img, threshold, centerline / scale) if mode == "outline"
             else hatch(img, spacing / scale, levels))
    return to_svg(w, h, scale, paths)
