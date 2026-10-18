"""16-bit mono WAV files that survive crashes.

The RIFF header is rewritten every few seconds, so if the app (or Windows) dies mid-call
the file is valid up to the last few seconds. read_wav() also copes with headers that were
never finalised (e.g. a process killed before close()).
"""

import os
import struct
import time
from pathlib import Path

import numpy as np

HEADER_EVERY_S = 2.0
FSYNC_EVERY_S = 10.0


def _header(rate: int, data_bytes: int) -> bytes:
    return (b"RIFF" + struct.pack("<I", 36 + data_bytes) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", data_bytes))


class CrashSafeWav:
    def __init__(self, path: Path, rate: int):
        self.path, self.rate = path, rate
        self._f = open(path, "wb")
        self._f.write(_header(rate, 0))
        self.data_bytes = 0
        self._last_header = self._last_sync = time.monotonic()

    def write(self, x: np.ndarray) -> None:
        if x.size:
            b = (np.clip(x, -1.0, 1.0) * 32767).astype("<i2").tobytes()
            self._f.write(b)
            self.data_bytes += len(b)
        if time.monotonic() - self._last_header >= HEADER_EVERY_S:
            self._fix_header()

    def write_silence(self, frames: int) -> None:
        if frames > 0:
            self._f.write(b"\x00\x00" * frames)
            self.data_bytes += 2 * frames

    def _fix_header(self, force_sync: bool = False) -> None:
        pos = self._f.tell()
        self._f.seek(0)
        self._f.write(_header(self.rate, self.data_bytes))
        self._f.seek(pos)
        self._f.flush()  # enough to survive an app crash
        now = time.monotonic()
        if force_sync or now - self._last_sync >= FSYNC_EVERY_S:
            # Force to disk (survives a power cut). Not every header rewrite: this runs on the
            # capture thread, and a slow disk must not stall audio draining.
            os.fsync(self._f.fileno())
            self._last_sync = now
        self._last_header = now

    def close(self) -> None:
        if not self._f.closed:
            self._fix_header(force_sync=True)
            self._f.close()

    @property
    def seconds(self) -> float:
        return self.data_bytes / 2 / self.rate


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    """(mono float32 samples, rate). Uses the real file size, not the header's data length."""
    b = Path(path).read_bytes()
    if len(b) < 44 or b[:4] != b"RIFF":
        return np.zeros(0, np.float32), 16000
    channels, rate = struct.unpack("<HI", b[22:28])
    bits = struct.unpack("<H", b[34:36])[0]
    # find the data chunk (usually at 36, but be tolerant)
    i = 12
    data_start = 44
    while i + 8 <= len(b):
        cid, size = b[i:i + 4], struct.unpack("<I", b[i + 4:i + 8])[0]
        if cid == b"data":
            data_start = i + 8
            break
        i += 8 + size + (size & 1)
    if bits != 16:
        raise ValueError(f"{path}: only 16-bit PCM is supported")
    raw = b[data_start:]
    raw = raw[: len(raw) // (2 * channels) * 2 * channels]
    x = np.frombuffer(raw, "<i2").astype(np.float32) / 32768.0
    return x.reshape(-1, channels).mean(axis=1) if channels > 1 else x, rate
