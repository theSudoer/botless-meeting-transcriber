"""Start Menu / Startup shortcuts.

The shortcut carries the same AppUserModelID the running app sets, so the window groups
under the icon you pin to the taskbar (otherwise Windows shows a second, generic Python icon).
Windows doesn't let programs pin themselves: open it from the Start Menu, then right-click
its taskbar icon > Pin to taskbar.
"""

import os
import sys
from pathlib import Path

from .config import APP_ID, APP_NAME, ICON_FILE

START_MENU = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
STARTUP = START_MENU / "Startup"
TASKBAR_PINS = (Path(os.environ["APPDATA"]) / "Microsoft" / "Internet Explorer" / "Quick Launch"
                / "User Pinned" / "TaskBar")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _pythonw() -> Path:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return w if w.exists() else exe


def _make_shortcut(path: Path, args: str) -> None:
    import pythoncom
    from win32com.propsys import propsys, pscon
    from win32com.shell import shell

    link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER,
                                      shell.IID_IShellLink)
    link.SetPath(str(_pythonw()))
    link.SetArguments(args)
    link.SetWorkingDirectory(str(PROJECT_ROOT))
    link.SetDescription("Bot-free meeting transcripts")
    if ICON_FILE.exists():
        link.SetIconLocation(str(ICON_FILE), 0)
    store = link.QueryInterface(propsys.IID_IPropertyStore)
    store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(APP_ID))
    store.Commit()
    path.parent.mkdir(parents=True, exist_ok=True)
    link.QueryInterface(pythoncom.IID_IPersistFile).Save(str(path), 0)


def _refresh_shortcut(lnk: Path) -> None:
    """Point an existing shortcut (e.g. the user's taskbar pin) at the current icon and app ID."""
    import pythoncom
    from win32com.propsys import propsys, pscon
    from win32com.shell import shell

    link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER,
                                      shell.IID_IShellLink)
    persist = link.QueryInterface(pythoncom.IID_IPersistFile)
    persist.Load(str(lnk), 2)  # STGM_READWRITE: needed to change the property store
    link.SetIconLocation(str(ICON_FILE), 0)
    store = link.QueryInterface(propsys.IID_IPropertyStore)
    store.SetValue(pscon.PKEY_AppUserModel_ID, propsys.PROPVARIANTType(APP_ID))
    store.Commit()
    persist.Save(str(lnk), 0)


def our_shortcuts() -> list[Path]:
    """Every shortcut that launches this app: Start Menu, Startup and the taskbar pin."""
    found = []
    for folder in (START_MENU, STARTUP, TASKBAR_PINS):
        for lnk in folder.glob("*.lnk") if folder.exists() else []:
            if lnk.stem == APP_NAME or "meetingrecorder" in _arguments(lnk).lower():
                found.append(lnk)
    return found


def _arguments(lnk: Path) -> str:
    try:
        import pythoncom
        from win32com.shell import shell

        link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER,
                                          shell.IID_IShellLink)
        link.QueryInterface(pythoncom.IID_IPersistFile).Load(str(lnk))
        return link.GetArguments()
    except Exception:
        return ""


def refresh_icons() -> list[Path]:
    """Write the icon file and update every existing shortcut, then ask Explorer to redraw icons."""
    import subprocess

    from PySide6.QtWidgets import QApplication

    from .app import save_icon_file

    _app = QApplication.instance() or QApplication([])  # needed to render the icon
    save_icon_file()
    updated = []
    for lnk in our_shortcuts():
        _refresh_shortcut(lnk)
        updated.append(lnk)
    # Refresh Explorer's icon cache. Full System32 path: a bare name could resolve to a planted
    # ie4uinit.exe in the working directory or next to python.exe.
    ie4uinit = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "ie4uinit.exe"
    if ie4uinit.exists():
        subprocess.run([str(ie4uinit), "-show"], check=False, timeout=30)  # noqa: S603 - fixed system binary
    return updated


def install() -> Path:
    from PySide6.QtWidgets import QApplication

    from .app import save_icon_file

    _app = QApplication.instance() or QApplication([])  # needed to render the icon
    save_icon_file()
    target = START_MENU / f"{APP_NAME}.lnk"
    _make_shortcut(target, "-m meetingrecorder")
    refresh_icons()  # also updates an existing taskbar pin / startup shortcut
    return target


def uninstall() -> None:
    for p in (START_MENU / f"{APP_NAME}.lnk", STARTUP / f"{APP_NAME}.lnk"):
        p.unlink(missing_ok=True)


def startup_enabled() -> bool:
    return (STARTUP / f"{APP_NAME}.lnk").exists()


def set_startup(enabled: bool) -> None:
    path = STARTUP / f"{APP_NAME}.lnk"
    if enabled:
        _make_shortcut(path, "-m meetingrecorder --minimized")
    else:
        path.unlink(missing_ok=True)
