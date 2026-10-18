"""Crash-safe WAV: valid while still being written, and readable if the process dies."""

import numpy as np

from meetingrecorder import wavio


def test_round_trip_and_silence(tmp_path):
    w = wavio.CrashSafeWav(tmp_path / "a.wav", 16000)
    w.write(np.full(16000, 0.5, np.float32))
    w.write_silence(8000)
    w.close()
    x, rate = wavio.read_wav(tmp_path / "a.wav")
    assert rate == 16000 and len(x) == 24000
    assert abs(x[:16000].mean() - 0.5) < 1e-3 and not x[16000:].any()


def test_readable_without_close(tmp_path, monkeypatch):
    """Simulate a crash: data written, header never finalised by close()."""
    monkeypatch.setattr(wavio, "HEADER_EVERY_S", 3600)  # header still says 0 bytes of data
    w = wavio.CrashSafeWav(tmp_path / "b.wav", 48000)
    w.write(np.full(4800, 0.25, np.float32))
    w._f.flush()  # what's on disk at the moment of the "crash"
    x, rate = wavio.read_wav(tmp_path / "b.wav")
    assert rate == 48000 and len(x) == 4800
    w.close()


def test_garbage_file_reads_as_empty(tmp_path):
    (tmp_path / "c.wav").write_bytes(b"not a wav")
    x, _ = wavio.read_wav(tmp_path / "c.wav")
    assert x.size == 0
