"""Renders every island state (and the type box and settings) to PNGs, for design checks and the README.

    python tests/shots.py [out_dir]
"""
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["EARSHOT_HOME"] = tempfile.mkdtemp(prefix="earshot-shots-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from earshot import config  # noqa: E402
from earshot.config import S  # noqa: E402

app = QApplication(sys.argv)
from earshot import island as I  # noqa: E402

I.load_fonts()
config.first_run()
out = Path(sys.argv[1] if len(sys.argv) > 1 else "shots")
out.mkdir(parents=True, exist_ok=True)
noop = lambda *a, **k: None  # noqa: E731
act = {k: noop for k in ("talk", "type", "hush", "settings", "undo", "quit", "say")}
bus = I.Bus()
isl = I.Island(bus, act)
isl.clock.stop()


def settle(n=90):
    for _ in range(n):
        isl.last = time.time() - 0.016
        isl.tick()
        time.sleep(0.004)
    isl.W.snap(), isl.H.snap()
    isl.place()


def shot(name, setup, frames=90):
    setup()
    settle(frames)
    img = isl.grab().toImage()
    W, H = max(720, img.width() + 160), img.height() + 70
    bg = QImage(W, H, QImage.Format_ARGB32)
    p = QPainter(bg)
    g = QLinearGradient(0, 0, W, H)
    g.setColorAt(0, QColor("#DCD7EC"))
    g.setColorAt(1, QColor("#F4EFE6"))
    p.fillRect(bg.rect(), g)
    p.fillRect(QRectF(0, 0, W, 14), QColor("#1E1D26"))  # the taskbar it hangs from
    p.drawImage((W - img.width()) // 2, 14, img)
    p.end()
    bg.save(str(out / f"{name}.png"))


isl.ears = "sleep"
shot("1-rest", lambda: None)
shot("2-peek", lambda: (setattr(isl, "hover", True), setattr(isl, "hover_t", 0), isl.set_mode("peek")))
isl.hover = False
isl.left_at = time.time() + 99  # "the mouse just left": the menu stays
shot("3-menu", lambda: isl.set_mode("menu"))


def listen():
    isl.set_mode("listen")
    for v in (0.1, 0.5, 0.9, 0.6, 0.3, 0.8, 0.4, 0.7, 0.2, 0.6, 0.9, 0.5, 0.3, 0.7):
        isl.levels.append(v)


shot("4-listen", listen)
shot("5-think", lambda: bus.state.emit("think", {"heard": "open my dev space"}) or isl.on_state("think", {"heard": "open my dev space"}))
isl.on_state("answer", {"text": "Dev space is up. Ship something.", "tier": "reflex", "ms": 4, "undo": True})
shot("6-answer-reflex", lambda: setattr(isl, "until", time.time() + 99))
isl.on_state("answer", {"text": "Kilimanjaro, at 5,895 m. It's a dormant volcano in Tanzania, and you can hike "
                                "to the top without climbing gear.", "tier": "cloud", "ms": 1240})
shot("7-answer-cloud", lambda: setattr(isl, "until", time.time() + 99))
isl.on_state("answer", {"text": "Opening Spotify.", "tier": "cloud", "ms": 980, "undo": True,
                        "learned": "i wanna hear some music"})
shot("8-answer-learned", lambda: setattr(isl, "until", time.time() + 99))
isl.set_mode("rest")
isl.on_timer("t1", "tea", time.time() + 4 * 60 + 12)
isl.timers["t1"] = ("tea", time.time() + 4 * 60 + 12, time.time() - 48)
shot("9-timer", lambda: None)
isl.timers.clear()
shot("10-ring", lambda: isl.on_ring("t1", "tea"), frames=40)
isl.set_mode("rest")
shot("11-toast", lambda: isl.on_toast({"title": "slowship deployed", "text": "posted by your CI script", "seconds": 99}))
isl.set_mode("rest")
isl.ears = "deaf"
shot("12-hushed", lambda: setattr(isl, "toast_d", None))
isl.ears = "sleep"
for c in ("lime", "electric", "mint"):
    S["color"] = c
    shot(f"13-listen-{c}", listen)
S["color"] = "bubblegum"

isl.ears = "talk"
isl.set_mode("rest")


def live(state, **d):
    def go():
        isl.on_state("live", {"state": "connecting"})
        isl.on_state("live", {"state": state, **d})
        for v in (0.1, 0.6, 0.9, 0.5, 0.8, 0.3):
            isl.levels.append(v)
        isl.out_level = 0.7 if state == "speaking" else 0.0
    return go


shot("16-live-speaking", live("speaking", you="what should I cook tonight",
                              it="How about a quick paneer bhurji? Ten minutes, one pan, and you probably have everything."))
shot("17-live-listening", live("listening", you="actually, can you set a timer for ten minutes"))
shot("18-live-tool", live("listening", you="open my dev space", tool="open_workspace"))
isl.on_state("live_end", {})
isl.ears = "sleep"

box = I.TypeBox(bus)
box.edit.setText("make this email sound more formal")
box.resize(560, 74)
box.grab().save(str(out / "14-typebox.png"))
panel = I.Settings(act, type("B", (), {"mem": type("M", (), {"facts": lambda s: ["a"] * 3, "count": lambda s: 12,
                                                                    "unlearn": noop, "q": noop})()})(), bus)
panel.meter.level, panel.meter.peak = 0.42, 0.55
panel.refresh()
panel.setFixedHeight(min(panel.inner.sizeHint().height() + 28, 824))  # like open() on this screen
panel.grab().save(str(out / "15-settings.png"))
print("saved to", out.resolve())
