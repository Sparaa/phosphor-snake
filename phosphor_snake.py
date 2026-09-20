#!/usr/bin/env python3
"""PHOSPHOR SNAKE — snake on a red-phosphor CRT terminal.

Native Linux (GTK3 window, pycairo rendering, zero extra deps beyond python3-gi + pycairo + Pillow).
The look is the GPU Pulse / Soundscape "pulse" family: #ff2a3d phosphor on near-black, scanlines, a rolling
CRT band, a vignetted bezel, glowing text, and a segmented spectrum analyzer with phosphor persistence and
peak hold — here the "spectrum" is the snake's body mass per column, so it dances as you play.

Keys: arrows / WASD / HJKL move · Space start / pause · R restart · F or F11 fullscreen · Q / Esc quit.
Flags: --wrap (no walls) · --cells 32x22 · --screenshot out.png [--size WxH] · --bench (3 s, prints fps).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from collections import deque

import cairo

# ---------------------------------------------------------------------------------------------- palette (pulse scene)
def rgb(h: int) -> tuple[float, float, float]:
    return ((h >> 16 & 255) / 255, (h >> 8 & 255) / 255, (h & 255) / 255)

def mix(a, b, t):
    t = max(0.0, min(1.0, t))
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)

def scale(c, k):
    return (min(1, c[0] * k), min(1, c[1] * k), min(1, c[2] * k))

BOOST = 1.5               # the lit phosphor sits 50 % above the scene palette so the red pops on a dark room's monitor
BG = rgb(0x050001)        # window / outside the bezel
DISC = rgb(0x080102)      # the field
GRID = rgb(0x2a070c)      # scope furniture
LINE = rgb(0x3a0a10)
DIM = rgb(0x1d0508)       # embers
TRAIL = scale(rgb(0x7a101c), BOOST)   # phosphor persistence
LIT = scale(rgb(0xd8182c), BOOST)     # lit segments
HOT = scale(rgb(0xff4455), BOOST)     # the tip
PEAK = scale(rgb(0xff6070), BOOST)    # peak-hold marker
BLOOM = rgb(0xff2a3c)
TEXT = scale(rgb(0xff2a3d), BOOST)
TEXT_DIM = scale(rgb(0xa0141f), BOOST)
BEZEL = scale(rgb(0xd8182c), BOOST)
SNAKE = LIT                           # one colour, head to tail
FONT = "DejaVu Sans Mono"

# ---------------------------------------------------------------------------------------------- game
DIRS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
OPPOSITE = {"up": "down", "down": "up", "left": "right", "right": "left"}
HIGHSCORE = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "phosphor-snake", "highscore.json")


class Game:
    """Grid state + the analog quantities the renderer animates (embers, hit flash, spectrum levels)."""

    def __init__(self, cols=32, rows=22, wrap=False, seed=None):
        self.cols, self.rows, self.wrap = cols, rows, wrap
        self.rng = random.Random(seed)
        self.best = self._load_best()
        self.log: deque[tuple[float, str]] = deque(maxlen=6)
        self.level = [0.0] * cols      # spectrum: smoothed body mass per column
        self.peak = [0.0] * cols
        self.t = 0.0
        self.reset(attract=True)

    # -- persistence
    def _load_best(self) -> int:
        try:
            with open(HIGHSCORE) as f:
                return int(json.load(f).get("best", 0))
        except (OSError, ValueError):
            return 0

    def _save_best(self) -> None:
        try:
            os.makedirs(os.path.dirname(HIGHSCORE), exist_ok=True)
            with open(HIGHSCORE, "w") as f:
                json.dump({"best": self.best}, f)
        except OSError:
            pass

    # -- lifecycle
    def reset(self, attract=False) -> None:
        cx, cy = self.cols // 2, self.rows // 2
        self.snake: deque[tuple[int, int]] = deque([(cx - i, cy) for i in range(4)])   # head first, heading right
        self.direction = "right"
        self.queue: deque[str] = deque()
        self.score = 0
        self.eaten = 0
        self.embers: dict[tuple[int, int], float] = {}
        self.hit = 0.0             # eat flash (1 → 0)
        self.burst = 0.0           # death flash
        self.state = "attract" if attract else "playing"
        self.acc = 0.0
        self.food = self._spawn()
        self.started_at = self.t
        if not attract:
            self.say("LINK ENGAGED")

    def say(self, msg: str) -> None:
        self.log.append((self.t, msg))

    @property
    def length(self) -> int:
        return len(self.snake)

    @property
    def step_ms(self) -> float:
        """Speed ramps with food eaten: 150 ms → 60 ms per step."""
        return max(60.0, 150.0 - 4.0 * self.eaten)

    @property
    def speed(self) -> float:
        """0..1 for the SPEED meter."""
        return (150.0 - self.step_ms) / 90.0

    def _spawn(self) -> tuple[int, int]:
        body = set(self.snake)
        free = [(x, y) for x in range(self.cols) for y in range(self.rows) if (x, y) not in body]
        return self.rng.choice(free) if free else (-1, -1)

    # -- input
    def turn(self, d: str) -> None:
        last = self.queue[-1] if self.queue else self.direction
        if d == last or d == OPPOSITE[last] or len(self.queue) >= 2:
            return
        self.queue.append(d)

    def toggle(self) -> None:
        if self.state == "attract":
            self.reset(); self.state = "playing"
        elif self.state == "playing":
            self.state = "paused"; self.say("HOLD")
        elif self.state == "paused":
            self.state = "playing"; self.say("RESUME")
        elif self.state == "over":
            self.reset()

    # -- simulation
    def next_head(self) -> tuple[int, int]:
        """The cell the head is moving into (unwrapped: may lie one past the field edge)."""
        dx, dy = DIRS[self.direction]
        hx, hy = self.snake[0]
        return hx + dx, hy + dy

    def will_grow(self) -> bool:
        nx, ny = self.next_head()
        if self.wrap:
            nx, ny = nx % self.cols, ny % self.rows
        return (nx, ny) == self.food

    @property
    def progress(self) -> float:
        """0..1: how far the snake is between its last cell boundary and the next (drives the smooth render)."""
        return 0.0 if self.state == "attract" else max(0.0, min(1.0, self.acc / self.step_ms))

    def step(self) -> None:
        # `direction` was latched at the previous boundary, so what the frame drew the head gliding toward is the
        # cell it now enters; a queued turn takes effect from here (the next glide), never mid-cell.
        nx, ny = self.next_head()
        if self.wrap:
            nx, ny = nx % self.cols, ny % self.rows
        elif not (0 <= nx < self.cols and 0 <= ny < self.rows):
            return self._die("WALL CONTACT")
        grow = (nx, ny) == self.food
        body = list(self.snake)
        if not grow:
            body.pop()                     # the tail moves away this step, so its cell is not a collision
        if (nx, ny) in body:
            return self._die("SELF CONTACT")
        self.snake.appendleft((nx, ny))
        if self.queue:
            self.direction = self.queue.popleft()
        if grow:
            self.eaten += 1
            gained = 10 + self.length // 5
            self.score += gained
            self.hit = 1.0
            self.food = self._spawn()
            self.say(f"TARGET ACQUIRED  +{gained}")
            if self.score > self.best:
                self.best = self.score
                self._save_best()
        else:
            self.snake.pop()               # solid body only: no phosphor trail behind the tail (2026-09-19)

    def _die(self, why: str) -> None:
        self.state = "over"
        self.burst = 1.0
        for c in self.snake:
            self.embers[c] = 1.0
        self.say(f"SIGNAL LOST — {why}")

    def tick(self, dt: float) -> None:
        """Advance analog state by dt seconds; run grid steps when playing."""
        self.t += dt
        if self.state == "playing":
            self.acc += dt * 1000
            while self.acc >= self.step_ms and self.state == "playing":
                self.acc -= self.step_ms
                self.step()
        decay = math.exp(-dt * 2.2)
        for c in list(self.embers):
            v = self.embers[c] * decay
            if v < 0.02:
                del self.embers[c]
            else:
                self.embers[c] = v
        self.hit *= math.exp(-dt * 4.0)
        self.burst *= math.exp(-dt * 1.6)
        # spectrum: body mass per column, fast attack / slow release, peak hold (the pulse scene's rules)
        counts = [0] * self.cols
        for x, _ in self.snake:
            counts[x] += 1
        cap = max(4.0, self.rows * 0.5)
        for i in range(self.cols):
            v = min(1.0, counts[i] / cap) ** 1.1
            self.level[i] += (v - self.level[i]) * (0.6 if v > self.level[i] else 0.14)
            self.peak[i] = max(self.level[i], self.peak[i] - dt * 0.22)


# ---------------------------------------------------------------------------------------------- renderer
class Renderer:
    """Draws one frame of a Game into a cairo ARGB32 surface. Static CRT layers are cached per size."""

    def __init__(self):
        self.size = (0, 0)
        self.surface = None
        self.crt = None            # scanlines + vignette, one cached full-size layer
        self.glyphs: dict[tuple, tuple] = {}   # (text, size, color, bold, glow) → (surface, pad, ascent, width)

    def resize(self, w: int, h: int) -> None:
        if (w, h) == self.size:
            return
        self.size = (w, h)
        self.surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        sl = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 3)
        c = cairo.Context(sl); c.set_source_rgba(0, 0, 0, 0.42); c.rectangle(0, 2, 1, 1); c.fill()
        scan = cairo.SurfacePattern(sl); scan.set_extend(cairo.EXTEND_REPEAT); scan.set_filter(cairo.FILTER_NEAREST)
        self.crt = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        c = cairo.Context(self.crt)
        c.set_source(scan); c.paint()
        vig = cairo.RadialGradient(w / 2, h / 2, min(w, h) * 0.45, w / 2, h / 2, max(w, h) * 0.75)
        vig.add_color_stop_rgba(0, 0, 0, 0, 0); vig.add_color_stop_rgba(0.6, 0.03, 0, 0, 0.35); vig.add_color_stop_rgba(1, 0, 0, 0, 0.85)
        c.set_source(vig); c.paint()

    # -- text with a phosphor glow. Each distinct string is rasterised once (halo = three widening strokes at
    # falling alpha) into a small surface and blitted afterwards: the strokes were 60 % of the frame time.
    def text(self, cr, x, y, s, size=13, color=TEXT, bold=True, glow=0.35, align="left"):
        key = (s, size, color, bold, glow)
        hit = self.glyphs.get(key)
        if hit is None:
            if len(self.glyphs) > 512:
                self.glyphs.clear()
            cr.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
            cr.set_font_size(size)
            ext, fe = cr.text_extents(s), cr.font_extents()
            pad = int(size * 0.5) + 2
            gw, gh = int(math.ceil(ext.x_advance + abs(ext.x_bearing))) + pad * 2, int(math.ceil(fe[0] + fe[1])) + pad * 2
            gs = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, gw), max(1, gh))
            g = cairo.Context(gs)
            g.select_font_face(FONT, cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD if bold else cairo.FONT_WEIGHT_NORMAL)
            g.set_font_size(size)
            ox, oy = pad - min(0.0, ext.x_bearing), pad + fe[0]
            if glow > 0:
                g.set_line_join(cairo.LINE_JOIN_ROUND)
                for width, alpha in ((0.75, 0.16), (0.42, 0.28), (0.18, 0.55)):
                    g.move_to(ox, oy); g.text_path(s)
                    g.set_source_rgba(*BLOOM, glow * alpha); g.set_line_width(size * width); g.stroke()
            g.move_to(ox, oy); g.set_source_rgb(*color); g.show_text(s)
            gs.flush()
            hit = self.glyphs[key] = (gs, ox, oy, ext.width, ext.x_bearing)
        gs, ox, oy, width, xb = hit
        if align == "center":
            x -= width / 2 + xb
        elif align == "right":
            x -= width + xb
        cr.set_source_surface(gs, x - ox, y - oy); cr.paint()
        return width

    @staticmethod
    def rrect(cr, x, y, w, h, r):
        r = min(r, w / 2, h / 2)
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()

    def meter(self, cr, x, y, w, label, value, text, segs=18):
        """GPU Pulse style segmented bar: ▰▰▰▱▱ in phosphor."""
        self.text(cr, x, y, label, 11, TEXT_DIM, glow=0.15)
        bx = x + 74
        sw = (w - 74 - 70) / segs
        lit = round(max(0.0, min(1.0, value)) * segs)
        for i in range(segs):
            on = i < lit
            col = HOT if on and i == lit - 1 else (LIT if on else DIM)
            cr.set_source_rgb(*col)
            cr.rectangle(bx + i * sw, y - 9, sw * 0.72, 10)
            cr.fill()
        self.text(cr, x + w, y, text, 11, TEXT, glow=0.2, align="right")

    def spectrum(self, cr, x, y, w, h, g: Game, segments=16):
        """The pulse analyzer, straightened: one bar per field column, phosphor persistence, peak hold, bloom."""
        n = g.cols
        pitch = h / segments
        bw = w / n
        flicker = 0.95 + 0.05 * math.sin(g.t * 47.0) * math.sin(g.t * 13.0)
        cr.set_source_rgb(*GRID)
        for k in range(5):                                  # range rings → horizontal grid
            yy = y + h - (k / 4) * h
            cr.rectangle(x, yy - 0.5, w, 1); cr.fill()
        for i in range(n):
            lv = min(1.0, g.level[i] * (1 + 0.08 * g.hit))
            lit = round(lv * segments)
            pk = min(segments - 1, round(g.peak[i] * segments))
            bx = x + i * bw
            if lit > 0:                                     # bloom quad behind the lit part
                cr.set_source_rgba(*BLOOM, 0.16)
                cr.rectangle(bx - bw * 0.6, y + h - lit * pitch, bw * 2.2, lit * pitch); cr.fill()
            for j in range(segments):
                if j < lit:
                    col = scale(HOT if j == lit - 1 else LIT, flicker * (0.7 + 0.3 * j / segments))
                elif j == pk and pk > 0:
                    col = scale(PEAK, 0.85 * flicker)
                elif j < pk:
                    col = scale(TRAIL, 0.35 + 0.65 * (1 - (j - lit) / max(1, pk - lit)))
                else:
                    col = scale(DIM, max(0.25, 1 - j / segments))
                cr.set_source_rgb(*col)
                cr.rectangle(bx + bw * 0.21, y + h - (j + 1) * pitch + pitch * 0.3, bw * 0.58, pitch * 0.7)
                cr.fill()

    def render(self, g: Game) -> cairo.ImageSurface:
        w, h = self.size
        cr = cairo.Context(self.surface)
        cr.set_source_rgb(*BG); cr.paint()
        margin = 18
        panel_w = max(250, int(w * 0.26))
        gap = 16
        # -- field geometry
        fx0, fy0, fw, fh = margin, margin + 44, w - margin * 2 - panel_w - gap, h - margin * 2 - 44
        cell = max(4, int(min(fw / g.cols, fh / g.rows)))
        gw, gh = cell * g.cols, cell * g.rows
        gx, gy = fx0 + (fw - gw) // 2, fy0 + (fh - gh) // 2
        pad = 10
        # bezel + field
        self.rrect(cr, gx - pad, gy - pad, gw + pad * 2, gh + pad * 2, 8)
        cr.set_source_rgb(*mix(DISC, (0.06, 0.004, 0.008), g.hit)); cr.fill_preserve()
        cr.set_source_rgb(*scale(BEZEL, 0.6 + 0.4 * max(g.hit, g.burst))); cr.set_line_width(1.5); cr.stroke()
        # grid dots
        cr.set_source_rgb(*GRID)
        for yy in range(g.rows + 1):
            cr.rectangle(gx, gy + yy * cell - 0.5, gw, 1)
        for xx in range(g.cols + 1):
            cr.rectangle(gx + xx * cell - 0.5, gy, 1, gh)
        cr.fill()
        # embers (phosphor persistence where the snake was)
        for (cx, cy), v in g.embers.items():
            cr.set_source_rgb(*mix(DIM, TRAIL, v * 0.8))
            cr.rectangle(gx + cx * cell + 2, gy + cy * cell + 2, cell - 4, cell - 4); cr.fill()
        # food: a target blip with a breathing ring and bloom
        if g.state != "attract" and g.food[0] >= 0:
            fxp, fyp = gx + (g.food[0] + 0.5) * cell, gy + (g.food[1] + 0.5) * cell
            pulse = 0.5 + 0.5 * math.sin(g.t * 6.0)
            grad = cairo.RadialGradient(fxp, fyp, 0, fxp, fyp, cell * 1.8)
            grad.add_color_stop_rgba(0, *BLOOM, 0.35 + 0.25 * pulse); grad.add_color_stop_rgba(1, *BLOOM, 0)
            cr.set_source(grad); cr.arc(fxp, fyp, cell * 1.8, 0, 2 * math.pi); cr.fill()
            cr.set_source_rgb(*mix(LIT, HOT, pulse)); cr.arc(fxp, fyp, cell * 0.28, 0, 2 * math.pi); cr.fill()
            cr.set_source_rgba(*PEAK, 0.5 + 0.4 * pulse); cr.set_line_width(1.2)
            cr.arc(fxp, fyp, cell * (0.42 + 0.1 * pulse), 0, 2 * math.pi); cr.stroke()
        # snake: one continuous rounded stroke through the cell centres, one colour head to tail (a sub-path
        # restarts where the body wraps across the field edge); the head gets a soft aura, not a brighter body.
        # Smooth motion: between grid steps the head is drawn `progress` of the way into the cell it is entering
        # and the tail `progress` of the way out of its cell (unless the coming step grows), so the body glides
        # at a constant speed instead of jumping a cell every step. Clipped to the field for the wrap case.
        n = len(g.snake)
        centre = lambda c: (gx + (c[0] + 0.5) * cell, gy + (c[1] + 0.5) * cell)
        adjacent = lambda a, b: abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1
        prog = g.progress if g.state in ("playing", "paused") else 0.0
        hxp, hyp = centre(g.snake[0]) if n else (0.0, 0.0)
        if n and prog > 0:
            dx, dy = DIRS[g.direction]
            hxp, hyp = hxp + dx * prog * cell, hyp + dy * prog * cell
        if n:
            col = SNAKE if g.state != "over" else mix(SNAKE, DIM, 1 - g.burst)
            cr.save(); cr.rectangle(gx, gy, gw, gh); cr.clip()
            cr.set_line_cap(cairo.LINE_CAP_ROUND); cr.set_line_join(cairo.LINE_JOIN_ROUND); cr.set_line_width(cell * 0.68)
            cr.new_path()
            cr.move_to(hxp, hyp); cr.line_to(hxp, hyp)          # a zero-length segment still draws its round cap
            prev = None
            for i, c in enumerate(g.snake):
                px, py = centre(c)
                if i == n - 1 and n >= 2 and prog > 0 and not g.will_grow() and adjacent(c, g.snake[-2]):
                    qx, qy = centre(g.snake[-2])
                    px, py = px + (qx - px) * prog, py + (qy - py) * prog     # the tail leaves its cell
                if prev is not None and not adjacent(c, prev):
                    cr.move_to(px, py); cr.line_to(px, py)
                else:
                    cr.line_to(px, py)
                prev = c
            cr.set_source_rgb(*col); cr.stroke()
            cr.restore()
        if n and g.state != "over":
            grad = cairo.RadialGradient(hxp, hyp, cell * 0.3, hxp, hyp, cell * 2.2)
            grad.add_color_stop_rgba(0, *BLOOM, 0.3 + 0.3 * g.hit); grad.add_color_stop_rgba(1, *BLOOM, 0)
            cr.set_source(grad); cr.arc(hxp, hyp, cell * 2.2, 0, 2 * math.pi); cr.fill()
        # -- HUD on the field
        self.text(cr, margin, margin + 14, "SERPENT LINK PROTOCOL // FIELD TELEMETRY", 11, TEXT_DIM, glow=0.15)
        self.text(cr, margin, margin + 32, "[ STATION :: PHOSPHOR SNAKE ]", 13, TEXT)
        self.text(cr, gx + gw, margin + 14, f"SCORE {g.score:06d}", 12, TEXT, align="right")
        self.text(cr, gx + gw, margin + 32, f"BEST  {g.best:06d}", 11, TEXT_DIM, glow=0.15, align="right")
        # -- overlays
        blink = (int(g.t * 2) % 2) == 0
        mid_x, mid_y = gx + gw / 2, gy + gh / 2
        if g.state == "attract":
            self.text(cr, mid_x, mid_y - 26, "PHOSPHOR SNAKE", 42, HOT, glow=0.5, align="center")
            self.text(cr, mid_x, mid_y + 6, "── RED PHOSPHOR TERMINAL EDITION ──", 12, TEXT_DIM, glow=0.15, align="center")
            if blink:
                self.text(cr, mid_x, mid_y + 44, "PRESS SPACE TO ENGAGE", 15, TEXT, align="center")
            self.text(cr, mid_x, gy + gh - 18, "ARROWS / WASD  ·  SPACE HOLD  ·  R RESET  ·  F FULLSCREEN  ·  Q QUIT", 10, TEXT_DIM, glow=0.1, align="center")
        elif g.state == "paused":
            if blink:
                self.text(cr, mid_x, mid_y + 8, "── HOLD ──", 30, HOT, glow=0.5, align="center")
        elif g.state == "over":
            self.text(cr, mid_x, mid_y - 16, "SIGNAL LOST", 38, HOT, glow=0.55, align="center")
            self.text(cr, mid_x, mid_y + 14, f"SCORE {g.score}   LENGTH {g.length}", 13, TEXT, align="center")
            if blink:
                self.text(cr, mid_x, mid_y + 44, "SPACE OR R TO RE-ENGAGE", 13, TEXT_DIM, glow=0.15, align="center")
        # -- telemetry panel
        px = w - margin - panel_w
        py = margin
        self.rrect(cr, px, py, panel_w, h - margin * 2, 4)
        cr.set_source_rgb(*DISC); cr.fill_preserve(); cr.set_source_rgb(*LINE); cr.set_line_width(1); cr.stroke()
        ix, iw = px + 14, panel_w - 28
        yy = py + 22
        self.text(cr, ix, yy, "TERMLINK // TELEMETRY", 12, TEXT); yy += 12
        cr.set_source_rgb(*LINE); cr.rectangle(ix, yy, iw, 1); cr.fill(); yy += 26
        self.meter(cr, ix, yy, iw, "LENGTH", min(1.0, g.length / (g.cols * g.rows * 0.35)), f"{g.length:4d}"); yy += 24
        self.meter(cr, ix, yy, iw, "SPEED", g.speed, f"{1000 / g.step_ms:4.1f}/s"); yy += 24
        self.meter(cr, ix, yy, iw, "SIGNAL", 1.0 if g.state == "playing" else 0.35 if g.state == "paused" else 0.0,
                   {"playing": "LIVE", "paused": "HOLD", "over": "LOST", "attract": "IDLE"}[g.state]); yy += 30
        cr.set_source_rgb(*LINE); cr.rectangle(ix, yy, iw, 1); cr.fill(); yy += 20
        self.text(cr, ix, yy, "SPECTRUM :: BODY MASS PER COLUMN", 10, TEXT_DIM, glow=0.12); yy += 10
        spec_h = max(90, int((h - margin * 2) * 0.30))
        self.spectrum(cr, ix, yy, iw, spec_h, g); yy += spec_h + 14
        # bass/mid/treble-style readout of the field thirds
        thirds = [sum(g.level[i] for i in range(g.cols) if (i * 3) // g.cols == k) / max(1, g.cols / 3) for k in range(3)]
        for lab, v in zip(("LEFT", "MID ", "RGHT"), thirds):
            bar = "▮" * int(min(1, v * 2.5) * 14) + "▯" * (14 - int(min(1, v * 2.5) * 14))
            self.text(cr, ix, yy, f"{lab} {bar} {int(min(1, v * 2.5) * 100):3d}%", 11, TEXT, glow=0.15); yy += 16
        yy += 8
        cr.set_source_rgb(*LINE); cr.rectangle(ix, yy, iw, 1); cr.fill(); yy += 20
        self.text(cr, ix, yy, "EVENT LOG", 10, TEXT_DIM, glow=0.12); yy += 16
        for ts, msg in list(g.log)[-5:]:
            age = g.t - ts
            self.text(cr, ix, yy, f"{int(ts) // 60:02d}:{int(ts) % 60:02d}  {msg}", 10, mix(TEXT_DIM, TEXT, max(0, 1 - age / 6)), glow=0.1); yy += 15
        self.text(cr, px + panel_w / 2, h - margin - 10, "▮ " + time.strftime("%H:%M:%S") + " ▮", 10, TEXT_DIM, glow=0.12, align="center")
        # -- CRT layers: rolling band, then the cached scanlines + vignette, then flicker
        band_y = ((g.t % 7.0) / 7.0) * (h * 1.3) - h * 0.25
        band = cairo.LinearGradient(0, band_y, 0, band_y + h * 0.22)
        band.add_color_stop_rgba(0, 1, 0.24, 0.31, 0); band.add_color_stop_rgba(0.5, 1, 0.24, 0.31, 0.06); band.add_color_stop_rgba(1, 1, 0.24, 0.31, 0)
        cr.set_source(band); cr.rectangle(0, band_y, w, h * 0.22); cr.fill()
        cr.set_source_surface(self.crt, 0, 0); cr.paint()
        flick = 0.03 * (0.5 + 0.5 * math.sin(g.t * 37.0) * math.sin(g.t * 11.0))
        cr.set_source_rgba(0, 0, 0, flick); cr.paint()
        self.surface.flush()
        return self.surface


# ---------------------------------------------------------------------------------------------- GTK front end
def demo_game(cols, rows, wrap) -> Game:
    """A staged mid-game state for --screenshot: a long snake caught mid-glide, a fresh hit."""
    g = Game(cols, rows, wrap, seed=7)
    g.reset(); g.state = "playing"
    path = [(6 + i, 7) for i in range(14)] + [(19, 8 + i) for i in range(6)] + [(18 - i, 13) for i in range(8)]
    g.snake = deque(reversed(path))
    g.direction = "left"
    g.food = (24, 5); g.score = 180; g.eaten = 12; g.hit = 0.6
    g.say("TARGET ACQUIRED  +12"); g.say("TARGET ACQUIRED  +13")
    g.state = "paused"                                  # settle the analog layers without moving the snake
    for _ in range(40):
        g.tick(0.05)
    g.state = "playing"; g.hit = 0.6
    g.acc = g.step_ms * 0.55                              # mid-glide: head and tail between cells
    return g


def render_icon(path: str, size: int = 256) -> None:
    """App icon: a bezelled dark tile with a glowing phosphor snake and a target blip."""
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
    cr = cairo.Context(surf)
    r = Renderer()
    r.rrect(cr, 0, 0, size, size, size * 0.18); cr.set_source_rgb(*DISC); cr.fill_preserve()
    cr.set_source_rgb(*BEZEL); cr.set_line_width(size * 0.03); cr.stroke()
    cell = size / 9
    cr.set_source_rgb(*GRID)
    for k in range(1, 9):
        cr.rectangle(k * cell - 0.5, cell, 1, size - 2 * cell); cr.rectangle(cell, k * cell - 0.5, size - 2 * cell, 1)
    cr.fill()
    body = [(2, 6), (2, 5), (2, 4), (2, 3), (3, 3), (4, 3), (5, 3), (5, 4), (5, 5), (6, 5)]
    cr.set_line_cap(cairo.LINE_CAP_ROUND); cr.set_line_join(cairo.LINE_JOIN_ROUND); cr.set_line_width(cell * 0.68)
    for i, (x, y) in enumerate(body):
        (cr.move_to if i == 0 else cr.line_to)((x + 0.5) * cell, (y + 0.5) * cell)
    cr.set_source_rgb(*SNAKE); cr.stroke()
    hx, hy = 6.5 * cell, 5.5 * cell
    g = cairo.RadialGradient(hx, hy, 0, hx, hy, cell * 2.2); g.add_color_stop_rgba(0, *BLOOM, 0.45); g.add_color_stop_rgba(1, *BLOOM, 0)
    cr.set_source(g); cr.arc(hx, hy, cell * 2.2, 0, 2 * math.pi); cr.fill()
    fx, fy = 7.5 * cell, 2.5 * cell
    cr.set_source_rgb(*HOT); cr.arc(fx, fy, cell * 0.22, 0, 2 * math.pi); cr.fill()
    cr.set_source_rgba(*PEAK, 0.8); cr.set_line_width(size * 0.012); cr.arc(fx, fy, cell * 0.42, 0, 2 * math.pi); cr.stroke()
    sl = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 3); c = cairo.Context(sl); c.set_source_rgba(0, 0, 0, 0.35); c.rectangle(0, 2, 1, 1); c.fill()
    pat = cairo.SurfacePattern(sl); pat.set_extend(cairo.EXTEND_REPEAT); pat.set_filter(cairo.FILTER_NEAREST)
    r.rrect(cr, 0, 0, size, size, size * 0.18); cr.set_source(pat); cr.fill()
    surf.write_to_png(path)


def run_gtk(args) -> int:
    import gi
    gi.require_version("Gtk", "3.0"); gi.require_version("Gdk", "3.0"); gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import Gdk, GdkPixbuf, GLib, Gtk
    from PIL import Image

    g = Game(args.cols, args.rows, args.wrap)
    r = Renderer()
    win = Gtk.Window(title="PHOSPHOR SNAKE")
    win.set_default_size(1100, 720)
    css = Gtk.CssProvider(); css.load_from_data(b"window { background-color: #050001; }")
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    # The image lives in a Gtk.Layout: a Layout never asks the window to grow to its child's size, so a frame that is
    # exactly the content area cannot start a resize negotiation. (A bare Gtk.Image did, and on Wayland with client-
    # side decorations the window allocation also includes the shadow margins → the window never settled and never
    # showed a frame.)
    layout = Gtk.Layout(); img = Gtk.Image(); layout.put(img, 0, 0); win.add(layout)
    keys = {"Up": "up", "Down": "down", "Left": "left", "Right": "right", "w": "up", "s": "down", "a": "left", "d": "right",
            "k": "up", "j": "down", "h": "left", "l": "right"}
    state = {"last": time.perf_counter(), "frames": 0, "t0": time.perf_counter(), "full": False}

    def on_key(_w, ev):
        name = Gdk.keyval_name(ev.keyval) or ""
        if name in keys:
            g.turn(keys[name])
        elif name in ("space", "p", "P"):
            g.toggle()
        elif name in ("r", "R"):
            g.reset(); g.state = "playing"; g.say("LINK ENGAGED")
        elif name in ("f", "F", "F11"):
            state["full"] = not state["full"]
            win.fullscreen() if state["full"] else win.unfullscreen()
        elif name in ("q", "Q", "Escape"):
            Gtk.main_quit()
        return True

    def frame(*_):
        now = time.perf_counter()
        dt = min(0.1, now - state["last"]); state["last"] = now
        g.tick(dt)
        alloc = layout.get_allocation()                 # the content area, without any client-side decoration margins
        w, h = max(320, alloc.width), max(240, alloc.height)
        if (w, h) != r.size:
            img.set_size_request(w, h)                  # a Layout allocates children by their request: without this the
            layout.move(img, 0, 0)                      # image kept its empty 16x16 box and the frame was centred off-window
        r.resize(w, h)
        surf = r.render(g)
        im = Image.frombuffer("RGBA", (w, h), bytes(surf.get_data()), "raw", "BGRA", surf.get_stride(), 1).convert("RGB")
        pix = GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(im.tobytes()), GdkPixbuf.Colorspace.RGB, False, 8, w, h, w * 3)
        img.set_from_pixbuf(pix)
        state["frames"] += 1
        if args.bench and now - state["t0"] > 3.0:
            print(f"{state['frames'] / (now - state['t0']):.1f} fps at {w}x{h}")
            Gtk.main_quit()
        return True

    win.connect("key-press-event", on_key)
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    if args.bench:
        g.reset(); g.state = "playing"
    # Render inside GTK's frame clock (update phase), so every frame is followed by a paint. A plain 16 ms timeout
    # at default priority that does ~15 ms of work is always due again the moment it returns and starves GTK's
    # lower-priority redraw idle: the window stayed black on X11 and never mapped at all on Wayland.
    win.add_tick_callback(frame)
    Gtk.main()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="PHOSPHOR SNAKE — snake on a red-phosphor CRT terminal")
    ap.add_argument("--wrap", action="store_true", help="no walls: the field wraps around")
    ap.add_argument("--cells", default="32x22", help="field size COLSxROWS (default 32x22)")
    ap.add_argument("--screenshot", metavar="PNG", help="render a staged frame to PNG and exit (no window)")
    ap.add_argument("--size", default="1100x720", help="window / screenshot size WxH")
    ap.add_argument("--bench", action="store_true", help="run 3 s with the window and print the frame rate")
    ap.add_argument("--icon", metavar="PNG", help="render the app icon to PNG and exit")
    args = ap.parse_args()
    args.cols, args.rows = (int(v) for v in args.cells.lower().split("x"))
    if args.icon:
        render_icon(args.icon); print(f"wrote {args.icon}"); return 0
    if args.screenshot:
        w, h = (int(v) for v in args.size.lower().split("x"))
        r = Renderer(); r.resize(w, h)
        r.render(demo_game(args.cols, args.rows, args.wrap)).write_to_png(args.screenshot)
        print(f"wrote {args.screenshot} ({w}x{h})")
        return 0
    return run_gtk(args)


if __name__ == "__main__":
    sys.exit(main())
