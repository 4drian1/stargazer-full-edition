"""Snapshot lock shared by the web app and local Agent worker processes."""
import os
from threading import Lock

from rules_lab.paths import STATE


class SnapshotLock:
    def __init__(self):
        self.local = Lock()
        self.file = None

    def acquire(self, *, blocking=False):
        if not self.local.acquire(blocking=blocking):
            return False
        file = None
        try:
            path = STATE/'locks'/'snapshot-agent.lock'
            path.parent.mkdir(parents=True, exist_ok=True)
            file = path.open('a+b', buffering=0)
            file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if file is not None:
                file.close()
            self.local.release()
            return False
        self.file = file
        return True

    def release(self):
        self.file.close()
        self.file = None
        self.local.release()
