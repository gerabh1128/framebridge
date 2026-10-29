"""Negative build-input tests; no package installation or target imports."""
import base64
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))
from build_profiles import target_profile, validate_vendor


class VendorValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.profile = target_profile("mint22-amd64-py312")
        self.make_package("cffi", "_cffi_backend.cpython-312-x86_64-linux-gnu.so")
        self.make_package("pydantic_core", "pydantic_core/_pydantic_core.cpython-312-x86_64-linux-gnu.so")
        self.make_package("rpds_py", "rpds/rpds.cpython-312-x86_64-linux-gnu.so")

    def make_package(self, name, relative, tag="cp312-cp312-manylinux_2_28_x86_64", abi_extra=b""):
        native = self.root / relative
        native.parent.mkdir(parents=True, exist_ok=True)
        native.write_bytes(b"\x7fELF\x02\x01" + b"\0" * 12 + (62).to_bytes(2, "little") + abi_extra)
        info = self.root / (name + "-1.0.dist-info")
        info.mkdir(exist_ok=True)
        (info / "METADATA").write_text("Name: " + name + "\nVersion: 1.0\nRequires-Python: >=3.10\n")
        (info / "WHEEL").write_text("Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: " + tag + "\n")
        self.write_record(info, native)
        return info, native

    def write_record(self, info, native):
        rows = []
        for path in (native, info / "METADATA", info / "WHEEL"):
            digest = base64.urlsafe_b64encode(hashlib.sha256(path.read_bytes()).digest()).decode().rstrip("=")
            rows.append(path.relative_to(self.root).as_posix() + ",sha256=" + digest + "," + str(path.stat().st_size))
        rows.append((info / "RECORD").relative_to(self.root).as_posix() + ",,")
        (info / "RECORD").write_text("\n".join(rows) + "\n")

    def test_valid_target_and_abi3(self):
        self.make_package("cryptography", "cryptography/_rust.abi3.so", "cp311-abi3-manylinux_2_34_x86_64")
        result = validate_vendor(self.root, self.profile)
        self.assertEqual(len(result["native_extensions"]), 4)
        self.assertEqual(self.profile["validation_status"], "candidate")

    def test_wrong_wheel_abi_rejected(self):
        self.make_package("cffi", "_cffi_backend.cpython-312-x86_64-linux-gnu.so", "cp313-cp313-manylinux_2_28_x86_64")
        with self.assertRaisesRegex(RuntimeError, "wheel ABI"):
            validate_vendor(self.root, self.profile)

    def test_wrong_extension_abi_rejected(self):
        old = self.root / "_cffi_backend.cpython-312-x86_64-linux-gnu.so"
        old.unlink()
        self.make_package("cffi", "_cffi_backend.cpython-313-x86_64-linux-gnu.so")
        with self.assertRaisesRegex(RuntimeError, "extension ABI"):
            validate_vendor(self.root, self.profile)

    def test_wrong_elf_cpu_rejected(self):
        info, path = self.make_package("cffi", "_cffi_backend.cpython-312-x86_64-linux-gnu.so")
        data = bytearray(path.read_bytes()); data[18:20] = (183).to_bytes(2, "little")
        path.write_bytes(data); self.write_record(info, path)
        with self.assertRaisesRegex(RuntimeError, "ELF amd64"):
            validate_vendor(self.root, self.profile)

    def test_new_glibc_rejected(self):
        self.make_package("cffi", "_cffi_backend.cpython-312-x86_64-linux-gnu.so", abi_extra=b"GLIBC_2.35")
        with self.assertRaisesRegex(RuntimeError, "glibc 2.34"):
            validate_vendor(self.root, self.profile)

    def test_requires_python_rejected(self):
        info, path = self.make_package("cffi", "_cffi_backend.cpython-312-x86_64-linux-gnu.so")
        with (info / "METADATA").open("a") as stream: stream.write("Requires-Python: >=3.13\n")
        # Replace the original field to avoid duplicate-header ambiguity.
        (info / "METADATA").write_text("Name: cffi\nVersion: 1.0\nRequires-Python: >=3.13\n")
        self.write_record(info, path)
        with self.assertRaisesRegex(RuntimeError, "Requires-Python"):
            validate_vendor(self.root, self.profile)

    def test_version_set_change_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "names/versions"):
            validate_vendor(self.root, self.profile, {"cffi": "9.9"})

    def test_modified_native_record_rejected(self):
        with (self.root / "_cffi_backend.cpython-312-x86_64-linux-gnu.so").open("ab") as stream:
            stream.write(b"altered")
        with self.assertRaisesRegex(RuntimeError, "RECORD digest"):
            validate_vendor(self.root, self.profile)

    def test_missing_wheel_rejected(self):
        (self.root / "cffi-1.0.dist-info/WHEEL").unlink()
        with self.assertRaisesRegex(RuntimeError, "WHEEL/RECORD"):
            validate_vendor(self.root, self.profile)

    def test_native_record_requires_digest(self):
        record = self.root / "cffi-1.0.dist-info/RECORD"
        rows = record.read_text().splitlines()
        rows[0] = rows[0].split(",")[0] + ",,"
        record.write_text("\n".join(rows) + "\n")
        with self.assertRaisesRegex(RuntimeError, "RECORD digest missing"):
            validate_vendor(self.root, self.profile)

    def test_pure_metadata_on_native_rejected(self):
        self.make_package("cffi", "_cffi_backend.cpython-312-x86_64-linux-gnu.so", "py3-none-any")
        with self.assertRaisesRegex(RuntimeError, "native WHEEL"):
            validate_vendor(self.root, self.profile)


if __name__ == "__main__":
    unittest.main()
