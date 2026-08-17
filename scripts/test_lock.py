"""Cross-process scan lock tests.

Run: python scripts/test_lock.py
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from jobradar.lock import ScanLock, pid_alive   # noqa: E402

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}\n     got:  {got!r}\n     want: {want!r}")


tmp = Path(tempfile.mkdtemp())
path = tmp / "scan.lock"

# --- basic acquire / release -------------------------------------------------
a = ScanLock(path)
check("acquire on a free lock", a.acquire(), True)
check("owner is recorded", (a.owner() or {}).get("pid"), os.getpid())

b = ScanLock(path)
check("second holder is refused", b.acquire(), False)

a.release()
check("released lock is free", a.owner(), None)
check("another holder can now take it", b.acquire(), True)
b.release()

# --- a crashed scan must not wedge the lock forever --------------------------
# Simulate: a PID that no longer exists.
path.write_text(json.dumps({"pid": 999999, "started": time.time(), "label": "scan"}),
                encoding="utf-8")
c = ScanLock(path)
check("dead owner is treated as stale", c.owner(), None)
check("stale lock can be taken over", c.acquire(), True)
c.release()

# An entry older than max_age is stale even if the PID happens to be alive.
path.write_text(json.dumps({"pid": os.getpid(), "started": time.time() - 10_000,
                            "label": "scan"}), encoding="utf-8")
d = ScanLock(path, max_age=3600)
check("expired lock is stale despite a live pid", d.owner(), None)
check("expired lock can be taken over", d.acquire(), True)
d.release()

# --- pid liveness ------------------------------------------------------------
check("own pid is alive", pid_alive(os.getpid()), True)
check("absurd pid is not alive", pid_alive(999999), False)
check("pid 0 is not alive", pid_alive(0), False)

# A genuinely dead child must read as dead, which is what makes takeover safe.
child = subprocess.Popen([sys.executable, "-c", "pass"])
child.wait()
time.sleep(0.3)
check("exited child is not alive", pid_alive(child.pid), False)

# --- a real second process is locked out -------------------------------------
holder = ScanLock(path)
holder.acquire()
probe = subprocess.run(
    [sys.executable, "-c",
     "import sys; sys.path.insert(0, r'%s');"
     "from jobradar.lock import ScanLock;"
     "print(ScanLock(r'%s').acquire())"
     % (str(Path(__file__).resolve().parent.parent), str(path))],
    capture_output=True, text=True,
)
check("separate process is refused the held lock", probe.stdout.strip(), "False")
holder.release()

probe = subprocess.run(
    [sys.executable, "-c",
     "import sys; sys.path.insert(0, r'%s');"
     "from jobradar.lock import ScanLock;"
     "l = ScanLock(r'%s'); print(l.acquire()); l.release()"
     % (str(Path(__file__).resolve().parent.parent), str(path))],
    capture_output=True, text=True,
)
check("separate process gets the released lock", probe.stdout.strip(), "True")

if failures:
    print(f"FAILED {len(failures)} check(s):\n")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All scan-lock checks passed.")
