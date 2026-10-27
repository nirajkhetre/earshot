"""One runnable check: does what you say reach the right tool, and how fast?

    python tests/check.py          rules + learned phrases + Gemma (Ollama running), tools replaced by recorders
    python tests/check.py --ears   also: Kokoro speaks "hey <name>, ..." and the ears must wake and transcribe it

Uses a throwaway data folder, so your real settings and memory are untouched. Nothing on your PC is changed.
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

os.environ["EARSHOT_HOME"] = tempfile.mkdtemp(prefix="earshot-check-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from earshot import config, hands  # noqa: E402
from earshot import brain as B  # noqa: E402

config.first_run()
calls = []
for name, fn in list(hands.TOOLS.items()):
    if name in ("remember", "forget", "recall"):
        continue  # memory is real, in the throwaway folder

    def rec(*a, _n=name, _f=fn, **kw):
        calls.append((_n, kw))
        return f"ok {_n}"
    rec.learn, rec.cloud, rec.__doc__, rec.__signature__ = fn.learn, fn.cloud, fn.__doc__, __import__("inspect").signature(fn)
    rec.__name__ = name
    hands.TOOLS[name] = rec
hands.APPS.update({"Spotify": "x", "Visual Studio Code": "x", "Google Chrome": "x", "Notepad": "x", "WhatsApp": "x"})
config.gemini_key = B.gemini_key = lambda: ""  # rules + Gemma only

REFLEX = [
    ("hey nova open my dev space", "open_workspace", {"name": "dev"}),
    ("Okay Nova, open coding space.", "open_workspace", {"name": "coding"}),
    ("close dev space", "close_workspace", {"name": "dev"}),
    ("remember this as hackathon", "save_workspace", {"name": "hackathon"}),
    ("where was I", "open_workspace", {"name": "last"}),
    ("Set a timer for five minutes call tea.", "timer", {"minutes": 5.0, "label": "tea"}),
    ("set a 10 minute timer for pasta", "timer", {"minutes": 10.0, "label": "pasta"}),
    ("remind me in 20 minutes to call mom", "timer", {"minutes": 20.0, "label": "call mom"}),
    ("half an hour timer", "timer", {"minutes": 30.0, "label": ""}),
    ("volume to seventy", "set_volume", {"level": 70}),
    ("Set the volume to 30 percent.", "set_volume", {"level": 30}),
    ("turn the volume down", "change_volume", {"by": -10}),
    ("volume up by twenty", "change_volume", {"by": 20}),
    ("Hey Nova crank it up to like seventy", "set_volume", {"level": 70}),
    ("mute", "mute", {"on": True}),
    ("next song", "media", {"action": "next"}),
    ("pause the music", "media", {"action": "play_pause"}),
    ("lock the screen", "lock_pc", {}),
    ("what time is it", "clock", {}),
    ("how much battery do I have", "battery", {}),
    ("open spotify", "open_app", {"name": "spotify"}),
    ("open VS Code", "open_app", {"name": "vs code"}),
    ("open downloads", "open_folder", {"path": "downloads"}),
    ("open youtube.com", "open_url", {"url": "youtube.com"}),
    ("play lofi beats on youtube", "web_search", {"query": "lofi beats", "site": "youtube"}),
    ("search for mechanical keyboards on amazon", "web_search", {"query": "mechanical keyboards", "site": "amazon"}),
    ("switch to chrome", "switch_to", {"name": "chrome"}),
    ("close discord", "close_app", {"name": "discord"}),
    ("call yourself Jarvis", "rename_assistant", {"name": "jarvis"}),
    ("pomodoro", "run_skill", {"name": "study sprint"}),
    ("take a screenshot", "screenshot", {}),
    ("stop listening", "set_listening", {"on": False}),
    ("hey Nova, let's talk", "start_talk", {}),
    ("can we chat for a bit", "start_talk", {}),
    ("dev space khol do", "open_workspace", {"name": "dev"}),
    ("awaaz kam karo", "change_volume", {"by": -10}),
]
LOCAL = [  # odd phrasing: Gemma must pick the tool (none of these are the router prompt's examples)
    ("crank it up to like seventy", "set_volume"),
    ("i wanna hear some spotify", "open_app"),
    ("bring my downloads folder up", "open_folder"),
    ("make it so i can't hear anything", "mute"),
    ("ping me in a quarter of an hour about the laundry", "timer"),
    ("turn the music way down", "change_volume"),
    ("i'm done with discord, shut it", "close_app"),
    ("can you get notepad going", "open_app"),
    ("i need to see what's on reddit", ("open_url", "web_search")),
    ("what's the tallest mountain in africa", (None, "web_search")),  # a question: answer it, or search; never a PC action
]


def main():
    br = B.Brain()
    bad = 0
    for text, want, args in REFLEX:
        calls.clear()
        t = time.perf_counter()
        r = br.handle(text)
        ms = (time.perf_counter() - t) * 1000
        got = calls[0] if calls else (None, {})
        same = lambda a, b: a.lower() == b.lower() if isinstance(a, str) and isinstance(b, str) else a == b  # noqa: E731
        ok = got[0] == want and all(same(got[1].get(k), v) for k, v in args.items()) and r["tier"] == "reflex"
        bad += not ok
        print(f"{'ok ' if ok else 'BAD'} {ms:6.1f} ms  {r['tier']:9} {text!r:45} -> {got}")
    # a learned phrase: what a model solved once is a reflex next time
    br.mem.learn("lights out", "lock_pc", {})
    calls.clear()
    r = br.handle("hey nova lights out")
    ok = calls and calls[0][0] == "lock_pc" and r["tier"] == "reflex"
    bad += not ok
    print(f"{'ok ' if ok else 'BAD'} learned phrase -> {calls}")
    r = br.handle("remember that my college folder is D:/College")
    print("ok " if "remember" in r["text"].lower() else "BAD", "memory:", r["text"], "|", br.handle("what do you know about me")["text"])
    try:
        B.ollama("/api/tags", None) if False else B.urllib.request.urlopen(B.OLLAMA + "/api/tags", timeout=2)
    except Exception:
        print("-- Ollama isn't running: skipping the Gemma checks")
        return bad
    br.warm()
    for text, want in LOCAL:
        calls.clear()
        t = time.perf_counter()
        r = br.handle(text)
        ms = (time.perf_counter() - t) * 1000
        ok = (calls[0][0] if calls else None) in (want if isinstance(want, tuple) else (want,))
        bad += not ok
        print(f"{'ok ' if ok else 'BAD'} {ms:6.0f} ms  {r['tier']:9} {text!r:50} -> {calls[:1] or r['text'][:60]}")
    return bad


def ears_check():
    """Kokoro says it, the real ears pipeline must wake on the name and hear the rest."""
    import numpy as np
    from earshot import ears as E
    from earshot.voice import Voice
    v = Voice()
    v.load()
    got = []

    def utterance(x):  # what App.respond does: the tail of "hey Nova" alone means keep listening
        t = e.transcribe(x)
        got.append(t)
        if not B.clean(E.after_name(t, "Nova"), "Nova") and got.count("retry") == 0:
            got.append("retry")
            e.listen(wait=5.0)

    e = E.Ears.__new__(E.Ears)  # the real class, without opening the mic
    E.Ears.__init__.__globals__["threading"].Thread = lambda **k: type("T", (), {"start": lambda s: None})()
    E.Ears.__init__(e, on_wake=lambda: got.append("wake"), on_utterance=utterance,
                    on_level=lambda x: None, on_quiet=lambda: got.append("quiet"))
    bad = 0
    def say(text):
        a = v.tts.generate(text, sid=16)
        x = np.array(a.samples, np.float32)
        return np.interp(np.linspace(0, len(x) - 1, int(len(x) * 16000 / a.sample_rate)), np.arange(len(x)), x).astype(np.float32)

    # "|" = a one-second pause: "hey Nova" ... then the command
    for text in ["Hey Nova, open my dev space.", "Okay Nova, set a timer for ten minutes.", "Hey Nova. | Open my dev space.",
                 "Hey Nova, mute."]:
        got.clear()
        parts = [say(t) for t in text.split("|")]
        x = np.concatenate([np.zeros(8000, np.float32), *sum(([p, np.zeros(16000, np.float32)] for p in parts), []),
                            np.zeros(24000, np.float32)])
        e.mode = "sleep"
        for i in range(0, len(x), E.BLOCK):
            e.ring.append(x[i:i + E.BLOCK])
            e.hear(x[i:i + E.BLOCK])
        heard = next((E.after_name(g, "Nova") for g in got
                      if g not in ("wake", "quiet", "retry") and B.clean(E.after_name(g, "Nova"), "Nova")), "")
        ok = "wake" in got and B.reflex(B.clean(heard, "Nova")) is not None
        bad += not ok
        print(f"{'ok ' if ok else 'BAD'} ears: {text!r} -> {got} -> {B.reflex(B.clean(heard, 'Nova'))}")
    return bad


if __name__ == "__main__":
    bad = main() + (ears_check() if "--ears" in sys.argv else 0)
    shutil.rmtree(os.environ["EARSHOT_HOME"], ignore_errors=True)
    print("ALL GOOD" if not bad else f"{bad} FAILED")
    sys.exit(1 if bad else 0)
