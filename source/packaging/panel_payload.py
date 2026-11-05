"""Build the deterministic, role-gated panel payload without Node at build time."""
from hashlib import sha256
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "expanded"))
from framebridge_expanded.installation import PANEL_FILES

WHEEL = "vendor/waitress-3.0.2-py3-none-any.whl"
WHEEL_SHA256 = "c56d67fd6e87c2ee598b76abdd4e96cfad1f24cacdea5078d382b1f9d7b5ed2e"


def build_panel_payload(source, target):
    if sha256((source / WHEEL).read_bytes()).hexdigest() != WHEEL_SHA256:
        raise ValueError("The bundled panel server wheel differs from the reviewed version.")
    notices = []
    for name in ("lucide-react", "react", "react-dom", "scheduler", "waitress"):
        notices.append(name + "\n\n" + (source / "licenses" / (name + ".txt")).read_text())
    with ZipFile(target, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(PANEL_FILES):
            info = ZipInfo(name, (2026, 10, 27, 0, 0, 0))
            info.external_attr = 0o100644 << 16
            data = "\n\n".join(notices).encode() if name == "THIRD-PARTY-NOTICES.txt" else (source / name).read_bytes()
            archive.writestr(info, data, compress_type=ZIP_DEFLATED, compresslevel=9)
