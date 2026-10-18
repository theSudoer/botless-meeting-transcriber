"""Session folder -> transcript.md (Me / Others, timestamped), on the GPU with faster-whisper.

Pipeline (each step comes from testing on real calls, see docs/how-it-works.md):
  1. every raw device track is cut into its sound regions and each region is transcribed on
     its own; Silero VAD over a long mostly-silent file silently drops speech;
  2. "Others" only keeps audio from the output(s) Teams was playing on at that moment, so a
     video on your laptop speakers doesn't end up in the meeting transcript;
  3. "Me" lines that repeat an Others line within a couple of seconds are speaker echo (calls
     on a speaker) and are dropped;
  4. classic Whisper hallucinations are dropped ("You" always, "Thank you." etc. at low confidence);
  5. consecutive lines from the same side are merged into paragraphs.
Audio is deleted afterwards only if the transcript has content and keep_audio is off.
"""

import json
import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path

import numpy as np
from rapidfuzz import fuzz

from .config import Settings
from .wavio import read_wav
from .winaudio import resample_to_16k

log = logging.getLogger(__name__)
RATE = 16000
LABEL = {"me": "Me", "them": "Others"}
HALLUCINATIONS = {"", "you", "thank you", "thank you very much", "thanks for watching",
                  "thank you for watching", "please subscribe", "so", "bye"}
ALWAYS_DROP = {"", "you"}
ECHO_WINDOW_S = 2.5
ECHO_SIMILARITY = 80
TEAMS_SPAN_PAD_S = 3.0
PARAGRAPH_GAP_S = 2.5


# ----------------------------------------------------------------------------- model

def _add_cuda_dll_dirs() -> None:
    """Make the pip-installed nvidia-* DLLs (cuBLAS, cuDNN) visible to CTranslate2."""
    import site

    for sp in site.getsitepackages():
        for bin_dir in (Path(sp) / "nvidia").glob("*/bin"):
            os.add_dll_directory(str(bin_dir))
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ["PATH"]


_add_cuda_dll_dirs()
from faster_whisper import WhisperModel  # noqa: E402

_model_lock = threading.Lock()


# Pinned model revisions (Hugging Face commit hashes), so a changed upstream repo can't silently
# swap the model this app runs. Unknown names fall back to the latest revision.
MODEL_REVISIONS = {
    "large-v3-turbo": ("mobiuslabsgmbh/faster-whisper-large-v3-turbo", "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf"),
}


def load_model(name: str) -> WhisperModel:
    """GPU (float16) if possible, CPU (int8, slower) otherwise. Offline once the model is cached."""
    from faster_whisper.utils import download_model

    repo, revision = MODEL_REVISIONS.get(name, (name, None))
    try:
        path = download_model(repo, local_files_only=True, revision=revision)  # no network if cached
    except Exception:
        log.info("model %s not cached yet: downloading once from Hugging Face (%s)", name, repo)
        path = download_model(repo, revision=revision)
    try:
        return WhisperModel(path, device="cuda", compute_type="float16")
    except Exception:
        log.exception("CUDA unavailable, falling back to CPU (slower)")
        return WhisperModel(path, device="cpu", compute_type="int8")


# ----------------------------------------------------------------------------- regions

def speech_regions(x: np.ndarray, rate: int = RATE, frame_s: float = 0.03, pad_s: float = 0.6,
                   merge_gap_s: float = 1.5, min_len_s: float = 0.2) -> list[tuple[int, int]]:
    """Sample ranges with sound clearly above the track's own noise floor (gap-fill zeros ignored)."""
    n = int(rate * frame_s)
    m = len(x) // n
    if m == 0:
        return []
    frames = x[: m * n].reshape(m, n).astype(np.float64)
    db = 20 * np.log10(np.maximum(np.sqrt(np.mean(frames**2, axis=1)), 1e-6))
    real = np.any(frames != 0, axis=1)
    if not real.any():
        return []
    gate = max(-55.0, float(np.percentile(db[real], 20)) + 10.0)
    active = np.flatnonzero((db > gate) & real)
    if active.size == 0:
        return []
    regions, start, prev = [], active[0], active[0]
    for i in active[1:]:
        if (i - prev) * frame_s > merge_gap_s:
            regions.append((start, prev))
            start = i
        prev = i
    regions.append((start, prev))
    pad = int(pad_s / frame_s)
    return [(max(0, a - pad) * n, min(m, b + 1 + pad) * n) for a, b in regions if (b - a + 1) * frame_s >= min_len_s]


def _run(model: WhisperModel, audio: np.ndarray, vad: bool) -> list:
    segs, _ = model.transcribe(audio, language="en", beam_size=5, vad_filter=vad,
                               vad_parameters={"min_silence_duration_ms": 500},
                               condition_on_previous_text=False)
    return list(segs)


def transcribe_regions(model: WhisperModel, x: np.ndarray, offset: float) -> list[dict]:
    """Transcribe each sound region separately; times are on the session timeline."""
    out = []
    for a, b in speech_regions(x):
        region = x[a:b]
        segs = _run(model, region, vad=True)
        if not segs:  # short replies ("yes") can fall under the VAD: retry, keep only confident text
            segs = [s for s in _run(model, region, vad=False) if s.no_speech_prob < 0.5 and s.avg_logprob > -1.0]
        for s in segs:
            out.append({"start": round(offset + a / RATE + s.start, 2), "end": round(offset + a / RATE + s.end, 2),
                        "text": s.text.strip(), "lp": round(s.avg_logprob, 3), "nsp": round(s.no_speech_prob, 3)})
    return out


# ----------------------------------------------------------------------------- cleanup

def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


def is_hallucination(seg: dict) -> bool:
    n = _norm(seg["text"])
    if n in ALWAYS_DROP:
        return True
    return n in HALLUCINATIONS and seg["lp"] < -0.5


def in_teams_spans(seg: dict, spans: list[list[float]]) -> bool:
    return any(a - TEAMS_SPAN_PAD_S <= seg["start"] <= b + TEAMS_SPAN_PAD_S for a, b in spans)


def drop_echo(me: list[dict], others: list[dict]) -> list[dict]:
    """Remove Me lines that repeat an Others line from just before/after (speaker bleed into the mic)."""
    kept = []
    for m in me:
        echo = any(abs(m["start"] - o["start"]) <= ECHO_WINDOW_S
                   and fuzz.partial_ratio(_norm(m["text"]), _norm(o["text"])) >= ECHO_SIMILARITY
                   for o in others if len(_norm(o["text"])) >= 3)
        if not echo:
            kept.append(m)
    return kept


def dedupe_same_side(segs: list[dict]) -> list[dict]:
    """The same Others line captured on two outputs at once -> keep one."""
    out: list[dict] = []
    for s in sorted(segs, key=lambda s: s["start"]):
        if any(abs(s["start"] - o["start"]) <= 1.5 and fuzz.ratio(_norm(s["text"]), _norm(o["text"])) >= 90
               for o in out[-5:]):
            continue
        out.append(s)
    return out


def paragraphs(lines: list[dict]) -> list[dict]:
    out: list[dict] = []
    for s in sorted(lines, key=lambda s: s["start"]):
        if out and out[-1]["who"] == s["who"] and s["start"] - out[-1]["end"] <= PARAGRAPH_GAP_S:
            out[-1]["text"] += " " + s["text"]
            out[-1]["end"] = max(out[-1]["end"], s["end"])
        else:
            out.append(dict(s))
    return out


def fmt(t: float) -> str:
    h, rem = divmod(int(max(t, 0)), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02}:{m:02}:{s:02}"


def build_lines(me: list[dict], them: list[dict]) -> list[dict]:
    me = [s for s in me if not is_hallucination(s)]
    them = dedupe_same_side([s for s in them if not is_hallucination(s)])
    me = drop_echo(me, them)
    return paragraphs([{**s, "who": "Me"} for s in me] + [{**s, "who": "Others"} for s in them])


def render_markdown(meta: dict, lines: list[dict]) -> str:
    started = datetime.fromisoformat(meta["started"])
    mins = max(1, round((meta.get("duration_s") or 0) / 60))
    me_dev = sorted({t["device"] for t in meta["tracks"] if t["role"] == "me"})
    them_ids = set(meta.get("teams_outputs") or {})
    them_dev = sorted({t["device"] for t in meta["tracks"] if t["role"] == "them"
                       and (not them_ids or t["endpoint_id"] in them_ids)})
    out = [f"# Meeting {started:%d %b %Y, %H:%M} ({mins} min)", "",
           f"_Recorded locally. Me = {', '.join(me_dev) or 'no microphone recorded'}; "
           f"Others = {', '.join(them_dev) or 'no output recorded'}._", ""]
    if not lines:
        out.append("_No speech was detected._")
    for ln in lines:
        out += [f"**[{fmt(ln['start'])}] {ln['who']}:** {ln['text']}", ""]
    return "\n".join(out).rstrip() + "\n"


# ----------------------------------------------------------------------------- entry point

def _track_path(folder: Path, rel: str) -> Path | None:
    """The track's WAV, but only if it really is inside this session's audio folder.

    session.json is just a file on disk; never let it point the app at other files.
    """
    raw_audio = folder / "audio"
    if raw_audio.is_symlink() or raw_audio.is_junction():  # resolve() would follow it elsewhere
        log.warning("refusing an audio folder that is a link: %s", raw_audio)
        return None
    audio_dir = raw_audio.resolve()
    p = (folder / rel).resolve()
    if p.suffix.lower() != ".wav" or not p.is_relative_to(audio_dir):
        log.warning("ignoring track outside the session's audio folder: %r", rel)
        return None
    return p


OWN_WAV = re.compile(r"^\d{2}_(me|them)_[A-Za-z0-9_]*\.wav$")  # the names engine._Track gives its files


def _delete_audio(folder: Path, track_files: list[Path]) -> bool:
    """Delete only WAVs this app recorded, then the audio folder if it's empty.

    That's the tracks listed in session.json plus any file with the app's own track naming
    (a track opened in the last few seconds before a crash may not be listed yet).
    """
    audio_dir = folder / "audio"
    if audio_dir.is_dir() and not (audio_dir.is_symlink() or audio_dir.is_junction()):
        track_files = list(track_files) + [p for p in audio_dir.iterdir()
                                           if p.is_file() and OWN_WAV.match(p.name) and p not in track_files]
    for p in track_files:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            log.warning("could not delete %s (in use?)", p, exc_info=True)
    try:
        audio_dir.rmdir()  # only succeeds if nothing else is in there
    except OSError:
        pass
    return not any(p.exists() for p in track_files)


def transcribe_session(folder: Path, settings: Settings) -> Path:
    folder = Path(folder)
    meta_path = folder / "session.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if meta.get("version") != 1:
        raise ValueError(f"{meta_path} is not a MeetingRecorder session")
    spans_by_ep = meta.get("teams_outputs") or {}
    track_files: list[Path] = []
    with _model_lock:
        model = load_model(settings.model)
        try:
            me, them_all, them = [], [], []
            per_device: dict[str, dict] = {}
            for t in meta["tracks"]:
                path = _track_path(folder, t["file"])
                if path is None or not path.exists():
                    continue
                track_files.append(path)
                x, rate = read_wav(path)
                segs = transcribe_regions(model, resample_to_16k(x, rate), t["start_s"])
                if t["role"] == "me":
                    me += segs
                    continue
                them_all += segs
                # Only what Teams was playing, on the device it was playing on (no Teams seen: keep all).
                kept = segs if not spans_by_ep else [
                    s for s in segs if in_teams_spans(s, spans_by_ep.get(t["endpoint_id"], []))]
                them += kept
                d = per_device.setdefault(t["device"], {"kept": 0, "dropped": 0})
                d["kept"] += len(kept)
                d["dropped"] += len(segs) - len(kept)
        finally:
            del model
    # Safety net: the Teams-device filter must never silently delete the other side.
    filter_overridden = bool(them_all) and not them
    if filter_overridden:
        log.warning("Teams-output filter removed every Others line in %s; keeping all outputs", folder.name)
        them = them_all
    lines = build_lines(me, them)
    out = folder / "transcript.md"
    md = render_markdown(meta, lines)
    if filter_overridden:
        md = md.replace("\n\n", "\n\n_Note: couldn't tell which output Teams used, so sound from every output is "
                        "included (other apps' audio may appear under Others)._\n\n", 1)
    out.write_text(md, encoding="utf-8")
    audio_deleted = False
    if lines and not settings.keep_audio and track_files:
        audio_deleted = _delete_audio(folder, track_files)
    meta["transcript"] = {"status": "done" if lines else "empty", "lines": len(lines), "file": out.name,
                          "finished": datetime.now().isoformat(timespec="seconds"), "audio_deleted": audio_deleted,
                          "others_by_device": per_device, "teams_filter_overridden": filter_overridden}
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    log.info("transcribed %s: %d lines, audio deleted=%s", folder.name, len(lines), audio_deleted)
    return out
