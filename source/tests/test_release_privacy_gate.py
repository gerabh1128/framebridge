"""Regression tests for scanning the actual published container formats."""

import gzip
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))
from release_privacy import PrivacyError, ReleasePrivacy


class ReleasePrivacyGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.policy = self.work / "identifiers.json"
        self.policy.write_text(json.dumps(["review-fixture-host", "192.0.2.91"]))
        self.scanner = ReleasePrivacy(self.policy)

    def source(self, name, data):
        file = self.work / "test-source.tar.gz"
        with tarfile.open(file, "w:gz") as archive:
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
        return file

    def test_rejects_private_metadata_in_source_without_printing_it(self):
        file = self.source("source/requirements.txt", b"Contact REVIEW-FIXTURE-HOST at 192.0.2.91")
        with self.assertRaisesRegex(PrivacyError, "^source archive member 1: private-identifier$"):
            self.scanner.source(file)

    def test_rejects_private_marker_in_source_member_name(self):
        file = self.source("source/review-fixture-host/setup.txt", b"ordinary text")
        with self.assertRaisesRegex(PrivacyError, "private-identifier"):
            self.scanner.source(file)

    def test_scans_gzip_member_contents(self):
        packed = gzip.compress(b"review-fixture-host")
        with self.assertRaisesRegex(PrivacyError, "private-identifier"):
            self.scanner.source(self.source("source/docs/guide.gz", packed))

    def test_allows_role_names_attribution_and_public_key_headers(self):
        file = self.source("source/LICENSE.txt",
                           b"workstation public.example.org example@gmail.com\n-----BEGIN PUBLIC KEY-----")
        self.assertEqual(self.scanner.source(file), 1)

    def test_rejects_common_sensitive_shapes(self):
        for data, reason in (
            (b"https://drive." + b"google.com/file/d/SYNTHETIC_EXAMPLE/view", "drive-document-link"),
            (b"gh" + b"p_" + b"A" * 36, "credential-token"),
            (b"-----BEGIN OPENSSH " + b"PRIVATE KEY-----\n" + b"A" * 32, "private-key-material"),
            (b"https://fixture:" + b"fixture-credential" + b"@proxy.example", "credential-in-url"),
        ):
            with self.subTest(reason=reason), self.assertRaisesRegex(PrivacyError, reason):
                self.scanner.source(self.source("source/proof.txt", data))
        with self.assertRaisesRegex(PrivacyError, "private-file"):
            self.scanner.source(self.source("source/.ssh/config", b"test fixture"))

    def test_checks_archive_owner_and_extended_metadata(self):
        file = self.work / "test-source.tar.gz"
        for field in ("uname", "gname", "comment"):
            with self.subTest(field=field), tarfile.open(file, "w:gz") as archive:
                entry = tarfile.TarInfo("source/readme.txt")
                if field == "comment":
                    entry.pax_headers = {field: "review-fixture-host"}
                else:
                    setattr(entry, field, "review-fixture-host")
                archive.addfile(entry)
            with self.assertRaisesRegex(PrivacyError, "private-identifier"):
                self.scanner.source(file)

    def test_rejects_bad_policy_without_showing_value(self):
        self.policy.write_text('"\"')
        with self.assertRaisesRegex(PrivacyError, "^Invalid private identifier policy$"):
            ReleasePrivacy(self.policy)

    def test_rendered_website_checks_personal_contact_and_private_paths(self):
        site = self.work / "website"
        site.mkdir()
        page = site / "index.html"
        page.write_text("<p>Support the project</p>")
        self.assertEqual(self.scanner.site(site)["files"], 1)
        page.write_text("<p>example" + "@gmail.com</p>")
        with self.assertRaisesRegex(PrivacyError, "^website file 1: personal-contact$"):
            self.scanner.site(site)
        page.unlink()
        page.symlink_to(self.policy)
        with self.assertRaisesRegex(PrivacyError, "symbolic link"):
            self.scanner.site(site)

    @unittest.skipUnless(shutil.which("dpkg-deb"), "Debian builder required")
    def test_checks_debian_control_and_data_separately(self):
        stage = self.work / "package"
        control = stage / "DEBIAN/control"
        data = stage / "usr/share/framebridge/profile.txt"
        control.parent.mkdir(parents=True)
        data.parent.mkdir(parents=True)
        package = self.work / "test.deb"

        def build(maintainer, payload):
            control.write_text("Package: framebridge-fixture\nVersion: 1\nArchitecture: all\n"
                               f"Maintainer: {maintainer}\nDescription: Synthetic test\n")
            data.write_bytes(payload)
            subprocess.run(["dpkg-deb", "--root-owner-group", "--build", str(stage), str(package)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        build("Project <contact@example.org>", b"default profile")
        self.assertEqual(set(self.scanner.deb(package)), {"control", "data"})
        build("Example <example" + "@gmail.com>", b"default profile")
        with self.assertRaisesRegex(PrivacyError, r"^Debian control member \d+: personal-contact$"):
            self.scanner.deb(package)
        build("Project <contact@example.org>", b"review-fixture-host")
        with self.assertRaisesRegex(PrivacyError, r"^Debian data member \d+: private-identifier$"):
            self.scanner.deb(package)


if __name__ == "__main__":
    unittest.main()
