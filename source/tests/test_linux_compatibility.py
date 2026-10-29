"""Candidate profile/ABI selection; tests use inert native-file fixtures."""
import hashlib
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import stat
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from framebridge_expanded import compatibility as compat
from framebridge_expanded import runtime, uninstall
from framebridge_expanded import display_context, network


class LinuxCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.prefix = Path(self.temp.name)

    def artifact(self, profile_id):
        _distro, _release, minor, abi = compat.PROFILES[profile_id]
        vendor = self.prefix / compat.VENDOR_RELATIVE
        vendor.mkdir(parents=True, exist_ok=True)
        for old in vendor.rglob("*.so"):
            old.unlink()
        # These inert files test selection only; package tests inspect real ELF.
        suffix = ".cpython-" + abi[2:] + "-x86_64-linux-gnu.so"
        for rel in ["_cffi_backend", "pydantic_core/_pydantic_core", "rpds/rpds"]:
            path = vendor / (rel + suffix)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic ABI filename fixture, never imported")
        manifest = dict(schema=1, profile_id=profile_id, architecture="amd64",
                        python_minor=minor, python_abi=abi, validation_status="candidate")
        (self.prefix / "release-profile.json").write_text(json.dumps(manifest))
        return manifest

    def check(self, distro, version, python, **options):
        return compat.assess(self.prefix, os_release={"ID": distro, "VERSION_ID": version},
                             machine=options.pop("machine", "x86_64"), python_version=python,
                             libc=options.pop("libc", ("glibc", "2.39")), **options)

    def test_explicit_candidate_profiles_require_matching_artifacts(self):
        targets = [("debian13-amd64-py313", "debian", "13", (3, 13)),
                   ("ubuntu24.04-amd64-py312", "ubuntu", "24.04", (3, 12)),
                   ("ubuntu26.04-amd64-py314", "ubuntu", "26.04", (3, 14)),
                   ("mint22-amd64-py312", "linuxmint", "22.3", (3, 12)),
                   ("lmde7-amd64-py313", "linuxmint", "7", (3, 13))]
        for name, distro, version, python in targets:
            with self.subTest(name=name):
                self.artifact(name)
                result = self.check(distro, version, python)
                self.assertTrue(result["ok"], result)
                self.assertEqual(name, result["profile_id"])
                self.assertEqual("candidate", result["validation_status"])
                self.assertTrue(result["warnings"])

    def test_version_and_derivative_identity_do_not_silently_opt_in(self):
        self.artifact("ubuntu24.04-amd64-py312")
        for distro, version in [("ubuntu", "22.04"), ("ubuntu", "24.10"),
                                ("linuxmint", "21.3"), ("debian", "12"), ("pop", "24.04")]:
            with self.subTest(distro=distro, version=version):
                self.assertFalse(self.check(distro, version, (3, 12))["ok"])
        self.assertIsNone(compat.profile_for({"ID": "other", "ID_LIKE": "ubuntu", "VERSION_ID": "24.04"}))

    def test_public_builder_profiles_are_accepted_by_the_runtime_without_claiming_validation(self):
        # Use the real public builder's profile output, rather than duplicating
        # its validation_status value in a fixture. Archive/vendor verification
        # has separate tests; --check-vendor does not stage/install a package.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))
        import archive_sources
        import build_expanded
        targets = [("debian13-amd64-py313", "debian", "13", (3, 13)),
                   ("ubuntu24.04-amd64-py312", "ubuntu", "24.04", (3, 12)),
                   ("mint22-amd64-py312", "linuxmint", "22.3", (3, 12))]
        for profile_id, distro, version, python in targets:
            with self.subTest(profile=profile_id):
                self.artifact(profile_id)
                output = io.StringIO()
                globals_before = {name: getattr(build_expanded, name) for name in
                                  ("VERSION", "PUBLIC_RELEASE", "SOURCE_ARCHIVE", "PUBLIC_FILENAME", "RELEASE", "STAGE", "PREFIX")}
                args = ["--target", profile_id, "--public-release", "--source-manifest", str(self.prefix / "source.json"), "--check-vendor"]
                if profile_id != "debian13-amd64-py313":
                    args += ["--vendor-dir", str(self.prefix / "reviewed-cp312")]
                with patch.multiple(build_expanded, **globals_before), \
                     patch.object(build_expanded.subprocess, "run"), \
                     patch.object(build_expanded, "vendor_versions", return_value={}), \
                     patch.object(build_expanded, "validate_vendor", return_value={"vendor_sha256": "a" * 64}), \
                     patch.object(build_expanded, "release_source", return_value={"filename": "FrameBridge-0.3.4-source.tar.gz", "sha256": "b" * 64}), \
                     patch.object(build_expanded, "legal_preflight"), \
                     patch.object(archive_sources, "source_files", return_value={}), \
                     contextlib.redirect_stdout(output):
                    build_expanded.main(args)
                manifest = json.loads(output.getvalue())
                self.assertEqual(manifest["validation_status"], "release_build")
                (self.prefix / "release-profile.json").write_text(json.dumps(manifest))
                result = self.check(distro, version, python)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["validation_status"], manifest["validation_status"])
                self.assertIn("Release assembly does not establish a tested installation", result["warnings"][0])
                self.assertNotEqual(result["validation_status"], "validated")

    def test_interpreter_artifact_and_cpu_must_all_match(self):
        self.artifact("ubuntu24.04-amd64-py312")
        self.assertFalse(self.check("ubuntu", "24.04", (3, 13))["ok"])
        self.assertFalse(self.check("linuxmint", "22.1", (3, 12))["ok"])
        self.assertFalse(self.check("ubuntu", "24.04", (3, 12), machine="aarch64")["ok"])

    def test_nonstandard_interpreter_abi_is_rejected(self):
        self.artifact("ubuntu24.04-amd64-py312")
        self.assertFalse(self.check("ubuntu", "24.04", (3, 12), implementation="PyPy")["ok"])
        self.assertFalse(self.check("ubuntu", "24.04", (3, 12), abi_flags="t")["ok"])

    def test_mixed_native_abi_and_missing_extensions_are_rejected(self):
        self.artifact("ubuntu24.04-amd64-py312")
        vendor = self.prefix / compat.VENDOR_RELATIVE
        path = next(vendor.glob("_cffi_backend*.so"))
        path.rename(path.with_name(path.name.replace("cpython-312", "cpython-313")))
        result = self.check("ubuntu", "24.04", (3, 12))
        self.assertFalse(result["ok"])
        self.assertTrue(any("native extension does not match" in issue for issue in result["issues"]))
        self.artifact("ubuntu24.04-amd64-py312")
        path = next(vendor.glob("_cffi_backend*.so"))
        path.rename(path.with_name(path.name.replace("cpython-312", "cpython-312t")))
        self.assertFalse(self.check("ubuntu", "24.04", (3, 12))["ok"])
        self.artifact("ubuntu24.04-amd64-py312")
        next((vendor / "rpds").glob("*.so")).unlink()
        self.assertFalse(self.check("ubuntu", "24.04", (3, 12))["ok"])

    def test_libc_floor_and_libc_family_are_checked(self):
        self.artifact("ubuntu24.04-amd64-py312")
        for libc in [("glibc", "2.33"), ("musl", "1.2.5"), ("", "")]:
            with self.subTest(libc=libc):
                self.assertFalse(self.check("ubuntu", "24.04", (3, 12), libc=libc)["ok"])
        self.assertTrue(self.check("ubuntu", "24.04", (3, 12), libc=("glibc", "2.34"))["ok"])

    def test_invalid_or_inconsistent_manifest_fails_closed(self):
        original = self.artifact("ubuntu24.04-amd64-py312")
        path = self.prefix / "release-profile.json"
        for value in ["{", "[]", json.dumps({**original, "schema": True}),
                      json.dumps({**original, "python_abi": "cp313"}),
                      json.dumps({**original, "validation_status": "unknown"})]:
            with self.subTest(value=value):
                path.write_text(value)
                result = self.check("ubuntu", "24.04", (3, 12))
                self.assertFalse(result["ok"])
                self.assertTrue(any("Invalid release-profile" in issue for issue in result["issues"]))

    def test_missing_manifest_cannot_silently_relabel_legacy_vendor(self):
        self.artifact("debian13-amd64-py313")
        (self.prefix / "release-profile.json").unlink()
        self.assertFalse(self.check("linuxmint", "22.1", (3, 13))["ok"])
        self.assertTrue(self.check("debian", "13", (3, 13))["ok"])
        with patch.object(compat, "INSTALLED_PREFIX", self.prefix):
            self.assertFalse(self.check("debian", "13", (3, 13))["ok"])

    def test_wayland_diagnostic_never_claims_native_desktop_control(self):
        result = compat.display_support({"XDG_SESSION_TYPE": "wayland", "DISPLAY": ":0"}, {"kind": "text"})
        self.assertFalse(result["native_wayland_cua"])
        self.assertFalse(result["physical_x11_verified"])
        self.assertIn("XWayland", result["notes"][0])
        virtual = compat.display_support({"WAYLAND_DISPLAY": "wayland-0"}, {"kind": "virtual-x11"})
        self.assertTrue(virtual["private_virtual_x11"])
        self.assertFalse(virtual["native_wayland_cua"])

    def test_doctor_uses_profile_result_and_start_stops_before_creating_files(self):
        rejected = dict(ok=False, profile_id="ubuntu24.04-amd64-py312", issues=["ABI mismatch"], warnings=[])
        with patch.object(runtime, "state_path", return_value=self.prefix), \
             patch.object(runtime, "load_config", return_value={"role": "node", "headless": "auto"}), \
             patch.object(runtime, "environment", return_value={"XDG_SESSION_TYPE": "wayland"}), \
             patch.object(runtime.shutil, "which", return_value="/fixture/command"), \
             patch.object(compat, "assess", return_value=rejected), \
             patch.object(display_context, "missing_graphical_commands", return_value=[]), \
             patch.object(display_context, "describe", return_value={"kind": "text"}), \
             patch.object(network, "inspect", return_value={}), \
             patch.object(runtime, "status", return_value={"running": False}):
            doctor = runtime.doctor(self.prefix)
            self.assertFalse(doctor["ok"])
            self.assertIn("ABI mismatch", doctor["issues"])
            self.assertFalse(doctor["compatible_debian_13_amd64"])
            self.assertEqual("wayland", doctor["display_support"]["session_type"])
            self.assertFalse(runtime.start(self.prefix)["ok"])
            self.assertFalse((self.prefix / "logs").exists())


class ManagedInterpreterValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = "/home/fixture/profile"
        self.unit = self.root / ("framebridge-autostart-" + hashlib.sha256(self.state.encode()).hexdigest()[:16] + ".service")

    def unit_text(self, interpreter):
        return '\n'.join(["# FrameBridge managed autostart v1", 'Environment="PYTHONPATH=' + uninstall.MODULE_ROOT + '"',
                          "ExecStart=" + interpreter + " -B -m framebridge_expanded.autostart _run --state " + self.state])

    def test_matching_minor_system_interpreters_preserve_cleanup(self):
        self.assertEqual(compat.SYSTEM_PYTHONS, uninstall.SYSTEM_PYTHONS)
        for interpreter in sorted(compat.SYSTEM_PYTHONS):
            with self.subTest(interpreter=interpreter):
                self.unit.write_text(self.unit_text(interpreter))
                self.assertTrue(uninstall.managed_unit(self.unit, os.getuid()))

    def test_arbitrary_interpreters_duplicate_commands_and_links_remain_rejected(self):
        for interpreter in ["/tmp/python3.12", "/usr/bin/python3.11", "/usr/local/bin/python3.14"]:
            self.unit.write_text(self.unit_text(interpreter))
            self.assertFalse(uninstall.managed_unit(self.unit, os.getuid()))
        self.unit.write_text(self.unit_text("/usr/bin/python3.12") + "\nExecStart=/bin/true")
        self.assertFalse(uninstall.managed_unit(self.unit, os.getuid()))
        real = self.root / "real"
        real.write_text(self.unit_text("/usr/bin/python3.12"))
        self.unit.unlink()
        self.unit.symlink_to(real)
        self.assertFalse(uninstall.managed_unit(self.unit, os.getuid()))


class RootHelperCleanupTests(unittest.TestCase):
    def test_root_purge_uses_only_fixed_helper_and_bounded_environment(self):
        calls = []
        def run(args, **options):
            calls.append((args, options))
            return SimpleNamespace(returncode=0)
        with patch.object(uninstall.os, "getuid", return_value=0), \
             patch.object(uninstall.os.path, "lexists", return_value=True), \
             patch.object(uninstall, "_trusted_root_file", return_value=True):
            uninstall.purge_credential_helpers(run=run)
        self.assertEqual([str(uninstall.CREDENTIAL_HELPER), "purge-all"], calls[0][0])
        self.assertEqual({"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, calls[0][1]["env"])
        self.assertEqual(30, calls[0][1]["timeout"])

    def test_nonroot_or_unsafe_helper_never_executes(self):
        calls = []
        with patch.object(uninstall.os, "getuid", return_value=1000):
            with self.assertRaises(RuntimeError):
                uninstall.purge_credential_helpers(run=lambda *a, **kw: calls.append(a))
        with patch.object(uninstall.os, "getuid", return_value=0), \
             patch.object(uninstall.os.path, "lexists", return_value=True), \
             patch.object(uninstall, "_trusted_root_file", return_value=False):
            with self.assertRaises(RuntimeError):
                uninstall.purge_credential_helpers(run=lambda *a, **kw: calls.append(a))
        self.assertEqual([], calls)

    def test_failure_to_purge_pauses_removal(self):
        with patch.object(uninstall.os, "getuid", return_value=0), \
             patch.object(uninstall.os.path, "lexists", return_value=True), \
             patch.object(uninstall, "_trusted_root_file", return_value=True):
            with self.assertRaises(RuntimeError):
                uninstall.purge_credential_helpers(run=lambda *a, **kw: SimpleNamespace(returncode=1))

    def test_fixed_helper_trust_rejects_links_nonroot_and_writable_parents(self):
        target = uninstall.CREDENTIAL_HELPER
        for mode, owner in [(stat.S_IFLNK | 0o777, 0), (stat.S_IFREG | 0o755, 1000),
                            (stat.S_IFREG | 0o777, 0)]:
            with self.subTest(mode=mode, owner=owner), \
                 patch.object(Path, "lstat", return_value=SimpleNamespace(st_mode=mode, st_uid=owner)):
                self.assertFalse(uninstall._trusted_root_file(target))
        def info(path):
            return SimpleNamespace(st_uid=0, st_mode=(stat.S_IFREG | 0o755) if path == target else (stat.S_IFDIR | 0o775))
        with patch.object(Path, "lstat", autospec=True, side_effect=info):
            self.assertFalse(uninstall._trusted_root_file(target))


if __name__ == "__main__":
    unittest.main()
