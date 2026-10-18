"""Entry point.

  pythonw -m meetingrecorder              run the app (what the Start Menu shortcut does)
  python  -m meetingrecorder --install    create the Start Menu shortcut (then pin it)
  python  -m meetingrecorder --uninstall  remove the shortcuts
"""

import argparse
import ctypes
import getpass
import hashlib
import logging
import sys
import threading
import traceback
from logging.handlers import RotatingFileHandler

from .config import APP_ID, APP_NAME, LOG_DIR


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    logging.getLogger("faster_whisper").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> None:
    ap = argparse.ArgumentParser(prog="meetingrecorder")
    ap.add_argument("--install", action="store_true", help="create the Start Menu shortcut")
    ap.add_argument("--uninstall", action="store_true", help="remove the shortcuts")
    ap.add_argument("--minimized", action="store_true", help="start hidden in the tray")
    args = ap.parse_args()
    setup_logging()
    log = logging.getLogger("meetingrecorder")

    if args.install or args.uninstall:
        from . import installer

        if args.uninstall:
            installer.uninstall()
            print("Shortcuts removed.")
        else:
            path = installer.install()
            print(f"Created {path}\nOpen MeetingRecorder from the Start Menu, then right-click its taskbar icon "
                  "> Pin to taskbar.")
        return

    # Same ID as the shortcut, so the window groups under the pinned icon.
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)

    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtWidgets import QApplication, QMessageBox

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    from .app import make_icon

    app.setWindowIcon(make_icon("recording"))  # taskbar button, dialogs, Alt+Tab
    app.setQuitOnLastWindowClosed(False)  # keeps running in the tray

    # Single instance: a named mutex in this logon session decides (other users and sessions can't
    # create it), and a user-only local pipe lets a second launch bring the first window forward.
    # The pipe name is a hash, so the Windows username isn't exposed in plain text.
    import win32api
    import win32event
    import winerror

    mutex = win32event.CreateMutex(None, False, f"Local\\{APP_ID}")  # noqa: F841 (held for the process lifetime)
    already_running = win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS  # read right after CreateMutex
    server_name = f"{APP_NAME}-{hashlib.sha256(getpass.getuser().encode()).hexdigest()[:16]}"
    if already_running:
        sock = QLocalSocket()
        sock.connectToServer(server_name)
        if sock.waitForConnected(500):
            sock.close()
        return
    server = QLocalServer()
    server.setSocketOptions(QLocalServer.UserAccessOption)
    if not server.listen(server_name):
        log.warning("bring-to-front pipe unavailable: %s", server.errorString())

    def on_crash(exc_type, exc, tb):
        log.critical("unhandled error:\n%s", "".join(traceback.format_exception(exc_type, exc, tb)))
        QMessageBox.critical(None, APP_NAME, f"Something went wrong:\n{exc}\n\nDetails are in {LOG_DIR / 'app.log'}")

    sys.excepthook = on_crash
    threading.excepthook = lambda a: log.critical(
        "thread %s crashed:\n%s", a.thread.name if a.thread else "?",
        "".join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)))

    from .app import MainWindow
    from .config import load_settings
    from .engine import Engine

    settings = load_settings()
    settings.transcripts_path.mkdir(parents=True, exist_ok=True)
    engine = Engine(settings)
    engine.start()
    win = MainWindow(engine, settings, server)
    if not args.minimized:
        win.show()
    log.info("started")
    code = app.exec()
    engine.shutdown()
    sys.exit(code)


if __name__ == "__main__":
    main()
