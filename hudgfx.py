"""Gauge panel for the stream HUD: the dashboard's arc gauges drawn into an RGBA PNG without image libraries.

Only the pixels around the arcs are recomputed on an update (a few thousand per gauge), so redrawing every
couple of seconds stays cheap even on a Raspberry Pi. Numbers and labels are not in the picture: ffmpeg
draws them with drawtext on top (see text_layout).
"""
import math
import struct
import zlib

CW, CH, GAP = 176, 150, 12   # one gauge card and the space between cards, px
CX, CY = CW / 2, 90          # arc centre inside a card
R, SW, GLOW = 57, 9, 7       # arc radius, stroke width, glow width
SPAN = 270                   # degrees, opening at the bottom like the dashboard
BG = (14, 16, 36, 205)
BORDER = (255, 255, 255, 34)
TRACK = (255, 255, 255, 20)
STOPS = [(72, 224, 255), (157, 123, 255), (255, 95, 168), (255, 181, 71)]  # same as heatColor() on the page


def heat(f):
    f = min(max(f, 0.0), 1.0) * (len(STOPS) - 1)
    i = min(int(f), len(STOPS) - 2)
    t = f - i
    return tuple(round(a + (b - a) * t) for a, b in zip(STOPS[i], STOPS[i + 1]))


def _point(deg_from_start, r=R):
    a = math.radians(135 + deg_from_start)
    return CX + r * math.cos(a), CY + r * math.sin(a)


def _seg_dist(px, py, ax, ay, bx, by):
    vx, vy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * vx + (py - ay) * vy) / (vx * vx + vy * vy)))
    return math.hypot(px - ax - t * vx, py - ay - t * vy)


def _blend(buf, i, rgb, a):
    """Source-over of colour rgb with alpha a (0..1) onto the RGBA pixel at byte offset i."""
    if a <= 0:
        return
    da = buf[i + 3] / 255
    oa = a + da * (1 - a)
    for k in range(3):
        buf[i + k] = round((rgb[k] * a + buf[i + k] * da * (1 - a)) / oa)
    buf[i + 3] = round(oa * 255)


class Panel:
    @staticmethod
    def width(n):
        return n * CW + (n - 1) * GAP

    def __init__(self, n):
        self.n, self.w, self.h = n, self.width(n), CH
        self.bg = bytearray(self.w * self.h * 4)
        for g in range(n):
            self._card(g * (CW + GAP))
        # pixels near the arc, once: (offset inside a card, x, y, distance to the centre, degrees from the arc start)
        band = SW / 2 + GLOW + 1
        self.ring = []
        for y in range(CH):
            for x in range(CW):
                px, py = x + 0.5, y + 0.5
                d = math.hypot(px - CX, py - CY)
                if abs(d - R) > band + 6:  # +6: the target tick reaches a bit further than the stroke
                    continue
                a = (math.degrees(math.atan2(py - CY, px - CX)) - 135) % 360
                self.ring.append(((y * self.w + x) * 4, px, py, d, a))

    def _card(self, x0):
        r = 14
        for y in range(CH):
            for x in range(CW):
                # signed distance to the rounded rectangle's edge (negative inside)
                qx, qy = abs(x + 0.5 - CW / 2) - (CW / 2 - r), abs(y + 0.5 - CH / 2) - (CH / 2 - r)
                d = math.hypot(max(qx, 0), max(qy, 0)) + min(max(qx, qy), 0) - r
                inside = min(max(0.5 - d, 0), 1)
                if not inside:
                    continue
                i = (y * self.w + x0 + x) * 4
                self.bg[i:i + 4] = bytes((*BG[:3], round(BG[3] * inside)))
                edge = min(max(1.5 - abs(d + 0.5), 0), 1)  # 1px border along the inside edge
                _blend(self.bg, i, BORDER[:3], BORDER[3] / 255 * edge)

    @staticmethod
    def _arc_dist(px, py, d, a, upto):
        """Distance from a pixel to the stroke's centre line of an arc from 0 to `upto` degrees, round caps."""
        if a <= upto:
            return abs(d - R)
        sx, sy = _point(0)
        ex, ey = _point(upto)
        return min(math.hypot(px - sx, py - sy), math.hypot(px - ex, py - ey))

    def render(self, gauges):
        """gauges: [(fraction 0..1 or None, target fraction or None)], one per card. Returns PNG bytes."""
        buf = bytearray(self.bg)
        half = SW / 2
        for g, (f, tgt) in enumerate(gauges[:self.n]):
            base = g * (CW + GAP) * 4
            col = heat(f or 0)
            upto = SPAN * min(max(f or 0, 0), 1)
            tick = None
            if tgt is not None:
                tick = (*_point(SPAN * min(max(tgt, 0), 1), R + 8), *_point(SPAN * min(max(tgt, 0), 1), R - 8))
            for off, px, py, d, a in self.ring:
                i = base + off
                cov = min(max(half - self._arc_dist(px, py, d, a, SPAN) + 0.5, 0), 1)
                _blend(buf, i, TRACK[:3], TRACK[3] / 255 * cov)
                if f:
                    dv = self._arc_dist(px, py, d, a, upto)
                    if dv > half:
                        _blend(buf, i, col, 0.38 * max(0.0, 1 - (dv - half) / GLOW) ** 2)
                    _blend(buf, i, col, min(max(half - dv + 0.5, 0), 1))
                if tick:
                    _blend(buf, i, (255, 255, 255), min(max(2 - _seg_dist(px, py, *tick), 0), 1))
        return png(self.w, self.h, buf)


def text_layout(n):
    """Where ffmpeg puts the text of card g, relative to the panel's top-left corner:
    (label x, label y), (value centre x, value y), (sub centre x, sub y)."""
    return [((g * (CW + GAP) + 16, 14), (g * (CW + GAP) + CX, CY - 21), (g * (CW + GAP) + CX, CY + 15)) for g in range(n)]


def png(w, h, rgba):
    raw = b"".join(b"\x00" + bytes(rgba[y * w * 4:(y + 1) * w * 4]) for y in range(h))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b""))
