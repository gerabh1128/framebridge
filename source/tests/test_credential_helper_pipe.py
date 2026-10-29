"""A stalled unprivileged stdout reader must not retain the registry lock.

The subprocess disables installation/caller/crypto operations and uses only its
own temporary directory. No sudo, host files, accounts or credentials are used.
"""
import array
import fcntl
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from framebridge_expanded import credential_helper

CHILD = r"""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
spec = importlib.util.spec_from_file_location("pipe_canary_helper", sys.argv[1])
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
helper.ROOT = Path(sys.argv[2])
helper._installation = lambda: None
helper._prepare_root = lambda: None
helper._cleanup_temporaries = lambda: None
helper._caller = lambda: SimpleNamespace(pw_uid=12345, pw_name="synthetic", pw_dir="/synthetic-only")
helper.decrypt = lambda *args: b"X" * 16384
# The only file is our own temporary lock; simulate its expected root metadata.
actual_fstat = helper.os.fstat
helper.os.fstat = lambda fd: SimpleNamespace(st_mode=actual_fstat(fd).st_mode,
                                            st_uid=0, st_nlink=actual_fstat(fd).st_nlink)
raise SystemExit(helper.main(["decrypt", "a" * 64, "b" * 64]))
"""


PROTECT_CHILD = CHILD.replace(
    'raise SystemExit(helper.main(["decrypt", "a" * 64, "b" * 64]))',
    '\n'.join([
        'helper.INPUT_TIMEOUT = 0.1',
        'helper.unauthorize = lambda *args, **kwargs: None',
        'helper.protect = lambda *args: b"SYNTHETIC-CIPHER-ONLY" * 8',
        'raise SystemExit(helper.main(["protect", "a" * 64]))',
    ]),
)


class CredentialHelperPipeTests(unittest.TestCase):
    def test_open_stdin_without_eof_times_out_and_releases_registry_lock(self):
        with tempfile.TemporaryDirectory(prefix="framebridge-input-canary-") as directory:
            child = None
            lock_fd = None
            try:
                started = time.monotonic()
                child = subprocess.Popen([sys.executable, "-I", "-S", "-c", PROTECT_CHILD,
                                          str(Path(credential_helper.__file__).resolve()), directory],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, close_fds=True)
                self.assertEqual(1, child.wait(timeout=1), "Protect must reject an input stream without EOF")
                self.assertLess(time.monotonic() - started, 1, "Synthetic input timeout exceeded its bound")
                # Do not close stdin or communicate(): both would supply EOF and
                # could hide an unbounded read or a leaked registry lock.
                self.assertFalse(child.stdin.closed)
                lock_fd = os.open(Path(directory) / ".lock", os.O_RDWR)
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    self.fail("Protect retained the registry lock after its input deadline")
                finally:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                self.assertEqual(b"", child.stdout.read())
                self.assertIn(b"protected credential operation failed", child.stderr.read())
            finally:
                if child is not None:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=3)
                    for stream in (child.stdin, child.stdout, child.stderr):
                        stream.close()
                if lock_fd is not None:
                    os.close(lock_fd)

    @unittest.skipUnless(hasattr(fcntl, "F_SETPIPE_SZ"), "Linux pipe-size controls required")
    def test_stalled_stdout_reader_does_not_hold_global_registry_lock(self):
        with tempfile.TemporaryDirectory(prefix="framebridge-pipe-canary-") as directory:
            read_fd, write_fd = os.pipe()
            child = None
            lock_fd = None
            try:
                capacity = fcntl.fcntl(write_fd, fcntl.F_SETPIPE_SZ, 4096)
                child = subprocess.Popen([sys.executable, "-I", "-S", "-c", CHILD,
                                          str(Path(credential_helper.__file__).resolve()), directory],
                                         stdin=subprocess.DEVNULL, stdout=write_fd,
                                         stderr=subprocess.PIPE, close_fds=True)
                os.close(write_fd)
                write_fd = None
                deadline = time.monotonic() + 5
                buffered = array.array("i", [0])
                while time.monotonic() < deadline:
                    fcntl.ioctl(read_fd, termios.FIONREAD, buffered, True)
                    if buffered[0] == capacity:
                        break
                    if child.poll() is not None:
                        self.fail("Synthetic helper exited before filling the canary pipe: " + child.stderr.read().decode())
                    time.sleep(0.01)
                self.assertEqual(capacity, buffered[0], "Synthetic pipe did not fill")
                self.assertIsNone(child.poll(), "Helper should be waiting for the synthetic stdout reader")
                lock_fd = os.open(Path(directory) / ".lock", os.O_RDWR)
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    self.fail("Helper retained the registry lock while stdout was blocked")
                finally:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                received = bytearray()
                deadline = time.monotonic() + 5
                while True:
                    remaining = deadline - time.monotonic()
                    self.assertGreater(remaining, 0, "Synthetic helper pipe did not finish")
                    ready, _, _ = select.select([read_fd], [], [], remaining)
                    self.assertTrue(ready, "Synthetic helper pipe timed out")
                    block = os.read(read_fd, 4096)
                    if not block:
                        break
                    received.extend(block)
                self.assertEqual(b"X" * 16384, bytes(received))
                self.assertEqual(0, child.wait(timeout=3), child.stderr.read().decode())
            finally:
                if child is not None:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=3)
                    child.stderr.close()
                os.close(read_fd)
                if write_fd is not None:
                    os.close(write_fd)
                if lock_fd is not None:
                    os.close(lock_fd)


if __name__ == "__main__":
    unittest.main()
