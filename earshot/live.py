"""Talk mode: a free-form, interruptible voice conversation with Gemini Live, using the same tools.

    mic (the ears' stream, 16 kHz) -> PCM16 -> Live session (Google's VAD decides when you're done)
    Live -> 24 kHz PCM16 -> a buffer -> speakers. "interrupted" (you talked over it) empties the buffer at once.
    Live -> tool_call -> hands.TOOLS / MCP in a worker thread -> tool response. Google Search is built in.

Sessions resume: GoAway or a dropped socket reconnects with the last handle, and sliding-window compression lifts
the 15-minute cap. It ends when you say bye (the model calls end_talk), click "end", or nobody talks for 45 s.
"""
import asyncio
import threading
import time

import numpy as np
import sounddevice as sd

from . import hands
from .config import S, gemini_key
from .hands import TOOLS, schema

RULES = ("\nYou are now in a live spoken conversation, like a call with a friend. Talk naturally and briefly; "
         "it's fine to be interrupted. Use your tools for anything on the PC and Google Search for anything current. "
         "When the user says goodbye or wants to stop talking, say a short bye and call end_talk.")
END_TALK = {"name": "end_talk", "description": "End the live conversation (the user said bye or wants to stop).",
            "parameters": {"type": "object", "properties": {}, "required": []}}
NOT_LIVE = {"web_answer"}  # Live searches Google itself
BARGE = 0.045  # mic level that counts as talking over the speakers (see feed)
IDLE_S = 45


class Player:
    """The model's voice, played as it streams in. flush() drops whatever is queued: that's the barge-in."""

    def __init__(self, on_level=lambda x: None):
        self.buf, self.lock, self.last, self.on_level = bytearray(), threading.Lock(), 0.0, on_level
        self.out = sd.RawOutputStream(samplerate=24000, channels=1, dtype="int16", blocksize=1200, callback=self.cb)
        self.out.start()

    def cb(self, outdata, frames, _t, _status):
        n = frames * 2
        with self.lock:
            chunk = bytes(self.buf[:n])
            del self.buf[:n]
        if chunk:
            self.last = time.time()
        a = np.frombuffer(chunk.ljust(n, b"\0"), dtype=np.int16)
        outdata[:] = a.tobytes()
        self.on_level(min(1.0, float(np.sqrt(np.mean((a / 32768.0) ** 2))) * 6))

    def add(self, data):
        with self.lock:
            self.buf += data

    def flush(self):
        with self.lock:
            self.buf.clear()

    def busy(self):
        return bool(self.buf) or time.time() - self.last < 0.35

    def close(self):
        self.flush()
        self.out.stop()
        self.out.close()


class Talk:
    """One conversation. on_event(kind, value): state (connecting/listening/speaking/ended), you, it (captions),
    tool (a tool ran), error."""

    def __init__(self, brain, on_event, on_out_level=lambda x: None, connect=None, player=None):
        self.brain, self.on_event, self.on_out_level = brain, on_event, on_out_level
        self.connect, self.fake_player, self.player = connect, player, player  # tests pass fakes
        self.active, self.handle, self.loop, self.ending = False, None, None, False

    # ----- from other threads -----
    def start(self):
        if self.active:
            return
        self.active, self.ending, self.handle = True, False, None
        threading.Thread(target=lambda: asyncio.run(self.main()), daemon=True, name="talk").start()

    def stop(self):
        self.active = False
        if self.player:
            self.player.flush()
        if self.loop:
            self.loop.call_soon_threadsafe(self.stop_ev.set)

    def feed(self, x):
        """Mic audio from the ears (float32, 16 kHz). While it is talking, only a voice clearly louder than the
        speakers' echo gets through, so it doesn't interrupt itself.
        ponytail: an energy gate, not echo cancellation. Headphones make barge-in work at any volume."""
        if not self.loop:
            return
        level = float(np.sqrt(np.mean(x * x)))
        if level > 0.02:
            self.heard_at = time.time()
        if self.player and self.player.busy() and level < BARGE:
            x = np.zeros_like(x)
        pcm = (np.clip(x, -1, 1) * 32767).astype("<i2").tobytes()
        self.loop.call_soon_threadsafe(self.push, pcm)

    def push(self, pcm):
        if self.q.full():
            self.q.get_nowait()  # never let a stalled socket build up seconds of old audio
        self.q.put_nowait(pcm)

    # ----- the session -----
    def config(self):
        from google.genai import types
        decls = [types.FunctionDeclaration(name=s["name"], description=s["description"],
                                           parameters_json_schema=s["parameters"])
                 for s in (schema(f) for n, f in TOOLS.items() if n not in NOT_LIVE)]
        if self.brain.mcp:
            decls += [types.FunctionDeclaration(name=k, description=d, parameters_json_schema=p)
                      for k, (_, _, d, p) in self.brain.mcp.tools.items()]
        decls.append(types.FunctionDeclaration(name=END_TALK["name"], description=END_TALK["description"],
                                               parameters_json_schema=END_TALK["parameters"]))
        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=self.brain.system() + RULES,
            tools=[types.Tool(function_declarations=decls), types.Tool(google_search=types.GoogleSearch())],
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=S["live_voice"]))),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            realtime_input_config=types.RealtimeInputConfig(automatic_activity_detection=types.AutomaticActivityDetection(
                prefix_padding_ms=200, silence_duration_ms=600)),  # Google suggests 500-800 ms: shorter splits you
            context_window_compression=types.ContextWindowCompressionConfig(sliding_window=types.SlidingWindow()),
            session_resumption=types.SessionResumptionConfig(handle=self.handle))

    async def main(self):
        self.loop, self.q, self.stop_ev = asyncio.get_running_loop(), asyncio.Queue(maxsize=50), asyncio.Event()
        self.heard_at = self.spoke_at = time.time()
        connect = self.connect or self.brain.client().aio.live.connect
        self.player = self.fake_player
        if self.player is None:  # a fresh speaker stream per conversation
            try:
                self.player = Player(self.on_out_level)
            except Exception as e:
                self.on_event("error", f"no speakers: {e}")
                self.active = False
        fails = 0
        self.on_event("state", "connecting")
        while self.active:
            try:
                async with connect(model=S["live_model"], config=self.config()) as session:
                    fails = 0
                    self.on_event("state", "listening")
                    why = await self.run(session)
                if why != "goaway":
                    break
            except Exception as e:  # a dropped socket: resume with the handle, twice, then give up
                fails += 1
                if not self.active or fails > 2:
                    self.on_event("error", short(e))
                    break
                await asyncio.sleep(0.6 * fails)
        self.active = False
        if self.player and self.player is not self.fake_player:
            self.player.close()
        self.loop = None
        self.on_out_level(0.0)
        self.on_event("state", "ended")

    async def run(self, session):
        send = asyncio.create_task(self.sender(session))
        hear = asyncio.create_task(self.receiver(session))
        stop = asyncio.create_task(self.stop_ev.wait())
        idle = asyncio.create_task(self.watch_idle())
        done, pending = await asyncio.wait({hear, stop, idle}, return_when=asyncio.FIRST_COMPLETED)
        for t in (send, *pending):
            t.cancel()
        if hear in done and hear.exception():
            raise hear.exception()
        return hear.result() if hear in done else "ended"

    async def sender(self, session):
        from google.genai import types
        while True:
            pcm = await self.q.get()
            await session.send_realtime_input(audio=types.Blob(data=pcm, mime_type="audio/pcm;rate=16000"))

    async def watch_idle(self):
        while True:
            await asyncio.sleep(1)
            if self.ending and time.time() - self.ending > 2.5 and not self.player.busy():
                return  # it said bye; let the bye arrive and finish playing first
            if time.time() - max(self.heard_at, self.spoke_at) > IDLE_S and not self.player.busy():
                self.on_event("it", "I'll be here. Say my name anytime.")
                return

    async def receiver(self, session):
        """-> "goaway" (reconnect) or "ended". session.receive() stops after each turn, hence the outer loop."""
        from google.genai import types
        you, it, speaking = "", "", False
        while True:
            async for m in session.receive():
                if m.session_resumption_update and m.session_resumption_update.resumable \
                        and m.session_resumption_update.new_handle:
                    self.handle = m.session_resumption_update.new_handle
                if m.go_away:
                    return "goaway"
                sc = m.server_content
                if sc:
                    if sc.interrupted:  # you talked over it: stop mid-word
                        self.player.flush()
                        speaking, it = False, ""
                        self.on_event("state", "listening")
                    if sc.input_transcription and sc.input_transcription.text:
                        you += sc.input_transcription.text
                        self.on_event("you", you.strip())
                    if sc.model_turn:
                        for part in sc.model_turn.parts or []:
                            if part.inline_data and part.inline_data.data:
                                self.player.add(part.inline_data.data)
                                self.spoke_at = time.time()
                                if not speaking:
                                    speaking = True
                                    self.on_event("state", "speaking")
                    if sc.output_transcription and sc.output_transcription.text:
                        it += sc.output_transcription.text
                        self.on_event("it", it.strip())
                    if sc.turn_complete:
                        you, it, speaking = "", "", False
                        self.on_event("state", "listening")
                if m.tool_call:
                    replies = []
                    for fc in m.tool_call.function_calls or []:
                        if fc.name == END_TALK["name"]:
                            self.ending, out = time.time(), "ending the conversation"
                        else:
                            self.on_event("tool", fc.name)
                            out = await asyncio.to_thread(self.call, fc.name, dict(fc.args or {}))
                        replies.append(types.FunctionResponse(id=fc.id, name=fc.name, response={"result": out}))
                    await session.send_tool_response(function_responses=replies)

    def call(self, name, args):
        hands.com()
        return self.brain.call(name, args)


def short(e):
    s = str(e)
    if "API key" in s or "401" in s or "403" in s or "PERMISSION" in s:
        return "Gemini didn't accept the key. Check it in settings."
    if "429" in s or "RESOURCE_EXHAUSTED" in s:
        return "The free Live quota is used up for now."
    if "404" in s or "not found" in s.lower():
        return f"{S['live_model']} isn't available to this key."
    return s[:120]
