"""The brain: picks the cheapest tier that can do what you said.

    reflex     rules + phrases it has learned. No model, a few ms, works offline.
    on-device  Gemma 4 E2B through Ollama: odd phrasing -> a tool call. Offline and private.
    cloud      Gemini (free tier): questions, writing, the web, your screen, your MCP servers.

Every tier ends in the same tool calls (hands.TOOLS), so escalating needs no special cases. When a model turns a
phrase into one simple action and you don't undo it, the phrase is learned: next time it's a reflex.
"""
import asyncio
import io
import json
import re
import sqlite3
import threading
import time
import urllib.request
import wave
from collections import deque
from datetime import datetime
from pathlib import Path

import numpy as np

from . import hands
from .config import HOME, S, gemini_key
from .hands import TOOLS, UNDO, best, norm, schema, tool, ui

OLLAMA = "http://127.0.0.1:11434"


# ---------- reading what you said ----------
NUMS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                    "fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}
NUMS.update({"thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
             "hundred": 100, "a": 1, "an": 1, "half": 0.5, "a half": 0.5, "half an": 0.5, "max": 100, "full": 100})


def num(s):
    s = s.strip()
    try:
        return float(s)
    except ValueError:
        pass
    parts = s.replace("-", " ").split()
    if " ".join(parts) in NUMS:
        return NUMS[" ".join(parts)]
    if len(parts) == 2 and parts[0] in NUMS and parts[1] in NUMS:  # "twenty five"
        return NUMS[parts[0]] + NUMS[parts[1]]
    return None


HINGLISH = [  # Hindi puts the verb last: "dev space khol do" -> "open dev space"
    (r"^(.+?) (khol|kholo|khol do|khol de|chalu karo|chalu kar do|start karo|start kar do|laga do)$", r"open \1"),
    (r"^(.+?) (band karo|band kar do|band kar de|hata do)$", r"close \1"),
    (r"^(awaaz|awaz|volume|sound) (badhao|badha do|tez karo|tez kar do|upar karo)$", "volume up"),
    (r"^(awaaz|awaz|volume|sound) (kam karo|kam kar do|dheere karo|dheemi karo|neeche karo)$", "volume down"),
    (r"^(gaana|gana|music|song) (chalao|bajao|play karo|laga do)$", "play music"),
    (r"^(gaana|gana|music|song) (roko|rok do|band karo|pause karo)$", "pause music"),
    (r"^(agla|next) (gaana|gana|song)( chalao| lagao)?$", "next song"),
    (r"^(kitne baje hain|kitna baja hai|time kya hua|time kya hai|abhi kya time hai)$", "what time is it"),
    (r"^(.+?) yaad (rakhna|rakho|rakh lo)$", r"remember \1"),
]


def clean(text, name):
    """Drop punctuation, 'hey <name>', fillers and 'please'. Keeps your capitals, so typed or remembered text
    stays as you said it; matching is case-blind."""
    # Parakeet sometimes glues a verb to its object ("OpenSpotify"); split only there, not YouTube or McKenzie
    t = re.sub(r"\b(Open|Close|Play|Start|Launch|Switch|Search|Set|Turn|Mute|Lock|Remind|Type)(?=[A-Z])", r"\1 ", text)
    t = re.sub(r"[^\w\s%'.:/-]", " ", t).replace("'", " ").replace("%", " ")
    t = re.sub(r"\.(?=\s|$)", " ", t)  # drop full stops, keep 2.5 and youtube.com
    t = re.sub(r"\s+percent\b", "", " ".join(t.split()), flags=re.I)
    lead = (rf"^((hey|hi|hello|ok|okay|yo|a|uh|um|so|please|pls|kindly|and|can you|could you|would you|will you|"
            rf"i want you to|i need you to|go ahead and|{re.escape(name)})\s+)+")
    t = re.sub(lead, "", t + " ", flags=re.I).strip()
    t = re.sub(r"\s+(please|for me|right now|now|thanks|thank you)$", "", t, flags=re.I)
    for pat, rep in HINGLISH:
        t = re.sub(pat, rep, t, flags=re.I)
    return t


ACTION = re.compile(r"^(open|close|launch|start|run|play|pause|stop|set|turn|mute|unmute|lock|search|find|switch|show|"
                    r"take|type|save|remember|delete|send|move|go|put|make|change|create|install|download)\b", re.I)
QUESTION = re.compile(r"^(what|who|whom|whose|why|how|when|where|which|is|are|was|can you tell|could you tell|should|"
                      r"would|explain|tell me|write|draft|summari[sz]e|translate|define|compare|give me|help me|suggest|"
                      r"plan|calculate|convert|look at|read|check|rewrite|make this|fix this|describe)\b")


def reflex(t):
    """Rules for the everyday stuff: (tool, args), or None to ask a model."""
    m = lambda p: re.fullmatch(p, t, re.I)  # noqa: E731
    if m(r"(never ?mind|cancel|nothing|forget it|ignore that|stop)"):
        return "cancel", {}
    if m(r"(undo|undo (that|it|this)|take (that|it) back|revert( that)?|go back)"):
        return "undo", {}
    if m(r"(stop listening|go to sleep|hush|be quiet|shut up|mute yourself|sleep)"):
        return "set_listening", {"on": False}
    if m(r"(let s|lets|can we|could we|i (want|wanna) to|i wanna) (talk|chat)( for a (bit|while|minute))?|talk to me|"
         r"chat with me|(talk|live|chat|conversation) mode|let s have a (chat|conversation)|start (a )?conversation"):
        return "start_talk", {}
    if m(r"(what s |what is )?(the )?(time|date|day)( is it| now| today| right now)?|what (time|day|date) is it( today)?|"
         r"tell me the (time|date)"):
        return "clock", {}
    if m(r"(how much )?(battery|charge)( is)?( left| level| status| percent| percentage)?( do i have)?|is it charging|"
         r"how s (my|the) battery"):
        return "battery", {}
    if g := m(r"(set |put |make |change |turn |crank |pump |bring )?(the )?(volume|sound|audio|it|music)( level)?"
              r"( up| down)? (to |at )(like |about |around )?(\w+( \w+)?)"):
        n = num(g[8].lower())
        if n is not None:
            return "set_volume", {"level": int(n)}
    if g := m(r"(turn |crank |bring |pump )?(the )?(volume|sound|audio|it|music) (way |all the way )?(up|down)"
              r"( by (\w+( \w+)?))?( a bit| a little| more| a lot)?"):
        by = num(g[7].lower()) if g[7] else 30 if g[4] or (g[9] or "").strip() == "a lot" else 10
        return "change_volume", {"by": int(by) * (1 if g[5].lower() == "up" else -1)}
    if m(r"(louder|turn it up|a bit louder|volume up)"):
        return "change_volume", {"by": 10}
    if m(r"(quieter|softer|turn it down|a bit quieter|volume down)"):
        return "change_volume", {"by": -10}
    if g := m(r"(mute|unmute)s?( the)?( volume| sound| audio| speakers| music)?"):
        return "mute", {"on": g[1].lower() == "mute"}
    if m(r"(play|pause|resume|continue)( the)?( music| song| track| video| it| playback)?|pause music|play music"):
        return "media", {"action": "play_pause"}
    if m(r"(next|skip)( the)?( song| track| one| video)?|skip (this|it)|next song"):
        return "media", {"action": "next"}
    if m(r"(previous|last|prev)( song| track| one| video)|play (that|the last song) again"):
        return "media", {"action": "previous"}
    if m(r"lock( the| my)?( pc| screen| computer| laptop| it| windows)?"):
        return "lock_pc", {}
    if m(r"show( me)?( the| my)? desktop|minimi[sz]e (everything|all)( windows)?|hide (everything|all windows)"):
        return "show_desktop", {}
    if m(r"(take |grab |capture )?(a )?screen ?shot|screen ?shot (this|it)"):
        return "screenshot", {}
    if g := m(r"(set |start |put on )?(a |an )?(\w+( and a half)?|half an|a half) (second|sec|minute|min|hour)s? timer( for (.+))?"):
        return timer_call(g[3], g[5], g[7])
    if g := m(r"(set |start )?(a )?timer (for |of )?(\w+( \w+)?) (second|sec|minute|min|hour)s?( (for|called|call|named|to) (.+))?"):
        return timer_call(g[4], g[6], g[9])
    if g := m(r"remind me in (\w+( \w+)?) (second|sec|minute|min|hour)s? (to |about )?(.+)"):
        return timer_call(g[1], g[3], g[5])
    if m(r"(cancel|stop|kill|clear) (the |all |my )?(timers?|alarms?|reminders?)"):
        return "cancel_timers", {}
    if g := m(r"(open|start|launch|load|bring up|set up|fire up)( up)?( my| the)? (.+?) ?(work ?space|space|mode|setup)"):
        return "open_workspace", {"name": g[4]}
    if g := m(r"(close|end|exit|quit|stop|leave|kill)( my| the)? (.+?) ?(work ?space|space|mode|setup)"):
        return "close_workspace", {"name": g[3]}
    if g := m(r"(remember|save) (this|these|these windows|this setup|my setup|everything|this layout)( windows| setup)? as"
              r"( my| the)? (.+?)( work ?space| space| mode| setup)?"):
        return "save_workspace", {"name": g[5]}
    if m(r"where was i|pick up where i left off|restore (my )?(last )?(session|setup)|bring back (my )?last session"):
        return "open_workspace", {"name": "last"}
    if m(r"(list|show)( me)?( my| all)? (work ?spaces|spaces)|what (work ?spaces|spaces) do i have"):
        return "list_workspaces", {}
    if g := m(r"(change|set) your name to (.+)|call yourself (.+)|your name is (.+?)( now)?|i ll call you (.+)"):
        return "rename_assistant", {"name": g[2] or g[3] or g[4] or g[6]}
    if g := m(r"(search|google|look up|find)( for)? (.+?) on (youtube|github|amazon|wikipedia|spotify|maps|reddit|google|images)"):
        return "web_search", {"query": g[3], "site": g[4].lower()}
    if g := m(r"(play|watch) (.+) on youtube|youtube (.+)"):
        return "web_search", {"query": g[2] or g[3], "site": "youtube"}
    if g := m(r"(google|search for|search the web for|search google for) (.+)"):
        return "web_search", {"query": g[2], "site": ""}
    if g := m(r"type (out )?(.+)"):
        return "type_text", {"text": g[2]}
    if g := m(r"(switch|jump|take me|go) (to|back to) (.+)"):
        return "switch_to", {"name": g[3]}
    if m(r"what do you (know|remember)( about me)?|what have you remembered"):
        return "recall", {}
    if g := m(r"remember (that )?(.+)"):
        return "remember", {"fact": g[2]}
    if g := m(r"forget (that |about )?(.+)"):
        return "forget", {"what": g[2]}
    if skill := hands.find_skill(t):
        return "run_skill", {"name": skill["name"]}
    if g := m(r"(open|launch|start|run|pull up|bring up|fire up)( up)?( the| my)? (.+?)( app| application| folder| website| site)?"):
        return open_call(g[4], g[5])
    if g := m(r"(close|quit|kill|exit) (.+?)( app| application)?"):
        if g[2].lower() not in ("this", "that", "it", "this window", "that window"):
            return "close_app", {"name": g[2]}
    return None


def timer_call(amount, unit, label):
    amount, unit = (amount or "").lower(), unit.lower()
    n = num(amount.replace(" and a half", "")) if amount else None
    if n is None:
        return None
    n += 0.5 if "and a half" in amount else 0
    minutes = n / 60 if unit.startswith("sec") else n * 60 if unit == "hour" else n
    return "timer", {"minutes": minutes, "label": (label or "").strip()}


def open_call(what, kind):
    if hands.find_workspace(what):
        return "open_workspace", {"name": what}
    if hands.known_folder(what) or (kind or "").strip() == "folder":
        return "open_folder", {"path": what}
    if re.fullmatch(r"[\w-]+(\.[\w-]+)+(/\S*)?", what.replace(" dot ", ".")):
        return "open_url", {"url": what.replace(" dot ", ".")}
    if hands.find_app(what):
        return "open_app", {"name": what}
    return None


# ---------- memory: facts you told it, phrases it learned ----------
class Memory:
    def __init__(self):
        self.db = sqlite3.connect(HOME / "memory.db", check_same_thread=False)
        self.lock = threading.Lock()
        with self.db:
            self.db.execute("create table if not exists facts (id integer primary key, text text, at real)")
            self.db.execute("create table if not exists learned (phrase text primary key, tool text, args text, "
                            "hits integer default 0, at real)")

    def q(self, sql, *a):
        with self.lock, self.db:
            return self.db.execute(sql, a).fetchall()

    def facts(self):
        return [r[0] for r in self.q("select text from facts order by id desc limit 60")]

    def learned(self, phrase):
        r = self.q("select tool, args from learned where phrase = ?", phrase)
        if r:
            self.q("update learned set hits = hits + 1 where phrase = ?", phrase)
            return r[0][0], json.loads(r[0][1])
        return None

    def learn(self, phrase, name, args):
        self.q("insert or replace into learned (phrase, tool, args, at) values (?, ?, ?, ?)", phrase, name,
               json.dumps(args), time.time())

    def unlearn(self, phrase=None):
        self.q("delete from learned where phrase = ?", phrase) if phrase else self.q("delete from learned")

    def count(self):
        return self.q("select count(*) from learned")[0][0]


MEM = None


@tool()
def remember(fact: str):
    """Remember a fact about the user for later (their folders, preferences, people, anything).

    fact: the fact, in the user's words"""
    MEM.q("insert into facts (text, at) values (?, ?)", fact.strip().rstrip("."), time.time())
    return "Got it, I'll remember that."


@tool()
def forget(what: str):
    """Forget remembered facts that mention something ('everything' forgets them all).

    what: a word or phrase from the fact, or everything"""
    if norm(what) in ("everything", "all", "it all"):
        n = len(MEM.facts())
        MEM.q("delete from facts")
        return f"Forgot all {n} things."
    rows = MEM.q("select id, text from facts")
    gone = [i for i, t in rows if norm(what) in norm(t)]
    for i in gone:
        MEM.q("delete from facts where id = ?", i)
    return f"Forgot {len(gone)} thing{'s' * (len(gone) != 1)}." if gone else f"I don't remember anything about {what}."


@tool()
def recall():
    """Say what you remember about the user."""
    f = MEM.facts()
    return ("I remember: " + "; ".join(f[:8]) + ("..." if len(f) > 8 else ".")) if f else "Nothing yet. Say 'remember that ...'."


# ---------- the models ----------
def ollama(path, body, timeout=60):
    req = urllib.request.Request(OLLAMA + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


LOCAL_TOOLS = ["open_app", "close_app", "switch_to", "open_folder", "open_url", "web_search", "open_workspace",
               "close_workspace", "set_volume", "change_volume", "mute", "media", "timer", "lock_pc", "show_desktop",
               "screenshot", "clock", "battery", "run_skill", "remember"]
ROUTER = """You turn a spoken command for a Windows PC into exactly one tool call. Never reply with text.
If no tool fits, or it is a question, or it needs knowledge, writing or the screen, call ask_cloud.
Examples:
"pump the volume to eighty" -> set_volume(level=80)
"a little quieter" -> change_volume(by=-10)
"remind me about the pizza in twenty minutes" -> timer(minutes=20, label="pizza")
"fire up whatsapp" -> open_app(name="WhatsApp")
"show me my documents" -> open_folder(path="documents")
"silence everything" -> mute(on=true)
"get rid of steam" -> close_app(name="Steam")
"who won the match yesterday" -> ask_cloud(why="knowledge")
"write an email to my professor" -> ask_cloud(why="writing")"""
ASK_CLOUD = {"name": "ask_cloud", "description": "Hand the request to the smarter cloud model. Use for questions, "
             "knowledge, current info, writing, maths, anything about the screen or selected text, or several steps.",
             "parameters": {"type": "object", "properties": {"why": {"type": "string", "description": "one word"}},
                            "required": []}}


class Brain:
    def __init__(self):
        global MEM, BRAIN
        MEM = self.mem = Memory()
        BRAIN = self
        self.recent = deque(maxlen=6)  # (when, you, me): so "and tomorrow?" makes sense
        self.cloud_off_until, self._client, self._key = 0, None, None
        self.mcp = Mcp(S["mcp"]) if S["mcp"] else None
        self.focus = None  # the window you were in when you opened the type box

    # ----- entry point -----
    def handle(self, text, source="voice"):
        """-> {"text", "tier", "undo", "quiet", "learned"}. Runs tools; call it from a worker thread."""
        hands.com()
        t = clean(text, S["name"])
        if not t:
            return {"text": "", "tier": "reflex", "quiet": True}
        hit = self.mem.learned(t.lower())
        if hit:
            return self.run([hit], "reflex")
        call = reflex(t)
        if call:
            return self.run([call], "reflex")
        cloud = self.cloud_ready()
        if not (cloud and QUESTION.match(t)):
            r = self.local(text)
            if r is not None:
                return r
        if cloud:
            r = self.cloud(text)
            if r is not None:
                return r
        if source == "voice" and not QUESTION.match(t):
            # spoken, not a question, and nothing understood it: probably misheard ("what's the time" -> "War's time").
            # Show what was heard and ask again, rather than letting a model improvise.
            return {"text": f"I heard “{text.strip(' .')}”. Say it again?", "tier": "on-device", "again": True}
        if ACTION.match(t):  # a request to do something that no tier could map to a tool: say so, never pretend
            return {"text": f"I couldn't turn “{t}” into an action. Try it plainer, like “open Chrome”"
                            + ("." if self.cloud_ready() else ", or add a Gemini key in settings for trickier asks."),
                    "tier": "on-device"}
        r = self.local(text, chat=True)  # offline: Gemma answers itself
        return r or {"text": "I can't reach Ollama or Gemini right now.", "tier": "offline"}

    def run(self, calls, tier):
        outs, undo_before = [], len(UNDO)
        for name, args in calls:
            if name == "cancel":
                return {"text": "Okay.", "tier": tier, "quiet": True}
            if name == "undo":
                return {"text": self.undo(), "tier": tier}
            outs.append(self.call(name, args))
        return {"text": " ".join(outs), "tier": tier, "undo": len(UNDO) > undo_before}

    def maybe_learn(self, text, done, undo_before):
        """One simple action from the cloud for a short phrase -> a reflex. Only Gemini's answers are learned:
        a small local model's slip would otherwise become permanent. Undoing the action unlearns the phrase."""
        phrase = clean(text, S["name"]).lower()
        if len(done) != 1 or not getattr(TOOLS.get(done[0][0]), "learn", False) or len(phrase.split()) > 8 \
                or done[0][2].startswith(("I couldn't", "No ", "Nothing", "I can't")):
            return None
        self.mem.learn(phrase, done[0][0], done[0][1])
        if len(UNDO) > undo_before:
            label, fn, at = UNDO[-1]
            UNDO[-1] = (label, lambda: (fn(), self.mem.unlearn(phrase)), at)
        return phrase

    def call(self, name, args):
        fn = TOOLS.get(name)
        if fn is None and self.mcp and name in self.mcp.tools:
            return self.mcp.call(name, args)
        if fn is None:
            return f"I don't have a tool called {name}."
        try:
            return str(fn(**(args or {})))
        except TypeError as e:
            return f"That didn't fit {name}: {e}"
        except Exception as e:
            return f"{name} failed: {e}"

    def undo(self):
        if not UNDO:
            return "Nothing to undo."
        label, fn, at = UNDO.pop()
        if time.time() - at > 180:
            return "That was too long ago to undo safely."
        fn()
        return f"Undone: {label}."

    # ----- context every model gets -----
    def system(self):
        try:
            fg = hands.user32.GetForegroundWindow()
            w = next((x for x in hands.windows() if x["hwnd"] == fg), None)
            where = f'{w["title"][:80]} ({Path(w["exe"]).stem})' if w else "the desktop"
        except Exception:
            where = "unknown"
        ws = ", ".join(d["name"] for d in hands.workspaces().values()) or "none"
        sk = ", ".join(hands.skills()) or "none"
        facts = "\n".join(f"- {f}" for f in self.mem.facts()) or "- (nothing yet)"
        s = (f"You are {S['name']}, a quick, warm assistant living in a Dynamic Island on the user's Windows 11 laptop. "
             "Use tools for anything doable on the PC; never claim you did something you didn't. Replies show on a "
             "small island and are often spoken: one or two short sentences, plain text, no markdown, no emoji.\n"
             f"Now: {datetime.now():%A %d %B %Y, %I:%M %p}. The user is in: {where}.\n"
             f"Workspaces: {ws}. Skills: {sk}.\nWhat you know about the user:\n{facts}")
        return s

    def history(self):
        now = time.time()
        return [(u, a) for at, u, a in self.recent if now - at < 180]

    # ----- on-device: Gemma -----
    def local(self, text, chat=False):
        if chat:  # offline: Gemma just talks, and must not claim actions it can't take here
            tools, msgs = [], [{"role": "system", "content": self.system() + "\nIn this reply you cannot open, close or "
                               "change anything on the PC. Never say you did or are doing something; just talk."}]
            for u, a in self.history():
                msgs += [{"role": "user", "content": u}, {"role": "assistant", "content": a}]
        else:  # a router, not a chatbot: a 2B model picks well from ~20 tools with a few examples
            tools = [{"type": "function", "function": schema(TOOLS[n])} for n in LOCAL_TOOLS if n in TOOLS]
            tools.append({"type": "function", "function": ASK_CLOUD})
            ws = ", ".join(d["name"] for d in hands.workspaces().values()) or "none"
            msgs = [{"role": "system", "content": ROUTER + f"\nWorkspaces: {ws}. Skills: {', '.join(hands.skills()) or 'none'}."}]
        msgs.append({"role": "user", "content": text})
        body = {"model": S["local_model"], "messages": msgs, "tools": tools, "stream": False, "think": False,
                "keep_alive": "30m", "options": {"temperature": 0}}
        try:
            msg = ollama("/api/chat", body).get("message", {})
            if not chat and not msg.get("tool_calls") and msg.get("content"):
                # it chatted ("which app?") instead of routing: one nudge, ~0.3 s
                msgs += [msg, {"role": "user", "content": "Do not ask. Respond with exactly one tool call now."}]
                msg = ollama("/api/chat", body).get("message", {})
        except Exception:
            return None
        calls = [(c["function"]["name"], c["function"].get("arguments") or {}) for c in msg.get("tool_calls") or []]
        if any(n == "ask_cloud" or n not in TOOLS for n, _ in calls):
            return None
        if calls:
            return self.run(calls, "on-device")
        answer = msg.get("content", "").strip()
        if not chat or not answer:
            return None  # the router talked instead of routing: let the cloud (or offline chat) answer
        self.recent.append((time.time(), text, answer))
        return {"text": answer, "tier": "on-device"}

    def warm(self, on=True):
        """Keep Gemma in VRAM (instant), or drop it (a game wants the GPU). False if Ollama or the model is missing."""
        try:
            ollama("/api/generate", {"model": S["local_model"], "keep_alive": "30m" if on else 0, "prompt": ""}, 120)
            return True
        except Exception:
            return False

    # ----- cloud: Gemini -----
    def cloud_ready(self):
        return bool(gemini_key()) and time.time() > self.cloud_off_until

    def client(self):
        from google import genai
        k = gemini_key()
        if self._client is None or k != self._key:
            self._client, self._key = genai.Client(api_key=k), k
        return self._client

    def gen(self, model, contents, **cfg):
        from google.genai import types
        try:
            return self.client().models.generate_content(model=model, contents=contents,
                                                         config=types.GenerateContentConfig(**cfg))
        except Exception as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                self.cloud_off_until = time.time() + 600
                ui.notify("Gemini's free quota is used up", "On-device only for 10 minutes.")
            raise

    def cloud(self, text):
        from google.genai import types
        decls = [types.FunctionDeclaration(name=s["name"], description=s["description"],
                                           parameters_json_schema=s["parameters"])
                 for s in (schema(f) for f in TOOLS.values())]
        if self.mcp:
            decls += [types.FunctionDeclaration(name=k, description=d, parameters_json_schema=p)
                      for k, (_, _, d, p) in self.mcp.tools.items()]
        contents = []
        for u, a in self.history():
            contents += [types.Content(role="user", parts=[types.Part(text=u)]),
                         types.Content(role="model", parts=[types.Part(text=a)])]
        contents.append(types.Content(role="user", parts=[types.Part(text=text)]))
        cfg = dict(system_instruction=self.system(), tools=[types.Tool(function_declarations=decls)],
                   automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True), temperature=0.3)
        done, undo_before, answer = [], len(UNDO), ""
        try:
            for _ in range(6):
                resp = self.gen(S["fast_model"], contents, **cfg)
                calls = resp.function_calls or []
                if not calls:
                    answer = (resp.text or "").strip()
                    break
                contents.append(resp.candidates[0].content)
                parts = []
                for c in calls:
                    out = self.call(c.name, dict(c.args or {}))
                    done.append((c.name, dict(c.args or {}), out))
                    parts.append(types.Part.from_function_response(name=c.name, response={"result": out}))
                contents.append(types.Content(role="user", parts=parts))
        except Exception as e:
            if done:
                return {"text": " ".join(o for _, _, o in done), "tier": "cloud", "undo": len(UNDO) > undo_before}
            print("cloud:", e)
            return None
        answer = answer or " ".join(o for _, _, o in done) or "Done."
        self.recent.append((time.time(), text, answer))
        return {"text": answer, "tier": "cloud", "undo": len(UNDO) > undo_before,
                "learned": self.maybe_learn(text, done, undo_before)}

    def text_model(self, instruction, text):
        """Plain text in, text out: Gemini if it's there, else Gemma."""
        if self.cloud_ready():
            try:
                return (self.gen(S["fast_model"], f"{instruction}\n\n---\n{text}").text or "").strip()
            except Exception:
                pass
        r = ollama("/api/chat", {"model": S["local_model"], "stream": False, "think": False, "keep_alive": "30m",
                                 "messages": [{"role": "user", "content": f"{instruction}\n\n---\n{text}"}]}, 120)
        return r["message"]["content"].strip()

    def transcribe(self, samples):
        """Hinglish mode: Gemini hears the clip itself (Parakeet has no Hindi)."""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
            w.writeframes((np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes())
        from google.genai import types
        r = self.gen(S["fast_model"], [types.Part.from_bytes(data=buf.getvalue(), mime_type="audio/wav"),
                                       "Transcribe this voice command exactly as spoken. Write Hindi words in Roman "
                                       "script (Hinglish). Output only the transcript."])
        return (r.text or "").strip()


BRAIN = None


# ---------- powers that need a model ----------
def grab_selection():
    """The text selected in the app you're in (via a quick Ctrl+C), or '' if nothing is selected."""
    if BRAIN and BRAIN.focus:
        hands.focus(BRAIN.focus)
        time.sleep(0.15)
    old = hands.clip_get()
    hands.clip_set("")
    hands.press(0x11, 0x43)  # Ctrl+C
    time.sleep(0.25)
    sel = hands.clip_get()
    if not sel:
        hands.clip_set(old)
    return sel, old


@tool(cloud=True)
def web_answer(question: str):
    """Answer a question with a live Google search: news, weather, scores, prices, facts that change.

    question: the question"""
    from google.genai import types
    r = BRAIN.gen(S["smart_model"], question, tools=[types.Tool(google_search=types.GoogleSearch())],
                  system_instruction="Answer for a voice assistant: one to three short sentences, plain text, no markdown.")
    return (r.text or "I couldn't find that.").strip()


@tool(cloud=True)
def about_this(question: str):
    """Answer a question about what the user is looking at ('explain this', 'what does this mean', 'summarize this',
    'what's on my screen'): their selected text if any, otherwise a screenshot of the screen.

    question: what they asked"""
    sel, _ = grab_selection()
    if sel:
        return BRAIN.text_model(f"{question}\nAnswer in at most three short sentences, plain text.", sel)
    from PIL import ImageGrab
    from google.genai import types
    ui.notify("Looking at your screen", "Sending a screenshot to Gemini")
    ui.hide(True)
    time.sleep(0.3)
    try:
        img = ImageGrab.grab()
    finally:
        ui.hide(False)
    img.thumbnail((1600, 1600))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    r = BRAIN.gen(S["smart_model"], [types.Part.from_bytes(data=buf.getvalue(), mime_type="image/png"), question],
                  system_instruction="You see the user's screen. Answer in at most three short sentences, plain text.")
    return (r.text or "I couldn't make it out.").strip()


@tool()
def rewrite_selection(instruction: str):
    """Rewrite the text selected in any app, in place: fix grammar, make it formal or shorter, translate, turn into
    bullets. With nothing selected, rewrites the clipboard instead.

    instruction: how to rewrite it"""
    sel, old = grab_selection()
    text = sel or old
    if not text.strip():
        return "Select some text (or copy it), then ask again."
    out = BRAIN.text_model(f"{instruction}. Return only the rewritten text, nothing else.", text)
    hands.clip_set(out)
    if sel:
        hands.press(0x11, 0x56)  # Ctrl+V over the selection
        hands.undoable("put the original back", lambda: hands.press(0x11, 0x5A))
        return "Rewritten."
    return "Done, it's on your clipboard."


# ---------- MCP: any server's tools become the assistant's ----------
class Mcp:
    """Connects the stdio MCP servers in settings["mcp"] (the usual mcpServers JSON shape) on a background loop."""

    def __init__(self, servers):
        self.tools, self.loop = {}, asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True, name="mcp").start()
        for name, cfg in servers.items():
            asyncio.run_coroutine_threadsafe(self.connect(name, cfg), self.loop)

    async def connect(self, name, cfg):
        """One long-lived task per server: the session's streams belong to this task, so it stays open."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        params = StdioServerParameters(command=cfg["command"], args=cfg.get("args", []), env=cfg.get("env"))
        try:
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                for t in tools:
                    key = re.sub(r"[^A-Za-z0-9_]", "_", f"{name}__{t.name}")[:64]
                    schema_ = {k: v for k, v in (getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {"type": "object"}).items() if k != "$schema"}
                    self.tools[key] = (session, t.name, (t.description or t.name)[:1000], schema_)
                ui.notify(f"{name} plugged in", f"{len(tools)} new tools, ask away")
                await asyncio.Event().wait()  # hold the session for the app's lifetime
        except Exception as e:
            ui.notify(f"MCP server {name} didn't start", str(e)[:120])

    def call(self, key, args):
        session, tname, _, _ = self.tools[key]
        res = asyncio.run_coroutine_threadsafe(session.call_tool(tname, args), self.loop).result(90)
        out = "\n".join(getattr(c, "text", "") for c in getattr(res, "content", []) or [])
        return out[:4000] or "Done."
