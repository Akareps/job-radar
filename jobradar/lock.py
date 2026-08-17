"""Cross-process scan lock.

Scans start from three independent places: the hourly scheduled task, the
dashboard's "Scan now" button, and the command line. The dashboard's in-process
threading.Lock only ever protected the first of those, so three scans once ran
concurrently (16:37, 16:44, 16:48) and the dashboard cheerfully reported that
nothing was running.

A lock file carrying the owner's PID fixes that across processes. A crashed or
killed scan leaves the file behind, so the lock is taken over when the recorded
process is gone or the entry is older than `max_age`.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

STALE_AFTER_SECONDS = 2 * 3600


def pid_alive(pid: int) -> bool:
    """Best-effort liveness check. Errs toward 'alive' only when unsure."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            # A finished process keeps a queryable handle; only STILL_ACTIVE
            # means it is genuinely running.
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    return True


class ScanLock:
    """Filesystem lock guarding "only one scan at a time, machine-wide"."""

    def __init__(self, path: str | Path, max_age: int = STALE_AFTER_SECONDS):
        self.path = Path(path)
        self.max_age = max_age
        self._held = False

    # ---------------------------------------------------------------- #

    def owner(self) -> dict | None:
        """The live holder's record, or None if free or stale."""
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

        pid = int(record.get("pid", -1))
        started = float(record.get("started", 0))
        if time.time() - started > self.max_age:
            return None
        if not pid_alive(pid):
            return None
        return record

    def acquire(self, label: str = "scan") -> bool:
        holder = self.owner()
        if holder is not None:
            return False

        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Stale or absent: take it over. O_EXCL would refuse to reclaim a
        # leftover file, so replace it outright and re-read to confirm we won
        # the race against another process doing exactly the same thing.
        record = {"pid": os.getpid(), "started": time.time(), "label": label}
        self.path.write_text(json.dumps(record), encoding="utf-8")
        time.sleep(0.05)
        try:
            confirmed = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if int(confirmed.get("pid", -1)) != os.getpid():
            return False

        self._held = True
        return True

    def release(self) -> None:
        if not self._held:
            return
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
            if int(record.get("pid", -1)) == os.getpid():
                self.path.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
        self._held = False

    def __enter__(self) -> "ScanLock":
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    @property
    def held(self) -> bool:
        return self._held
