"""Offline regression tests for the stable APT mirror transfer policy."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location("refresh",Path(__file__).with_name("refresh-stable-apt.py"))
refresh=importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)

class CacheTests(unittest.TestCase):
    name="pool/main/f/framebridge-original-expanded/example.deb"
    data=b"synthetic verified fixture"
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        self.repo=root/"repo"
        self.work=root/"stage"
        self.repo.mkdir()
        self.work.mkdir()
        self.cache=self.repo/self.name
        self.cache.parent.mkdir(parents=True)
        self.digest=hashlib.sha256(self.data).hexdigest()
    def call(self):
        return refresh.package_from_cache_or_origin(self.work,self.repo,self.name,self.digest,len(self.data))
    def test_cached_package_no_download(self):
        self.cache.write_bytes(self.data)
        with patch.object(refresh,"get",side_effect=AssertionError("network forbidden")) as get:
            self.assertEqual(self.call(),self.cache)
            get.assert_not_called()
    def test_wrong_cached_bytes_fail_closed(self):
        self.cache.write_bytes(b"x"*len(self.data))
        with patch.object(refresh,"get",side_effect=AssertionError("network forbidden")) as get:
            with self.assertRaisesRegex(RuntimeError,"Immutable pool conflict"):self.call()
            get.assert_not_called()
    def test_missing_package_requests_exact_signed_path(self):
        with patch.object(refresh,"get",return_value=self.work/self.name) as get:
            self.assertEqual(self.call(),self.work/self.name)
            get.assert_called_once_with(self.work,self.name,len(self.data))
    def test_symlink_refused(self):
        target=self.work/"data"
        target.write_bytes(self.data)
        self.cache.symlink_to(target)
        with patch.object(refresh,"get",side_effect=AssertionError("network forbidden")):
            with self.assertRaisesRegex(RuntimeError,"Symlink refused"):self.call()
    def test_unexpected_path_refused(self):
        with patch.object(refresh,"get",side_effect=AssertionError("network forbidden")):
            with self.assertRaises(RuntimeError):refresh.package_from_cache_or_origin(self.work,self.repo,"../example.deb",self.digest,len(self.data))
if __name__=="__main__":unittest.main()
