"""How often does a spoken command reach the right action? Kokoro speaks each phrase in 4 voices, twice (its renders
vary), and the real ears (wake word -> VAD -> Parakeet -> after_name) and the rule tier take it from there.

    python tests/voice_matrix.py
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ["EARSHOT_HOME"] = tempfile.mkdtemp(prefix="earshot-voice-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from earshot import ears as E, hands  # noqa: E402
from earshot.brain import clean, reflex  # noqa: E402
from earshot.voice import Voice  # noqa: E402

hands.APPS.update({"Spotify": "x"})
CASES = [("Hey Nova, open my dev space.", "open_workspace"), ("Hey Nova. | Open my dev space.", "open_workspace"),
         ("Hey Nova, mute.", "mute"), ("Okay Nova, set a timer for ten minutes.", "timer"),
         ("Hey Nova, what time is it?", "clock"), ("Hey Nova, open Spotify.", "open_app")]  # "|" = a 1 s pause
VOICES = (16, 3, 26, 31, 33)  # US, US, UK, Indian-English x2
NOT_WAKE = ["Hey Noah, come here.", "Hey Mona, what's up?", "Hey, can you pass the notebook?",
            "Okay, I'll be there in five minutes.", "Hey Siri, play some music.", "Hey Google, what's the weather?",
            "Hey man, nice to meet you.", "I went to the store and bought novels.", "Hey, no worries at all.",
            "Hey Rohan, come over.", "So the deadline for the project is next week."]


def main():
    v = Voice.__new__(Voice)
    Voice.load(v)

    def say(t, sid):
        a = v.tts.generate(t, sid=sid)
        x = np.array(a.samples, np.float32)
        return np.interp(np.linspace(0, len(x) - 1, int(len(x) * 16000 / a.sample_rate)), np.arange(len(x)), x).astype(np.float32)

    E.Ears.__init__.__globals__["threading"].Thread = lambda **k: type("T", (), {"start": lambda s: None})()
    segs, log = [], []
    e = E.Ears.__new__(E.Ears)  # the real class, without opening the mic
    E.Ears.__init__(e, on_wake=lambda: log.append("wake"), on_utterance=segs.append, on_level=lambda x: None,
                    on_quiet=lambda: log.append("quiet"))
    total = good = woke = 0
    for text, want in CASES:
        for sid in VOICES:
            for _ in range(2):
                parts = [say(t, sid) for t in text.split("|")]
                x = np.concatenate([np.zeros(8000, np.float32), *sum(([p, np.zeros(16000, np.float32)] for p in parts), []),
                                    np.zeros(24000, np.float32)])
                segs.clear(), log.clear()
                e.mode = "sleep"
                e.ring.clear()
                e.kws.reset_stream(e.stream)
                for i in range(0, len(x), E.BLOCK):
                    e.ring.append(x[i:i + E.BLOCK])
                    e.hear(x[i:i + E.BLOCK])
                heard = E.after_name(e.transcribe(segs[0]), "Nova") if segs else ""
                call = reflex(clean(heard, "Nova")) if heard else None
                ok = "wake" in log and call is not None and call[0] == want
                total, good, woke = total + 1, good + ok, woke + ("wake" in log)
                if not ok:
                    print(f"miss  {text!r} voice {sid}: {log} heard {heard!r} -> {call}")
    false = 0
    for t in NOT_WAKE:
        for sid in (31, 3, 26):
            x = np.concatenate([np.zeros(8000, np.float32), say(t, sid), np.zeros(24000, np.float32)])
            log.clear()
            e.mode = "sleep"
            e.ring.clear()
            e.kws.reset_stream(e.stream)
            for i in range(0, len(x), E.BLOCK):
                e.ring.append(x[i:i + E.BLOCK])
                e.hear(x[i:i + E.BLOCK])
            if "wake" in log:
                false += 1
                print(f"FALSE WAKE  {t!r} voice {sid}")
    print(f"woke on {woke}/{total}; {good}/{total} reached the right action ({100 * good / total:.0f}%); "
          f"false wakes {false}/{len(NOT_WAKE) * 3}")
    return good / total if false == 0 else 0


if __name__ == "__main__":
    sys.exit(0 if main() >= 0.85 else 1)
