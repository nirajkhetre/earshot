"""Paths, settings and the Gemini key.

Your stuff lives in %APPDATA%\\earshot: settings.json, memory.db, and the workspaces/ and skills/ folders you edit.
The Gemini key goes to Windows Credential Manager, never to a file.
"""
import json
import os
import secrets
import shutil
import sys
import threading
import time
import winreg
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = Path(os.environ.get("EARSHOT_MODELS") or ROOT / "models")
HOME = Path(os.environ.get("EARSHOT_HOME") or Path(os.environ.get("APPDATA", Path.home())) / "earshot")
WORKSPACES, SKILLS = HOME / "workspaces", HOME / "skills"
PORT = int(os.environ.get("EARSHOT_PORT", 7878))
FONTS = Path(__file__).parent / "fonts"

# one loud accent at a time; the island is always ink
COLORS = {"bubblegum": "#FF6EC7", "lime": "#C6FF3D", "electric": "#7B8CFF", "tangerine": "#FF9F43",
          "mint": "#3DFFC5", "lilac": "#C3A6FF"}
# Kokoro v1.0 speakers worth offering: id -> label
VOICES = {3: "Heart (US)", 2: "Bella (US)", 7: "Nova (US)", 15: "Liam (US)", 16: "Michael (US)",
          21: "Emma (UK)", 26: "George (UK)", 31: "Alpha (Indian)", 33: "Omega (Indian)"}

# Gemini Live's own voices, for talk mode
LIVE_VOICES = {"Kore": "Kore (firm)", "Puck": "Puck (upbeat)", "Aoede": "Aoede (breezy)", "Leda": "Leda (youthful)",
               "Zephyr": "Zephyr (bright)", "Charon": "Charon (informative)", "Fenrir": "Fenrir (excitable)",
               "Orus": "Orus (firm)"}

DEFAULTS = {
    "name": "Nova",
    "color": "bubblegum",
    "voice": 3,
    "speak": True,            # say short answers out loud when you spoke to it
    "follow_up": True,        # keep listening a few seconds after an answer
    "pause_in_calls": True,   # deaf while another app uses the mic
    "sensitivity": 0.5,       # wake phrase: 0 strict .. 1 eager
    "mic_gain": 1.0,          # mic boost, 1x .. 8x
    "language": "en",         # "en", or "hinglish" (the clip goes to Gemini to transcribe)
    "local_model": "gemma4:e2b",
    "fast_model": "gemini-3.1-flash-lite",
    "smart_model": "gemini-3.8-flash",
    "live_model": "gemini-3.8-live",
    "live_voice": "Kore",
    "autostart": False,
    "mcp": {},                # {"name": {"command": "...", "args": [...]}}: any MCP server becomes a skill
}


class Settings(dict):
    """settings.json as a dict; set() saves and tells whoever listens."""

    def __init__(self):
        HOME.mkdir(parents=True, exist_ok=True)
        self.file, self.lock, self.listeners = HOME / "settings.json", threading.Lock(), []
        saved = {}
        if self.file.exists():
            try:
                saved = json.loads(self.file.read_text(encoding="utf-8"))
            except ValueError:
                pass
        super().__init__({**DEFAULTS, **saved})

    def set(self, **kw):
        with self.lock:
            self.update(kw)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self, indent=2), encoding="utf-8")
            tmp.replace(self.file)
        for fn in self.listeners:
            fn(kw)

    def accent(self):
        return COLORS.get(self["color"], COLORS["bubblegum"])


S = Settings()


LOG = HOME / "earshot.log"
_log_lock = threading.Lock()


def log(*parts):
    """One line in %APPDATA%\\earshot\\earshot.log: what it heard on wake attempts, what it did, what broke.
    Local only; trimmed at every start (see __main__)."""
    line = time.strftime("%H:%M:%S ") + " ".join(str(p) for p in parts)
    with _log_lock:
        try:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def first_run():
    """Copy the example workspaces and skills once, so there is something to say on day one."""
    for d in (WORKSPACES, SKILLS):
        if not d.exists():
            shutil.copytree(Path(__file__).parent / "examples" / d.name, d)


def token():
    """Shared secret for the local HTTP endpoint, so a web page can't drive the assistant."""
    f = HOME / "token"
    if not f.exists():
        f.write_text(secrets.token_urlsafe(24), encoding="utf-8")
    return f.read_text(encoding="utf-8").strip()


CRED = "earshot/gemini"


def gemini_key():
    k = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if k:
        return k
    try:
        import win32cred
        return win32cred.CredRead(CRED, win32cred.CRED_TYPE_GENERIC)["CredentialBlob"].decode("utf-16-le")
    except Exception:
        return ""


def set_gemini_key(k):
    import win32cred
    if not k:
        try:
            win32cred.CredDelete(CRED, win32cred.CRED_TYPE_GENERIC)
        except Exception:
            pass
        return
    win32cred.CredWrite({"Type": win32cred.CRED_TYPE_GENERIC, "TargetName": CRED, "UserName": "earshot",
                         "CredentialBlob": k, "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE}, 0)


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def set_autostart(on):
    """HKCU Run: no admin needed, starts at every sign-in."""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            pyw = Path(sys.executable).with_name("pythonw.exe")
            winreg.SetValueEx(k, "earshot", 0, winreg.REG_SZ, f'"{pyw}" "{ROOT / "run.pyw"}"')
        else:
            try:
                winreg.DeleteValue(k, "earshot")
            except FileNotFoundError:
                pass
