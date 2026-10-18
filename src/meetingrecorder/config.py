"""Settings (JSON in %APPDATA%) and standard paths."""

import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from pathlib import Path

APP_ID = "MeetingRecorder.Desktop"  # Windows AppUserModelID: groups the window under the pinned icon
APP_NAME = "MeetingRecorder"

APPDATA = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
LOCALDATA = Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME
LOG_DIR = LOCALDATA / "logs"
SETTINGS_FILE = APPDATA / "settings.json"
ICON_FILE = LOCALDATA / "mic-icon-v2.ico"  # new name whenever the icon changes: Windows caches icons by path

log = logging.getLogger(__name__)


def _default_transcripts_dir() -> str:
    return str(Path.home() / "Documents" / "MeetingTranscripts")


@dataclass
class Settings:
    transcripts_dir: str = ""
    prompt_on_call: bool = True  # "Teams call detected - record?" prompt
    auto_stop: bool = True  # stop recording once Teams has released the mic for auto_stop_s
    auto_stop_s: int = 30
    keep_audio: bool = False  # delete the WAVs once a non-empty transcript is written
    model: str = "large-v3-turbo"

    def __post_init__(self):
        if not self.transcripts_dir:
            self.transcripts_dir = _default_transcripts_dir()

    @property
    def transcripts_path(self) -> Path:
        return Path(self.transcripts_dir)


def load_settings() -> Settings:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        known = {f.name for f in fields(Settings)}
        return Settings(**{k: v for k, v in data.items() if k in known})
    except FileNotFoundError:
        return Settings()
    except Exception:
        log.exception("settings file unreadable, using defaults")
        return Settings()


def save_settings(s: Settings) -> None:
    APPDATA.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(asdict(s), indent=2), encoding="utf-8")
