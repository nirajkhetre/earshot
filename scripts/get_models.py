"""Downloads the voice models into ./models (~900 MB unpacked). Safe to re-run: skips what's there.

    wake word   sherpa-onnx KWS zipformer gigaspeech 3.3M  (open vocabulary: any name, no training)
    VAD         Silero
    ears        NVIDIA Parakeet TDT 0.6B v3, int8           (CC-BY-4.0)
    voice       Kokoro 82M v1.0 multi-lang, int8            (Apache-2.0)
Gemma 4 E2B comes from Ollama: ollama pull gemma4:e2b
"""
import sys
import tarfile
import urllib.request
from pathlib import Path

B = "https://github.com/k2-fsa/sherpa-onnx/releases/download/"
MODELS = Path(__file__).resolve().parent.parent / "models"
GET = ["kws-models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01.tar.bz2",
       "asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8.tar.bz2",
       "tts-models/kokoro-int8-multi-lang-v1_0.tar.bz2"]


def fetch(url, dest):
    def progress(blocks, size, total):
        if total > 0:
            done = min(100, blocks * size * 100 // total)
            sys.stdout.write(f"\r      {done:3d}%  of {total / 1e6:.0f} MB")
            sys.stdout.flush()
    tmp = dest.with_suffix(dest.suffix + ".part")
    urllib.request.urlretrieve(url, tmp, progress)
    tmp.replace(dest)  # a half-finished download never looks finished
    print()


def main():
    MODELS.mkdir(exist_ok=True)
    if not (MODELS / "silero_vad.onnx").exists():
        print("    silero_vad")
        fetch(B + "asr-models/silero_vad.onnx", MODELS / "silero_vad.onnx")
    for g in GET:
        name = g.split("/")[1].removesuffix(".tar.bz2")
        if (MODELS / name).exists():
            print(f"    {name}: already here")
            continue
        print(f"    {name}")
        archive = MODELS / (name + ".tar.bz2")
        fetch(B + g, archive)
        print("      unpacking ...")
        with tarfile.open(archive) as t:
            t.extractall(MODELS, filter="data")
        archive.unlink()
    print(f"    models ready in {MODELS}")


if __name__ == "__main__":
    main()
