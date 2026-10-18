"""Regression tests for region-based transcription.

Running Silero VAD over one long, mostly-silent track silently dropped real speech. These tests
pin the fix: short replies separated by long stretches of silence must all come back, at the
right times. They load the Whisper model (GPU if available), so they're marked `model` and
skipped in CI. Run them with:  uv run pytest -m model
"""

from pathlib import Path

import numpy as np
import pytest

from meetingrecorder import transcriber as tt
from meetingrecorder.wavio import read_wav

FIX = Path(__file__).resolve().parent / "fixtures"  # Windows text-to-speech clips, no real voices

pytestmark = pytest.mark.model


@pytest.fixture(scope="module")
def model():
    return tt.load_model("large-v3-turbo")


def place(parts, total_s: float, floor_db: float | None = None) -> np.ndarray:
    x = np.zeros(int(total_s * 16000), np.float32)
    if floor_db is not None:  # a live mic's noise floor between replies
        x += np.random.default_rng(1).standard_normal(len(x)).astype(np.float32) * 10 ** (floor_db / 20)
    for t, name in parts:
        clip, _ = read_wav(FIX / name)
        i = int(t * 16000)
        x[i:i + len(clip)] += clip * 0.3
    return x


def texts_near(segs, t: float, tol: float = 2.0) -> str:
    return " ".join(s["text"] for s in segs if abs(s["start"] - t) <= tol).lower()


@pytest.mark.parametrize("floor_db", [None, -60.0])
def test_short_replies_in_long_silence(model, floor_db):
    """A "Me" track: 3 short replies across 4 minutes, digital silence (or a quiet floor) between."""
    x = place([(75.0, "tts_yes.wav"), (140.0, "tts_sounds_good.wav"), (215.0, "tts_friday.wav")], 240.0, floor_db)
    segs = tt.transcribe_regions(model, x, offset=10.0)  # offset = track start on the session timeline
    assert "yes" in texts_near(segs, 85.0)
    assert "sounds good" in texts_near(segs, 150.0)
    assert "friday" in texts_near(segs, 225.0)
    assert len(segs) <= 4, f"unexpected extra (hallucinated?) segments: {segs}"


def test_pure_silence_gives_nothing(model):
    assert tt.transcribe_regions(model, np.zeros(16000 * 60, np.float32), 0.0) == []
