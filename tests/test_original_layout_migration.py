"""Regression tests for package-layout upgrades in the manual Original launcher.

Run with: python3 -m unittest discover -s tests -p test_original_layout_migration.py
These tests use temporary profiles and never touch a live installation.
"""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import types
import unittest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "source/packaging/framebridge-original"
LINKS = (
    (".local/share/framebridge/fbterm/bin", "fbterm/bin"),
    (".local/share/framebridge/tool-adapter/bin", "tool-adapter/bin"),
    (".local/lib/framebridge-native/host", "framebridge-native/host"),
    ("Descargas/framebridge_v0_1/host", "framebridge-native/host"),
)


class OriginalLayoutMigration(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="framebridge-upgrade-links-")
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.prefix = base / "pkg"
        (self.prefix / "bin").mkdir(parents=True)
        shutil.copyfile(SCRIPT, self.prefix / "bin/framebridge-original")
        (self.prefix / "layout.json").write_text(json.dumps({
            "fbterm_bin": "original/workstation/fbterm/bin",
            "adapter_bin": "original/workstation/tool-adapter/bin",
            "native_host_dir": "original/workstation/framebridge-native/host",
            "native_host": "original/workstation/framebridge-native/host/native",
        }))
        for _, suffix in LINKS:
            (self.prefix / "original/workstation" / suffix).mkdir(parents=True, exist_ok=True)
        loader = importlib.machinery.SourceFileLoader(
            "framebridge_original_migration_test", str(self.prefix / "bin/framebridge-original"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        self.module = importlib.util.module_from_spec(spec)
        loader.exec_module(self.module)
        self.state = base / "state"
        self.home = base / "home"
        self.runtime = base / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.module.settings = lambda args: (self.state, self.home,
                                              {"XDG_RUNTIME_DIR": str(self.runtime)})

    def old_links(self, role="legacy-role"):
        for rel, suffix in LINKS:
            dest = self.home / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.symlink_to(self.prefix / "original" / role / suffix,
                            target_is_directory=True)

    def prepare(self):
        return self.module.prepare(types.SimpleNamespace())

    def assert_new_links(self):
        for rel, suffix in LINKS:
            dest = self.home / rel
            self.assertTrue(dest.is_symlink())
            self.assertEqual(dest.resolve(),
                             (self.prefix / "original/workstation" / suffix).resolve())

    def test_upgrade_all_stale_links_and_idempotence(self):
        self.old_links("legacy-role")
        self.prepare()
        self.assert_new_links()
        manifests = list(self.state.glob("layout-migration-*.json"))
        self.assertEqual(len(manifests), 1)
        self.assertEqual(len(json.loads(manifests[0].read_text())["links"]), 4)
        self.prepare()
        self.assertEqual(list(self.state.glob("layout-migration-*.json")), manifests)

    def test_fresh_install(self):
        self.prepare()
        self.assert_new_links()
        self.prepare()
        self.assertEqual(list(self.state.glob("layout-migration-*.json")), [])

    def test_reject_custom_dangling_symlink(self):
        self.old_links("legacy-role")
        dest = self.home / LINKS[0][0]
        dest.unlink()
        custom = Path(self.temporary.name) / "unrelated" / "fbterm"
        dest.symlink_to(custom, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "Unexpected link"):
            self.prepare()
        self.assertEqual(os.readlink(dest), str(custom))

    def test_reject_existing_legacy_target(self):
        self.old_links("legacy-role")
        (self.prefix / "original/legacy-role/fbterm/bin").mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError, "Unexpected link"):
            self.prepare()
        self.assertEqual(os.readlink(self.home / LINKS[0][0]),
                         str(self.prefix / "original/legacy-role/fbterm/bin"))

    def test_partially_migrated_profile(self):
        self.old_links("legacy-role")
        dest = self.home / LINKS[0][0]
        dest.unlink()
        dest.symlink_to(self.prefix / "original/workstation/fbterm/bin",
                        target_is_directory=True)
        self.prepare()
        self.assert_new_links()
        manifest = next(self.state.glob("layout-migration-*.json"))
        self.assertEqual(len(json.loads(manifest.read_text())["links"]), 3)


if __name__ == "__main__":
    unittest.main()
