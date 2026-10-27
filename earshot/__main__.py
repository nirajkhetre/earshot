"""earshot: an always-on assistant in a Dynamic Island for Windows.

    python -m earshot                          run it (the island appears; say "hey Nova")
    python -m earshot notify "Build done" "slowship deployed"     pop a toast on the island from any script
    python -m earshot ask "open dev space"     do something, as if you'd said it
    python -m earshot autostart on|off         start with Windows
    python -m earshot selftest "hey Nova, what time is it"   speak it into the running app's ears (Kokoro voice)

Ctrl+Alt+Space: talk without the wake word. Ctrl+Alt+K: type instead. Ctrl+Alt+L: talk mode on/off.
"""
import ctypes
import faulthandler
import json
import os
import sys
import threading
import time
import traceback
import urllib.request
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import HOME, LOG, PORT, S, first_run, log, set_autostart, token


def cli(args):
    if args[0] == "autostart":
        set_autostart(args[1:2] != ["off"])
        S.set(autostart=args[1:2] != ["off"])
        return print("autostart", "off" if args[1:2] == ["off"] else "on")
    path, body = {"notify": ("/notify", {"title": args[1], "text": " ".join(args[2:])}),
                  "selftest": ("/selftest", {"text": " ".join(args[1:])})}.get(args[0],
                                                                              ("/ask", {"text": " ".join(args[1:])}))
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "X-Earshot-Token": token()})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            print(json.loads(r.read()).get("text", "ok"))
    except OSError:
        print("earshot isn't running. Start it with: python -m earshot")


def already_running():
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\earshot-island")
    return ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


def hotkeys(on_talk, on_type, on_live, notify):
    """Global hotkeys need their own thread with a Windows message loop."""
    u = ctypes.windll.user32
    keys = {1: (0x20, "Ctrl+Alt+Space", on_talk), 2: (0x4B, "Ctrl+Alt+K", on_type), 3: (0x4C, "Ctrl+Alt+L", on_live)}
    for i, (vk, name, _) in keys.items():
        if not u.RegisterHotKey(None, i, 0x4003, vk):  # MOD_ALT|MOD_CONTROL|MOD_NOREPEAT
            notify({"title": f"{name} is taken", "text": "another app owns that shortcut", "seconds": 8})
    msg = wintypes.MSG()
    while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == 0x0312 and msg.wParam in keys:  # WM_HOTKEY
            keys[msg.wParam][2]()


class App:
    def __init__(self):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication

        from . import hands
        from .brain import Brain
        from .island import Bus, Island, Settings, TypeBox, load_fonts, tray
        from .voice import Voice

        self.qt = QApplication(sys.argv)
        self.qt.setQuitOnLastWindowClosed(False)
        load_fonts()
        first_run()
        from .live import Talk

        self.hands, self.ears = hands, None
        self.bus, self.brain, self.voice = Bus(), Brain(), Voice()
        self.live = Talk(self.brain, self.on_talk, on_out_level=self.bus.outlevel.emit)
        act = {"talk": self.talk, "type": self.type, "hush": self.hush, "settings": self.settings,
               "undo": lambda: self.work(self.undo), "quit": self.quit,
               "say": lambda t: self.work(lambda: self.voice.say(t)),
               "live": self.toggle_live, "end_live": self.live.stop}
        self.island = Island(self.bus, act)
        self.island.on_fullscreen = lambda fs: self.work(lambda: self.brain.warm(not fs))  # a game wants the VRAM
        self.box, self.panel = TypeBox(self.bus), Settings(act, self.brain, self.bus)
        self.tray = tray(act)
        self.bus.ask.connect(lambda t: self.work(lambda: self.answer(t, "typed")))
        ui = hands.ui
        ui.notify = lambda title, text="", **kw: self.bus.toast.emit({"title": title, "text": text, **kw})
        ui.timer = lambda tid, label, ends: self.bus.timer.emit(tid, label, ends)
        ui.ring = self.ring
        ui.hush = lambda on: self.hush(on)
        ui.hide = lambda on: (self.bus.hide.emit(on), time.sleep(0.05))
        ui.talk = self.start_live
        S.listeners.append(self.settings_changed)
        self.work(self.start_ears)
        self.work(self.check_ollama)
        self.work(hands.load_apps)
        threading.Thread(target=hotkeys, args=(self.talk_hotkey, self.type_hotkey, self.toggle_live,
                                               self.bus.toast.emit), daemon=True).start()
        threading.Thread(target=self.serve, daemon=True).start()
        self.snap = QTimer()
        self.snap.timeout.connect(lambda: self.work(self.save_last))
        self.snap.start(10 * 60 * 1000)  # "where was I" always has something to restore

    # ----- plumbing -----
    def work(self, fn):
        threading.Thread(target=fn, daemon=True).start()

    def start_ears(self):
        from .config import MODELS
        from .ears import ASR, KWS, Ears
        if not (KWS.exists() and ASR.exists() and (MODELS / "silero_vad.onnx").exists()):
            log(f"voice models missing in {MODELS}")
            return self.bus.toast.emit({"title": "voice models missing", "text": "run setup.bat (or scripts/get_models.py)",
                                        "seconds": 30})
        try:
            self.ears = Ears(on_wake=self.wake, on_utterance=self.heard, on_quiet=self.quiet,
                             on_level=self.bus.level.emit,
                             on_mode=lambda m: self.bus.state.emit("ears", {"mode": m, "call": self.ears.call}),
                             on_error=lambda m: self.bus.toast.emit({"title": "microphone", "text": m, "seconds": 10}))
            self.bus.state.emit("ears", {"mode": self.ears.mode, "call": self.ears.call})
            self.bus.toast.emit({"title": f"{S['name']} is listening", "text": f"say “hey {S['name']}”",
                                 "seconds": 4})
        except Exception as e:
            log("ears failed to start:\n" + traceback.format_exc())
            self.bus.toast.emit({"title": "couldn't open the ears", "text": str(e)[:100], "seconds": 12})

    def check_ollama(self):
        """Gemma is optional. Say once if it's missing, instead of failing quietly on every odd phrasing."""
        if not self.brain.warm():
            log("Ollama or the local model isn't available")
            self.bus.toast.emit({"title": "on-device brain is off", "seconds": 12,
                                 "text": f"install Ollama, then: ollama pull {S['local_model']}"})

    def settings_changed(self, kw):
        if self.ears and ("name" in kw or "sensitivity" in kw):
            self.ears.rename(S["name"])
        if "autostart" in kw:
            from .config import set_autostart
            set_autostart(kw["autostart"])
        if "pause_in_calls" in kw and self.ears and not kw["pause_in_calls"]:
            self.ears.call = ""
            self.ears.sleep()

    # ----- the conversation -----
    def wake(self):
        from .voice import chime
        self.woke_at, self.woke = time.time(), True
        chime()
        self.bus.state.emit("listen", {})

    def heard(self, samples):
        self.work(lambda: self.respond(samples))

    def respond(self, samples):
        try:
            self._respond(samples)
        except Exception:  # never leave the ears stuck in "busy"
            log("respond crashed:\n" + traceback.format_exc())
            self.bus.state.emit("answer", {"text": "Something broke on my side. It's in earshot.log.", "tier": "error"})
            self.ears.sleep()

    def _respond(self, samples):
        self.bus.state.emit("think", {"heard": ""})
        text = ""
        if S["language"] == "hinglish" and self.brain.cloud_ready():
            try:
                text = self.brain.transcribe(samples)
            except Exception:
                text = ""
        text = text or self.ears.transcribe(samples)
        log(f"heard {len(samples) / 16000:.1f}s: {text!r}")
        woke, self.woke = getattr(self, "woke", False), False
        self.follow_up = not woke  # nobody said the name: a follow-up, or the hotkey
        if woke:  # the clip starts with "hey <name>": keep only what came after it
            from .ears import after_name
            text = after_name(text, S["name"])
        from .brain import clean
        if len(clean(text, S["name"])) < 2:
            if time.time() - getattr(self, "woke_at", 0) < 3:
                # just the tail of "hey Nova" (you paused after the name): keep listening for the command
                self.woke_at = 0
                self.ears.listen(wait=5.0)
                self.bus.state.emit("listen", {})
                return
            self.bus.state.emit("rest", {})  # a false wake, or a cough: back to sleep without a word
            self.ears.sleep()
            return
        self.answer(text, "voice")

    def answer(self, text, source):
        self.bus.state.emit("think", {"heard": text})
        t = time.perf_counter()
        try:
            r = self.brain.handle(text, source)
        except Exception as e:
            r = {"text": f"Something broke: {e}", "tier": "error"}
        r["ms"] = (time.perf_counter() - t) * 1000
        log(f"answer [{r.get('tier')} {r['ms']:.0f} ms]: {r.get('text', '')[:120]!r}")
        if self.live.active:  # "let's talk" started talk mode: the island and the mic belong to it now
            return r
        if r.get("again") and getattr(self, "follow_up", False):
            r["quiet"] = True  # a follow-up nobody named it for, and it made no sense: room noise or its own echo
        if r.get("quiet"):
            self.bus.state.emit("rest", {})
            if self.ears:
                self.ears.sleep()
            return r
        self.bus.state.emit("answer", r)
        if source == "voice" and self.ears:
            if S["speak"] and r.get("text") and len(r["text"]) < 260:
                self.ears.busy()
                try:
                    self.voice.say(r["text"])
                except Exception:  # no speakers, a device that vanished: the answer is on the island anyway
                    log("voice:\n" + traceback.format_exc())
            if S["follow_up"]:
                self.ears.listen(wait=5.0, skip=0.8)  # answer back without the name (minus its own echo)
            else:
                self.ears.sleep()
        return r

    def quiet(self):
        self.bus.state.emit("quiet", {})
        self.ears.sleep()

    # ----- talk mode (Gemini Live) -----
    def start_live(self):
        from .config import gemini_key
        if not gemini_key():
            return "Talk mode needs a free Gemini key. Paste one in settings."
        if not self.ears:
            return "Still waking up, one second."
        if not self.live.active:
            self.voice.stop()
            self.live.start()
            self.ears.talk(self.live.feed)
            self.bus.state.emit("live", {"state": "connecting", "you": "", "it": ""})
        return "Talk mode. Go ahead."

    def toggle_live(self):
        if self.live.active:
            return self.live.stop()
        r = self.start_live()
        if not self.live.active:
            self.bus.toast.emit({"title": "talk mode", "text": r, "seconds": 6})

    def on_talk(self, kind, value):
        if kind == "state" and value == "ended":
            if self.ears:
                self.ears.sleep()
            self.bus.state.emit("live_end", {})
        elif kind == "error":
            self.bus.toast.emit({"title": "talk mode stopped", "text": value, "seconds": 8})
        else:
            self.bus.state.emit("live", {kind: value})

    def undo(self):
        self.bus.state.emit("answer", {"text": self.brain.undo(), "tier": "reflex"})

    def ring(self, tid, label):
        self.bus.ring.emit(tid, label)
        from .voice import chime
        for _ in range(3):
            chime()
            time.sleep(0.35)
        if S["speak"] and self.ears:
            self.ears.busy()
            self.voice.say(f"Time's up{': ' + label if label else ''}.")
            self.ears.sleep()

    # ----- buttons and hotkeys -----
    def talk(self):
        if not self.ears:
            return self.bus.toast.emit({"title": "still waking up", "text": "give me a second", "seconds": 2})
        if self.live.active:
            return
        from .voice import chime
        self.voice.stop()
        chime()
        self.ears.listen(wait=6.0)
        self.bus.state.emit("listen", {})

    def talk_hotkey(self):
        self.talk()

    def type(self):
        self.brain.focus = self.hands.user32.GetForegroundWindow()  # "rewrite this" acts on the app you were in
        self.box.open()

    def type_hotkey(self):
        self.bus.cmd.emit("type")  # widgets only on the UI thread

    def hush(self, on):
        if self.ears:
            self.ears.hush(on)

    def settings(self):
        self.panel.open()

    def save_last(self):
        try:
            self.hands.save_workspace("last")
        except Exception as e:
            print("snapshot:", e)

    def quit(self):
        log("quit (from the island, tray or settings)")
        self.save_last()
        self.qt.quit()

    # ----- the local endpoint: scripts, Stream Deck, CI, anything can talk to the island -----
    def serve(self):
        app, secret = self, token()

        class H(BaseHTTPRequestHandler):
            def reply(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                e = app.ears
                self.reply(200, {"ok": True, "name": S["name"], "ears": e.mode if e else "loading",
                                 "mic_busy_by": e.call if e else ""}) if self.path == "/ping" else self.reply(404, {})

            def do_POST(self):
                if self.headers.get("X-Earshot-Token") != secret:  # a web page can't send this header cross-site
                    return self.reply(403, {"error": "token"})
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                except ValueError:
                    return self.reply(400, {"error": "json"})
                if self.path == "/notify":
                    app.bus.toast.emit({"title": str(body.get("title", ""))[:80], "text": str(body.get("text", ""))[:200],
                                        "seconds": float(body.get("seconds", 6))})
                    return self.reply(200, {"text": "shown"})
                if self.path == "/ask":
                    r = app.answer(str(body.get("text", "")), "api")
                    return self.reply(200, {k: v for k, v in r.items() if k in ("text", "tier", "ms", "learned")})
                if self.path == "/selftest":  # Kokoro says it into the ears, exactly where your voice would go
                    if not app.ears or not app.voice.tts:
                        return self.reply(503, {"text": "still loading"})
                    import numpy as np
                    said = str(body.get("text") or f"hey {S['name']}, what time is it?")
                    a = app.voice.tts.generate(said, sid=16)
                    x = np.asarray(a.samples, np.float32)
                    x = np.interp(np.linspace(0, len(x) - 1, int(len(x) * 16000 / a.sample_rate)), np.arange(len(x)), x)
                    x = x / max(1e-6, float(np.abs(x).max())) * float(body.get("volume", 0.5))  # 0.015 = a quiet mic
                    pad = np.zeros(8000, np.float32)
                    app.ears.inject(np.concatenate([pad, x.astype(np.float32), pad, pad, pad]))
                    log(f"selftest: injected {said!r} at peak {float(body.get('volume', 0.5))}")
                    return self.reply(200, {"text": "speaking it into the ears; watch the island and earshot.log"})
                if self.path == "/show":
                    app.bus.state.emit("peek", {})
                    return self.reply(200, {"text": "hi"})
                self.reply(404, {})

            def log_message(self, *a):
                pass

        try:
            ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
        except OSError as e:
            self.bus.toast.emit({"title": f"port {PORT} is busy", "text": str(e)[:80]})


def start_log():
    """pythonw has no console: print() and tracebacks would vanish. Send them, and native crash dumps of every
    thread, to %APPDATA%\\earshot\\earshot.log (trimmed to the last 100 KB when it passes 300 KB)."""
    HOME.mkdir(parents=True, exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > 300_000:
        LOG.write_text(LOG.read_text(encoding="utf-8", errors="replace")[-100_000:], encoding="utf-8")
    out = open(LOG, "a", encoding="utf-8", buffering=1)
    if sys.stderr is None:
        sys.stdout = sys.stderr = out
    faulthandler.enable(out, all_threads=True)
    threading.excepthook = lambda a: log(f"thread {a.thread.name} crashed:\n" + "".join(
        traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)))
    sys.excepthook = lambda *e: log("crashed:\n" + "".join(traceback.format_exception(*e)))
    log(f"---- earshot starting (pid {os.getpid()}, name {S['name']})")


def main():
    args = sys.argv[1:]
    if args and args[0] in ("notify", "ask", "autostart", "selftest"):
        return cli(args)
    start_log()
    if already_running():
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/show", b"{}", {"X-Earshot-Token": token()})
        try:
            urllib.request.urlopen(req, timeout=3)
        except OSError:
            pass
        return
    app = App()
    sys.exit(app.qt.exec())


if __name__ == "__main__":
    main()
