"""Portable release catalog verification, independent of live release credentials."""
import json,runpy,subprocess,unittest,copy
from pathlib import Path
BASE=Path(__file__).resolve().parent
C=runpy.run_path(str(BASE/"release_catalog.py"))
def fixture():
    m=json.loads((BASE/"release.example.json").read_text())
    m.update(package_mode="portable",version="0.3.9+fbterm.16",state="ready",apt_repository_url="https://framebridge.betancourt.one/debian/stable/",signing_key_url="/debian/stable/framebridge-archive-keyring.gpg",checksums_url="/downloads/fbterm16/SHA256SUMS",checksums_signature_url="/downloads/fbterm16/SHA256SUMS.asc",signing_fingerprint=C["FINGERPRINT"])
    def asset(n):
        return dict(filename=n,url="/downloads/fbterm16/"+n,sha256="a"*64,size_bytes=123)
    m["profiles"]=[dict(asset("FrameBridge-test.deb"),profile_id="linux-amd64-cp313",suite="stable",architecture="amd64",python="3.13.18",signature_url="/downloads/fbterm16/FrameBridge-test.deb.asc")]
    m["source"]=dict(asset("source.tar.gz"),verification="signed-checksums")
    m["dependency_sources"]=[dict(asset(n),verification="signed-checksums") for n in ("runtime.tar","deps1.tar.gz","deps2.tar.gz")]
    m["source_manifest"]={"url":"/downloads/fbterm16/source-manifest.json"}
    return m
class PortableCatalog(unittest.TestCase):
    def test_one_profile_signed_scope(self):
        m=fixture();C["validate"](m,True)
        h=C["download"](m)
        self.assertIn("One Linux package.",h)
        self.assertNotIn("Three distribution profiles",h)
        self.assertIn("signed SHA256SUMS",h)
        self.assertEqual(subprocess.run(["bash","-n"],input=C["portable_install_commands"](m),text=True,capture_output=True).returncode,0)
    def test_reject_wrong_suite_and_extra_profiles(self):
        m=fixture();m["profiles"][0]["suite"]="trixie"
        with self.assertRaises(ValueError):C["validate"](m,True)
        m=fixture();m["profiles"].append(copy.deepcopy(m["profiles"][0]))
        with self.assertRaises(ValueError):C["validate"](m,True)
    def test_reject_unverified_source(self):
        m=fixture();m["dependency_sources"][0]["verification"]="none"
        with self.assertRaises(ValueError):C["validate"](m,True)
    def test_current_guide_is_exact_frozen_export(self):
        raw=(BASE.parent/"source/docs/COMMAND-GUIDE.json").read_bytes()
        old=json.loads(raw);g=C["publication_guide"](raw)
        self.assertEqual(old["version"],"0.3.9+fbterm.18")
        self.assertEqual(old,g)
        self.assertEqual(g["guide_sha256"],C["guide_digest"]({k:v for k,v in g.items() if k!="guide_sha256"}))
if __name__=="__main__":unittest.main()
