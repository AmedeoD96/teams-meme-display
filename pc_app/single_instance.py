"""Refuse to run two copies of the app at once.

Two instances mean two workers racing for the same COM port. Whichever one loses reports the port
as busy and can never recover on its own: Reconnect cannot take a handle away from another
process, so the board sits on DISCONNECTED with a button that does nothing. That is an easy trap
to fall into from a packaged .exe -- autostart, then launching it again from the Start menu -- and
an impossible one to diagnose without Task Manager, so it is stopped at startup instead.

A Windows mutex is used rather than a lock file because the kernel drops it when the process ends,
however it ends. There is no stale lock to clean up after a crash.
"""

from __future__ import annotations

import ctypes
import logging
import sys

log = logging.getLogger(__name__)

#: Not prefixed with "Global\\", so the claim is per login session: a second user signed in to the
#: same machine has their own board and is entitled to their own copy.
MUTEX_NAME = "TeamsMemeDisplay-single-instance"

ERROR_ALREADY_EXISTS = 183

#: Held for the life of the process. Closing the handle would give the claim up.
_handle: int | None = None


def _kernel32():
    if not sys.platform.startswith("win"):
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Without these a 64-bit handle comes back through a C int and is truncated.
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
    return kernel32


def claim(name: str = MUTEX_NAME) -> bool:
    """Become *the* instance. False means another copy is already running.

    Anything unexpected counts as a success: refusing to start because the check itself broke
    would be worse than the problem it guards against.
    """
    global _handle
    kernel32 = _kernel32()
    if kernel32 is None:
        return True
    try:
        handle = kernel32.CreateMutexW(None, False, name)
        error = ctypes.get_last_error()
    except Exception as exc:  # pragma: no cover - a broken ctypes is not worth blocking on
        log.debug("could not claim the single-instance mutex: %s", exc)
        return True
    if not handle:
        log.debug("could not claim the single-instance mutex: error %d", error)
        return True
    if error == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return False
    _handle = handle
    return True


def release() -> None:
    """Give the claim up. The process ending does this anyway; tests need it sooner."""
    global _handle
    kernel32 = _kernel32()
    if kernel32 is not None and _handle is not None:
        kernel32.CloseHandle(ctypes.c_void_p(_handle))
    _handle = None


def warn_already_running(title: str = "Teams status meme display") -> None:
    """Say so somewhere a windowed build can be seen -- it has no console to print to."""
    if sys.stderr is not None or not sys.platform.startswith("win"):
        return  # a console run has the log line, which says the same thing
    try:
        ctypes.windll.user32.MessageBoxW(
            None,
            "The app is already running.\n\n"
            "Look for its icon in the notification area, next to the clock.",
            title,
            0x40,  # MB_ICONINFORMATION
        )
    except Exception:  # pragma: no cover - never let a message box stop an exit
        pass
