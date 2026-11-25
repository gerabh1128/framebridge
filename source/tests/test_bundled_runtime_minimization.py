"""Only the pinned private runtime's unused package installer is excluded."""
import sys
from pathlib import Path
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"packaging"))
import bundled_runtime as b

class RuntimeMinimization(unittest.TestCase):
    def test_omit_exact_installer_surfaces(self):
        for n in ("python/bin/pip","python/bin/pip3","python/bin/pip3.13",
                  "python/lib/python3.13/ensurepip",
                  "python/lib/python3.13/ensurepip/_bundled/pip.whl",
                  "python/lib/python3.13/site-packages/pip",
                  "python/lib/python3.13/site-packages/pip/_vendor/urllib3/util/url.py",
                  "python/lib/python3.13/site-packages/pip-26.3.dist-info/METADATA"):
            self.assertTrue(b.omitted_runtime_member(n),n)
    def test_keep_required_runtime_and_other_names(self):
        for n in ("python/bin/python3.13","python/lib/python3.13/ssl.py",
                  "python/lib/python3.13/venv/__init__.py",
                  "python/lib/python3.13/site-packages/pip_other",
                  "python/lib/python3.13/site-packages/pip-helper.py",
                  "python/lib/python3.13/lib-dynload/_ssl.so",
                  "python/lib/libpython3.13.so.1.0"):
            self.assertFalse(b.omitted_runtime_member(n),n)
if __name__=="__main__":unittest.main()
