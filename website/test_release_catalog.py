#!/usr/bin/python3
"""Catalog guards and rendered shell syntax; no signing or installation."""
import copy
import json
from pathlib import Path
import runpy
import subprocess
import unittest

BASE = Path(__file__).resolve().parent
catalog = runpy.run_path(str(BASE / "release_catalog.py"))

class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.meta = json.loads((BASE / "release.example.json").read_text())

    def ready(self):
        meta = copy.deepcopy(self.meta)
        meta.update(state="ready", signing_fingerprint=catalog["FINGERPRINT"],
                    checksums_url="/downloads/TEST-SHA256SUMS",
                    checksums_signature_url="/downloads/TEST-SHA256SUMS.asc")
        meta["source"] = dict(filename="TEST-source.tar.gz", url="/downloads/TEST-source.tar.gz",
                              signature_url="/downloads/TEST-source.tar.gz.asc", sha256="a"*64, size_bytes=1)
        for index, profile in enumerate(meta["profiles"]):
            name = "TEST-profile-" + str(index) + ".deb"
            profile.update(filename=name, url="/downloads/"+name,
                           signature_url="/downloads/"+name+".asc", sha256="a"*64, size_bytes=1)
        return meta

    def test_preparation_has_no_install_or_download_claim(self):
        catalog["validate"](self.meta, False)
        text = catalog["download"](self.meta)
        self.assertIn("Release preparation", text)
        self.assertNotIn("sudo apt install", text)
        self.assertNotIn("Download .deb", text)

    def test_three_profile_shell_commands_are_valid_and_key_scoped(self):
        meta = self.ready()
        catalog["validate"](meta, True)
        for profile in meta["profiles"]:
            commands = catalog["apt_commands"](meta, profile)
            result = subprocess.run(["bash", "-n"], input=commands, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Suites: " + profile["suite"], commands)
            self.assertIn("Signed-By:", commands)
            self.assertIn("primary_count", commands)
            self.assertNotIn("trusted=yes", commands)
            self.assertNotIn("apt-key", commands)

    def test_wrong_distribution_metadata_is_rejected(self):
        self.meta["profiles"][1]["label"] = "Ubuntu 26.04"
        with self.assertRaisesRegex(ValueError, "Profile identity"):
            catalog["validate"](self.meta, False)

    def test_duplicate_profiles_are_rejected(self):
        self.meta["profiles"][1] = self.meta["profiles"][0]
        with self.assertRaisesRegex(ValueError, "Duplicate profile"):
            catalog["validate"](self.meta, False)

    def test_extra_shell_characters_in_key_url_are_rejected(self):
        meta = self.ready()
        meta["signing_key_url"] = "/debian/key';echo-malicious"
        with self.assertRaises(ValueError):
            catalog["validate"](meta, True)

    def test_missing_artifact_checksum_is_rejected(self):
        meta = self.ready()
        meta["profiles"][2]["sha256"] = ""
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            catalog["validate"](meta, True)

if __name__ == "__main__":
    unittest.main(verbosity=2)
