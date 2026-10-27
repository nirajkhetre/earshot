"""Always-on hearing: the wake phrase (your assistant's name), then one utterance, then text.

    mic 16 kHz -> keyword spotter, tuned eager: "hey/ok/hi <name>" or just "<name>". Open vocabulary, so renaming
                  needs no training. Eager catches accents; it also fires on things that aren't the name, so...
               -> Parakeet transcribes the last 1.6 s and the name must really be in it (~60 ms). Two stages, like
                  real assistants: on 40 wake phrases (half Indian-English voices) 40 woke, 0 of 42 others did.
               -> Silero VAD cuts one utterance (ends at ~0.45 s of silence), Parakeet turns it into text.

Nothing is recorded. Audio sits in a 1.6 s ring buffer until the wake word; after it, only the utterance is decoded.
While another app records from a mic (a call), it goes deaf on its own.
"""
import collections
import ctypes
import difflib
import os
import queue
import re
import sys
import threading
import time
import traceback
from pathlib import Path

import numpy as np
import sentencepiece as spm
import sherpa_onnx
import sounddevice as sd

from .config import MODELS, S, log

SR, BLOCK = 16000, 800  # 50 ms blocks: the level meter updates 20 times a second
KWS = MODELS / "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
ASR = MODELS / "sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
KWS_FILE = "-epoch-12-avg-2-chunk-16-left-64.int8.onnx"


def keywords(name, sensitivity=0.5):
    """'Nova' -> (tokenized keyword lines, the tags a hit for them reports). The spotter is only stage one, so it's
    eager (at 0.5: threshold 0.06, boost 2.5); has_name() on Parakeet's transcript is the real judge."""
    sp = spm.SentencePieceProcessor(model_file=str(KWS / "bpe.model"))
    name = " ".join(name.upper().split())
    thr, boost = round(max(0.02, 0.2 - 0.28 * sensitivity), 3), round(1.5 + 2 * sensitivity, 1)
    lines, tags = [], set()
    for lead in ("HEY", "OK", "OKAY", "HI", "HAY", ""):  # "" = the bare name: "Nova, open Spotify"
        tag = f"{lead}_{name}".strip("_").replace(" ", "_")
        tags.add(tag)
        lines.append(" ".join(sp.encode(f"{lead} {name}".strip(), out_type=str)) + f" :{boost} #{thr} @{tag}")
    return "/".join(lines), tags


# apps that hold the mic the whole time they run: counting them as calls would mean never listening
ALWAYS_ON = {"voicemod", "nvidia broadcast", "krisp", "steelseriessonar", "sonar"}


def after_name(text, name):
    """'...so yeah, hey Nova, open my dev space' -> 'open my dev space'. Everything up to the first time the name
    is heard (within the first six words) is the wake phrase or stray room noise. Parakeet sometimes glues the
    phrase into one word ("Heinova") or bends the name ("Novah"), so the match is loose."""
    i = name_at(text, name)
    return text if i is None else " ".join(text.split()[i:])


def name_at(text, name):
    """Index of the first word after the name, if the name is among the first six words; else None."""
    words, key, n = text.split(), re.sub(r"[^a-z]", "", name.lower()), len(name.split())
    for i in range(min(6, len(words))):
        k = re.sub(r"[^a-z]", "", "".join(words[i:i + n]).lower())
        if k and (k.endswith(key) or len(key) >= 4 and difflib.SequenceMatcher(None, k, key).ratio() >= 0.8):
            return i + n
    return None


def mic_busy_elsewhere():
    """Name of another app recording from any microphone right now, or ''.

    Asks Windows' audio engine for the live capture sessions. (The privacy log in the registry,
    ...\\ConsentStore\\microphone, isn't reliable: a crashed app stays "in use" forever, and on some builds new use
    isn't logged at all.)"""
    import comtypes
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioSessionControl2, IAudioSessionManager2
    comtypes.CoInitialize()
    devices = AudioUtilities.GetDeviceEnumerator().EnumAudioEndpoints(1, 1)  # eCapture, DEVICE_STATE_ACTIVE
    me = os.getpid()
    for i in range(devices.GetCount()):
        mgr = devices.Item(i).Activate(IAudioSessionManager2._iid_, CLSCTX_ALL, None).QueryInterface(IAudioSessionManager2)
        sessions = mgr.GetSessionEnumerator()
        for j in range(sessions.GetCount()):
            s = sessions.GetSession(j).QueryInterface(IAudioSessionControl2)
            pid = s.GetProcessId()
            if s.GetState() == 1 and pid not in (0, me):  # AudioSessionStateActive: recording right now
                name = Path(exe_path(pid)).stem or "another app"
                if name.lower() not in ALWAYS_ON:
                    return name
    return ""


def exe_path(pid):
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf, n = ctypes.create_unicode_buffer(520), ctypes.c_ulong(520)
        return buf.value if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else ""
    finally:
        k32.CloseHandle(h)


class Ears:
    """Modes: sleep (waiting for the name), listen (taking one utterance), busy (thinking/speaking: audio
    ignored), deaf (you hushed it), call (another app has the mic), talk (streaming to a live conversation)."""

    def __init__(self, on_wake, on_utterance, on_level, on_quiet, on_mode=lambda m: None, on_error=lambda m: None):
        self.on_wake, self.on_utterance, self.on_level, self.on_quiet, self.on_mode, self.on_error = (
            on_wake, on_utterance, on_level, on_quiet, on_mode, on_error)
        self.lock, self.asr_lock, self.injected = threading.Lock(), threading.Lock(), collections.deque()
        self.kws = sherpa_onnx.KeywordSpotter(
            tokens=str(KWS / "tokens.txt"), encoder=str(KWS / f"encoder{KWS_FILE}"),
            decoder=str(KWS / f"decoder{KWS_FILE}"), joiner=str(KWS / f"joiner{KWS_FILE}"),
            keywords_file=str(KWS / "keywords.txt"), num_threads=1)
        self.asr = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(ASR / "encoder.int8.onnx"), decoder=str(ASR / "decoder.int8.onnx"),
            joiner=str(ASR / "joiner.int8.onnx"), tokens=str(ASR / "tokens.txt"),
            # one thread: 0.4 s per command instead of 0.2, but ONNX Runtime's extra threads spin forever after the
            # first decode (measured: 4 threads left 63% of a core busy while idle, 1 thread leaves 0%)
            num_threads=1, model_type="nemo_transducer")
        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = str(MODELS / "silero_vad.onnx")
        cfg.silero_vad.threshold = 0.5
        cfg.silero_vad.min_silence_duration = 0.45
        cfg.silero_vad.min_speech_duration = 0.15
        cfg.silero_vad.max_speech_duration = 15
        cfg.sample_rate = SR
        self.vad_cfg, self.win = cfg, cfg.silero_vad.window_size
        self.vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=30)
        self.after_wake, self.fed, self.fed_off, self.pending = False, [], 0, np.zeros(0, np.float32)
        self.phrases, self.skip_blocks = queue.Queue(maxsize=3), 0
        self.agc_peak, self.gain, self.loudest, self.loudest_at = 0.025, 12.0, 0.0, time.time()
        self.checked, self.check_s = 0, 0.0
        self.ring = collections.deque(maxlen=int(1.6 * SR) // BLOCK)  # holds the whole "hey <name>" when it fires
        self.mode, self.hushed, self.call, self.sink = "sleep", False, "", None
        self.rename(S["name"], S["sensitivity"])
        self.alive = True
        threading.Thread(target=self.run, daemon=True, name="ears").start()
        threading.Thread(target=self.watch_calls, daemon=True, name="calls").start()
        threading.Thread(target=self.check_phrases, daemon=True, name="phrases").start()

    # ---------- control (any thread) ----------
    def rename(self, name, sensitivity=None):
        kw, tags = keywords(name, S["sensitivity"] if sensitivity is None else sensitivity)
        with self.lock:
            self.stream, self.tags, self.name = self.kws.create_stream(kw), tags, name
            self.name_words = {"hey", "hi", "ok", "okay", "a", "uh", "um", *name.lower().split()}

    def listen(self, wait=6.0, preroll=False, skip=0.0):
        """Take one utterance now (after the wake word, a hotkey, or as a follow-up). skip: seconds of mic audio to
        throw away first: right after it speaks, the mic's buffer still holds the end of its own voice."""
        with self.lock:
            self.reset_vad()
            self.skip_blocks = int(skip * SR / BLOCK)
            if preroll:  # all of "hey nova" and whatever followed it: Parakeet hears the name whole, after_name() cuts it
                self.feed_vad(np.concatenate(list(self.ring)) if self.ring else np.zeros(0, np.float32))
            self.deadline, self.heard, self.after_wake = time.time() + wait, False, preroll
            self.set_mode("listen")

    def sleep(self):
        with self.lock:
            self.sink = None
            self.reset_vad()
            self.set_mode("deaf" if self.hushed else "call" if self.call else "sleep")

    def talk(self, sink):
        """Talk mode: every block goes straight to sink (the live session); no wake word, no VAD here."""
        with self.lock:
            self.sink = sink
            self.set_mode("talk")

    def busy(self):
        with self.lock:
            self.set_mode("busy")

    def hush(self, on):
        self.hushed = on
        if self.mode in ("sleep", "deaf", "call"):
            self.sleep()

    def set_mode(self, m):
        if m != self.mode:
            log(f"ears: {self.mode} -> {m}")
            self.mode = m
            self.on_mode(m)

    def said_name(self, hit):
        """Stage two: did Parakeet hear the name in the last 1.6 s? (~60 ms; the mic buffers meanwhile.)"""
        heard = self.transcribe(np.concatenate(list(self.ring)))
        ok = name_at(heard, self.name) is not None
        log(f"spotter heard {hit}; parakeet: " + (f"{heard!r} -> WAKE" if ok else "no name -> ignored"))
        return ok

    def check_phrases(self):
        """Wake path two, for quiet mics and accents the spotter misses: while asleep, every short phrase the VAD
        cuts goes to Parakeet (which heard the name at every loudness we tried, down to a whisper's peak of 0.015).
        Name in the first words -> wake; the rest of the phrase, if any, is the command. Words without the name
        are dropped on the spot: never logged, never kept."""
        while self.alive:
            self.check_phrase(*self.phrases.get())

    def check_phrase(self, seg, raw_s):
        t0 = time.perf_counter()
        heard = self.transcribe(seg)
        self.checked += 1
        self.check_s += time.perf_counter() - t0
        if name_at(heard, self.name) is None:
            w = re.findall(r"[a-z']+", heard.lower())
            if w and w[0] in ("hey", "hi", "hay", "ok", "okay"):  # sounds like a wake attempt: what did the name become?
                log(f"phrase starts with {' '.join(w[:2])!r}: not the name (only these two words are logged)")
            return
        rest = [w for w in re.findall(r"[a-z']+", after_name(heard, self.name).lower()) if w not in self.name_words]
        with self.lock:
            if self.mode != "sleep":
                return  # the spotter already took it, or you're mid-conversation
            if rest:
                self.set_mode("busy")
        log(f"phrase ({raw_s:.1f}s) has the name: {heard!r} -> WAKE")
        if rest:
            self.on_wake()
            self.on_utterance(seg)
        else:
            self.listen(wait=6.0)
            self.on_wake()

    def only_the_name(self, seg):
        """Is this short clip just the wake phrase? (~50 ms to decode; the mic buffers meanwhile.)"""
        rest = after_name(self.transcribe(seg), self.name).lower().replace(",", " ").replace(".", " ").split()
        return all(w.strip("!?'") in self.name_words for w in rest)

    def with_lead_in(self, front, lead=0.3):
        """The VAD cuts right at your first sound, and Parakeet often returns nothing for a clip with no lead-in
        (or clips "Open" to "Up in"). Take the 0.3 s before it from what we fed."""
        heard = np.concatenate(self.fed)
        n, s0 = len(front.samples), front.start - self.fed_off
        return heard[max(0, s0 - int(lead * SR)): s0 + n].astype(np.float32)

    def transcribe(self, samples):
        pad = np.zeros(int(0.25 * SR), np.float32)  # a little quiet on both sides, the way it was trained
        with self.asr_lock:  # the ears thread (wake check) and a worker (your command) can both get here
            st = self.asr.create_stream()
            st.accept_waveform(SR, np.concatenate([pad, samples, pad]))
            self.asr.decode_stream(st)
            return st.result.text.strip()

    def level_up(self, x):
        """Automatic gain: a quiet mic (many laptop mics sit at 20-30% in Windows) gets your voice up to where the spotter and the
        VAD work. Tested: the spotter caught 11/12 wake phrases at a peak of 0.05 but 5/12 at 0.015. Tracks the loudest
        recent sound (slow decay) and boosts up to 12x; "mic boost" in settings multiplies on top.
        ponytail: peak-tracking AGC; a loud bang lowers the gain for ~a minute. Per-band/RMS AGC if that bites."""
        peak = float(np.abs(x).max())
        self.agc_peak = max(peak, self.agc_peak * 0.9993, 0.01)  # ~0.7% per second back down after a loud sound
        self.loudest = max(self.loudest, peak)
        if time.time() - self.loudest_at > 30:  # a number, not audio: is your voice reaching it, and how loud?
            if self.loudest > 0.003 or self.checked:
                log(f"mic: loudest in 30 s = {self.loudest:.3f}, auto-gain {self.gain:.1f}x; "
                    f"{self.checked} phrases checked ({self.check_s:.1f} s of Parakeet)")
            self.loudest, self.loudest_at, self.checked, self.check_s = 0.0, time.time(), 0, 0.0
        self.gain = min(12.0, max(1.0, 0.3 / self.agc_peak)) * S["mic_gain"]
        return np.clip(x * self.gain, -1, 1) if self.gain != 1 else x

    def inject(self, samples):
        """Self-test: play this audio into the pipeline in place of the mic, block by block, in real time."""
        blocks = [samples[i:i + BLOCK] for i in range(0, len(samples) - BLOCK + 1, BLOCK)]
        self.injected.extend(blocks)

    # ---------- the loop ----------
    def reset_vad(self):
        self.vad.reset()
        self.pending, self.fed, self.fed_off = np.zeros(0, np.float32), [], 0

    def feed_vad(self, x):
        self.fed.append(x)  # recent audio, so a segment can be cut with the 0.3 s just before it
        if len(self.fed) > 400 and not self.vad.is_speech_detected():  # 20 s: keep the last 10 (asleep, it runs all day)
            self.fed_off += sum(len(b) for b in self.fed[:200])
            del self.fed[:200]
        self.pending = np.concatenate([self.pending, x])
        while len(self.pending) >= self.win:
            self.vad.accept_waveform(self.pending[:self.win])
            self.pending = self.pending[self.win:]

    def run(self):
        while self.alive:
            try:
                # latency="high": a bigger buffer, so a wake check (~60 ms) never drops the words that follow it
                with sd.InputStream(samplerate=SR, channels=1, dtype="float32", blocksize=BLOCK, latency="high") as mic:
                    while self.alive:
                        x = mic.read(BLOCK)[0][:, 0].copy()
                        if self.injected:  # a self-test is talking
                            x = self.injected.popleft()
                        x = self.level_up(x)
                        self.ring.append(x)
                        self.hear(x)
            except Exception as e:  # unplugged headset, sleep/resume, no mic: wait and reopen the default device
                if str(e) != getattr(self, "last_error", None):  # once per new problem, not every 2 s
                    self.last_error = str(e)
                    log("ears loop:\n" + traceback.format_exc())
                    self.on_error(f"can't open the microphone: {e}"[:100])
                time.sleep(2)

    def hear(self, x):
        fire, utterance, quiet = False, None, False
        self.on_level(min(1.0, float(np.sqrt(np.mean(x * x))) * 14))  # always: the mic meter in settings uses it
        with self.lock:
            sink = self.sink if self.mode == "talk" else None
            if sink:
                pass
            elif self.mode == "sleep":
                self.stream.accept_waveform(SR, x)
                while self.kws.is_ready(self.stream):
                    self.kws.decode_stream(self.stream)
                    hit = self.kws.get_result(self.stream)
                    if hit:
                        self.kws.reset_stream(self.stream)
                        # the model's demo words ("HEY SIRI"...) stay live: ignore them. Ours go to stage two.
                        fire = fire or (hit in self.tags and self.said_name(hit))
                if not fire:  # wake path two: hand each short phrase to Parakeet (check_phrases)
                    self.feed_vad(x)
                    while not self.vad.empty():
                        raw_s = len(self.vad.front.samples) / SR
                        seg = self.with_lead_in(self.vad.front)
                        self.vad.pop()
                        if 0.3 <= raw_s <= 6.0 and not self.phrases.full():
                            self.phrases.put_nowait((seg, raw_s))
            elif self.mode == "listen" and self.skip_blocks > 0:
                self.skip_blocks -= 1  # the tail of its own voice, still in the mic's buffer
            elif self.mode == "listen":
                self.feed_vad(x)
                if self.vad.is_speech_detected():
                    self.heard = True
                    self.deadline = max(self.deadline, time.time() + 1.0)
                if not self.vad.empty():
                    raw_s = len(self.vad.front.samples) / SR
                    seg = self.with_lead_in(self.vad.front)
                    self.vad.pop()
                    if self.after_wake and raw_s < 1.5 and self.only_the_name(seg):
                        self.after_wake = False  # "hey Nova" ... (a pause) ... the command is still coming
                        self.deadline = time.time() + 5.0
                    else:
                        utterance = seg
                        self.set_mode("busy")
                elif time.time() > self.deadline and not self.vad.is_speech_detected():
                    self.set_mode("busy")
                    quiet = True
        if sink:
            sink(x)
        if fire:
            self.listen(preroll=True)
            self.on_wake()
        if utterance is not None:
            self.on_utterance(utterance)
        if quiet:
            self.on_quiet()

    def watch_calls(self):
        while self.alive:
            try:
                busy = mic_busy_elsewhere() if S["pause_in_calls"] else ""
            except Exception as e:  # never let the call check cost you the wake word
                log("call check:", e)
                busy = ""
            if busy != self.call:
                log(f"mic busy by: {busy!r}" if busy else "mic free again")
                self.call = busy
                if self.mode in ("sleep", "call"):
                    self.sleep()
            time.sleep(2)
