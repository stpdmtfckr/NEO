"""Serialize profile read/modify/write transactions across NEO processes."""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import time


@contextmanager
def profile_write_lock(settings_path: Path, *, timeout: float = 2.0):
    """Lock a stable sidecar, never the settings inode replaced during a save.

    Acquisition is bounded; failure leaves settings untouched. The operating
    system releases the lock if a writer exits unexpectedly.
    """
    lock_path = Path(settings_path).with_name('.settings-write.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a+b') as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b'\0')
            handle.flush()
        if os.name == 'nt':
            import msvcrt
            def acquire():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            def release():
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            def acquire():
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            def release():
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                acquire()
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Settings are being saved by another NEO process; try again') from exc
                time.sleep(min(.02, remaining))
        try:
            yield
        finally:
            release()
