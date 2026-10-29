"""Release assembly rejects stale or mismatched corresponding source."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))
import build_expanded
from release_metadata import release_source


class PublicReleaseSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.file = self.root / "module.py"
        self.file.write_text("# reviewed release source\n")
        self.archive = self.root / "FrameBridge-0.3.4-source.tar.gz"
        self.vendor_paths = {
            "cp313": "source/sources/original-geranas/FrameBridge-Fleet-Hub/vendor",
            "cp312": "source/target-vendors/cp312",
        }
        vendor_data = b"# reviewed vendor input\n"
        self.vendor_files = {"module.py": vendor_data,
                             "package/__init__.py": b"# package\n",
                             "package-1.dist-info/METADATA": b"Name: package\nVersion: 1\n"}
        self.payloads = {"source/module.py": self.file.read_bytes(),
                         **{path + "/" + relative: data for path in self.vendor_paths.values()
                            for relative, data in self.vendor_files.items()}}
        self.write_archive()
        # The reviewed vendor format uses Path component ordering. In particular
        # package/__init__.py comes before package-1.dist-info/METADATA.
        digest = hashlib.sha256()
        for relative in ("module.py", "package/__init__.py", "package-1.dist-info/METADATA"):
            digest.update(relative.encode() + b"\0")
            digest.update(hashlib.sha256(self.vendor_files[relative]).digest())
        self.vendor = {"vendor_sha256": digest.hexdigest()}
        self.data = {
            "version": "0.3.4", "status": "RELEASE_SOURCE_ASSEMBLED",
            "archive": self.archive.name, "bytes": self.archive.stat().st_size,
            "sha256": hashlib.sha256(self.archive.read_bytes()).hexdigest(),
            "vendor_inputs": {"vendors": {abi: {**self.vendor, "path": path} for abi, path in self.vendor_paths.items()}},
            "files": [{"path": path, "sha256": hashlib.sha256(data).hexdigest()} for path, data in self.payloads.items()],
        }
        self.manifest = self.root / "SOURCE-ARCHIVE-MANIFEST.json"

    def write_archive(self):
        with tarfile.open(self.archive, "w:gz") as archive:
            for path, data in self.payloads.items():
                info = tarfile.TarInfo("FrameBridge-0.3.4/" + path)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))

    def check(self, profile="debian13-amd64-py313", version="0.3.4"):
        self.manifest.write_text(json.dumps(self.data))
        return release_source(self.manifest, version, profile, {"source/module.py": self.file}, self.vendor)

    def test_release_binds_archive_source_and_both_vendor_abis(self):
        for profile in ("debian13-amd64-py313", "ubuntu24.04-amd64-py312", "mint22-amd64-py312"):
            self.assertEqual(self.check(profile)["sha256"], self.data["sha256"])

    def test_source_changed_after_archive_is_rejected(self):
        self.file.write_text("# later edit\n")
        with self.assertRaisesRegex(RuntimeError, "Package source differs"):
            self.check()

    def test_archive_tamper_is_rejected(self):
        with self.archive.open("ab") as stream:
            stream.write(b"altered")
        with self.assertRaisesRegex(RuntimeError, "archive hash or size"):
            self.check()

    def test_changed_manifest_cannot_relabel_old_archive_contents(self):
        self.file.write_text("# later edit\n")
        self.data["files"][0]["sha256"] = hashlib.sha256(self.file.read_bytes()).hexdigest()
        with self.assertRaisesRegex(RuntimeError, "archive content differs"):
            self.check()

    def test_relabelled_alternate_vendor_bytes_are_rejected_even_by_debian_build(self):
        self.payloads[self.vendor_paths["cp312"] + "/module.py"] = b"# different vendor bytes\n"
        self.write_archive()
        self.data.update(bytes=self.archive.stat().st_size, sha256=hashlib.sha256(self.archive.read_bytes()).hexdigest())
        self.data["files"] = [{"path": path, "sha256": hashlib.sha256(data).hexdigest()} for path, data in self.payloads.items()]
        with self.assertRaisesRegex(RuntimeError, "Archived vendor bytes differ"):
            self.check()

    def test_unsafe_archive_member_is_rejected_without_extracting(self):
        self.payloads["../escape"] = b"unwanted path"
        self.write_archive()
        self.data.update(bytes=self.archive.stat().st_size, sha256=hashlib.sha256(self.archive.read_bytes()).hexdigest())
        self.data["files"].append({"path": "../escape", "sha256": hashlib.sha256(b"unwanted path").hexdigest()})
        with self.assertRaisesRegex(RuntimeError, "Unsafe release source archive path"):
            self.check()

    def test_release_requires_cp312_corresponding_source(self):
        del self.data["vendor_inputs"]["vendors"]["cp312"]
        with self.assertRaisesRegex(RuntimeError, "cp312 and cp313"):
            self.check()

    def test_vendor_input_cannot_differ_from_corresponding_source(self):
        self.data["vendor_inputs"]["vendors"]["cp312"]["vendor_sha256"] = "b" * 64
        with self.assertRaisesRegex(RuntimeError, "vendor input differs"):
            self.check("mint22-amd64-py312")

    def test_candidate_version_and_experimental_profile_are_not_public_release(self):
        for profile, version in (("lmde7-amd64-py313", "0.3.4"), ("debian13-amd64-py313", "0.3.4~dev.1")):
            with self.assertRaisesRegex(RuntimeError, "stable version and an official"):
                self.check(profile, version)

    def test_public_metadata_names_source_without_claiming_signature_or_vm_qa(self):
        docs = self.root / "docs"
        docs.mkdir()
        identity = self.check()
        with mock.patch.object(build_expanded, "PUBLIC_RELEASE", True), \
             mock.patch.object(build_expanded, "SOURCE_ARCHIVE", identity), \
             mock.patch.object(build_expanded, "VERSION", "0.3.4"):
            build_expanded.install_legal_material(docs)
        result = json.loads((docs / "COMMUNITY-RELEASE-STATUS.json").read_text())
        self.assertEqual(result["status"], "PUBLIC_RELEASE_BUILD_ASSEMBLED")
        self.assertEqual(result["source_archive"], identity)
        self.assertEqual(result["signature_status"], "EXTERNAL_RELEASE_RECORD")
        self.assertEqual(result["vm_lifecycle_validation"], "NOT_ASSERTED_BY_BUILDER")
        self.assertNotIn("compatibility candidate", (docs / "copyright").read_text())


if __name__ == "__main__":
    unittest.main()
