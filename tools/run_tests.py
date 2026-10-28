#!/usr/bin/env python3
"""Run source tests with generated fixtures and an isolated application environment."""
from pathlib import Path
import os
import stat
import subprocess
import sys
import tempfile


def main():
    root = Path(__file__).resolve().parents[1]
    qa = root / "source/qa"
    qa.mkdir(mode=0o700, exist_ok=True)
    info = qa.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SystemExit("The generated source/qa directory must be private and owned by the test user.")
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith(("FRAMEBRIDGE_", "FBTERM_", "FB_"))}
    with tempfile.TemporaryDirectory(prefix="unit-test-home-", dir=qa) as home:
        environment.update(
            HOME=home, XDG_DATA_HOME=str(Path(home) / ".local/share"),
            XDG_CONFIG_HOME=str(Path(home) / ".config"),
            PYTHONPATH=str(root / "source/expanded"), PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s",
                                 str(root / "source/tests")], cwd=root, env=environment)
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
