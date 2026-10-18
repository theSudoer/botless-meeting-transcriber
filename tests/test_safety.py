"""session.json is untrusted input: it must not be able to point the app at other files."""

import json

import pytest

from meetingrecorder.config import Settings
from meetingrecorder.transcriber import _delete_audio, _track_path, transcribe_session


def make_session(tmp_path):
    folder = tmp_path / "2026-01-15_1000"
    (folder / "audio").mkdir(parents=True)
    return folder


def test_track_inside_audio_folder_is_allowed(tmp_path):
    folder = make_session(tmp_path)
    assert _track_path(folder, "audio/00_me_Mic.wav") == (folder / "audio" / "00_me_Mic.wav").resolve()


@pytest.mark.parametrize("rel", ["../../secret.wav", "audio/../../secret.wav", "C:/Windows/Media/chimes.wav",
                                 "session.json", "audio/notes.txt"])
def test_traversal_and_non_wav_are_rejected(tmp_path, rel):
    assert _track_path(make_session(tmp_path), rel) is None


def test_delete_audio_only_touches_listed_files(tmp_path):
    folder = make_session(tmp_path)
    ours = folder / "audio" / "00_me_Mic.wav"
    other = folder / "audio" / "keep-me.wav"
    ours.write_bytes(b"x")
    other.write_bytes(b"x")
    assert _delete_audio(folder, [ours]) is True
    assert not ours.exists() and other.exists()  # the folder stays because it isn't empty


def test_delete_audio_also_removes_unlisted_track_from_a_crash(tmp_path):
    folder = make_session(tmp_path)
    listed = folder / "audio" / "00_them_Speakers.wav"
    unlisted = folder / "audio" / "01_me_Headset_Mic.wav"  # opened seconds before a crash, not in session.json
    for p in (listed, unlisted):
        p.write_bytes(b"x")
    assert _delete_audio(folder, [listed]) is True
    assert not (folder / "audio").exists()


def test_foreign_session_json_is_refused(tmp_path):
    folder = make_session(tmp_path)
    (folder / "session.json").write_text(json.dumps({"tracks": []}))  # no version: not ours
    with pytest.raises(ValueError):
        transcribe_session(folder, Settings(transcripts_dir=str(tmp_path)))
