"""Recording engine: Teams call detection, device following, capture, health alarms.

Threads (none of them is the Qt thread; the UI polls Engine.status()):
  scanner    every 1 s: which devices exist, which ones Teams uses, call start/end,
             what should be captured, alarms, auto-stop. Never touches capture streams.
  capture    every 10 ms while recording: opens/closes tracks to match the scanner's wish
             list, drains audio into crash-safe WAVs. Never blocked by slow scans.
  transcribe one job at a time, held while a Teams call is active, low thread priority.

Safety rules (see docs/how-it-works.md): shared-mode capture only, never plays audio, never changes
device settings, and only opens a microphone while another app (Teams) is actively using it,
releasing it within ~1 s of that app letting go.
"""

import copy
import ctypes
import json
import logging
import os
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from . import winaudio as wa
from .config import Settings
from .wavio import CrashSafeWav

log = logging.getLogger(__name__)

SCAN_S = 1.0
DRAIN_S = 0.01
RECONCILE_S = 0.25
RETRY_OPEN_S = 3.0
STALE_SCAN_S = 3.0  # wish list older than this: release microphones (fail closed if the scanner hangs)
META_EVERY_S = 5.0
CALL_ON_SCANS = 2  # Teams must hold a mic for 2 scans before we call it a call (filters blips)
ALARM_THEM_S = 6  # Teams audibly playing but our capture hears nothing for this long
ALARM_YOU_S = 10  # in a call but no mic track open for this long


@dataclass
class Status:
    call_active: bool = False
    call_mic: list[str] = field(default_factory=list)  # Teams' mic(s)
    call_outputs: list[str] = field(default_factory=list)  # where Teams plays
    recording: bool = False
    rec_started: float | None = None  # time.time()
    rec_folder: str | None = None
    rec_saw_call: bool = False
    them_db: float = -120.0
    you_db: float = -120.0
    them_devices: list[str] = field(default_factory=list)
    you_devices: list[str] = field(default_factory=list)
    alarms: list[str] = field(default_factory=list)
    transcribing: str | None = None
    queued: int = 0
    transcripts_version: int = 0  # bumps whenever a transcript appears/changes
    last_error: str | None = None


def _peak_db(p: float) -> float:
    return float(20 * np.log10(max(p, 1e-6)))


def _set_thread_priority_below_normal() -> None:
    try:
        k32 = ctypes.windll.kernel32
        k32.SetThreadPriority(k32.GetCurrentThread(), -1)  # THREAD_PRIORITY_BELOW_NORMAL
    except Exception:
        log.debug("could not lower the transcription thread's priority", exc_info=True)


class _Track:
    def __init__(self, ep: wa.Endpoint, audio_dir: Path, t0: float, index: int):
        self.ep = ep
        self.role = "them" if ep.flow == "render" else "me"
        self.stream = wa.WasapiStream(ep)
        safe = re.sub(r"[^A-Za-z0-9]+", "_", ep.name).strip("_")[:40]
        self.path = audio_dir / f"{index:02d}_{self.role}_{safe}.wav"
        self.wav = CrashSafeWav(self.path, self.stream.rate)
        self.t0 = t0
        self.closed_at: float | None = None
        self.closed_reason: str | None = None
        self._recent: list[np.ndarray] = []
        self.stream.start()

    def drain(self) -> None:
        x = self.stream.read()
        if x.size:
            self.wav.write(x)
            self._recent.append(x)

    def take_level(self) -> float:
        lvl = wa.dbfs(np.concatenate(self._recent)) if self._recent else -120.0
        self._recent = []
        return lvl

    def close(self, reason: str) -> None:
        if self.closed_reason:
            return
        now = time.perf_counter()
        self.drain()
        self.stream.stop()
        self.wav.write_silence(self.stream.pad_to(now))
        self.wav.close()
        self.closed_at, self.closed_reason = now, reason

    def info(self) -> dict:
        s = self.stream
        return {
            "file": f"audio/{self.path.name}", "role": self.role, "device": self.ep.name,
            "endpoint_id": self.ep.id, "bluetooth": self.ep.is_bluetooth, "rate": s.rate,
            "start_s": round(s.start_t - self.t0, 4), "duration_s": round(s.frames / s.rate, 3),
            "gap_filled_s": round(s.gap_frames / s.rate, 3),
            "closed_at_s": round(self.closed_at - self.t0, 3) if self.closed_at else None,
            "closed_reason": self.closed_reason or (f"device error {s.error}" if s.error else None),
        }


class Engine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.RLock()
        self._status = Status()
        self._shutdown = threading.Event()
        self._desired: dict[str, wa.Endpoint] = {}  # scanner -> capture
        self._desired_at = 0.0  # time.monotonic() of the last successful scan
        self._levels: dict[str, tuple[str, float]] = {}  # capture -> scanner: endpoint id -> (role, dB)
        self._open_ids: set[str] = set()
        self._capture_thread: threading.Thread | None = None
        self._capture_stop = threading.Event()
        self._session: dict | None = None
        self._jobs: queue.Queue[Path] = queue.Queue()
        self._scanner = threading.Thread(target=self._scan_loop, name="scanner", daemon=True)
        self._transcriber = threading.Thread(target=self._transcribe_loop, name="transcriber", daemon=True)

    # ------------------------------------------------------------------ public API (any thread)

    def start(self) -> None:
        self._recover_interrupted()
        self._scanner.start()
        self._transcriber.start()

    def _recover_interrupted(self) -> None:
        """Sessions still marked 'recording' were cut off (crash, power loss): finish them.

        The WAVs are crash-safe and session.json is rewritten every few seconds, so
        everything up to the last few seconds is transcribed.
        """
        base = self.settings.transcripts_path
        for meta_path in base.glob("*/session.json") if base.exists() else []:
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if meta.get("version") != 1:
                    continue  # not one of ours
                if meta.get("status") == "recording":  # cut off mid-call
                    meta["status"] = "interrupted"
                    meta["duration_s"] = max((t["start_s"] + t["duration_s"] for t in meta.get("tracks", [])),
                                             default=0.0)
                    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
                elif "transcript" in meta or not (meta_path.parent / "audio").exists():
                    continue  # done, or nothing left to transcribe
                # else: stopped but never transcribed (e.g. the app was quit right after recording)
                log.warning("recovering untranscribed session %s", meta_path.parent.name)
                self.transcribe(meta_path.parent)
            except Exception:
                log.exception("could not recover %s", meta_path)

    def shutdown(self) -> None:
        self.stop_recording("app closing")
        self._shutdown.set()

    def status(self) -> Status:
        with self._lock:
            return copy.deepcopy(self._status)

    def start_recording(self, reason: str = "manual") -> None:
        with self._lock:
            if self._status.recording:
                return
            base = self.settings.transcripts_path
            name = datetime.now().strftime("%Y-%m-%d_%H%M")
            folder = base / name
            n = 2
            while folder.exists():
                folder, n = base / f"{name}_{n}", n + 1
            (folder / "audio").mkdir(parents=True)
            self._session = {
                "version": 1, "started": datetime.now().isoformat(timespec="seconds"), "status": "recording",
                "start_reason": reason, "tracks": [], "teams_outputs": {}, "events": [],
            }
            self._capture_stop.clear()
            self._status.recording = True
            self._status.rec_started = time.time()
            self._status.rec_folder = str(folder)
            self._status.rec_saw_call = self._status.call_active
            self._capture_thread = threading.Thread(target=self._capture_loop, args=(folder,), name="capture",
                                                    daemon=True)
            self._capture_thread.start()
        log.info("recording started (%s) -> %s", reason, folder)

    def stop_recording(self, reason: str = "manual") -> None:
        with self._lock:
            if not self._status.recording:
                return
            self._session["stop_reason"] = reason
            self._capture_stop.set()
            thread = self._capture_thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=15)
        log.info("recording stopped (%s)", reason)

    def transcribe(self, folder: Path) -> None:
        """Queue a session folder for (re)transcription."""
        self._jobs.put(Path(folder))
        with self._lock:
            self._status.queued = self._jobs.qsize()

    # ------------------------------------------------------------------ scanner

    def _scan_loop(self) -> None:
        wa.com_thread_init()
        call_scans = 0
        last_teams_mic = 0.0
        them_alarm_s = you_alarm_s = 0
        while not self._shutdown.is_set():
            t = time.monotonic()
            try:
                teams_mics, other_mics, teams_outs, outs = [], [], [], []
                desired = {}  # what to capture (only used while recording)
                for ep in wa.list_endpoints():
                    active = [s for s in wa.sessions(ep) if s.state == "Active" and s.pid != 0]
                    teams_active = [s for s in active if s.is_teams]
                    if ep.flow == "capture":
                        if teams_active:
                            teams_mics.append((ep, max(s.peak for s in teams_active)))
                        elif active:
                            other_mics.append(ep)
                    else:
                        outs.append(ep)
                        if teams_active:
                            teams_outs.append((ep, max(s.peak for s in teams_active)))
                        # Outputs: passive loopback, except a legacy hands-free output, which is
                        # only touched while some other app is already using it.
                        if not ep.is_call_mode or active:
                            desired[ep.id] = ep
                # Mic: only one another app is actively using (Teams' wins). Never one nobody uses,
                # so we can never push a Bluetooth headset into call mode ourselves.
                for ep in [ep for ep, _ in teams_mics] or other_mics:
                    desired[ep.id] = ep
                raw_call = bool(teams_mics)
                call_scans = call_scans + 1 if raw_call else 0
                if raw_call:
                    last_teams_mic = t

                with self._lock:
                    st = self._status
                    was_call = st.call_active
                    st.call_active = call_scans >= CALL_ON_SCANS or (was_call and raw_call)
                    st.call_mic = [ep.name for ep, _ in teams_mics]
                    st.call_outputs = [ep.name for ep, _ in teams_outs]
                    self._desired = desired
                    self._desired_at = time.monotonic()
                    if st.recording:
                        if st.call_active:
                            st.rec_saw_call = True
                        rel = time.perf_counter() - self._session["t0"] if "t0" in self._session else None
                        if rel is not None:
                            for ep, _ in teams_outs:
                                spans = self._session["teams_outputs"].setdefault(ep.id, [])
                                if spans and rel - spans[-1][1] <= SCAN_S * 2.5:
                                    spans[-1][1] = round(rel, 1)
                                else:
                                    spans.append([round(rel, 1), round(rel, 1)])
                        levels = dict(self._levels)
                        them = [db for eid, (role, db) in levels.items() if role == "them"
                                and (not teams_outs or eid in {ep.id for ep, _ in teams_outs})]
                        you = [db for role, db in levels.values() if role == "me"]
                        st.them_db = max(them, default=-120.0)
                        st.you_db = max(you, default=-120.0)
                        st.them_devices = [ep.name for ep, _ in teams_outs] or [
                            desired[eid].name for eid in levels if levels[eid][0] == "them" and eid in desired]
                        st.you_devices = [desired[eid].name for eid in levels if levels[eid][0] == "me"
                                          and eid in desired]
                        # Alarms: only during a call, only when something is clearly wrong.
                        teams_loud = any(p > 0.05 for _, p in teams_outs)
                        them_alarm_s = them_alarm_s + 1 if (st.call_active and teams_loud and st.them_db < -70) else 0
                        you_alarm_s = you_alarm_s + 1 if (st.call_active and not you) else 0
                        alarms = []
                        if them_alarm_s >= ALARM_THEM_S:
                            alarms.append("Teams is playing sound but the recorder hears nothing from it.")
                        if you_alarm_s >= ALARM_YOU_S:
                            alarms.append("You're in a call but your microphone isn't being recorded.")
                        st.alarms = alarms
                        auto_stop = (self.settings.auto_stop and st.rec_saw_call and not raw_call
                                     and t - last_teams_mic >= self.settings.auto_stop_s)
                    else:
                        # Idle meters: passive readings only (no streams opened).
                        st.them_db = _peak_db(max((p for _, p in teams_outs), default=max(
                            (wa.endpoint_peak(ep) for ep in outs), default=0.0)))
                        st.you_db = _peak_db(max((p for _, p in teams_mics), default=0.0))
                        st.them_devices = [ep.name for ep, _ in teams_outs]
                        st.you_devices = [ep.name for ep, _ in teams_mics]
                        st.alarms = []
                        them_alarm_s = you_alarm_s = 0
                        auto_stop = False
                if auto_stop:
                    self.stop_recording(f"Teams call ended ({self.settings.auto_stop_s}s after the mic was released)")
            except Exception:
                # Fail closed for microphones: if we can't confirm another app is using a mic,
                # we must not keep it open. Outputs (passive loopback) can stay.
                log.exception("scan failed; releasing microphones until the next good scan")
                with self._lock:
                    self._desired = {k: ep for k, ep in self._desired.items() if ep.flow == "render"}
            self._shutdown.wait(max(0.0, SCAN_S - (time.monotonic() - t)))

    # ------------------------------------------------------------------ capture

    def _event(self, msg: str) -> None:
        rel = time.perf_counter() - self._session.get("t0", time.perf_counter())
        self._session["events"].append(f"[{rel:8.1f}s] {msg}")
        log.info("rec: %s", msg)

    def _write_meta(self, folder: Path, tracks: list[_Track]) -> None:
        with self._lock:
            meta = {k: v for k, v in self._session.items() if k != "t0"}
        meta["tracks"] = [t.info() for t in tracks]
        tmp = folder / "session.json.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(meta, indent=2))
            f.flush()
            os.fsync(f.fileno())  # survive power loss, not just an app crash
        tmp.replace(folder / "session.json")

    def _capture_loop(self, folder: Path) -> None:
        wa.com_thread_init()
        t0 = time.perf_counter()
        with self._lock:
            self._session["t0"] = t0
        audio_dir = folder / "audio"
        open_tracks: dict[str, _Track] = {}
        all_tracks: list[_Track] = []
        failed: dict[str, float] = {}
        last_reconcile = last_meta = last_level = 0.0
        try:
            while not self._capture_stop.is_set():
                now = time.perf_counter()
                if now - last_reconcile >= RECONCILE_S:
                    last_reconcile = now
                    with self._lock:
                        desired = dict(self._desired)
                        stale = time.monotonic() - self._desired_at > STALE_SCAN_S
                    if stale:  # scanner stalled: we can no longer confirm anyone else uses a mic
                        desired = {k: ep for k, ep in desired.items() if ep.flow == "render"}
                    for eid, tr in list(open_tracks.items()):
                        if tr.stream.invalidated:
                            tr.close(f"device error {tr.stream.error}")
                            self._event(f"lost '{tr.ep.name}' ({tr.stream.error}); will reopen if it comes back")
                            del open_tracks[eid]
                        elif eid not in desired:
                            tr.close("released")
                            self._event(f"released '{tr.ep.name}'")
                            del open_tracks[eid]
                    for eid, ep in desired.items():
                        if eid in open_tracks or now < failed.get(eid, 0.0):
                            continue
                        # The wish list can be up to a second old: re-confirm right before opening a
                        # mic that another app is still actively using it (the piggyback rule).
                        if ep.flow == "capture" and not any(
                                s.state == "Active" and s.pid != 0 for s in wa.sessions(ep)):
                            continue
                        try:
                            tr = _Track(ep, audio_dir, t0, len(all_tracks))
                        except Exception as e:
                            failed[eid] = now + RETRY_OPEN_S
                            self._event(f"could not open '{ep.name}': {e}")
                            continue
                        open_tracks[eid] = tr
                        all_tracks.append(tr)
                        self._event(f"recording {tr.role} from '{ep.name}' ({tr.stream.rate} Hz)")
                    with self._lock:
                        self._open_ids = set(open_tracks)
                for tr in list(open_tracks.values()):
                    tr.drain()
                if now - last_level >= SCAN_S:
                    last_level = now
                    levels = {eid: (tr.role, tr.take_level()) for eid, tr in open_tracks.items()}
                    with self._lock:
                        self._levels = levels
                if now - last_meta >= META_EVERY_S:
                    last_meta = now
                    self._write_meta(folder, all_tracks)
                time.sleep(DRAIN_S)
        except Exception as e:
            log.exception("capture loop crashed")
            with self._lock:
                self._status.last_error = f"Recording error: {e}"
        finally:
            for tr in open_tracks.values():
                try:
                    tr.close("recording stopped")
                except Exception:
                    log.exception("closing track failed")
            with self._lock:
                self._session["status"] = "stopped"
                self._session["stopped"] = datetime.now().isoformat(timespec="seconds")
                self._session["duration_s"] = round(time.perf_counter() - t0, 1)
                self._levels, self._open_ids = {}, set()
            self._write_meta(folder, all_tracks)
            with self._lock:
                st = self._status
                st.recording = False
                st.rec_started = st.rec_folder = None
                st.alarms = []
                st.transcripts_version += 1
            self.transcribe(folder)

    # ------------------------------------------------------------------ transcription

    def _transcribe_loop(self) -> None:
        _set_thread_priority_below_normal()
        while not self._shutdown.is_set():
            try:
                folder = self._jobs.get(timeout=1.0)
            except queue.Empty:
                continue
            # Hold heavy GPU work until the call is over (and we're not recording).
            while not self._shutdown.is_set():
                st = self.status()
                if not st.call_active and not st.recording:
                    break
                with self._lock:
                    self._status.transcribing = f"{folder.name} (waiting for the call to end)"
                time.sleep(5)
            with self._lock:
                self._status.transcribing = folder.name
                self._status.queued = self._jobs.qsize()
            try:
                from .transcriber import transcribe_session  # heavy import (CUDA), only when needed

                transcribe_session(folder, self.settings)
            except Exception as e:
                log.exception("transcription failed for %s", folder)
                with self._lock:
                    self._status.last_error = f"Transcription failed for {folder.name}: {e}"
            finally:
                with self._lock:
                    self._status.transcribing = None
                    self._status.queued = self._jobs.qsize()
                    self._status.transcripts_version += 1
