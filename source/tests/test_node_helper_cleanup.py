"""Policy-name inspection only: no sudo, registry reads or rule contents."""
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from framebridge_expanded import autostart, credential_helper


class NodeHelperCleanupInspectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "profile"
        self.policies = self.root / "policies"
        self.policies.mkdir()
        self.profile = autostart._profile_id(self.state)
        self.uid = str(os.getuid())
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(autostart, "_helper_ready").start()
        mock.patch.object(credential_helper, "SUDOERS", self.policies).start()
        self.trust = mock.patch.object(credential_helper, "_root_path", side_effect=lambda path, **kw: path)
        self.trust.start()

    def test_own_canonical_decrypt_and_authorization_names_require_root_cleanup(self):
        names = [
            "framebridge-credential-" + self.uid + "-" + self.profile + "-" + "a" * 64,
            "framebridge-credential-authorize-" + self.uid + "-" + self.profile + "-host",
            "framebridge-credential-authorize-" + self.uid + "-" + self.profile + "-user",
        ]
        for name in names:
            with self.subTest(name=name):
                path = self.policies / name
                path.touch()
                with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("Do not read rule contents")):
                    self.assertTrue(autostart._node_helper_cleanup_required(self.state))
                path.unlink()

    def test_ambiguous_names_require_review_even_alongside_a_canonical_grant(self):
        canonical = "framebridge-credential-" + self.uid + "-" + self.profile + "-" + "a" * 64
        for name in ["framebridge-credential-" + self.uid + "-" + self.profile + "-invalid",
                     "framebridge-credential-authorize-" + self.uid + "-" + self.profile + "-unknown"]:
            with self.subTest(name=name), mock.patch.object(Path, "iterdir", return_value=iter([self.policies / canonical, self.policies / name])):
                with self.assertRaisesRegex(RuntimeError, "ambiguous.*administrator review"):
                    autostart._node_helper_cleanup_required(self.state)

    def test_symlink_in_own_namespace_still_requires_root_cleanup(self):
        path = self.policies / ("framebridge-credential-" + self.uid + "-" + self.profile + "-" + "a" * 64)
        path.symlink_to(self.root / "nonexistent")
        self.assertTrue(autostart._node_helper_cleanup_required(self.state))

    def test_permission_denied_listing_is_not_treated_as_empty(self):
        with mock.patch.object(Path, "iterdir", side_effect=PermissionError("synthetic denied")):
            self.assertTrue(autostart._node_helper_cleanup_required(self.state))

    def test_empty_verified_policy_directory_omits_privileged_cleanup(self):
        self.assertFalse(autostart._node_helper_cleanup_required(self.state))

    def test_insecure_parent_or_policy_directory_is_not_trusted(self):
        self.trust.stop()
        with mock.patch.object(credential_helper, "SUDOERS", Path("/etc/sudoers.d")):
            for unsafe, kind, uid, mode in [
                (Path("/etc"), stat.S_IFDIR, 0, 0o775),
                (Path("/etc/sudoers.d"), stat.S_IFLNK, 0, 0o777),
                (Path("/etc/sudoers.d"), stat.S_IFDIR, 12345, 0o755),
            ]:
                def info(path, *, selected=unsafe, selected_kind=kind, selected_uid=uid, selected_mode=mode):
                    return os.stat_result((selected_kind | selected_mode if path == selected else stat.S_IFDIR | 0o755,
                                           1, 1, 1, selected_uid if path == selected else 0, 0, 0, 0, 0, 0))
                with self.subTest(path=unsafe, kind=kind, uid=uid, mode=mode), mock.patch.object(Path, "lstat", info), mock.patch.object(Path, "iterdir") as listing:
                    self.assertTrue(autostart._node_helper_cleanup_required(self.state))
                    listing.assert_not_called()


if __name__ == "__main__":
    unittest.main()
