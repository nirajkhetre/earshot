"""The island: an ink notch hanging from the top of the screen, with a soft creature that is your assistant's face.

    rest     a thin lip with two ear tips peeking out (a countdown pill while a timer runs)
    peek     hover: its name and what it's doing
    menu     click: talk, type, hush, settings
    listen   ears up, the halo breathes with your voice
    think    eyes up; what it heard
    answer   the reply, which brain answered and how fast, undo / copy
    toast    anything that posts to it: timers, MCP servers, your scripts

It never takes focus; only the type box and settings do. QPainter all the way: nothing here looks like Windows.
"""
import ctypes
import math
import os
import random
import sys
import time
from collections import deque

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import (QColor, QCursor, QFont, QFontDatabase, QFontMetricsF, QGuiApplication, QIcon,
                           QLinearGradient, QPainter, QPainterPath, QPen, QPixmap, QRadialGradient)
from PySide6.QtWidgets import (QAbstractButton, QApplication, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QMenu, QPushButton, QScrollArea, QSlider, QSystemTrayIcon, QVBoxLayout, QWidget)

from .config import COLORS, FONTS, HOME, LIVE_VOICES, S, SKILLS, VOICES, WORKSPACES, gemini_key, set_gemini_key

INK, INK2, LINE = QColor("#0B0B10"), QColor("#16151D"), QColor("#2A2833")
TXT, DIM, FAINT = QColor("#F4F3F8"), QColor("#8E8B9C"), QColor(255, 255, 255, 20)
HI, PORCELAIN, LO, EYE = QColor("#FFFFFF"), QColor("#ECE9F5"), QColor("#B4AEC9"), QColor("#1B1924")
UI, DISPLAY, MONO = "Space Grotesk", "Unbounded", "Geist Mono"
EAR, PAD = 12, 26  # concave flares where the notch meets the screen edge; room for the shadow


def load_fonts():
    for f in FONTS.glob("*.ttf"):
        QFontDatabase.addApplicationFont(str(f))


def font(px, family=UI, weight=QFont.Normal):
    f = QFont()
    f.setFamilies([family, "Segoe UI Variable Display", "Segoe UI"])
    f.setPixelSize(round(px))
    f.setWeight(weight)
    return f


def accent(a=255):
    c = QColor(S.accent())
    c.setAlpha(a)
    return c


def with_alpha(c, a):
    c = QColor(c)
    c.setAlpha(max(0, min(255, int(a))))
    return c


class Spring:
    """Slightly underdamped: a little overshoot reads as alive."""

    def __init__(self, x, k=260.0, d=24.0):
        self.x = self.target = float(x)
        self.v, self.k, self.d = 0.0, k, d

    def step(self, dt):
        self.v += (self.k * (self.target - self.x) - self.d * self.v) * dt
        self.x += self.v * dt

    def snap(self):
        self.x, self.v = self.target, 0.0

    def moving(self):
        return abs(self.v) > 0.5 or abs(self.target - self.x) > 0.5


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", ctypes.c_long * 4), ("rcWork", ctypes.c_long * 4),
                ("dwFlags", ctypes.c_ulong)]


def fullscreen_app(own=0):
    """The window in front fills the main screen exactly: a game, a video, slides. (Maximized windows don't:
    they hang over the edges by their invisible borders.) From lowkey's island."""
    u = ctypes.windll.user32
    h = u.GetForegroundWindow()
    if not h or h == own:
        return False
    cls = ctypes.create_unicode_buffer(32)
    u.GetClassNameW(h, cls, 32)
    if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd"):
        return False
    r, mi = (ctypes.c_long * 4)(), MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    u.GetWindowRect(h, r)
    if not u.GetMonitorInfoW(u.MonitorFromWindow(h, 2), ctypes.byref(mi)) or not mi.dwFlags & 1:
        return False
    return all(abs(a - b) <= 1 for a, b in zip(r, mi.rcMonitor))


def reduced_motion():
    on = ctypes.c_int(1)
    ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(on), 0)  # SPI_GETCLIENTAREAANIMATION
    return not on.value


def force_foreground(hwnd):
    u = ctypes.windll.user32
    u.keybd_event(0x12, 0, 0, 0)  # a tap of Alt lets us take the foreground from another app
    u.keybd_event(0x12, 0, 2, 0)
    u.SetForegroundWindow(hwnd)


class Bus(QObject):
    """Threads talk to the UI only through these (Qt queues them onto the UI thread)."""
    state = Signal(str, dict)
    level = Signal(float)
    toast = Signal(dict)
    timer = Signal(str, str, float)
    ring = Signal(str, str)
    hide = Signal(bool)
    ask = Signal(str)
    outlevel = Signal(float)
    cmd = Signal(str)


# ---------- the creature ----------
class Creature:
    """A soft porcelain blob with two big ears and pill eyes. Ears perk at its name and fold when hushed; eyes
    follow your cursor, blink, look up while thinking and smile when it's done."""

    def __init__(self):
        self.ear = Spring(0.6, 200, 14)
        self.eye = Spring(1.0, 420, 30)
        self.lx, self.ly = Spring(0, 90, 16), Spring(0, 90, 16)
        self.squash = Spring(0, 320, 9)
        self.halo = Spring(0, 140, 20)
        self.mood, self.blink_at = "idle", time.time() + 3

    def poke(self, amount=0.22):
        self.squash.v -= amount * 14

    def update(self, dt, now, mood, level=0.0, look=(0.0, 0.0), voice=0.0):
        """level: your mic (the halo breathes with it). voice: its own speech (it bounces as it talks)."""
        self.mood = mood
        self.ear.target = {"listen": 1.0, "think": 0.72, "happy": 0.92, "sleep": 0.0, "call": 0.12,
                           "talk": 0.95}.get(mood, 0.62)
        self.squash.target = -0.16 * voice if mood == "talk" else 0.0
        if now > self.blink_at + 0.13:
            self.blink_at = now + random.uniform(2.2, 5.5)
        blinking = self.blink_at <= now <= self.blink_at + 0.13
        self.eye.target = (0.1 if mood in ("sleep", "call") else 0.12 if blinking else 1.18 if mood == "listen" else 1.0)
        self.lx.target, self.ly.target = (0.55, -0.85) if mood == "think" else look
        self.halo.target = (0.3 + level * 1.1) if mood == "listen" else (0.15 + level) if mood == "talk" else 0.0
        for s in (self.ear, self.eye, self.lx, self.ly, self.squash, self.halo):
            s.step(dt)

    def draw(self, p, cx, cy, R, now):
        p.save()
        p.setPen(Qt.NoPen)
        sq = max(-0.22, min(0.22, self.squash.x))
        if self.halo.x > 0.02:
            hr = R * (1.35 + 0.7 * self.halo.x)
            g = QRadialGradient(cx, cy, hr)
            g.setColorAt(0, accent(int(140 * min(1.0, self.halo.x))))
            g.setColorAt(1, accent(0))
            p.setBrush(g)
            p.drawEllipse(QPointF(cx, cy), hr, hr)
        for side in (-1, 1):  # ears first: they sit behind the head
            p.save()
            p.translate(cx + side * R * 0.46, cy - R * 0.58 * (1 - sq))
            wig = math.sin(now * 5 + side) * 4 * self.halo.x
            p.rotate(side * (100 - 82 * self.ear.x) + wig)
            L, Wd = R * 1.05, R * 0.52
            g = QLinearGradient(-Wd / 2, 0, Wd / 2, 0)
            g.setColorAt(0, PORCELAIN if side < 0 else LO)
            g.setColorAt(1, LO if side < 0 else PORCELAIN)
            p.setBrush(g)
            p.drawRoundedRect(QRectF(-Wd / 2, -L, Wd, L + R * 0.25), Wd / 2, Wd / 2)
            p.setBrush(accent(190))
            p.drawRoundedRect(QRectF(-Wd * 0.21, -L * 0.84, Wd * 0.42, L * 0.62), Wd * 0.21, Wd * 0.21)
            p.restore()
        g = QRadialGradient(cx - R * 0.38, cy - R * 0.5, R * 1.7)
        g.setColorAt(0, HI)
        g.setColorAt(0.42, PORCELAIN)
        g.setColorAt(1, LO)
        p.setBrush(g)
        p.drawEllipse(QPointF(cx, cy + R * sq * 0.5), R * (1 + sq), R * (1 - sq))
        if self.mood in ("happy", "listen", "talk"):  # blush
            p.setBrush(accent(70))
            for side in (-1, 1):
                p.drawEllipse(QPointF(cx + side * R * 0.56, cy + R * 0.3), R * 0.18, R * 0.11)
        ex, ey = R * 0.33, cy - R * 0.04 + self.ly.x * R * 0.12
        lx = self.lx.x * R * 0.14
        if self.mood == "happy":
            p.setPen(QPen(EYE, max(1.4, R * 0.11), Qt.SolidLine, Qt.RoundCap))
            p.setBrush(Qt.NoBrush)
            for side in (-1, 1):
                path = QPainterPath()
                x0 = cx + side * ex + lx
                path.moveTo(x0 - R * 0.13, ey + R * 0.05)
                path.quadTo(x0, ey - R * 0.16, x0 + R * 0.13, ey + R * 0.05)
                p.drawPath(path)
        else:
            w, h = R * 0.2, max(R * 0.06, R * 0.44 * self.eye.x)
            for side in (-1, 1):
                x0 = cx + side * ex + lx
                p.setBrush(EYE)
                p.drawRoundedRect(QRectF(x0 - w / 2, ey - h / 2, w, h), w / 2, min(w, h) / 2)
                if self.eye.x > 0.5:
                    p.setBrush(QColor(255, 255, 255, 220))
                    p.drawEllipse(QPointF(x0 + w * 0.12, ey - h * 0.24), w * 0.17, w * 0.17)
        p.restore()


def creature_icon(size=64):
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHints(QPainter.Antialiasing)
    c = Creature()
    c.ear.x = 0.9
    c.draw(p, size / 2, size * 0.6, size * 0.3, 0)
    p.end()
    return QIcon(pm)


# ---------- the island ----------
TIERS = {"reflex": "reflex", "on-device": "on-device", "cloud": "cloud", "offline": "offline"}


class Island(QWidget):
    def __init__(self, bus, act):
        super().__init__()
        self.bus, self.act = bus, act
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowDoesNotAcceptFocus
                            | Qt.NoDropShadowWindowHint)
        for a in (Qt.WA_TranslucentBackground, Qt.WA_ShowWithoutActivating):
            self.setAttribute(a)
        self.setMouseTracking(True)
        self.W, self.H = Spring(118, 300, 26), Spring(7, 300, 26)
        self.face = Creature()
        self.mode, self.ears, self.call_app = "rest", "loading", ""
        self.heard, self.reply, self.until, self.toast_d = "", {}, 0.0, None
        self.timers, self.levels = {}, deque([0.0] * 32, maxlen=32)
        self.live, self.out_level = {}, 0.0
        self.hover, self.hover_t, self.left_at, self.hot, self.chips = False, 0.0, 0.0, None, {}
        self.fs, self.fs_t, self.mode_t, self.last = False, 0.0, time.time(), time.time()
        self.calm = reduced_motion()
        self.on_fullscreen = lambda fs: None
        bus.state.connect(self.on_state)
        bus.level.connect(lambda x: self.levels.append(x))
        bus.outlevel.connect(self.on_outlevel)
        bus.toast.connect(self.on_toast)
        bus.timer.connect(self.on_timer)
        bus.ring.connect(self.on_ring)
        bus.hide.connect(self.on_hide)
        bus.cmd.connect(self.on_cmd)
        self.clock = QTimer(self)
        self.clock.timeout.connect(self.tick)
        self.clock.start(16)
        self.place()
        self.show()

    # ----- what the app tells it (slots: Qt runs them on the UI thread whoever emits) -----
    def on_hide(self, on):
        self.setWindowOpacity(0.0 if on else 1.0)  # out of the way for a screenshot

    def on_cmd(self, c):
        self.act[c]()

    def on_outlevel(self, x):
        self.out_level = x

    def set_mode(self, m):
        if m != self.mode:
            self.mode, self.mode_t = m, time.time()

    def on_state(self, name, d):
        now = time.time()
        if name == "ears":
            self.ears, self.call_app = d["mode"], d.get("call", "")
        elif name == "listen":
            self.set_mode("listen")
            self.face.poke()
        elif name == "think":
            self.heard = d.get("heard", "")
            self.set_mode("think")
        elif name == "answer":
            self.reply = d
            self.layout_answer()
            self.set_mode("answer")
            self.face.poke(0.3)
        elif name in ("rest", "quiet"):
            if self.mode in ("listen", "think") or name == "rest" and self.mode != "answer":
                self.set_mode("rest")
        elif name == "peek":
            self.until = now + 2.5
            self.set_mode("peek")
        elif name == "live":
            if self.mode != "live":
                self.live = {"state": "connecting", "you": "", "it": "", "last": "it", "tool": None}
                self.face.poke(0.3)
            for k in ("you", "it"):
                if k in d:
                    self.live["last"] = k  # the caption shows whoever spoke last
            if "you" in d or "it" in d or d.get("state") == "speaking":
                self.live["tool"] = None
            self.live.update(d)
            self.set_mode("live")
        elif name == "live_end":
            if self.mode == "live":
                self.set_mode("rest")

    def on_toast(self, d):
        self.toast_d = {**d, "until": time.time() + float(d.get("seconds", 5))}
        if self.mode in ("rest", "peek", "toast"):
            self.set_mode("toast")
            self.face.poke(0.18)

    def on_timer(self, tid, label, ends):
        if ends:
            self.timers[tid] = (label, ends, time.time())
        else:
            self.timers.pop(tid, None)

    def on_ring(self, tid, label):
        self.timers.pop(tid, None)
        self.on_toast({"title": "time's up", "text": label or "your timer", "seconds": 9, "ring": True})

    def layout_answer(self):
        text = self.reply.get("text") or "Done."
        fm = QFontMetricsF(font(15))
        w = min(560.0, max(380.0, fm.horizontalAdvance(text) + 52))
        th = fm.boundingRect(QRectF(0, 0, w - 46, 4000), Qt.TextWordWrap, text).height()
        th = min(th, fm.lineSpacing() * 7)
        self.answer_size = (w, 54 + th + 50)
        self.until = time.time() + max(5.0, 1.8 + len(text.split()) * 0.34)

    # ----- the clock -----
    def mood(self, now):
        if self.mode == "live":
            st = self.live.get("state")
            return "talk" if st == "speaking" else "think" if st == "connecting" or self.live.get("tool") else "listen"
        if self.mode == "listen" or self.ears == "listen":
            return "listen"
        if self.mode == "think":
            return "think"
        if self.mode in ("answer", "toast") and now - self.mode_t < 1.6:
            return "happy"
        return {"deaf": "sleep", "call": "call", "loading": "sleep"}.get(self.ears, "idle")

    def size_for(self, m):
        if m == "rest":
            return (178, 32) if self.timers else (118, 7)
        return {"peek": (330, 44), "menu": (420, 100), "listen": (350, 58), "think": (400, 58),
                "toast": (390, 54), "live": (470, 76)}.get(m) or self.answer_size

    def tick(self):
        now = time.time()
        dt, self.last = min(0.033, now - self.last), now
        if now - self.fs_t > 0.5:
            fs = fullscreen_app(int(self.winId()))
            if fs != self.fs:
                self.fs = fs
                self.on_fullscreen(fs)
            self.fs_t = now
        m = self.mode
        if m == "answer" and now > self.until and not self.hover:
            self.set_mode("rest")
        elif m == "toast" and (not self.toast_d or now > self.toast_d["until"]) and not self.hover:
            self.set_mode("rest")
        elif m == "menu" and not self.hover and now - self.left_at > 1.4:
            self.set_mode("rest")
        elif m == "peek" and not self.hover and now - self.left_at > 0.5 and now > self.until:
            self.set_mode("rest")
        elif m == "rest" and self.hover and now - self.hover_t > 0.12:
            self.set_mode("peek")
        elif m == "rest" and self.toast_d and now < self.toast_d["until"]:
            self.set_mode("toast")  # a toast that arrived while something else was showing
        tw, th = self.size_for(self.mode)
        if self.fs and self.mode == "rest":
            th = 0  # a game or a video: tuck all the way in
        self.W.target, self.H.target = tw, th
        for s in (self.W, self.H):
            s.step(dt)
            if self.calm:
                s.snap()
        cur, g = QCursor.pos(), self.mapToGlobal(self.rect().topLeft())
        cx, cy = g.x() + PAD + EAR + 30, g.y() + self.H.x / 2
        look = (max(-1, min(1, (cur.x() - cx) / 500)), max(-1, min(1, (cur.y() - cy) / 300)))
        lv = sum(list(self.levels)[-3:]) / 3
        self.face.update(dt, now, self.mood(now), lv, look, self.out_level)
        self.place()
        self.update()
        busy = self.W.moving() or self.H.moving() or self.mode != "rest" or self.timers or self.hover
        ms = 16 if busy else 50
        if self.clock.interval() != ms:
            self.clock.setInterval(ms)

    def place(self):
        scr = QGuiApplication.primaryScreen()
        a = scr.geometry() if self.fs else scr.availableGeometry()  # hangs below a top taskbar
        w, h = max(0.0, self.W.x), max(0.0, self.H.x)
        ww, wh = w + 2 * (EAR + PAD), h + PAD + 8
        self.setGeometry(round(a.center().x() - ww / 2), a.top(), round(ww), max(1, round(wh)))

    # ----- painting -----
    def body(self):
        return QRectF(PAD + EAR, 0, max(0.0, self.W.x), max(0.5, self.H.x))

    def notch(self, b):
        d, x0, x1 = b.height(), b.left(), b.right()
        e = min(EAR, d * 0.6)
        r = max(0.5, min(22.0, d / 2, b.width() / 2))
        P = QPainterPath()
        P.moveTo(x0 - e, 0)
        P.quadTo(x0, 0, x0, e)
        P.lineTo(x0, d - r)
        P.arcTo(QRectF(x0, d - 2 * r, 2 * r, 2 * r), 180, 90)
        P.lineTo(x1 - r, d)
        P.arcTo(QRectF(x1 - 2 * r, d - 2 * r, 2 * r, 2 * r), 270, 90)
        P.lineTo(x1, e)
        P.quadTo(x1, 0, x1 + e, 0)
        P.closeSubpath()
        return P

    def paintEvent(self, _):
        if self.H.x < 0.6:
            return
        now = time.time()
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        b = self.body()
        path = self.notch(b)
        p.setBrush(Qt.NoBrush)
        p.setClipRect(QRectF(0, EAR + 2, self.width(), self.height()))  # no shadow up by the screen edge
        for i in range(7):  # a soft shadow, drawn as fading rings
            p.setPen(QPen(QColor(0, 0, 0, 34 - i * 5), 1.5 + i * 2.6))
            p.drawPath(path.translated(0, 2 + i * 0.8))
        p.setClipping(False)
        p.setPen(Qt.NoPen)
        p.setBrush(INK)
        p.drawPath(path)
        listening = self.mode == "listen" or self.ears == "listen"
        if listening:
            lv = max(self.levels)
            p.setPen(QPen(accent(int(70 + 150 * lv)), 1.6))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
        if self.mode == "rest" and not self.timers:
            self.draw_nubs(p, b)
        tw, th = self.size_for(self.mode)
        fade = max(0.0, min(1.0, (min(b.width() / max(1, tw), b.height() / max(1, th)) - 0.82) / 0.18))
        if self.mode == "toast" and self.toast_d and self.toast_d.get("ring"):
            pulse = 0.5 + 0.5 * math.sin(now * 7)
            p.setPen(Qt.NoPen)
            p.setBrush(accent(int(40 + 50 * pulse)))
            p.drawPath(path)
        p.setClipPath(path)
        p.setOpacity(fade)
        self.chips = {}
        getattr(self, "draw_" + self.mode)(p, b, now)
        p.end()

    def draw_nubs(self, p, b):
        """At rest: the tips of two ears peek out from under the lip. Folded flat when hushed."""
        down = self.ears in ("deaf", "call", "loading")
        c = b.center().x()
        for side in (-1, 1):
            x = c + side * 13
            h = 3.0 if down else 8.5 + 1.4 * math.sin(time.time() * 2 + side)
            p.setPen(Qt.NoPen)
            p.setBrush(with_alpha(PORCELAIN, 120) if down else PORCELAIN)
            p.drawRoundedRect(QRectF(x - 5, b.bottom() - 4, 10, h + 4), 5, 5)
            if not down:
                p.setBrush(accent(210))
                p.drawRoundedRect(QRectF(x - 2, b.bottom() - 1, 4, h - 1), 2, 2)
        if self.ears == "call":
            p.setBrush(accent())
            p.drawEllipse(QPointF(c, b.bottom() + 6), 2, 2)

    def text(self, p, rect, s, f, color, flags=Qt.AlignVCenter | Qt.AlignLeft):
        p.setFont(f)
        p.setPen(color)
        s = QFontMetricsF(f).elidedText(s, Qt.ElideRight, rect.width()) if not flags & Qt.TextWordWrap else s
        p.drawText(rect, flags, s)

    def chip(self, p, key, x, y, label, solid=False):
        f = font(12, UI, QFont.Medium)
        w = QFontMetricsF(f).horizontalAdvance(label) + 26
        r = QRectF(x, y, w, 28)
        hot = self.hot == key
        p.setPen(Qt.NoPen)
        p.setBrush(accent() if hot or solid else FAINT)
        p.drawRoundedRect(r, 14, 14)
        self.text(p, r, label, f, INK if hot or solid else TXT, Qt.AlignCenter)
        self.chips[key] = r
        return w

    def status(self):
        name = S["name"]
        return {"deaf": "hushed · click to wake me", "call": f"{self.call_app or 'a call'} has the mic · paused",
                "loading": "waking up...", "listen": "listening..."}.get(self.ears, f"say “hey {name}”")

    def draw_rest(self, p, b, now):
        if not self.timers:
            return
        tid, (label, ends, start) = min(self.timers.items(), key=lambda kv: kv[1][1])
        left = max(0, ends - now)
        cx, cy = b.left() + 20, b.center().y()
        p.setPen(QPen(FAINT, 3))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(cx, cy), 8, 8)
        p.setPen(QPen(accent(), 3, Qt.SolidLine, Qt.RoundCap))
        frac = left / max(1, ends - start)
        p.drawArc(QRectF(cx - 8, cy - 8, 16, 16), 90 * 16, int(frac * 360 * 16))
        mm = f"{int(left // 3600)}:{int(left % 3600 // 60):02d}:{int(left % 60):02d}" if left >= 3600 else \
            f"{int(left // 60)}:{int(left % 60):02d}"
        self.text(p, QRectF(cx + 16, b.top(), 64, b.height()), mm, font(13, MONO), TXT)
        more = f" +{len(self.timers) - 1}" if len(self.timers) > 1 else ""
        self.text(p, QRectF(cx + 76, b.top(), b.right() - cx - 88, b.height()), (label or "timer") + more,
                  font(11), DIM)

    def header(self, p, b, now, R=12, h=44, sub=None):
        cx, cy = b.left() + 28, b.top() + h / 2 + 1
        self.face.draw(p, cx, cy + 2, R, now)
        self.text(p, QRectF(cx + 22, b.top(), 160, h), S["name"], font(13, DISPLAY, QFont.Bold), TXT)
        self.text(p, QRectF(b.left() + 150, b.top(), b.width() - 164, h), sub or self.status(), font(12), DIM,
                  Qt.AlignVCenter | Qt.AlignRight)

    def draw_peek(self, p, b, now):
        self.header(p, b, now)

    def draw_menu(self, p, b, now):
        self.header(p, b, now)
        x, y = b.left() + 18, b.top() + 54
        x += self.chip(p, "talk", x, y, "talk", solid=True) + 8
        x += self.chip(p, "live", x, y, "live talk") + 8
        x += self.chip(p, "type", x, y, "type") + 8
        x += self.chip(p, "listen" if self.ears == "deaf" else "hush", x, y,
                       "listen again" if self.ears == "deaf" else "hush") + 8
        self.chip(p, "settings", x, y, "settings")

    def draw_listen(self, p, b, now):
        cx, cy = b.left() + 34, b.center().y() + 3
        self.face.draw(p, cx, cy, 16, now)
        self.text(p, QRectF(cx + 30, b.top() + 9, 200, 20), "listening", font(13, DISPLAY, QFont.Bold), TXT)
        self.text(p, QRectF(cx + 30, b.top() + 29, 220, 18), "go ahead, I'm all ears", font(12), DIM)
        lv = list(self.levels)
        for i in range(7):  # a little equalizer that follows your voice
            v = max(lv[-1 - i * 2:][:2] or [0])
            h = 5 + 26 * v
            x = b.right() - 26 - i * 9
            p.setPen(Qt.NoPen)
            p.setBrush(accent(255 - i * 22))
            p.drawRoundedRect(QRectF(x - 2.5, b.center().y() - h / 2, 5, h), 2.5, 2.5)

    def draw_think(self, p, b, now):
        cx, cy = b.left() + 34, b.center().y() + 3
        self.face.draw(p, cx, cy, 16, now)
        heard = f"“{self.heard}”" if self.heard else "hearing you..."
        self.text(p, QRectF(cx + 30, b.top() + 8, b.width() - 110, 22), heard, font(14), TXT)
        dots = "." * (1 + int(now * 3) % 3)
        self.text(p, QRectF(cx + 30, b.top() + 30, 200, 18), "thinking" + dots, font(12), DIM)
        ox, oy = b.right() - 30, b.center().y()
        for i in range(3):  # three dots orbiting
            a = now * 4 + i * 2.1
            p.setPen(Qt.NoPen)
            p.setBrush(accent(255 - i * 60))
            p.drawEllipse(QPointF(ox + 9 * math.cos(a), oy + 9 * math.sin(a)), 3 - i * 0.5, 3 - i * 0.5)

    def draw_answer(self, p, b, now):
        r = self.reply
        ms = r.get("ms")
        when = "" if ms is None else f" · {ms:.0f} ms" if ms < 1000 else f" · {ms / 1000:.1f} s"
        self.header(p, b, now, sub=TIERS.get(r.get("tier"), r.get("tier", "")) + when)
        body = QRectF(b.left() + 23, b.top() + 46, b.width() - 46, b.height() - 46 - 48)
        self.text(p, body, r.get("text") or "Done.", font(15), TXT, Qt.TextWordWrap | Qt.AlignTop | Qt.AlignLeft)
        x, y = b.left() + 18, b.bottom() - 40
        if r.get("undo"):
            x += self.chip(p, "undo", x, y, "undo") + 8
        x += self.chip(p, "copy", x, y, "copy") + 10
        if r.get("learned"):
            self.text(p, QRectF(x, y, b.right() - x - 16, 28), f"learned → next time instant",
                      font(11, MONO), accent())

    def draw_live(self, p, b, now):
        L = self.live
        cx, cy = b.left() + 36, b.top() + 40
        self.face.draw(p, cx, cy, 18, now)
        st = L.get("state", "")
        tool = L.get("tool")
        status = (f"using {tool.replace('_', ' ')}" if tool and st != "speaking" else
                  {"connecting": "connecting...", "speaking": "speaking", "listening": "listening"}.get(st, st))
        self.text(p, QRectF(cx + 32, b.top() + 10, 200, 20), f"{S['name']} · live", font(12, DISPLAY, QFont.Bold), TXT)
        f = font(11, MONO)
        self.text(p, QRectF(cx + 32 + QFontMetricsF(font(12, DISPLAY, QFont.Bold)).horizontalAdvance(
            f"{S['name']} · live") + 10, b.top() + 10, 160, 20), status, f, accent() if st == "speaking" else DIM)
        line, color = (L.get("it", ""), TXT) if L.get("last") == "it" else (L.get("you", ""), DIM)
        cap = QRectF(cx + 32, b.top() + 34, b.width() - (cx - b.left()) - 120, 26)
        fcap = font(14)
        p.setFont(fcap)
        p.setPen(color)
        p.drawText(cap, Qt.AlignVCenter | Qt.AlignLeft,  # newest words win: elide from the left
                   QFontMetricsF(fcap).elidedText(line or "say anything · interrupt me any time", Qt.ElideLeft,
                                                  cap.width()))
        self.chip(p, "end", b.right() - 70, b.center().y() - 12, "end", solid=True)

    def draw_toast(self, p, b, now):
        d = self.toast_d or {}
        cx, cy = b.left() + 30, b.center().y() + 2
        self.face.draw(p, cx, cy, 12, now)
        self.text(p, QRectF(cx + 24, b.top() + 8, b.width() - 70, 20), d.get("title", ""),
                  font(12, DISPLAY, QFont.Bold), accent() if d.get("ring") else TXT)
        self.text(p, QRectF(cx + 24, b.top() + 28, b.width() - 70, 18), d.get("text", ""), font(12), DIM)

    # ----- mouse -----
    def enterEvent(self, _):
        self.hover, self.hover_t = True, time.time()

    def leaveEvent(self, _):
        self.hover, self.left_at, self.hot = False, time.time(), None

    def mouseMoveEvent(self, e):
        pos = e.position()
        self.hot = next((k for k, r in self.chips.items() if r.contains(pos)), None)
        self.setCursor(Qt.PointingHandCursor if self.hot else Qt.ArrowCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            self.set_mode("menu")
            return
        k = self.hot
        if k == "talk":
            self.act["talk"]()
        elif k == "type":
            self.set_mode("rest")
            self.act["type"]()
        elif k in ("hush", "listen"):
            self.act["hush"](k == "hush")
            self.set_mode("rest")
        elif k == "live":
            self.set_mode("rest")
            self.act["live"]()
        elif k == "end":
            self.act["end_live"]()
        elif k == "settings":
            self.set_mode("rest")
            self.act["settings"]()
        elif k == "undo":
            self.act["undo"]()
        elif k == "copy":
            QApplication.clipboard().setText(self.reply.get("text", ""))
            self.on_toast({"title": "copied", "text": self.reply.get("text", ""), "seconds": 2})
        elif self.mode in ("rest", "peek"):
            self.set_mode("menu")
        elif self.mode == "menu" and self.ears == "deaf":
            self.act["hush"](False)
        elif self.mode in ("answer", "toast", "menu"):
            self.set_mode("rest")

    def mouseDoubleClickEvent(self, _):
        self.act["talk"]()


# ---------- typing instead of talking ----------
class TypeBox(QWidget):
    """Ctrl+Alt+K or "type": a text box under the island for the library, class, or 2 am."""

    def __init__(self, bus):
        super().__init__()
        self.bus, self.face = bus, Creature()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(560, 74)
        self.edit = QLineEdit(self)
        self.edit.setGeometry(64, 15, 470, 40)
        self.edit.setFont(font(16))
        self.edit.setStyleSheet("QLineEdit{background:transparent;border:none;color:#F4F3F8;}")
        self.edit.returnPressed.connect(self.send)
        self.t = QTimer(self)
        self.t.timeout.connect(self.anim)
        self.last = time.time()

    def open(self):
        a = QGuiApplication.primaryScreen().availableGeometry()
        self.move(a.center().x() - self.width() // 2, a.top() + 58)
        self.edit.clear()
        self.edit.setPlaceholderText(f"tell {S['name']} what to do, or ask anything")
        self.show()
        self.raise_()
        self.activateWindow()
        force_foreground(int(self.winId()))
        self.edit.setFocus()
        self.face.poke()
        self.t.start(16)

    def send(self):
        t = self.edit.text().strip()
        self.close_box()
        if t:
            self.bus.ask.emit(t)

    def close_box(self):
        self.hide()
        self.t.stop()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close_box()

    def changeEvent(self, e):
        if e.type() == e.Type.ActivationChange and not self.isActiveWindow() and self.isVisible():
            self.close_box()

    def anim(self):
        now = time.time()
        dt, self.last = min(0.033, now - self.last), now
        self.face.update(dt, now, "listen" if self.edit.text() else "idle")
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing)
        r = QRectF(4, 4, self.width() - 14, self.height() - 14)
        p.setPen(Qt.NoPen)
        p.setBrush(accent())
        p.drawRoundedRect(r.translated(6, 6), 22, 22)  # hard offset shadow
        p.setBrush(INK)
        p.setPen(QPen(accent(), 2))
        p.drawRoundedRect(r, 22, 22)
        self.face.draw(p, 38, r.center().y() + 3, 13, time.time())


# ---------- settings ----------
class Toggle(QAbstractButton):
    def __init__(self, on):
        super().__init__()
        self.setCheckable(True)
        self.setChecked(on)
        self.setFixedSize(46, 26)
        self.setCursor(Qt.PointingHandCursor)
        self.pos_ = 1.0 if on else 0.0
        self.anim = QVariantAnimation(self, duration=220)
        self.anim.valueChanged.connect(lambda v: (setattr(self, "pos_", v), self.update()))
        self.toggled.connect(lambda on: (self.anim.stop(), self.anim.setStartValue(self.pos_),
                                         self.anim.setEndValue(1.0 if on else 0.0), self.anim.start()))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(accent()) if self.isChecked() else LINE)
        p.drawRoundedRect(QRectF(0, 0, 46, 26), 13, 13)
        p.setBrush(INK if self.isChecked() else DIM)
        x = 4 + self.pos_ * 20
        p.drawEllipse(QRectF(x, 4, 18, 18))


class Swatch(QAbstractButton):
    def __init__(self, key):
        super().__init__()
        self.key = key
        self.setCheckable(True)
        self.setFixedSize(32, 32)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(key)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        if self.isChecked():
            p.setBrush(TXT)
            p.drawEllipse(QRectF(0, 0, 32, 32))
            p.setBrush(INK)
            p.drawEllipse(QRectF(2.5, 2.5, 27, 27))
        p.setBrush(QColor(COLORS[self.key]))
        p.drawEllipse(QRectF(5, 5, 22, 22))


class FaceView(QWidget):
    """The creature, big and alive, at the top of settings."""

    def __init__(self):
        super().__init__()
        self.face = Creature()
        self.setFixedSize(96, 96)
        self.last = time.time()
        t = QTimer(self)
        t.timeout.connect(self.tick)
        t.start(16)

    def tick(self):
        now = time.time()
        dt, self.last = min(0.033, now - self.last), now
        cur, g = QCursor.pos(), self.mapToGlobal(self.rect().center())
        self.face.update(dt, now, "listen" if self.underMouse() else "idle", 0.25 if self.underMouse() else 0,
                         (max(-1, min(1, (cur.x() - g.x()) / 400)), max(-1, min(1, (cur.y() - g.y()) / 300))))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing)
        self.face.draw(p, 48, 60, 27, time.time())


STYLE = """
QLabel {{ color:#F4F3F8; font-family:'Space Grotesk'; font-size:13px; }}
QLabel[cap="1"] {{ color:#8E8B9C; font-family:'Geist Mono'; font-size:10px; letter-spacing:1px; }}
QLabel[dim="1"] {{ color:#8E8B9C; font-size:12px; }}
QLineEdit {{ background:#16151D; color:#F4F3F8; border:2px solid #2A2833; border-radius:12px; padding:7px 10px;
            font-family:'Space Grotesk'; font-size:13px; selection-background-color:{a}; selection-color:#0B0B10; }}
QLineEdit:focus {{ border-color:{a}; }}
QLineEdit#name {{ background:transparent; border:none; border-bottom:2px dashed #2A2833; border-radius:0;
                 font-family:'Unbounded'; font-size:30px; font-weight:700; padding:0 0 4px 0; }}
QLineEdit#name:focus {{ border-bottom-color:{a}; }}
QPushButton {{ background:#1C1B24; color:#F4F3F8; border:2px solid #2F2D3A; border-radius:14px; padding:6px 13px;
              font-family:'Space Grotesk'; font-size:12px; }}
QPushButton:hover {{ border-color:{a}; }}
QPushButton:checked {{ background:{a}; color:#0B0B10; border-color:{a}; }}
QPushButton#close {{ border:none; background:transparent; font-size:18px; color:#8E8B9C; padding:0 6px; }}
QPushButton#close:hover {{ color:{a}; }}
QComboBox {{ background:#16151D; color:#F4F3F8; border:2px solid #2A2833; border-radius:12px; padding:6px 10px;
            font-family:'Space Grotesk'; font-size:13px; }}
QComboBox:hover {{ border-color:{a}; }}
QComboBox::drop-down {{ border:none; width:22px; }}
QComboBox QAbstractItemView {{ background:#16151D; color:#F4F3F8; border:2px solid #2A2833; outline:none;
                              selection-background-color:{a}; selection-color:#0B0B10; padding:4px; }}
QSlider::groove:horizontal {{ height:6px; background:#2A2833; border-radius:3px; }}
QSlider::sub-page:horizontal {{ background:{a}; border-radius:3px; }}
QSlider::handle:horizontal {{ background:#FFFFFF; width:18px; height:18px; margin:-6px 0; border-radius:9px; }}
QScrollArea, QWidget#inner {{ background:transparent; }}
QScrollBar:vertical {{ width:6px; background:transparent; margin:26px 0 26px 0; }}
QScrollBar::handle:vertical {{ background:#2A2833; border-radius:3px; min-height:40px; }}
QScrollBar::handle:vertical:hover {{ background:{a}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical, QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {{ height:0; background:transparent; }}
"""


def qlabel(text="", **props):
    w = QLabel(text)
    for k, v in props.items():
        w.setProperty(k, v)  # styled by STYLE's [cap="1"] / [dim="1"]
    return w


class MicMeter(QWidget):
    """What the ears hear, live: a bar that follows your voice, and a flash when your assistant hears its name.
    If the bar barely moves when you talk, turn up mic boost."""

    def __init__(self, bus):
        super().__init__()
        self.setFixedHeight(34)
        self.level, self.peak, self.woke_at = 0.0, 0.0, 0.0
        bus.level.connect(self.on_level)
        bus.state.connect(self.on_state)
        t = QTimer(self)
        t.timeout.connect(self.update)
        t.start(33)

    def on_level(self, x):
        self.level = x
        self.peak = max(x, self.peak - 0.02)

    def on_state(self, name, _d):
        if name == "listen":
            self.woke_at = time.time()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        w = self.width() - 150
        p.setPen(Qt.NoPen)
        p.setBrush(LINE)
        p.drawRoundedRect(QRectF(0, 11, w, 12), 6, 6)
        p.setBrush(accent())
        p.drawRoundedRect(QRectF(0, 11, max(12, w * min(1, self.level * 1.6)), 12), 6, 6)
        p.setBrush(TXT)
        p.drawRoundedRect(QRectF(w * min(1, self.peak * 1.6) - 2, 9, 4, 16), 2, 2)
        heard = time.time() - self.woke_at < 2.5
        f = font(12, UI, QFont.Medium)
        p.setFont(f)
        p.setPen(accent() if heard else DIM)
        msg = "heard my name!" if heard else ("hearing you" if self.level > 0.08 else f"say “hey {S['name']}”")
        p.drawText(QRectF(w + 12, 0, 138, 34), Qt.AlignVCenter | Qt.AlignLeft, msg)


class Settings(QWidget):
    def __init__(self, act, brain, bus):
        super().__init__()
        self.act, self.brain, self.drag = act, brain, None
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(470)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(4, 6, 14, 18)  # inside the card; right/bottom leave room for the offset shadow
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.inner = QWidget()
        self.inner.setObjectName("inner")
        self.scroll.setWidget(self.inner)
        outer.addWidget(self.scroll)
        v = QVBoxLayout(self.inner)
        v.setContentsMargins(26, 16, 16, 12)
        v.setSpacing(10)

        top = QHBoxLayout()
        top.addWidget(self.cap("EARSHOT · SETTINGS"))
        top.addStretch()
        x = QPushButton("×", objectName="close")
        x.setCursor(Qt.PointingHandCursor)
        x.clicked.connect(self.hide)
        top.addWidget(x)
        v.addLayout(top)

        hero = QHBoxLayout()
        self.faceview = FaceView()
        hero.addWidget(self.faceview)
        col = QVBoxLayout()
        col.addWidget(self.cap("ITS NAME IS ITS WAKE WORD"))
        self.name = QLineEdit(S["name"], objectName="name")
        self.name.setMaxLength(20)
        self.name.textEdited.connect(self.name_typed)
        self.name.editingFinished.connect(self.name_done)
        col.addWidget(self.name)
        self.say = qlabel(dim="1")
        col.addWidget(self.say)
        hero.addLayout(col, 1)
        v.addLayout(hero)

        v.addWidget(self.cap("COLOUR"))
        row = QHBoxLayout()
        self.swatches = []
        for k in COLORS:
            s = Swatch(k)
            s.setChecked(k == S["color"])
            s.clicked.connect(lambda _=False, k=k: self.pick_color(k))
            self.swatches.append(s)
            row.addWidget(s)
        row.addStretch()
        v.addLayout(row)

        v.addWidget(self.cap("VOICE"))
        row = QHBoxLayout()
        self.voice = QComboBox()
        for i, label in VOICES.items():
            self.voice.addItem(label, i)
        self.voice.setCurrentIndex(max(0, self.voice.findData(S["voice"])))
        self.voice.currentIndexChanged.connect(lambda _: S.set(voice=self.voice.currentData()))
        row.addWidget(self.voice, 1)
        hear = QPushButton("hear it")
        hear.setCursor(Qt.PointingHandCursor)
        hear.clicked.connect(lambda: self.act["say"](f"Hi! I'm {S['name']}. Say hey {S['name']} whenever you need me."))
        row.addWidget(hear)
        v.addLayout(row)

        v.addWidget(self.cap("TALK MODE VOICE · GEMINI LIVE · CTRL+ALT+L"))
        self.live_voice = QComboBox()
        for k, label in LIVE_VOICES.items():
            self.live_voice.addItem(label, k)
        self.live_voice.setCurrentIndex(max(0, self.live_voice.findData(S["live_voice"])))
        self.live_voice.currentIndexChanged.connect(lambda _: S.set(live_voice=self.live_voice.currentData()))
        v.addWidget(self.live_voice)

        v.addWidget(self.cap("WAKE WORD SENSITIVITY"))
        row = QHBoxLayout()
        row.addWidget(qlabel("strict", dim="1"))
        self.sens = QSlider(Qt.Horizontal)
        self.sens.setRange(0, 100)
        self.sens.setValue(int(S["sensitivity"] * 100))
        self.sens.sliderReleased.connect(lambda: S.set(sensitivity=self.sens.value() / 100))
        row.addWidget(self.sens, 1)
        row.addWidget(qlabel("eager", dim="1"))
        v.addLayout(row)

        v.addWidget(self.cap("MIC CHECK · TALK AND WATCH IT MOVE"))
        self.meter = MicMeter(bus)
        v.addWidget(self.meter)
        row = QHBoxLayout()
        row.addWidget(qlabel("mic boost", dim="1"))
        self.gain = QSlider(Qt.Horizontal)
        self.gain.setRange(10, 80)  # 1x .. 8x
        self.gain.setValue(int(S["mic_gain"] * 10))
        self.gain.valueChanged.connect(lambda v: self.gain_label.setText(f"{v / 10:.1f}x"))
        self.gain.sliderReleased.connect(lambda: S.set(mic_gain=self.gain.value() / 10))
        row.addWidget(self.gain, 1)
        self.gain_label = qlabel(f"{S['mic_gain']:.1f}x", dim="1")
        row.addWidget(self.gain_label)
        v.addLayout(row)

        v.addWidget(self.cap("I SPEAK"))
        row = QHBoxLayout()
        self.langs = {}
        for key, label in (("en", "English"), ("hinglish", "Hinglish (via Gemini)")):
            b = QPushButton(label, checkable=True)
            b.setCursor(Qt.PointingHandCursor)
            b.setChecked(S["language"] == key)
            b.clicked.connect(lambda _=False, key=key: self.pick_lang(key))
            self.langs[key] = b
            row.addWidget(b)
        row.addStretch()
        v.addLayout(row)

        v.addSpacing(4)
        for key, label in (("speak", "say short answers out loud"), ("follow_up", "keep listening after an answer"),
                           ("pause_in_calls", "go deaf while you're on a call"), ("autostart", "start with Windows")):
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            row.addStretch()
            t = Toggle(bool(S[key]))
            t.toggled.connect(lambda on, key=key: S.set(**{key: on}))
            row.addWidget(t)
            v.addLayout(row)

        v.addSpacing(4)
        v.addWidget(self.cap("CLOUD BRAIN · GEMINI FREE TIER"))
        row = QHBoxLayout()
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.Password)
        self.key.editingFinished.connect(self.key_done)
        row.addWidget(self.key, 1)
        get = QPushButton("get a free key ↗")
        get.setCursor(Qt.PointingHandCursor)
        get.clicked.connect(lambda: os.startfile("https://aistudio.google.com/apikey"))
        row.addWidget(get)
        v.addLayout(row)
        self.cloud = qlabel(dim="1")
        self.cloud.setWordWrap(True)
        v.addWidget(self.cloud)
        v.addWidget(self.cap("LOCAL BRAIN · OLLAMA MODEL"))
        self.local = QLineEdit(S["local_model"])
        self.local.editingFinished.connect(lambda: S.set(local_model=self.local.text().strip() or "gemma4:e2b"))
        v.addWidget(self.local)

        v.addSpacing(4)
        v.addWidget(self.cap("MEMORY"))
        row = QHBoxLayout()
        self.learned = qlabel(dim="1")
        row.addWidget(self.learned, 1)
        b = QPushButton("forget phrases")
        b.clicked.connect(lambda: (self.brain.mem.unlearn(), self.refresh()))
        row.addWidget(b)
        b = QPushButton("forget facts")
        b.clicked.connect(lambda: (self.brain.mem.q("delete from facts"), self.refresh()))
        row.addWidget(b)
        v.addLayout(row)
        row = QHBoxLayout()
        for label, path in (("workspaces", WORKSPACES), ("skills", SKILLS), ("data", HOME)):
            b = QPushButton(label + " ↗")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, path=path: os.startfile(path))
            row.addWidget(b)
        row.addStretch()
        q = QPushButton("quit")
        q.clicked.connect(self.act["quit"])
        row.addWidget(q)
        v.addLayout(row)
        self.restyle()

    def cap(self, s):
        return qlabel(s, cap="1")

    def restyle(self):
        self.setStyleSheet(STYLE.format(a=S.accent()))
        self.update()

    def refresh(self):
        n = S["name"]
        self.say.setText(f"say “hey {n}” or “ok {n}” · or Ctrl+Alt+Space")
        k = bool(gemini_key())
        self.key.setPlaceholderText("key saved · paste to replace" if k else "paste your Gemini key")
        self.cloud.setText((f"on · {S['fast_model']} / {S['smart_model']}" if k else
                            "off · everything except web, screen and Hinglish still works")
                           + " · the key lives in Windows Credential Manager")
        self.learned.setText(f"{self.brain.mem.count()} phrases · {len(self.brain.mem.facts())} facts")

    def open(self):
        self.refresh()
        a = QGuiApplication.primaryScreen().availableGeometry()
        full = self.inner.sizeHint().height() + 6 + 18 + 4
        self.setFixedHeight(min(full, a.height() - 40))  # taller than the screen: it scrolls
        self.move(a.right() - self.width() - 24, a.top() + 20)
        self.show()
        self.raise_()
        self.activateWindow()
        force_foreground(int(self.winId()))

    def name_typed(self, t):
        self.say.setText(f"say “hey {t.strip() or '...'}”")
        self.faceview.face.poke(0.12)

    def name_done(self):
        t = " ".join(self.name.text().split()).title()
        if not t or not all(c.isalpha() or c == " " for c in t):
            self.name.setText(S["name"])
            return
        if t != S["name"]:
            S.set(name=t)
            self.faceview.face.poke(0.35)
        self.name.setText(t)
        self.refresh()

    def pick_color(self, k):
        for s in self.swatches:
            s.setChecked(s.key == k)
        S.set(color=k)
        self.restyle()

    def pick_lang(self, k):
        for key, b in self.langs.items():
            b.setChecked(key == k)
        S.set(language=k)

    def key_done(self):
        k = self.key.text().strip()
        if k:
            set_gemini_key(k)
            self.key.clear()
        self.refresh()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing)
        r = QRectF(2, 2, self.width() - 12, self.height() - 12)
        p.setPen(Qt.NoPen)
        p.setBrush(accent())
        p.drawRoundedRect(r.translated(7, 7), 26, 26)
        p.setBrush(INK)
        p.setPen(QPen(LINE, 2))
        p.drawRoundedRect(r, 26, 26)

    def mousePressEvent(self, e):
        self.drag = e.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, e):
        if self.drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self.drag)

    def mouseReleaseEvent(self, _):
        self.drag = None


def tray(act):
    t = QSystemTrayIcon(creature_icon())
    t.setToolTip(f"earshot · {S['name']}")
    m = QMenu()
    m.setStyleSheet("QMenu{background:#0B0B10;color:#F4F3F8;border:2px solid #2A2833;border-radius:10px;padding:6px;"
                    "font-family:'Space Grotesk';font-size:13px;}QMenu::item{padding:6px 18px;border-radius:8px;}"
                    f"QMenu::item:selected{{background:{S.accent()};color:#0B0B10;}}")
    m.addAction("talk", act["talk"])
    m.addAction("type", act["type"])
    m.addAction("settings", act["settings"])
    m.addSeparator()
    m.addAction("quit", act["quit"])
    t.setContextMenu(m)
    t.activated.connect(lambda reason: act["settings"]() if reason == QSystemTrayIcon.Trigger else None)
    t.show()
    t.menu_ = m
    return t
