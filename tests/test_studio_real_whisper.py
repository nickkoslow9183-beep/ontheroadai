"""REAL transcription test (not a fixture): synthetic espeak-ng speech -> faster-whisper.

Skipped automatically unless espeak-ng is installed and the Whisper 'base.en' model is already
downloaded (tests never download models)."""

import shutil
import subprocess
from pathlib import Path

import pytest

from ontheroadai.studio import media, transcript


def _model_cached() -> bool:
    try:
        from huggingface_hub import try_to_load_from_cache

        return try_to_load_from_cache("Systran/faster-whisper-base.en", "model.bin") not in (None,)
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not shutil.which("espeak-ng") or not transcript.whisper_status()["installed"]
                                or not _model_cached(), reason="needs espeak-ng and a cached Whisper base.en model")


def test_real_whisper_word_timestamps(tmp_path):
    wav = tmp_path / "speech.wav"
    subprocess.run(["espeak-ng", "-v", "en-us", "-s", "150", "-w", str(wav),
                    "Who would you trust with the last shot? Honestly, it has to be Steph."], check=True)
    audio = media.extract_audio(wav, tmp_path / "a16k.wav")
    res = transcript.transcribe(audio, "base.en")
    text = " ".join(s["text"] for s in res["segments"]).lower()
    assert "last shot" in text and "trust" in text
    words = [w for s in res["segments"] for w in (s["words"] or [])]
    assert words and all(w["end"] >= w["start"] for w in words)
    assert all(words[i]["start"] <= words[i + 1]["start"] for i in range(len(words) - 1))
