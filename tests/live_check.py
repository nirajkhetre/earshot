"""Talk mode without a key: a scripted fake Gemini Live server drives the real session code.

    python tests/live_check.py            the scripted conversation + the echo gate + the speaker buffer
    python tests/live_check.py --net      also: a real connection with a bad key must fail politely

Checks that audio reaches the speaker buffer, talking over it flushes the buffer, captions stream, tools run and
are answered with the right ids, a GoAway reconnects with the resumption handle, and "bye" ends it after the bye.
"""
import asyncio
import contextlib
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["EARSHOT_HOME"] = tempfile.mkdtemp(prefix="earshot-live-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
from google.genai import types  # noqa: E402

from earshot import brain as B, hands, live  # noqa: E402

calls = []
real_set_volume = hands.TOOLS["set_volume"]


def fake_volume(level: int):
    calls.append(("set_volume", level))
    return f"Volume {level}."


fake_volume.learn, fake_volume.cloud, fake_volume.__doc__ = True, False, real_set_volume.__doc__
hands.TOOLS["set_volume"] = fake_volume  # no real volume change during the check


class FakePlayer:
    """Plays nothing; keeps the clock a real one would (24 kHz, 16-bit: 48,000 bytes a second)."""

    def __init__(self):
        self.bytes, self.flushes, self.until = 0, 0, 0.0

    def add(self, data):
        self.bytes += len(data)
        self.until = max(self.until, time.time()) + len(data) / 48000

    def flush(self):
        self.flushes += 1
        self.until = time.time()

    def busy(self):
        return time.time() < self.until


def msg(**kw):
    return types.LiveServerMessage(**kw)


def sc(**kw):
    return msg(server_content=types.LiveServerContent(**kw))


def audio(seconds):
    t = np.arange(int(24000 * seconds)) / 24000
    pcm = (0.1 * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2").tobytes()
    return sc(model_turn=types.Content(role="model", parts=[types.Part(inline_data=types.Blob(
        data=pcm, mime_type="audio/pcm;rate=24000"))]))


def call(cid, name, **args):
    return msg(tool_call=types.LiveServerToolCall(function_calls=[types.FunctionCall(id=cid, name=name, args=args)]))


WAIT = "wait for the tool response"
SCRIPT = [
    [  # first connection: a question that needs a tool, then Google says "going away"
        [msg(session_resumption_update=types.LiveServerSessionResumptionUpdate(new_handle="h1", resumable=True)),
         sc(input_transcription=types.Transcription(text="what time ")),
         sc(input_transcription=types.Transcription(text="is it")),
         call("c1", "clock"), WAIT,
         audio(0.4), sc(output_transcription=types.Transcription(text="It's evening.")), sc(turn_complete=True)],
        [msg(go_away=types.LiveServerGoAway(time_left="5s"))],
    ],
    [  # resumed: a long story you talk over, a volume change, then bye
        [sc(input_transcription=types.Transcription(text="tell me a long story")),
         audio(3.0), sc(output_transcription=types.Transcription(text="Once upon a time")),
         sc(interrupted=True), sc(turn_complete=True)],
        [sc(input_transcription=types.Transcription(text="set the volume to thirty")),
         call("c2", "set_volume", level=30), WAIT,
         audio(0.3), sc(output_transcription=types.Transcription(text="Thirty.")), sc(turn_complete=True)],
        [sc(input_transcription=types.Transcription(text="ok bye")),
         call("c3", "end_talk"), WAIT,
         audio(0.5), sc(output_transcription=types.Transcription(text="Bye!")), sc(turn_complete=True)],
    ],
]


class FakeSession:
    def __init__(self, turns):
        self.turns, self.audio_in, self.responses, self.tool_ev = turns, 0, [], asyncio.Event()

    async def send_realtime_input(self, audio=None, **_):
        self.audio_in += len(audio.data)

    async def send_tool_response(self, function_responses):
        self.responses += list(function_responses)
        self.tool_ev.set()

    async def receive(self):  # like the SDK: one turn per call
        if not self.turns:
            await asyncio.sleep(3600)
        for m in self.turns.pop(0):
            if m is WAIT:
                await self.tool_ev.wait()
                self.tool_ev.clear()
                continue
            await asyncio.sleep(0.03)
            yield m


SESSIONS, configs, sessions = [], [], []


@contextlib.asynccontextmanager
async def fake_connect(model, config):
    configs.append(config)
    s = FakeSession(SESSIONS[len(sessions)])
    sessions.append(s)
    yield s


def conversation():
    """Run twice on one Talk object: a second conversation in the same app run must work too."""
    bad = 0
    br = B.Brain()
    for round_ in (1, 2):
        configs.clear()
        sessions.clear()
        SESSIONS[:] = [[list(t) for t in s] for s in SCRIPT]
        calls.clear()
        print(f"-- conversation {round_}")
        bad += one_conversation(br, live.Talk(br, None, connect=fake_connect, player=FakePlayer()) if round_ == 1
                                else TALK[0])
    return bad


TALK = []


def one_conversation(br, talk):
    events = []
    talk.on_event = lambda k, v: events.append((k, v))
    talk.player = talk.fake_player = FakePlayer()
    TALK[:] = [talk]
    player = talk.fake_player
    t0 = time.time()
    talk.start()
    while talk.loop is None and time.time() - t0 < 5:
        time.sleep(0.01)
    while talk.active and time.time() - t0 < 20:  # the mic keeps streaming silence, like the ears do
        talk.feed(np.zeros(800, np.float32))
        time.sleep(0.05)
    took = time.time() - t0
    resp = [r for s in sessions for r in s.responses]
    checks = {
        "two connections, the second resumes with handle h1":
            len(configs) == 2 and configs[1].session_resumption.handle == "h1" and configs[0].session_resumption.handle is None,
        "the mic reached the server": sum(s.audio_in for s in sessions) > 0,
        "your words streamed as captions": ("you", "what time is it") in events,
        "its words streamed as captions": ("it", "It's evening.") in events,
        "it was heard speaking": ("state", "speaking") in events,
        "talking over it flushed its voice": player.flushes >= 1,
        "the clock tool ran and its answer went back with id c1":
            any(r.id == "c1" and "It's" in r.response["result"] for r in resp),
        "set_volume ran with 30 and was answered as c2": ("set_volume", 30) in calls and any(r.id == "c2" for r in resp),
        "end_talk was answered as c3": any(r.id == "c3" for r in resp),
        "it ended after the bye played": events[-1] == ("state", "ended") and not talk.active,
        "the tool and voice config is valid": all(isinstance(c, types.LiveConnectConfig) for c in configs),
    }
    for k, ok in checks.items():
        print("ok " if ok else "BAD", k)
    print(f"    ({took:.1f} s; {len(events)} events; tools offered: "
          f"{len(configs[0].tools[0].function_declarations)} + Google Search)")
    return sum(not ok for ok in checks.values())


def echo_gate():
    """While it talks, quiet mic audio (its own echo) is zeroed; a real voice over it gets through."""
    class Now:
        def call_soon_threadsafe(self, f, *a):
            f(*a)
    talk = live.Talk(None, lambda *a: None, player=FakePlayer())
    talk.loop, talk.q = Now(), asyncio.Queue(maxsize=50)
    talk.player.until = time.time() + 5  # it's mid-sentence
    talk.feed(np.full(800, 0.02, np.float32))   # its echo
    talk.feed(np.full(800, 0.2, np.float32))    # you, over it
    talk.player.until = 0                        # it's quiet
    talk.feed(np.full(800, 0.02, np.float32))   # you, softly
    echo, voice, soft = (np.frombuffer(talk.q.get_nowait(), "<i2") for _ in range(3))
    ok = not echo.any() and voice.any() and soft.any()
    print("ok " if ok else "BAD", "echo gate: echo zeroed while it talks, your voice passes, soft speech passes when it's quiet")
    return 0 if ok else 1


def speaker():
    """The real speaker buffer: plays a quiet 0.3 s tone, drains, and flush empties it."""
    levels = []
    p = live.Player(levels.append)
    t = np.arange(7200) / 24000
    p.add((0.05 * np.sin(2 * np.pi * 330 * t) * 32767).astype("<i2").tobytes())
    time.sleep(0.6)
    drained = not p.buf and max(levels) > 0
    p.add(b"\1\0" * 48000)
    p.flush()
    ok = drained and not p.buf
    p.close()
    print("ok " if ok else "BAD", "speaker buffer: played and drained a 0.3 s tone, flush empties it")
    return 0 if ok else 1


def bad_key():
    events = []
    br = B.Brain()
    B.gemini_key = lambda: "AIzaSy-not-a-real-key-earshot-check"
    talk = live.Talk(br, lambda k, v: events.append((k, v)))  # real connect, real speaker stream
    talk.start()
    t0 = time.time()
    while (talk.active or talk.loop) and time.time() - t0 < 30:
        time.sleep(0.1)
    err = next((v for k, v in events if k == "error"), "")
    ok = bool(err) and events[-1] == ("state", "ended")
    print("ok " if ok else "BAD", f"a bad key fails politely: {err!r}")
    return 0 if ok else 1


if __name__ == "__main__":
    bad = conversation() + echo_gate() + speaker() + (bad_key() if "--net" in sys.argv else 0)
    print("ALL GOOD" if not bad else f"{bad} FAILED")
    sys.exit(1 if bad else 0)
