#!/usr/bin/env python3
"""Integration checks with disposable DEBs and a disposable signing identity."""
import argparse
import contextlib
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("apt_builder", Path(__file__).with_name("build_apt_repository.py"))
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)

class RepositoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="framebridge-apt-tests-")
        cls.base = Path(cls.temp.name)
        cls.key_home = cls.base / "test-key"
        cls.key_home.mkdir(mode=0o700)
        subprocess.run(["gpg", "--no-options", "--homedir", str(cls.key_home), "--batch",
                        "--pinentry-mode", "loopback", "--passphrase", "", "--quick-generate-key",
                        "FrameBridge disposable test <test@invalid>", "ed25519", "sign", "0"],
                       check=True, capture_output=True)
        result = subprocess.run(["gpg", "--no-options", "--homedir", str(cls.key_home),
                                 "--with-colons", "--list-keys"], check=True, capture_output=True).stdout.decode()
        cls.original_fingerprint = builder.FINGERPRINT
        builder.FINGERPRINT = next(x.split(":")[9] for x in result.splitlines() if x.startswith("fpr:"))
        cls.public_key = cls.base / "test.asc"
        cls.public_key.write_bytes(subprocess.run(
            ["gpg", "--no-options", "--homedir", str(cls.key_home), "--armor", "--export", builder.FINGERPRINT],
            check=True, capture_output=True).stdout)
        cls.debs = {}
        for distro, (_, _, _, python) in builder.PROFILES.items():
            tree = cls.base / distro
            (tree / "DEBIAN").mkdir(parents=True)
            (tree / "DEBIAN").chmod(0o755)
            (tree / "DEBIAN/control").write_text(
                f"Package: {builder.PACKAGE}\nVersion: 0.3.3\nArchitecture: amd64\n"
                f"Maintainer: FrameBridge <test@invalid>\nDepends: python3 (>= {python})\n"
                f"Description: Disposable {distro} test package\n")
            (tree / "distribution.txt").write_text(distro)
            deb = cls.base / (distro + ".deb")
            subprocess.run(["dpkg-deb", "--build", str(tree), str(deb)], check=True, capture_output=True)
            cls.debs[distro] = deb

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["gpgconf", "--homedir", str(cls.key_home), "--kill", "gpg-agent"], capture_output=True)
        builder.FINGERPRINT = cls.original_fingerprint
        cls.temp.cleanup()

    def setUp(self):
        self.output = Path(tempfile.mkdtemp(dir=self.base, prefix="output-")) / "repository"

    def prepare(self, **changes):
        args = argparse.Namespace(output=self.output, version="0.3.3", public_key=self.public_key,
                                  epoch=1793199600, **self.debs)
        for key, value in changes.items():
            setattr(args, key, value)
        with contextlib.redirect_stdout(io.StringIO()):
            builder.prepare(args)
        raw = (self.output / "APT-INPUT-MANIFEST.json").read_bytes()
        return hashlib.sha256(raw).hexdigest(), json.loads(raw)

    def sign_test_releases(self):
        for source, signed, clearsign in builder.signature_paths(self.output, sign_only=True):
            subprocess.run(["gpg", "--no-options", "--homedir", str(self.key_home), "--batch",
                            "--pinentry-mode", "loopback", "--passphrase", "", "--local-user", builder.FINGERPRINT,
                            "--digest-algo", "SHA256", "--output", str(signed),
                            "--clearsign" if clearsign else "--detach-sign", str(source)],
                           check=True, capture_output=True)

    def test_actual_apt_update_accepts_three_signed_suites(self):
        expected, manifest = self.prepare()
        self.sign_test_releases()
        self.assertTrue(builder.verify_signatures(self.output, manifest)["verified"])
        for suite, *_ in builder.PROFILES.values():
            isolated = self.output.parent / ("apt-" + suite)
            isolated.mkdir()
            (isolated / "state/lists/partial").mkdir(parents=True)
            (isolated / "cache/archives/partial").mkdir(parents=True)
            source = isolated / "sources.list"
            source.write_text(f"deb [arch=amd64 signed-by={self.public_key}] file:{self.output} {suite} main\n")
            result = subprocess.run(["apt-get", "-o", f"Dir::State={isolated}/state",
                "-o", f"Dir::Cache={isolated}/cache", "-o", f"Dir::Etc::sourcelist={source}",
                "-o", "Dir::Etc::sourceparts=-", "-o", "Acquire::Languages=none",
                "-o", "Debug::NoLocking=1", "update"], capture_output=True, text=True, timeout=35)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Reading package lists", result.stdout)
        self.assertEqual(builder.check_inputs(self.output, expected)["version"], "0.3.3")

    def test_altered_package_is_rejected_before_signing(self):
        expected, manifest = self.prepare()
        p = self.output / manifest["artifacts"][1]["filename"]
        p.write_bytes(p.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "Input changed"):
            builder.check_inputs(self.output, expected)

    def test_altered_index_is_rejected(self):
        expected, _ = self.prepare()
        (self.output / "dists/noble/main/binary-amd64/Packages").write_text("altered")
        with self.assertRaisesRegex(ValueError, "Input changed"):
            builder.check_inputs(self.output, expected)

    def test_wrong_version_does_not_create_repository(self):
        with self.assertRaisesRegex(ValueError, "wrong version"):
            self.prepare(version="0.3.4")
        self.assertFalse(self.output.exists())

    def test_existing_output_never_overwritten(self):
        self.output.mkdir()
        sentinel = self.output / "sentinel"
        sentinel.write_text("published")
        with self.assertRaisesRegex(ValueError, "never overwrite"):
            self.prepare()
        self.assertEqual(sentinel.read_text(), "published")

    def test_manifest_traversal_is_rejected(self):
        _, manifest = self.prepare()
        manifest["files"]["../outside"] = {"bytes": 0, "sha256": hashlib.sha256(b"").hexdigest()}
        raw = json.dumps(manifest).encode()
        (self.output / "APT-INPUT-MANIFEST.json").write_bytes(raw)
        with self.assertRaisesRegex(ValueError, "Unsafe manifest path"):
            builder.check_inputs(self.output, hashlib.sha256(raw).hexdigest())

    def test_public_key_bundle_with_extra_primary_key_is_rejected(self):
        bundle = self.output.parent / "extra-primary-key.asc"
        bundle.write_bytes(self.public_key.read_bytes() + self.public_key.read_bytes())
        with self.assertRaisesRegex(ValueError, "exactly one official primary"):
            self.prepare(public_key=bundle)
        self.assertFalse(self.output.exists())


    def test_one_release_manifest_covers_downloads_and_preserves_dependency_signatures(self):
        source = self.output.parent / "FrameBridge-0.3.3-source.tar.gz"
        source.write_bytes(b"disposable source-archive fixture")
        original = builder.DEPENDENCY_SOURCE_ASSETS
        dependencies = []
        signatures = {}
        try:
            builder.DEPENDENCY_SOURCE_ASSETS = {}
            for index, name in enumerate(original):
                path = self.output.parent / name
                data = ("disposable dependency " + str(index)).encode()
                path.write_bytes(data)
                builder.DEPENDENCY_SOURCE_ASSETS[name] = (len(data), hashlib.sha256(data).hexdigest())
                signature = path.with_name(name + ".asc")
                subprocess.run(["gpg", "--no-options", "--homedir", str(self.key_home), "--batch",
                    "--pinentry-mode", "loopback", "--passphrase", "", "--local-user", builder.FINGERPRINT,
                    "--armor", "--output", str(signature), "--detach-sign", str(path)], check=True, capture_output=True)
                signatures[name] = signature.read_bytes()
                dependencies.append(path)
            expected, manifest = self.prepare(source=source, dependency_source=dependencies)
            self.assertEqual(len(manifest["download_assets"]), 7)
            for path in dependencies:
                self.assertEqual((self.output / "downloads" / (path.name + ".asc")).read_bytes(),
                                 signatures[path.name])
            self.sign_test_releases()
            self.assertTrue(builder.verify_signatures(self.output, manifest)["verified"])
            self.assertEqual(builder.check_inputs(self.output, expected)["version"], "0.3.3")
            source_copy = self.output / "downloads" / source.name
            source_copy.write_bytes(b"altered")
            with self.assertRaisesRegex(ValueError, "Input changed"):
                builder.check_inputs(self.output, expected)
        finally:
            builder.DEPENDENCY_SOURCE_ASSETS = original

    def test_changed_signed_release_fails_verification(self):
        _, manifest = self.prepare()
        self.sign_test_releases()
        path = self.output / "dists/trixie/InRelease"
        path.write_bytes(path.read_bytes().replace(b"Components: main", b"Components: evil"))
        with self.assertRaises(subprocess.CalledProcessError):
            builder.verify_signatures(self.output, manifest)

if __name__ == "__main__":
    unittest.main(verbosity=2)
