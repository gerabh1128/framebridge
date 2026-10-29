"""Candidate archive input selection; no host package or runtime changes."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))
import archive_sources


class CandidateArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base = self.root / "source"
        self.base.mkdir()
        self.input = self.base / "example.py"
        self.input.write_text("# candidate source\n")

    def run_archive(self, args):
        evidence = {"vendor_sha256": "a" * 64, "packages": [], "native_extensions": []}
        with mock.patch.object(archive_sources, "BASE", self.base), \
             mock.patch.object(archive_sources, "source_files", return_value={"source/example.py": self.input}), \
             mock.patch.object(archive_sources, "vendor_versions", return_value={}), \
             mock.patch.object(archive_sources, "validate_vendor", return_value=evidence), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as stderr:
            archive_sources.main(args)
        directory = "public-source" if "--public-release" in args else "candidate-source"
        return self.base / "release" / directory / archive_sources.ARCHIVE_NAME, stderr.getvalue()

    def test_default_archive_declares_missing_alternate_abis(self):
        output, warning = self.run_archive([])
        self.assertIn("cp312", warning)
        with tarfile.open(output) as archive:
            data = json.load(archive.extractfile(archive_sources.ARCHIVE_ROOT + "/TARGET-VENDOR-PROVENANCE.json"))
            readme = archive.extractfile(archive_sources.ARCHIVE_ROOT + "/README-SOURCE-ARCHIVE.md").read().decode()
        self.assertEqual(data["omitted_vendor_abis"], ["cp312", "cp314"])
        self.assertIn("--target debian13-amd64-py313", readme)
        self.assertIn("not a signed public release", readme)
        self.assertEqual(data["validation_status"], "candidate")

    def test_explicit_vendors_included_without_private_markers_or_bin(self):
        args = []
        for abi in ("cp312", "cp314"):
            vendor = self.root / abi; vendor.mkdir()
            (vendor / "module.py").write_text("# reviewed input\n")
            (vendor / "private.log").write_text("private canary\n")
            (vendor / "FB-ABI-PREPARED.json").write_text('{"private_path":"/home/example"}')
            (vendor / "bin").mkdir(); (vendor / "bin/tool").write_text("#!/tmp/private/python\n")
            args.extend(["--vendor-" + abi, str(vendor)])
        output, warning = self.run_archive(args)
        self.assertFalse(warning)
        with tarfile.open(output) as archive:
            names = archive.getnames()
            data = json.load(archive.extractfile(archive_sources.ARCHIVE_ROOT + "/TARGET-VENDOR-PROVENANCE.json"))
        self.assertIn(archive_sources.ARCHIVE_ROOT + "/source/target-vendors/cp312/module.py", names)
        self.assertIn(archive_sources.ARCHIVE_ROOT + "/source/target-vendors/cp314/module.py", names)
        self.assertFalse(any("FB-ABI-PREPARED" in name or name.endswith("bin/tool") or name.endswith("private.log") for name in names))
        self.assertEqual(set(data["vendors"]), {"cp312", "cp313", "cp314"})
        self.assertEqual(data["omitted_vendor_abis"], [])

    def test_public_archive_requires_matching_cp312_and_excludes_experimental_input(self):
        for args in (["--public-release"], ["--public-release", "--vendor-cp312", str(self.root), "--vendor-cp314", str(self.root)]):
            with self.assertRaises(SystemExit):
                self.run_archive(args)

    def test_public_archive_describes_three_profiles_without_claiming_publication(self):
        vendor = self.root / "cp312"; vendor.mkdir()
        (vendor / "module.py").write_text("# reviewed cp312 input\n")
        output, warning = self.run_archive(["--public-release", "--vendor-cp312", str(vendor)])
        self.assertFalse(warning)
        manifest = json.loads((output.parent / "SOURCE-ARCHIVE-MANIFEST.json").read_text())
        self.assertEqual(manifest["status"], "RELEASE_SOURCE_ASSEMBLED")
        self.assertEqual(set(manifest["vendor_inputs"]["vendors"]), {"cp312", "cp313"})
        with tarfile.open(output) as archive:
            readme = archive.extractfile(archive_sources.ARCHIVE_ROOT + "/README-SOURCE-ARCHIVE.md").read().decode()
        self.assertIn("--public-release --source-manifest", readme)
        self.assertNotIn("compatibility candidate source", readme)
        self.assertIn("Signatures, publication", readme)

    def test_alternate_vendor_symlink_is_rejected(self):
        vendor = self.root / "cp312"; vendor.mkdir()
        (vendor / "unsafe.py").symlink_to(self.input)
        with self.assertRaisesRegex(RuntimeError, "symlink rejected"):
            self.run_archive(["--vendor-cp312", str(vendor)])


if __name__ == "__main__":
    unittest.main()
