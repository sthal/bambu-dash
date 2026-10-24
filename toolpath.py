"""Extrusion toolpaths from a sliced Bambu .gcode.3mf, packed small enough for a browser 3D preview.

Output (little-endian):
    b"TP1"  uint8 version
    6 x float32    bbox min xyz, max xyz (mm)
    uint32         layer count
    uint8          feature count, then per feature:
        uint8  name length, name (utf-8)
        uint32 segment count
        uint16 x6 per segment: x0 y0 z0 x1 y1 z1, quantized over the bbox
        uint16 per segment: layer number, so the viewer can show "printed up to layer N"
"""
import io
import math
import re
import struct
import zipfile

FEATURES = ["Outer wall", "Inner wall", "Overhang wall", "Top surface", "Bottom surface", "Internal solid infill",
            "Sparse infill", "Bridge", "Support", "Support interface", "Brim", "Skirt", "Prime tower", "Other"]
_ALIASES = {"Support transition": "Support", "Gap infill": "Inner wall", "Ironing": "Top surface",
            "Floating vertical shell": "Inner wall", "Internal Bridge": "Bridge", "Skirt": "Skirt",
            "Wipe tower": "Prime tower", "Prime tower": "Prime tower"}
_WORD = re.compile(r"([XYZEIJR])(-?\d*\.?\d+)")
MIN_SEG = 0.35  # mm; shorter extrusions are merged into the next point to keep the preview light


def _feature(name):
    name = name.strip()
    if name in FEATURES:
        return name
    return _ALIASES.get(name, "Other")


def gcode_from_3mf(data, plate=1):
    z = zipfile.ZipFile(io.BytesIO(data))
    for cand in (f"Metadata/plate_{plate}.gcode", "Metadata/plate_1.gcode"):
        if cand in z.namelist():
            return z.read(cand).decode("utf-8", "replace")
    return None


def build(gcode):
    """Parse G-code into packed segments; returns bytes (see module doc) or None."""
    x = y = z = 0.0
    relative_e = True
    feature = "Other"
    segs = {f: [] for f in FEATURES}
    order = []          # (feature, index) to rebuild a global segment index per layer
    layer_marks = []    # global segment count at each layer change
    total = 0
    pend = None         # start point of a merged short extrusion
    for line in gcode.splitlines():
        if not line:
            continue
        c = line[0]
        if c == ";":
            if line.startswith("; FEATURE:"):
                feature = _feature(line[10:])
            elif line.startswith("; CHANGE_LAYER") or line.startswith(";LAYER_CHANGE"):
                layer_marks.append(total)
            continue
        if c != "G" and c != "M":
            continue
        code = line.split(";", 1)[0].split()
        if not code:
            continue
        op = code[0]
        if op == "M83":
            relative_e = True
            continue
        if op == "M82":
            relative_e = False
            continue
        if op not in ("G0", "G1", "G2", "G3"):
            continue
        w = dict(_WORD.findall(line.split(";", 1)[0]))
        nx, ny, nz = float(w.get("X", x)), float(w.get("Y", y)), float(w.get("Z", z))
        e = float(w.get("E", 0))
        extruding = e > 0 if relative_e else False  # absolute E is rare on Bambu; skip rather than guess
        if extruding and (nx != x or ny != y):
            pts = [(nx, ny)]
            if op in ("G2", "G3") and "I" in w and "J" in w:
                pts = _arc(x, y, nx, ny, float(w["I"]), float(w["J"]), op == "G2")
            px, py = (pend if pend else (x, y))
            for (ax, ay) in pts:
                if math.hypot(ax - px, ay - py) < MIN_SEG and (ax, ay) != pts[-1]:
                    continue
                if math.hypot(ax - px, ay - py) < MIN_SEG:
                    pend = (px, py)
                    break
                segs[feature].append((px, py, nz, ax, ay, nz))
                order.append(feature)
                total += 1
                px, py = ax, ay
                pend = None
        else:
            pend = None
        x, y, z = nx, ny, nz
    if not total:
        return None
    return _pack(segs, order, layer_marks)


def _arc(x0, y0, x1, y1, i, j, clockwise):
    cx, cy = x0 + i, y0 + j
    r = math.hypot(i, j)
    a0, a1 = math.atan2(y0 - cy, x0 - cx), math.atan2(y1 - cy, x1 - cx)
    if clockwise:
        while a1 >= a0:
            a1 -= 2 * math.pi
    else:
        while a1 <= a0:
            a1 += 2 * math.pi
    n = max(1, min(32, int(abs(a1 - a0) * r / 1.0)))  # about 1 mm chords
    return [(cx + r * math.cos(a0 + (a1 - a0) * k / n), cy + r * math.sin(a0 + (a1 - a0) * k / n)) for k in range(1, n + 1)]


def _pack(segs, order, layer_marks):
    allpts = [s for f in FEATURES for s in segs[f]]
    xs = [p for s in allpts for p in (s[0], s[3])]
    ys = [p for s in allpts for p in (s[1], s[4])]
    zs = [p for s in allpts for p in (s[2], s[5])]
    lo, hi = (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))
    span = [max(1e-6, hi[k] - lo[k]) for k in range(3)]

    def q(v, k):
        return int(round((v - lo[k]) / span[k] * 65535))

    # Segments are stored grouped by feature, so the per-layer index must follow that same order.
    out = io.BytesIO()
    out.write(b"TP1" + bytes([1]))
    out.write(struct.pack("<6f", *lo, *hi))
    feats = [f for f in FEATURES if segs[f]]
    # layer index per segment, in the original (print) order
    layer_of = []
    li, mi = 0, 0
    for gi in range(len(order)):
        while mi < len(layer_marks) and layer_marks[mi] <= gi:
            li = mi
            mi += 1
        layer_of.append(li)
    per_feat_layers = {f: [] for f in feats}
    for gi, f in enumerate(order):
        per_feat_layers[f].append(layer_of[gi])
    nlayers = max(1, len(layer_marks))
    out.write(struct.pack("<I", nlayers))
    out.write(struct.pack("<B", len(feats)))
    for f in feats:
        name = f.encode()
        out.write(struct.pack("<B", len(name)) + name)
        out.write(struct.pack("<I", len(segs[f])))
        buf = bytearray()
        for s in segs[f]:
            buf += struct.pack("<6H", q(s[0], 0), q(s[1], 1), q(s[2], 2), q(s[3], 0), q(s[4], 1), q(s[5], 2))
        out.write(buf)
        # per segment layer number, so the viewer can show "printed up to layer N"
        out.write(struct.pack(f"<{len(segs[f])}H", *[min(65535, l) for l in per_feat_layers[f]]))
    return out.getvalue()
