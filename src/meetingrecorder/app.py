"""The desktop app: main window, tray icon, "record this call?" prompt, transcripts list.

The UI never touches audio objects; it polls Engine.status() four times a second.
"""

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QByteArray, QMimeData, QPoint, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QGuiApplication, QIcon, QPainter, QPen, QPixmap
from PySide6.QtNetwork import QLocalServer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from . import installer
from .config import APP_NAME, ICON_FILE, Settings, save_settings
from .engine import Engine, Status

log = logging.getLogger(__name__)

COLORS = {"idle": "#6b7280", "call": "#2563eb", "recording": "#dc2626", "transcribing": "#d97706"}


ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def _icon_pixmap(state: str, size: int) -> QPixmap:
    """A round badge with a white studio microphone, drawn on a 64-unit grid.

    Strokes get relatively thicker at small sizes so the mic still reads at 16-24 px.
    """
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / 64, size / 64)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(COLORS.get(state, COLORS["idle"])))
    p.drawEllipse(QRectF(1, 1, 62, 62))
    white = QColor("white")
    small = size <= 24
    p.setBrush(white)
    if small:  # bolder, simpler glyph
        p.drawRoundedRect(QRectF(22, 8, 20, 30), 10, 10)
        stroke = 7.0
    else:
        p.drawRoundedRect(QRectF(24, 9, 16, 27), 8, 8)
        stroke = 4.5 if size >= 48 else 5.5
    pen = QPen(white, stroke, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawArc(QRectF(16, 16, 32, 30), 180 * 16, 180 * 16)  # cradle: bottom half of an ellipse
    p.drawLine(QPointF(32, 46), QPointF(32, 53))  # stand
    p.drawLine(QPointF(23, 54), QPointF(41, 54))  # base
    p.end()
    return pm


def make_icon(state: str = "idle", size: int = 64) -> QIcon:
    """App/tray icon (all sizes, so Windows never has to scale a big one down); colour = state."""
    icon = QIcon()
    for s in sorted(set(ICON_SIZES) | {size}):
        icon.addPixmap(_icon_pixmap(state, s))
    return icon


def save_icon_file() -> Path:
    """Write a multi-size .ico (PNG-compressed entries) for the shortcuts and the taskbar pin."""
    import struct

    from PySide6.QtCore import QBuffer, QByteArray, QIODevice

    images = []
    for s in ICON_SIZES:
        data = QByteArray()
        buf = QBuffer(data)
        buf.open(QIODevice.WriteOnly)
        _icon_pixmap("recording", s).save(buf, "PNG")
        images.append((s, bytes(data)))
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for s, png in images:
        dim = 0 if s >= 256 else s  # 0 means 256 in the ICO format
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset + len(blobs))
        blobs += png
    ICON_FILE.parent.mkdir(parents=True, exist_ok=True)
    ICON_FILE.write_bytes(header + entries + blobs)
    return ICON_FILE


def db_to_meter(db: float) -> int:
    return int(max(0, min(60, db + 60)))


def fmt_elapsed(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m:02}:{s:02}"


# ----------------------------------------------------------------------------- dialogs

class CallPrompt(QWidget):
    """Small always-on-top card: 'Teams call detected - record it?'."""

    def __init__(self, on_record, on_dismiss):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setStyleSheet("QWidget#card { background: #111827; border-radius: 10px; }"
                           "QLabel { color: white; } QPushButton { padding: 6px 14px; }")
        card = QWidget(self)
        card.setObjectName("card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(card)
        lay = QVBoxLayout(card)
        title = QLabel("Teams call detected")
        title.setFont(QFont("Segoe UI", 11, QFont.Bold))
        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        row = QHBoxLayout()
        rec = QPushButton("● Record this call")
        rec.setStyleSheet("background: #dc2626; color: white; font-weight: bold;")
        rec.clicked.connect(on_record)
        no = QPushButton("Not this time")
        no.clicked.connect(on_dismiss)
        row.addWidget(rec)
        row.addWidget(no)
        lay.addWidget(title)
        lay.addWidget(self.detail)
        lay.addLayout(row)
        self.setFixedWidth(340)

    def show_for(self, st: Status) -> None:
        self.detail.setText(f"Mic: {', '.join(st.call_mic) or '?'}\nSpeaker: {', '.join(st.call_outputs) or '?'}")
        self.adjustSize()
        geo = QGuiApplication.primaryScreen().availableGeometry()
        self.move(geo.bottomRight() - QPoint(self.width() + 16, self.height() + 16))
        self.show()
        self.raise_()


class SettingsDialog(QDialog):
    def __init__(self, parent, settings: Settings):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.s = settings
        form = QFormLayout(self)
        self.prompt = QCheckBox("Ask to record when a Teams call starts")
        self.prompt.setChecked(settings.prompt_on_call)
        self.auto_stop = QCheckBox("Stop recording when the Teams call ends")
        self.auto_stop.setChecked(settings.auto_stop)
        self.delay = QSpinBox()
        self.delay.setRange(5, 600)
        self.delay.setSuffix(" s after the call ends")
        self.delay.setValue(settings.auto_stop_s)
        self.keep = QCheckBox("Keep the audio files after the transcript is made")
        self.keep.setChecked(settings.keep_audio)
        self.startup = QCheckBox("Start MeetingRecorder when I sign in to Windows")
        self.startup.setChecked(installer.startup_enabled())
        folder_row = QHBoxLayout()
        self.folder = QLineEdit(settings.transcripts_dir)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        folder_row.addWidget(self.folder)
        folder_row.addWidget(browse)
        form.addRow(self.prompt)
        form.addRow(self.auto_stop)
        form.addRow("   Delay:", self.delay)
        form.addRow(self.keep)
        form.addRow(self.startup)
        form.addRow("Transcripts folder:", folder_row)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Transcripts folder", self.folder.text())
        if d:
            self.folder.setText(d)

    def apply(self) -> None:
        s = self.s
        s.prompt_on_call = self.prompt.isChecked()
        s.auto_stop = self.auto_stop.isChecked()
        s.auto_stop_s = self.delay.value()
        s.keep_audio = self.keep.isChecked()
        s.transcripts_dir = self.folder.text().strip() or s.transcripts_dir
        save_settings(s)
        try:
            installer.set_startup(self.startup.isChecked())
        except Exception:
            log.exception("could not change the startup shortcut")


# ----------------------------------------------------------------------------- main window

class MainWindow(QMainWindow):
    def __init__(self, engine: Engine, settings: Settings, server: QLocalServer):
        super().__init__()
        self.engine, self.settings, self.server = engine, settings, server
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(make_icon("recording"))
        self.resize(520, 640)
        self._last_state = None
        self._prev_call = False
        self._transcripts_version = -1
        self._known_done: set[str] | None = None
        self._quitting = False
        self._told_tray = False

        root = QWidget()
        lay = QVBoxLayout(root)
        lay.setSpacing(10)

        self.status_label = QLabel("Starting…")
        self.status_label.setFont(QFont("Segoe UI", 16, QFont.Bold))
        self.detail_label = QLabel("")
        self.detail_label.setWordWrap(True)
        self.detail_label.setStyleSheet("color: #6b7280;")
        lay.addWidget(self.status_label)
        lay.addWidget(self.detail_label)

        self.record_btn = QPushButton("● Start recording")
        self.record_btn.setMinimumHeight(48)
        self.record_btn.setFont(QFont("Segoe UI", 12, QFont.Bold))
        self.record_btn.clicked.connect(self._toggle_recording)
        lay.addWidget(self.record_btn)

        self.alarm = QLabel("")
        self.alarm.setWordWrap(True)
        self.alarm.setStyleSheet("background: #fee2e2; color: #991b1b; padding: 8px; border-radius: 6px;"
                                 "font-weight: bold;")
        self.alarm.hide()
        lay.addWidget(self.alarm)

        check = QGroupBox("Audio check")
        grid = QGridLayout(check)
        self.them_meter, self.you_meter = QProgressBar(), QProgressBar()
        self.them_dev, self.you_dev = QLabel(""), QLabel("")
        for i, (label, meter, dev) in enumerate((("Hearing them", self.them_meter, self.them_dev),
                                                ("Hearing you", self.you_meter, self.you_dev))):
            meter.setRange(0, 60)
            meter.setTextVisible(False)
            meter.setFixedHeight(14)
            dev.setStyleSheet("color: #6b7280;")
            dev.setWordWrap(True)
            grid.addWidget(QLabel(label), i * 2, 0)
            grid.addWidget(meter, i * 2, 1)
            grid.addWidget(dev, i * 2 + 1, 1)
        hint = QLabel("To test it any time: in Teams open Settings → Devices → <b>Make a test call</b>. "
                      "It's recorded and transcribed like a real meeting.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #6b7280;")
        grid.addWidget(hint, 4, 0, 1, 2)
        lay.addWidget(check)

        box = QGroupBox("Transcripts")
        bl = QVBoxLayout(box)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _: self._open())
        self.list.currentItemChanged.connect(lambda *_: self._update_buttons())
        bl.addWidget(self.list)
        row = QHBoxLayout()
        self.copy_btn = QPushButton("Copy transcript")
        self.copy_btn.clicked.connect(self._copy)
        self.open_btn = QPushButton("Open")
        self.open_btn.clicked.connect(self._open)
        self.folder_btn = QPushButton("Show in folder")
        self.folder_btn.clicked.connect(self._folder)
        self.again_btn = QPushButton("Transcribe again")
        self.again_btn.clicked.connect(self._again)
        for b in (self.copy_btn, self.open_btn, self.folder_btn, self.again_btn):
            row.addWidget(b)
        bl.addLayout(row)
        lay.addWidget(box, 1)

        footer = QHBoxLayout()
        self.note = QLabel("")
        self.note.setStyleSheet("color: #6b7280;")
        self.note.setWordWrap(True)
        settings_btn = QPushButton("Settings")
        settings_btn.clicked.connect(self._settings)
        footer.addWidget(self.note, 1)
        footer.addWidget(settings_btn)
        lay.addLayout(footer)
        self.setCentralWidget(root)

        self.prompt = CallPrompt(self._record_from_prompt, self._dismiss_prompt)

        self.tray = QSystemTrayIcon(make_icon("idle"), self)
        menu = QMenu()
        menu.addAction("Open MeetingRecorder", self.bring_to_front)
        self.tray_rec = QAction("Start recording", self)
        self.tray_rec.triggered.connect(self._toggle_recording)
        menu.addAction(self.tray_rec)
        menu.addSeparator()
        menu.addAction("Quit", self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.bring_to_front()
                                    if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
        self.tray.show()

        server.newConnection.connect(self._other_instance)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(250)
        self.refresh()

    # ------------------------------------------------------------------ status

    def refresh(self) -> None:
        st = self.engine.status()
        if st.recording:
            state = "recording"
            title = f"● Recording  {fmt_elapsed(time.time() - (st.rec_started or time.time()))}"
            detail = ("Will stop on its own when the Teams call ends." if self.settings.auto_stop and st.rec_saw_call
                      else "Press Stop when you're done.")
            self.record_btn.setText("■ Stop recording")
            self.record_btn.setStyleSheet("background: #111827; color: white;")
        else:
            state = "transcribing" if st.transcribing else ("call" if st.call_active else "idle")
            title = {"transcribing": "Writing the transcript…", "call": "Teams call in progress",
                     "idle": "Ready"}[state]
            detail = (st.transcribing if st.transcribing else
                      "Not recording." if st.call_active else
                      "Waiting for a Teams call. Nothing is being recorded.")
            self.record_btn.setText("● Start recording")
            self.record_btn.setStyleSheet("background: #dc2626; color: white;")
        self.status_label.setText(title)
        self.detail_label.setText(detail)
        self.tray_rec.setText("Stop recording" if st.recording else "Start recording")
        if state != self._last_state:
            self.tray.setIcon(make_icon(state))
            self.tray.setToolTip(f"{APP_NAME}: {title}")
            self._last_state = state

        self.them_meter.setValue(db_to_meter(st.them_db))
        self.you_meter.setValue(db_to_meter(st.you_db))
        self.them_dev.setText(", ".join(st.them_devices) or (
            "Any sound playing on this PC" if not st.call_active else "Teams isn't playing yet"))
        self.you_dev.setText(", ".join(st.you_devices) or ("Only recorded while Teams is using your mic"
                                                          if not st.call_active else "waiting for Teams' mic"))
        alarms = st.alarms + ([st.last_error] if st.last_error else [])
        self.alarm.setText("⚠ " + "\n⚠ ".join(alarms) if alarms else "")
        self.alarm.setVisible(bool(alarms))

        # Call prompt
        if st.call_active and not self._prev_call:
            if not st.recording and self.settings.prompt_on_call:
                self.prompt.show_for(st)
                self.tray.showMessage("Teams call detected", "Open MeetingRecorder to record it.",
                                      make_icon("call"), 8000)
                QApplication.alert(self, 0)
        if (not st.call_active or st.recording) and self.prompt.isVisible():
            self.prompt.hide()
        self._prev_call = st.call_active

        if st.transcripts_version != self._transcripts_version:
            self._transcripts_version = st.transcripts_version
            self.load_transcripts()
        self.note.setText("Saved in: " + self.settings.transcripts_dir)

    # ------------------------------------------------------------------ transcripts

    def load_transcripts(self) -> None:
        current = self._selected()
        self.list.clear()
        base = self.settings.transcripts_path
        folders = sorted((p for p in base.glob("*") if (p / "session.json").exists()), reverse=True) \
            if base.exists() else []
        done_now = set()
        for f in folders[:200]:
            try:
                meta = json.loads((f / "session.json").read_text(encoding="utf-8"))
            except Exception:
                log.warning("skipping unreadable session %s", f, exc_info=True)
                continue
            started = datetime.fromisoformat(meta.get("started", "2000-01-01T00:00:00"))
            mins = max(1, round((meta.get("duration_s") or 0) / 60))
            tr = meta.get("transcript") or {}
            if meta.get("status") == "recording":
                state = "recording…"
            elif tr.get("status") == "done":
                n = tr.get("lines", 0)
                state = f"{n} line{'s' if n != 1 else ''}"
                done_now.add(str(f))
            elif tr.get("status") == "empty":
                state = "no speech detected"
            else:
                state = "transcribing…"
            item = QListWidgetItem(f"{started:%a %d %b %Y, %H:%M}   ·   {mins} min   ·   {state}")
            item.setData(Qt.UserRole, str(f))
            self.list.addItem(item)
            if str(f) == current:
                self.list.setCurrentItem(item)
        if self._known_done is not None:
            for new in done_now - self._known_done:
                self.tray.showMessage("Transcript ready", f"{Path(new).name}: click 'Copy transcript' to paste it "
                                      "into your AI.", make_icon("idle"), 8000)
        self._known_done = done_now
        if self.list.currentRow() < 0 and self.list.count():
            self.list.setCurrentRow(0)
        self._update_buttons()

    def _selected(self) -> str | None:
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _update_buttons(self) -> None:
        f = self._selected()
        has_tr = bool(f) and (Path(f) / "transcript.md").exists()
        for b in (self.copy_btn, self.open_btn):
            b.setEnabled(has_tr)
        self.folder_btn.setEnabled(bool(f))
        self.again_btn.setEnabled(bool(f) and (Path(f) / "audio").exists())

    def _copy(self) -> None:
        f = self._selected()
        if f:
            mime = QMimeData()
            mime.setText((Path(f) / "transcript.md").read_text(encoding="utf-8"))
            # Ask Windows to keep meeting text out of clipboard history and cloud clipboard sync
            # (Win+V history would otherwise keep it, and sync could copy it off this PC).
            no = QByteArray(b"\x00\x00\x00\x00")
            for fmt in ("CanIncludeInClipboardHistory", "CanUploadToCloudClipboard"):
                mime.setData(f'application/x-qt-windows-mime;value="{fmt}"', no)
            QApplication.clipboard().setMimeData(mime)
            self.tray.showMessage("Copied", "Transcript copied to the clipboard.", make_icon("idle"), 2500)

    def _open(self) -> None:
        f = self._selected()
        if f and (Path(f) / "transcript.md").exists():
            os.startfile(Path(f) / "transcript.md")  # noqa: S606 - our own .md file, opened in the default app

    def _folder(self) -> None:
        f = self._selected()
        if f:
            os.startfile(f)  # noqa: S606 - opens our own session folder in Explorer

    def _again(self) -> None:
        f = self._selected()
        if f:
            self.engine.transcribe(Path(f))

    # ------------------------------------------------------------------ actions

    def _toggle_recording(self) -> None:
        if self.engine.status().recording:
            self.engine.stop_recording("stopped by you")
        else:
            self.engine.start_recording("started by you")
        self.refresh()

    def _record_from_prompt(self) -> None:
        self.prompt.hide()
        self.engine.start_recording("call prompt")
        self.refresh()

    def _dismiss_prompt(self) -> None:
        self.prompt.hide()

    def _settings(self) -> None:
        dlg = SettingsDialog(self, self.settings)
        if dlg.exec():
            dlg.apply()
            self._transcripts_version = -1

    def bring_to_front(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _other_instance(self) -> None:
        conn = self.server.nextPendingConnection()
        if conn:
            conn.close()
        self.bring_to_front()

    def closeEvent(self, event) -> None:
        if self._quitting:
            event.accept()
            return
        event.ignore()  # keep running in the tray so calls are still detected
        self.hide()
        if not self._told_tray:
            self.tray.showMessage(APP_NAME, "Still running here: it will ask when a Teams call starts. "
                                  "Right-click the icon to quit.", make_icon("idle"), 6000)
            self._told_tray = True

    def quit(self) -> None:
        st = self.engine.status()
        if st.recording and QMessageBox.question(
                self, APP_NAME, "A recording is in progress. Stop it and quit?") != QMessageBox.Yes:
            return
        if st.transcribing and QMessageBox.question(
                self, APP_NAME, "A transcript is still being written. Quit anyway? "
                "(The audio is kept; use 'Transcribe again' later.)") != QMessageBox.Yes:
            return
        self._quitting = True
        self.prompt.close()
        self.engine.shutdown()
        self.tray.hide()
        QApplication.quit()
