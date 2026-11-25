"""A public artifact must not be built without an external private policy."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))
from release_privacy import PrivacyError, external_public_policy
import archive_sources
import build_expanded


class PublicPolicyRequiredTests(unittest.TestCase):
    def test_missing_policy_refused_before_build(self):
        for main,args in ((archive_sources.main,["--public-release"]),
                          (build_expanded.main,["--target","debian13-amd64-py313","--public-release"])):
            with self.subTest(main=main.__module__),contextlib.redirect_stderr(io.StringIO()) as err:
                with self.assertRaises(SystemExit) as e:
                    main(args)
                self.assertEqual(e.exception.code,2)
                self.assertIn("external privacy policy",err.getvalue())

    def test_policy_outside_tree_required(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/"repository";root.mkdir()
            inside=root/"private-policy.json";inside.write_text(json.dumps(["fixture-private-host"]))
            outside=Path(temp)/"private-policy.json";outside.write_bytes(inside.read_bytes())
            for bad in (None,inside,Path(temp)/"missing.json"):
                with self.assertRaises(PrivacyError):
                    external_public_policy(bad,root)
            link=Path(temp)/"link.json";link.symlink_to(outside)
            with self.assertRaises(PrivacyError):
                external_public_policy(link,root)
            self.assertEqual(external_public_policy(outside,root),outside.resolve())
