"""The hands: everything the assistant can do on this PC, as plain functions.

Each @tool is offered to every brain tier (instant rules, local Gemma, Gemini) straight from its signature and
docstring, so a new power is one function. A tool returns one short sentence for the island; if it can be
reversed, it pushes an undo ("undo that").
"""
import ctypes
import difflib
import inspect
import json
import os
import re
import shutil
import subprocess
import threading
import time
import tomllib
import urllib.parse
import webbrowser
import winreg
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

import pythoncom
import win32api
import win32clipboard
import win32con
import win32gui
import win32process

from .config import HOME, S, SKILLS, WORKSPACES

user32, dwm, kernel32 = ctypes.windll.user32, ctypes.windll.dwmapi, ctypes.windll.kernel32
try:
    user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # real pixels everywhere (Qt already did this in the app)
except AttributeError:
    pass  # Windows 10 before 1703
NO_WINDOW = 0x08000000
TOOLS, UNDO = {}, []  # UNDO: (label, fn, at), newest last


def tool(learn=False, cloud=False):
    """learn: a phrase that ends in this call can become an instant reflex. cloud: needs Gemini."""
    def deco(fn):
        fn.learn, fn.cloud = learn, cloud
        TOOLS[fn.__name__] = fn
        return fn
    return deco


def undoable(label, fn):
    UNDO.append((label, fn, time.time()))
    del UNDO[:-20]


class UI:
    """Hooks the app fills in; printing keeps the tools usable from a plain script."""
    notify = staticmethod(lambda title, text="", **kw: print(f"[{title}] {text}"))
    timer = staticmethod(lambda tid, label, ends: None)
    ring = staticmethod(lambda tid, label: print("time's up:", label))
    hush = staticmethod(lambda on: None)
    hide = staticmethod(lambda on: None)
    talk = staticmethod(lambda: "Talk mode only works inside the app.")


ui = UI()
TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}


def schema(fn):
    """JSON schema from the signature; the docstring's first paragraph describes the tool, 'name: text' lines
    describe its parameters."""
    head, _, rest = (inspect.getdoc(fn) or fn.__name__).partition("\n\n")
    notes = dict(re.findall(r"^(\w+): (.+)$", rest, re.M))
    props, req = {}, []
    for n, p in inspect.signature(fn).parameters.items():
        props[n] = {"type": TYPES.get(p.annotation, "string"), "description": notes.get(n, n)}
        if p.default is inspect.Parameter.empty:
            req.append(n)
    return {"name": fn.__name__, "description": " ".join(head.split()),
            "parameters": {"type": "object", "properties": props, "required": req}}


def com():
    pythoncom.CoInitialize()  # pycaw/pyvda talk COM; every worker thread needs this once (repeat calls are free)


# ---------- matching what you said to what exists ----------
def norm(s):
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", s.lower()).split())


NICK = {"vs code": "visual studio code", "vscode": "visual studio code", "code": "visual studio code",
        "chrome": "google chrome", "edge": "microsoft edge", "explorer": "file explorer", "files": "file explorer",
        "cmd": "command prompt", "ppt": "powerpoint", "insta": "instagram", "store": "microsoft store",
        "teams": "microsoft teams", "calc": "calculator", "whatsapp web": "whatsapp"}


def best(query, names, cutoff=0.62):
    """(name, score): exact > prefix > every word present > edit similarity."""
    q = norm(NICK.get(norm(query), query))
    top, score = None, 0.0
    for n in names:
        k = norm(n)
        if not k:
            continue
        s = (1.0 if q == k else 0.93 if k.startswith(q + " ") or (len(q) > 3 and k.startswith(q)) else
             0.88 if q and set(q.split()) <= set(k.split()) else difflib.SequenceMatcher(None, q, k).ratio())
        if s > score:
            top, score = n, s
    return (top, score) if score >= cutoff else (None, score)


# ---------- windows ----------
class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def exe_of(h):
    _, pid = win32process.GetWindowThreadProcessId(h)
    hp = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not hp:
        return ""
    try:
        buf, n = ctypes.create_unicode_buffer(520), wintypes.DWORD(520)
        return buf.value if kernel32.QueryFullProcessImageNameW(hp, 0, buf, ctypes.byref(n)) else ""
    finally:
        kernel32.CloseHandle(hp)


def cloaked(h):
    v = ctypes.c_int(0)
    dwm.DwmGetWindowAttribute(h, 14, ctypes.byref(v), 4)  # DWMWA_CLOAKED: other virtual desktop, suspended UWP
    return v.value != 0


def windows():
    """Real app windows on this desktop: visible, titled, not tool windows or dialogs, not ours."""
    out, me = [], os.getpid()

    def cb(h, _):
        if (not user32.IsWindowVisible(h) or user32.GetWindow(h, 4) or cloaked(h)
                or user32.GetWindowLongW(h, -20) & 0x80):
            return True
        title, cls = win32gui.GetWindowText(h), win32gui.GetClassName(h)
        if title and cls not in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            if win32process.GetWindowThreadProcessId(h)[1] != me:
                out.append({"hwnd": h, "title": title, "cls": cls, "exe": exe_of(h)})
        return True
    win32gui.EnumWindows(cb, None)
    return out


def hwnds():
    return {w["hwnd"] for w in windows()}


def stem(w):
    return Path(w["exe"]).stem.lower()


def work_area():
    return win32api.GetMonitorInfo(win32api.MonitorFromPoint((0, 0), 1))["Work"]


GAP = 10
SPOTS = {"left": (0, 0, .5, 1), "right": (.5, 0, .5, 1), "top": (0, 0, 1, .5), "bottom": (0, .5, 1, .5),
         "top-left": (0, 0, .5, .5), "top-right": (.5, 0, .5, .5), "bottom-left": (0, .5, .5, .5),
         "bottom-right": (.5, .5, .5, .5), "center": (.15, .08, .7, .84), "left-third": (0, 0, 1 / 3, 1),
         "middle-third": (1 / 3, 0, 1 / 3, 1), "right-third": (2 / 3, 0, 1 / 3, 1),
         "left-two-thirds": (0, 0, 2 / 3, 1), "right-two-thirds": (1 / 3, 0, 2 / 3, 1)}


def frame(h):
    """The visible frame; GetWindowRect includes Windows 11's invisible resize borders."""
    r = RECT()
    dwm.DwmGetWindowAttribute(h, 9, ctypes.byref(r), ctypes.sizeof(r))  # DWMWA_EXTENDED_FRAME_BOUNDS
    return r.left, r.top, r.right, r.bottom


def place(h, where):
    if not where:
        return
    if where in ("full", "max"):
        user32.ShowWindow(h, 3)
        return
    fx, fy, fw, fh = SPOTS[where] if isinstance(where, str) else where
    l, t, r, b = work_area()
    W, H = r - l, b - t
    x, y, w, hh = l + fx * W + GAP / 2, t + fy * H + GAP / 2, fw * W - GAP, fh * H - GAP
    user32.ShowWindow(h, 9)  # restore from maximized/minimized first
    wr, fr = win32gui.GetWindowRect(h), frame(h)
    dl, dt, dr, db = fr[0] - wr[0], fr[1] - wr[1], wr[2] - fr[2], wr[3] - fr[3]
    user32.SetWindowPos(h, 0, int(x - dl), int(y - dt), int(w + dl + dr), int(hh + dt + db), 0x0004 | 0x0010)


def focus(h):
    if user32.IsIconic(h):
        user32.ShowWindow(h, 9)
    user32.keybd_event(0x12, 0, 0, 0)  # a tap of Alt lets a background app take the foreground
    user32.keybd_event(0x12, 0, 2, 0)
    user32.SetForegroundWindow(h)


def close_windows(hs):
    for h in hs:
        if user32.IsWindow(h):
            user32.PostMessageW(h, 0x0010, 0, 0)  # WM_CLOSE: apps can still ask to save


def press(*vks):
    for vk in vks:
        user32.keybd_event(vk, 0, 0, 0)
    for vk in reversed(vks):
        user32.keybd_event(vk, 0, 2, 0)


def clip_get():
    for _ in range(10):
        try:
            win32clipboard.OpenClipboard()
            try:
                return (win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT) else "")
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            time.sleep(0.05)  # another app holds the clipboard for a moment
    return ""


def clip_set(text):
    for _ in range(10):
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
                return
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            time.sleep(0.05)


def undo_new_windows(label, before):
    """Undo = close the windows that appeared after `before` (within 2 minutes, after that it's too late to guess)."""
    def fn():
        close_windows(hwnds() - before)
    undoable(label, fn)


# ---------- apps ----------
APPS = {}  # Start menu name -> AppID (classic and Store apps alike)


def load_apps(refresh=False):
    f = HOME / "apps.json"
    if f.exists() and not refresh and time.time() - f.stat().st_mtime < 6 * 3600:
        APPS.update(json.loads(f.read_text(encoding="utf-8")))
        return
    ps = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; Get-StartApps | ConvertTo-Json -Compress"
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                         encoding="utf-8", timeout=40, creationflags=NO_WINDOW).stdout
    data = json.loads(out or "[]")
    APPS.clear()
    APPS.update({d["Name"]: d["AppID"] for d in (data if isinstance(data, list) else [data]) if d.get("Name")})
    f.write_text(json.dumps(APPS), encoding="utf-8")


def find_app(name):
    if not APPS:
        load_apps()
    n, _ = best(name, APPS)
    f = HOME / "apps.json"
    if not n and (not f.exists() or time.time() - f.stat().st_mtime > 600):
        load_apps(refresh=True)  # installed something new? (at most every 10 minutes: a miss costs ~2 s)
        n, _ = best(name, APPS)
    return n


@tool(learn=True)
def open_app(name: str):
    """Open an installed app by name, like Spotify, VS Code, Chrome, WhatsApp, Notepad, Settings.

    name: the app's name as the user said it"""
    n = find_app(name)
    if not n:
        return f"I couldn't find an app called {name}."
    before = hwnds()
    os.startfile(f"shell:AppsFolder\\{APPS[n]}")
    undo_new_windows(f"close {n}", before)
    return f"Opening {n}."


def windows_named(name):
    ws = windows()
    keys = {f'{Path(w["exe"]).stem} {w["title"]}': w for w in ws}
    app = find_app(name) if APPS else None
    hits = [w for w in ws if norm(name) in norm(Path(w["exe"]).stem + " " + w["title"])]
    if not hits and app:
        hits = [w for w in ws if norm(app) in norm(w["title"]) or norm(Path(w["exe"]).stem) in norm(app)]
    if not hits:
        n, _ = best(name, keys, cutoff=0.55)
        hits = [keys[n]] if n else []
    return hits


@tool(learn=True)
def switch_to(name: str):
    """Bring an open window to the front, by app name or part of its title.

    name: app name or window title"""
    hits = windows_named(name)
    if not hits:
        return f"Nothing called {name} is open."
    focus(hits[0]["hwnd"])
    return f"Here's {hits[0]['title'][:40]}."


@tool()
def close_app(name: str):
    """Close an app's windows (it can still ask to save first).

    name: app name, e.g. Discord"""
    hits = windows_named(name)
    if not hits:
        return f"{name} isn't open."
    close_windows([w["hwnd"] for w in hits])
    return f"Closed {name}."


# ---------- sound, media, system ----------
def endpoint():
    com()
    from pycaw.pycaw import AudioUtilities
    return AudioUtilities.GetSpeakers().EndpointVolume


def volume_now():
    return round(endpoint().GetMasterVolumeLevelScalar() * 100)


def _set_volume(level):
    ev = endpoint()
    ev.SetMasterVolumeLevelScalar(max(0, min(100, level)) / 100, None)
    ev.SetMute(0, None)


@tool(learn=True)
def set_volume(level: int):
    """Set the speaker volume to an exact level.

    level: 0 to 100"""
    old = volume_now()
    _set_volume(int(level))
    undoable(f"volume back to {old}", lambda: _set_volume(old))
    return f"Volume {max(0, min(100, int(level)))}."


@tool(learn=True)
def change_volume(by: int):
    """Turn the volume up or down by an amount (negative = down).

    by: change in percent, like 10 or -20"""
    old = volume_now()
    _set_volume(old + int(by))
    undoable(f"volume back to {old}", lambda: _set_volume(old))
    return f"Volume {max(0, min(100, old + int(by)))}."


@tool(learn=True)
def mute(on: bool = True):
    """Mute or unmute the speakers.

    on: true to mute, false to unmute"""
    endpoint().SetMute(1 if on else 0, None)
    undoable("unmute" if on else "mute", lambda: endpoint().SetMute(0 if on else 1, None))
    return "Muted." if on else "Sound's back."


@tool(learn=True)
def media(action: str):
    """Control whatever music or video is playing: play_pause, next, previous or stop.

    action: play_pause, next, previous or stop"""
    vk = {"play_pause": 0xB3, "play": 0xB3, "pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2}
    press(vk.get(action, 0xB3))
    return {"next": "Skipped.", "previous": "Going back.", "stop": "Stopped."}.get(action, "Done.")


@tool(learn=True)
def lock_pc():
    """Lock the PC."""
    user32.LockWorkStation()
    return "Locked."


@tool(learn=True)
def show_desktop():
    """Minimize everything and show the desktop (again to bring it all back)."""
    press(0x5B, 0x44)  # Win+D
    undoable("bring the windows back", lambda: press(0x5B, 0x44))
    return "Desktop."


def known_folder(name):
    keys = {"desktop": "Desktop", "documents": "Personal", "docs": "Personal", "pictures": "My Pictures",
            "photos": "My Pictures", "music": "My Music", "videos": "My Video",
            "downloads": "{374DE290-123F-4565-9164-39C4925E467B}"}
    k = keys.get(norm(name).replace("my ", "").strip())
    if not k:
        return None
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") as r:
        return winreg.QueryValueEx(r, k)[0]


@tool()
def screenshot():
    """Take a screenshot of the whole screen and save it to Pictures/Screenshots."""
    from PIL import ImageGrab
    ui.hide(True)
    time.sleep(0.3)
    try:
        img = ImageGrab.grab(all_screens=True)
    finally:
        ui.hide(False)
    folder = Path(known_folder("pictures")) / "Screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"earshot {datetime.now():%Y-%m-%d %H.%M.%S}.png"
    img.save(path)
    return f"Saved to Screenshots."


class POWER(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte), ("BatteryLifePercent", ctypes.c_byte),
                ("SystemStatusFlag", ctypes.c_byte), ("BatteryLifeTime", ctypes.c_ulong),
                ("BatteryFullLifeTime", ctypes.c_ulong)]


@tool(learn=True)
def battery():
    """Tell the battery level and whether it's charging."""
    p = POWER()
    kernel32.GetSystemPowerStatus(ctypes.byref(p))
    pct = p.BatteryLifePercent & 0xFF
    if pct == 255:
        return "No battery here, you're on wall power."
    mins = p.BatteryLifeTime if p.BatteryLifeTime != 0xFFFFFFFF else None
    left = f", about {mins // 3600}h {mins % 3600 // 60}m left" if mins and not p.ACLineStatus else ""
    return f"{pct}%, {'charging' if p.ACLineStatus == 1 else 'on battery'}{left}."


@tool(learn=True)
def clock():
    """Tell the current time and date."""
    return datetime.now().strftime("It's %#I:%M %p, %A %#d %B.")


# ---------- timers ----------
TIMERS = {}


def fmt_minutes(m):
    s = round(m * 60)
    if s < 60:
        return f"{s} second"
    if s < 3600:
        return f"{s // 60} minute" + (f" {s % 60}s" if s % 60 else "")
    return f"{s // 3600} hour" + (f" {s % 3600 // 60} minute" if s % 3600 // 60 else "")


@tool()
def timer(minutes: float, label: str = ""):
    """Start a countdown that shows on the island and rings when done. Also for 'remind me in N minutes to X'.

    minutes: length in minutes (0.5 = 30 seconds)
    label: what it's for, optional"""
    minutes = float(minutes)
    if minutes <= 0:
        return "That timer is already over."
    tid, ends = f"t{time.time():.3f}", time.time() + minutes * 60
    t = threading.Timer(minutes * 60, lambda: (TIMERS.pop(tid, None), ui.ring(tid, label)))
    t.daemon = True
    TIMERS[tid] = {"label": label, "ends": ends, "t": t}
    t.start()
    ui.timer(tid, label, ends)
    undoable("cancel the timer", lambda: cancel_timer(tid))
    return f"{fmt_minutes(minutes)} timer" + (f" for {label}" if label else "") + ", go."


def cancel_timer(tid):
    t = TIMERS.pop(tid, None)
    if t:
        t["t"].cancel()
        ui.timer(tid, t["label"], 0)


@tool()
def cancel_timers():
    """Cancel all running timers."""
    n = len(TIMERS)
    for tid in list(TIMERS):
        cancel_timer(tid)
    return f"Cancelled {n} timer{'s' * (n != 1)}." if n else "No timers running."


# ---------- folders, web, typing ----------
def expand(s):
    return os.path.expandvars(os.path.expanduser(str(s)))


@tool(learn=True)
def open_folder(path: str):
    """Open a folder in File Explorer: downloads, documents, desktop, pictures, music, videos, or any full path.

    path: a folder name like downloads, or a full path"""
    p = known_folder(path) or expand(path)
    if not Path(p).exists():
        return f"I can't find the folder {path}."
    os.startfile(p)
    return f"Opened {Path(p).name or p}."


@tool(learn=True)
def open_url(url: str):
    """Open a website in the browser.

    url: a full URL or a domain like youtube.com"""
    url = url.strip()
    if not re.match(r"^[a-z]+://", url):
        url = "https://" + url.replace(" ", "")
    webbrowser.open(url)
    return f"Opening {urllib.parse.urlparse(url).netloc}."


SITES = {"youtube": "https://www.youtube.com/results?search_query={}", "github": "https://github.com/search?q={}",
         "amazon": "https://www.amazon.in/s?k={}", "wikipedia": "https://en.wikipedia.org/w/index.php?search={}",
         "spotify": "https://open.spotify.com/search/{}", "maps": "https://www.google.com/maps/search/{}",
         "reddit": "https://www.reddit.com/search/?q={}", "google": "https://www.google.com/search?q={}",
         "images": "https://www.google.com/search?tbm=isch&q={}"}


@tool()
def web_search(query: str, site: str = ""):
    """Search in the browser: Google by default, or youtube, github, amazon, wikipedia, spotify, maps, reddit, images.

    query: what to search for
    site: optional site name"""
    webbrowser.open(SITES.get(norm(site), SITES["google"]).format(urllib.parse.quote_plus(query)))
    return f"Searching {norm(site) or 'Google'} for {query}."


@tool()
def type_text(text: str):
    """Type text into whatever app has focus.

    text: the exact text to type"""
    old = clip_get()
    clip_set(text)
    press(0x11, 0x56)  # Ctrl+V
    threading.Timer(0.4, lambda: clip_set(old)).start()
    undoable("remove the typed text", lambda: press(0x11, 0x5A))
    return "Typed it."


@tool()
def set_listening(on: bool):
    """Turn listening for the wake word on or off (off = hushed until you turn it back on from the island).

    on: true to listen, false to stop"""
    ui.hush(not on)
    return "Listening again." if on else "Hushed. Click me or press Ctrl+Alt+Space when you need me."


@tool()
def start_talk():
    """Start talk mode: a free-form, back-and-forth voice conversation (interruptible) until the user says bye."""
    return ui.talk()


@tool()
def rename_assistant(name: str):
    """Change the assistant's name, which is also its wake word ('hey <name>').

    name: the new name, one or two words"""
    name = " ".join(name.split()[:2]).strip(" .!?").title()
    if not re.fullmatch(r"[A-Za-z][A-Za-z ]{1,20}", name):
        return "Names need to be letters, like Nova or Pixel."
    old = S["name"]
    S.set(name=name)
    undoable(f"back to {old}", lambda: S.set(name=old))
    return f"Call me {name} now. Say 'hey {name}'."


# ---------- workspaces ----------
OPENED = {}  # workspace name -> {"hwnds", "desktop"} for "close <name> space"
BROWSERS = {"chrome", "msedge", "firefox", "brave", "opera", "vivaldi", "arc"}
STEMS = {"wt": "windowsterminal", "code": "code"}


def workspaces():
    out = {}
    for f in sorted(WORKSPACES.glob("*.toml")):
        try:
            d = tomllib.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            ui.notify("workspace file has a typo", f"{f.name}: {e}")
            continue
        d.setdefault("name", f.stem)
        d["file"] = f
        out[d["name"]] = d
    return out


def find_workspace(name):
    name = re.sub(r"\b(work ?space|space|mode|setup)\b", "", name, flags=re.I).strip() or name
    phrases = {}
    for d in workspaces().values():
        for p in (d["name"], *d.get("phrases", [])):
            phrases[p] = d
    n, _ = best(name, phrases, cutoff=0.7)
    return phrases.get(n)


def browser(pref=None):
    """(exe path, stem) of the chosen or default browser."""
    prog = pref or ""
    if not prog:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice") as k:
                prog = winreg.QueryValueEx(k, "ProgId")[0].lower()
        except OSError:
            prog = "msedge"
    stem = next((b for b in ("chrome", "msedge", "firefox", "brave", "opera", "vivaldi") if b in prog.lower()),
                "msedge" if "edge" in prog.lower() else "chrome")
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(hive, rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{stem}.exe") as k:
                return winreg.QueryValueEx(k, "")[0], stem
        except OSError:
            pass
    return shutil.which(stem), stem


def resolve(cmd):
    """A real executable for a bare command. Plain which() can pick an extensionless script (Git puts a 'notepad'
    shell script on PATH) that Windows can't start."""
    if Path(cmd).suffix:
        return shutil.which(cmd) or cmd
    return next((w for ext in (".exe", ".cmd", ".bat", ".com") if (w := shutil.which(cmd + ext))), cmd)


def launch(item):
    """Start one workspace item. Returns the exe stems its window may come from (empty = unknown)."""
    if "url" in item:
        urls = item["url"] if isinstance(item["url"], list) else [item["url"]]
        exe, st = browser(item.get("browser"))
        if exe:
            subprocess.Popen([exe, "--new-window", *urls])
            return {st}
        for u in urls:
            webbrowser.open(u)
        return set(BROWSERS)
    if "folder" in item:
        subprocess.Popen(["explorer", expand(item["folder"])])
        return {"explorer"}
    if "app" in item:
        n = find_app(item["app"])
        if n:
            os.startfile(f"shell:AppsFolder\\{APPS[n]}")
        return set()
    cmd = expand(item["run"])
    path = resolve(cmd)
    args = [expand(a) for a in item.get("args", [])]
    cwd = expand(item["cwd"]) if "cwd" in item else None
    if path.lower().endswith((".cmd", ".bat")):
        subprocess.Popen(["cmd", "/c", path, *args], cwd=cwd, creationflags=NO_WINDOW)
    else:
        subprocess.Popen([path, *args], cwd=cwd)
    s = Path(path).stem.lower()
    return {STEMS.get(s, s)}


def arrange(want, before, wait, rec):
    """Place each new window as it shows up. Windows nobody claimed go to items that didn't say which app.
    rec["hwnds"] fills in as it goes, so "close <name> space" works even mid-launch."""
    got, end = rec["hwnds"], time.time() + wait
    while want and time.time() < end and not rec.get("closed"):
        time.sleep(0.3)
        fresh = [w for w in windows() if w["hwnd"] not in before and w["hwnd"] not in got]
        for it, stems in list(want):
            hint = it.get("match", "").lower()
            m = next((w for w in fresh if (hint in (w["title"] + w["exe"]).lower() if hint else stem(w) in stems)), None)
            if m is None and not stems and not hint and time.time() > end - wait / 2:
                m = fresh[0] if fresh else None  # unknown app: take what's left after a while
            if m:
                time.sleep(0.15)  # let it finish its own first layout
                place(m["hwnd"], it.get("place"))
                got.add(m["hwnd"])
                fresh.remove(m)
                want.remove((it, stems))
    if not rec.get("closed"):
        got |= hwnds() - before


@tool(learn=True)
def open_workspace(name: str):
    """Open a saved workspace: launches its apps, folders and sites, lays the windows out, optionally on its own
    virtual desktop. 'last' is the auto-saved setup from your last session.

    name: workspace name, e.g. dev, study, chill, last"""
    d = find_workspace(name)
    if not d:
        return f"No workspace called {name}. Open your windows and say 'remember this as {name}'."
    com()
    desk = None
    if d.get("desktop") == "new":
        try:
            import pyvda
            desk = pyvda.VirtualDesktop.create()
            try:
                desk.rename(d["name"])
            except Exception:
                pass
            desk.go()
            time.sleep(0.3)
        except Exception as e:
            desk = None
            print("virtual desktop:", e)
    for c in d.get("close", []):
        close_app(c)
    before, want = hwnds(), []
    for it in d.get("apps", []):
        try:
            want.append((it, launch(it)))
        except Exception as e:
            ui.notify("couldn't start", f"{it}: {e}")
    rec = OPENED[d["name"].lower()] = {"hwnds": set(), "desktop": desk}

    def finish():
        com()
        arrange(want, before, float(d.get("wait", 12)), rec)
    threading.Thread(target=finish, daemon=True).start()
    undoable(f"close {d['name']}", lambda: close_workspace(d["name"]))
    return d.get("say") or f"{d['name'].title()} space is coming up."


@tool(learn=True)
def close_workspace(name: str):
    """Close a workspace that was opened this session: its windows, and its virtual desktop if it got one.

    name: workspace name"""
    d = find_workspace(name)
    key = (d["name"] if d else name).lower()
    rec = OPENED.pop(key, None)
    if not rec:
        return f"I didn't open {name} this session, so I won't guess which windows are its."
    rec["closed"] = True
    close_windows(set(rec["hwnds"]))
    if rec["desktop"]:
        com()
        time.sleep(0.8)
        try:
            rec["desktop"].remove()
        except Exception as e:
            print("virtual desktop:", e)
    return f"Closed {key}."


def vscode_folders():
    f = Path(os.environ.get("APPDATA", "")) / "Code" / "User" / "globalStorage" / "storage.json"
    try:
        ws = json.loads(f.read_text(encoding="utf-8")).get("windowsState", {})
    except Exception:
        return []
    uris = [w.get("folder", "") for w in [ws.get("lastActiveWindow", {}), *ws.get("openedWindows", [])]]
    return [urllib.parse.unquote(u[len("file:///"):]).replace("/", "\\") for u in uris if u.startswith("file:///")]


def explorer_folders():
    try:
        import win32com.client
        return {int(w.HWND): w.Document.Folder.Self.Path for w in win32com.client.Dispatch("Shell.Application").Windows()}
    except Exception:
        return {}


def toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)  # a JSON string is a valid TOML basic string


def snapshot():
    """The windows open right now, as workspace items."""
    com()
    items, code, folders = [], vscode_folders(), explorer_folders()
    l, t, r, b = work_area()
    for w in windows():
        s, h = stem(w), w["hwnd"]
        if s == "explorer" and w["cls"] != "CabinetWClass":
            continue
        if h in folders:
            it = {"folder": folders[h]}
        elif s == "code":
            folder = next((p for p in code if Path(p).name.lower() in w["title"].lower()), None)
            it = {"run": "code", "args": [folder]} if folder else {"run": "code"}
        elif s == "windowsterminal":
            it = {"run": "wt"}
        elif s == "applicationframehost":
            it = {"app": w["title"]}
        elif s in BROWSERS:
            it = {"run": w["exe"], "args": ["--new-window"], "match": s}
        else:
            it = {"run": w["exe"]}
        if user32.IsZoomed(h):
            it["place"] = "full"
        elif not user32.IsIconic(h):
            x0, y0, x1, y1 = frame(h)
            it["place"] = [round(max(0, (x0 - l) / (r - l)), 3), round(max(0, (y0 - t) / (b - t)), 3),
                           round(min(1, (x1 - x0) / (r - l)), 3), round(min(1, (y1 - y0) / (b - t)), 3)]
        items.append(it)
    return items


@tool()
def save_workspace(name: str):
    """Remember the windows open right now (apps, folders, VS Code projects, positions) as a workspace to reopen
    by name later.

    name: what to call it"""
    items = snapshot()
    if not items:
        return "There's nothing open to remember."
    name = norm(name) or "untitled"
    lines = [f"# saved by earshot {datetime.now():%Y-%m-%d %H:%M}. Edit freely: see the examples for every option.",
             f"name = {toml_value(name)}", 'desktop = "current"  # "new" opens it on its own virtual desktop',
             f"say = {toml_value(f'{name} is back.')}", ""]
    for it in items:
        lines += ["[[apps]]", *(f"{k} = {toml_value(v)}" for k, v in it.items()), ""]
    WORKSPACES.mkdir(parents=True, exist_ok=True)
    (WORKSPACES / f"{name.replace(' ', '-')}.toml").write_text("\n".join(lines), encoding="utf-8")
    return f"Saved {len(items)} windows as {name}. Say 'open {name} space' any time."


@tool(learn=True)
def list_workspaces():
    """List the saved workspaces."""
    names = [d["name"] for d in workspaces().values() if d["name"] != "last"]
    return ("Your spaces: " + ", ".join(names) + ".") if names else "No workspaces yet."


# ---------- skills ----------
def skills():
    out = {}
    for f in sorted(SKILLS.glob("*.toml")):
        try:
            d = tomllib.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            ui.notify("skill file has a typo", f"{f.name}: {e}")
            continue
        d.setdefault("name", f.stem)
        out[d["name"]] = d
    return out


def find_skill(text, cutoff=0.86):
    phrases = {p: d for d in skills().values() for p in (d["name"], *d.get("phrases", []))}
    n, _ = best(text, phrases, cutoff=cutoff)
    return phrases.get(n)


@tool(learn=True)
def run_skill(name: str):
    """Run one of your skills (a saved routine of steps) by name.

    name: the skill's name"""
    d = find_skill(name, cutoff=0.7)
    if not d:
        return f"No skill called {name}. Drop a .toml file in the skills folder to teach me one."
    said = []
    for step in d.get("steps", []):
        if "run" in step:  # your own command, from your own file
            subprocess.Popen(step["run"], shell=True, creationflags=NO_WINDOW)
        elif "wait" in step:
            time.sleep(float(step["wait"]))
        elif step.get("tool") in TOOLS:
            out = TOOLS[step["tool"]](**{k: v for k, v in step.items() if k not in ("tool", "say_result")})
            if step.get("say_result"):
                said.append(str(out))
    return " ".join([d.get("reply") or f"Ran {d['name']}.", *said])
