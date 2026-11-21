import hashlib
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import select
import subprocess
import tempfile
import unittest


BIN = Path(__file__).resolve().parents[1] / "sources/original-asus/fbterm/bin"
RENDERER = BIN / "fbterm-output"
loader = importlib.machinery.SourceFileLoader("fbterm_output", str(RENDERER))
spec = importlib.util.spec_from_loader(loader.name, loader)
output = importlib.util.module_from_spec(spec)
loader.exec_module(output)


class ChunkedInput:
    def __init__(self, chunks):
        self.chunks = iter(chunks)

    def read1(self, _size):
        return next(self.chunks, b"")


class FBTermOutputTest(unittest.TestCase):
    def test_binary_record_and_text_representation_across_reads(self):
        euro = "€".encode()
        chunks = [
            b"estado\t" + euro[:1],
            euro[1:] + b"\x00\r\n",
            "\u0085".encode() + b"\xff\xe2",
        ]
        capture = io.BytesIO()
        with tempfile.TemporaryFile() as display:
            output.record_output(ChunkedInput(chunks), capture, display.fileno())
            display.seek(0)
            self.assertEqual(
                display.read(), b"estado\t" + euro + b"\\x00\\x0d\n\\x85\\xff\\xe2"
            )
        self.assertEqual(capture.getvalue(), b"".join(chunks))

    def test_binary_record_continues_after_the_display_closes(self):
        chunks = [b"primero\n", b"segundo\n", b"x" * 65536]
        capture = io.BytesIO()
        read_fd, display = os.pipe()
        os.close(read_fd)
        try:
            output.record_output(ChunkedInput(chunks), capture, display)
        finally:
            os.close(display)
        self.assertEqual(capture.getvalue(), b"".join(chunks))

    def test_display_and_binary_log_update_before_input_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stdout.bin"
            with subprocess.Popen(
                [RENDERER, path],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
            ) as process:
                try:
                    process.stdin.write(b"en curso\n")
                    process.stdin.flush()
                    self.assertEqual(select.select([process.stdout], [], [], 2)[0], [process.stdout])
                    self.assertEqual(process.stdout.readline(), b"en curso\n")
                    self.assertEqual(path.read_bytes(), b"en curso\n")
                finally:
                    process.stdin.close()
                    process.wait(timeout=2)
                self.assertEqual(process.returncode, 0)

    def test_runner_uses_safe_display_for_both_streams_and_preserves_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "bin").symlink_to(BIN, target_is_directory=True)
            job = root / "jobs" / "controlled-output"
            job.mkdir(parents=True)
            command = job / "command.bash"
            command.write_text(
                "IFS= read -r text\n"
                "printf '%s\\000\\n' \"$text\"\n"
                "printf 'diagnostico\\007\\n' >&2\n"
                "exit 7\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [BIN / "fbterm-run", job.name],
                input=b"salida\n",
                capture_output=True,
                timeout=6,
                env={**os.environ, "FBTERM_ROOT": str(root)},
            )
            self.assertEqual(result.returncode, 7)
            self.assertIn(b"salida\\x00\n", result.stdout)
            self.assertEqual(result.stderr, b"diagnostico\\x07\n")
            self.assertIn(b"FBTERM_JOB_COMPLETE", result.stdout)
            self.assertTrue((job / "DONE").is_file())
            receipt = dict(
                line.split("=", 1)
                for line in (job / "result.env").read_text().splitlines()
            )
            self.assertEqual(receipt["EXIT"], "7")
            for label, data in (
                ("STDOUT", b"salida\x00\n"),
                ("STDERR", b"diagnostico\x07\n"),
            ):
                self.assertEqual((job / f"{label.lower()}.bin").read_bytes(), data)
                self.assertEqual(receipt[f"{label}_SHA256"], hashlib.sha256(data).hexdigest())
                self.assertEqual(receipt[f"{label}_BYTES"], str(len(data)))
                self.assertEqual(receipt[f"{label}_TEE_EXIT"], "0")


if __name__ == "__main__":
    unittest.main()
