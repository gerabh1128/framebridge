"""The public vendor name root_model.py is not the build user's home directory."""
import importlib.util
from pathlib import Path
import sys
import unittest


PACKAGING = Path(__file__).resolve().parents[1] / "packaging"
sys.path.insert(0, str(PACKAGING))
spec = importlib.util.spec_from_file_location("privacy_test_builder", PACKAGING / "build_expanded.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class BuildPrivacyPathTests(unittest.TestCase):
    def test_public_module_name_does_not_block_a_root_user_build(self):
        data = b'{"path":"source/vendor/pydantic/root_model.py"}'
        self.assertFalse(builder.contains_private_path(data, "/root"))

    def test_actual_home_paths_are_still_rejected(self):
        for data in (b'"/root/private-file"', b'"/root"', b'input: /root\n',
                     b'file:///root/private-file', b'"/root/"'):
            with self.subTest(data=data):
                self.assertTrue(builder.contains_private_path(data, "/root"))

    def test_neighboring_names_are_not_this_home_directory(self):
        for data in (b'"/home/tester-other/file"', b'"/home/tester_backup/file"'):
            self.assertFalse(builder.contains_private_path(data, "/home/tester"))
        self.assertTrue(builder.contains_private_path(b'"/home/tester/file"', "/home/tester"))


if __name__ == "__main__":
    unittest.main()
