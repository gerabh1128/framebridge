#!/usr/bin/python3
"""Catalog guards and rendered shell syntax; no signing or installation."""
import copy
import hashlib
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
        guide = json.loads((BASE.parent / "source/docs/COMMAND-GUIDE.json").read_text())
        self.assertEqual(self.meta["version"], guide["version"])
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

class PublicGuideTests(unittest.TestCase):
    def setUp(self):
        # Keep the historical erratum fixture independent of the current guide.
        diagnostics = {
            "version": "0.3.7",
            "sections": [{
                "heading": "Doctor",
                "body": "The published 0.3.3 scope is Debian 13 with cp313 and Ubuntu 24.04/Mint 22.x with cp312.",
            }],
        }
        diagnostics["guide_sha256"] = catalog["guide_digest"](diagnostics)
        self.original = {
            "version": "0.3.7",
            "topics": {
                "diagnostics": diagnostics,
                "start": {"version": "0.3.7", "title": "Start here"},
            },
        }
        self.original["guide_sha256"] = catalog["guide_digest"](self.original)
        self.raw = json.dumps(self.original).encode()

    def test_erratum_is_a_distinct_web_copy_without_mutating_frozen_guide(self):
        published = catalog["publication_guide"](self.raw)
        doctor = next(s for s in published["topics"]["diagnostics"]["sections"] if s["heading"] == "Doctor")
        self.assertIn("The 0.3.7 release profiles are", doctor["body"])
        self.assertNotIn("published 0.3.3", doctor["body"])
        self.assertEqual(json.loads(self.raw), self.original)
        self.assertEqual(published["publication"]["frozen_guide_file_sha256"], hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(published["publication"]["frozen_guide_sha256"], self.original["guide_sha256"])
        self.assertNotEqual(published["guide_sha256"], self.original["guide_sha256"])
        for name, topic in self.original["topics"].items():
            if name != "diagnostics":
                self.assertEqual(published["topics"][name], topic)

    def test_publication_digests_match_the_modified_content(self):
        published = catalog["publication_guide"](self.raw)
        for value in (published, published["topics"]["diagnostics"]):
            self.assertEqual(value["guide_sha256"], catalog["guide_digest"]({k: v for k, v in value.items() if k != "guide_sha256"}))

    def test_invalid_frozen_export_digest_is_rejected(self):
        self.original["guide_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "export digest"):
            catalog["publication_guide"](json.dumps(self.original).encode())

    def test_unexpected_doctor_text_requires_review(self):
        doctor = next(s for s in self.original["topics"]["diagnostics"]["sections"] if s["heading"] == "Doctor")
        doctor["body"] = doctor["body"].replace("published 0.3.3", "published 0.3.7")
        topic = self.original["topics"]["diagnostics"]
        topic["guide_sha256"] = catalog["guide_digest"]({k: v for k, v in topic.items() if k != "guide_sha256"})
        self.original["guide_sha256"] = catalog["guide_digest"]({k: v for k, v in self.original.items() if k != "guide_sha256"})
        with self.assertRaisesRegex(ValueError, "Doctor text changed"):
            catalog["publication_guide"](json.dumps(self.original).encode())

    def test_other_versions_are_not_relabelled(self):
        current = (BASE.parent / "source/docs/COMMAND-GUIDE.json").read_bytes()
        self.assertEqual(catalog["publication_guide"](current), json.loads(current))
        self.original["version"] = "0.3.5"
        self.assertEqual(catalog["publication_guide"](json.dumps(self.original).encode()), self.original)

if __name__ == "__main__":
    unittest.main(verbosity=2)
