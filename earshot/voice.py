"""Speaks short replies with Kokoro v1.0 (local, Apache-2.0), streaming: it starts talking after the first sentence.
Ears go quiet while it talks, so it never wakes itself."""
import queue
import threading

import numpy as np
import sherpa_onnx
import sounddevice as sd

from .config import MODELS, S

KOKORO = MODELS / "kokoro-int8-multi-lang-v1_0"


class Voice:
    def __init__(self):
        self.tts, self.stop_flag = None, threading.Event()
        threading.Thread(target=self.load, daemon=True).start()

    def load(self):
        D = KOKORO
        self.tts = sherpa_onnx.OfflineTts(sherpa_onnx.OfflineTtsConfig(model=sherpa_onnx.OfflineTtsModelConfig(
            kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                model=str(D / "model.int8.onnx"), voices=str(D / "voices.bin"), tokens=str(D / "tokens.txt"),
                data_dir=str(D / "espeak-ng-data"), lang="en-us"),
            num_threads=1)))  # more threads start ~0.8 s sooner but keep spinning when idle (4 threads: 22% of a core)
        self.tts.generate("ok.", sid=S["voice"])  # warm up: the first real reply is ~3x faster

    def say(self, text, sid=None):
        """Blocks until done (call from a worker thread). Returns False if the voice isn't ready."""
        if not self.tts or not text:
            return False
        self.stop_flag.clear()
        q = queue.Queue()

        def play():  # plays sentence n while sentence n+1 is being generated
            with sd.OutputStream(samplerate=self.tts.sample_rate, channels=1, dtype="float32") as out:
                while (x := q.get()) is not None:
                    if not self.stop_flag.is_set():
                        out.write(x)
        player = threading.Thread(target=play, daemon=True)
        player.start()

        def chunk(samples, _progress):
            q.put(np.asarray(samples, dtype=np.float32).reshape(-1, 1))
            return 0 if self.stop_flag.is_set() else 1
        try:
            self.tts.generate(text, sid=S["voice"] if sid is None else sid, speed=1.05, callback=chunk)
        finally:
            q.put(None)
            player.join()
        return True

    def stop(self):
        self.stop_flag.set()


def chime(up=True):
    """A soft two-note blip: the ear perks (up) or relaxes (down). 120 ms, quiet."""
    sr, n = 24000, 2880
    t = np.arange(n) / sr
    f = np.where(t < 0.06, 880 if up else 1175, 1320 if up else 880)
    env = np.minimum(1, t / 0.005) * np.exp(-t * 18)
    with CHIME:  # sd.play shares one global stream: two threads at once is how audio libraries fall over
        try:
            sd.play((0.12 * np.sin(2 * np.pi * f * t) * env).astype(np.float32), sr)
        except Exception:
            pass


CHIME = threading.Lock()
