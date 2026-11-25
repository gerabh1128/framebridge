"""The single-binary source gate never relaxes legacy release requirements."""
import hashlib,io,json,sys,tarfile,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"packaging"))
from release_metadata import release_source,PORTABLE_RELEASE_PROFILE
from portable_release import identify

class PortableSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.ver="0.3.9+fbterm.16"
        self.spec={"runtime_sources":{"filename":"synthetic-source.tar","sha256":"a"*64,"bytes":12}}
        self.source={"source/packaging/portable-release-inputs.json":json.dumps(self.spec).encode(),
                     "source/packaging/portable-archive-keyring.gpg":b"synthetic public key fixture"}
        self.vendor_prefix="source/sources/original-hub/FrameBridge-Fleet-Hub/vendor"
        self.source[self.vendor_prefix+"/one.py"]=b"# synthetic"
        h=hashlib.sha256(b"one.py\0"+hashlib.sha256(b"# synthetic").digest()).hexdigest()
        self.vendor={"vendor_sha256":h,"path":self.vendor_prefix}
        self.current={}
        for i,(n,data) in enumerate(self.source.items()):
            p=self.root/("fixture-"+str(i));p.write_bytes(data);self.current[n]=p
        self.archive=self.root/("FrameBridge-"+self.ver+"-source.tar.gz")
        with tarfile.open(self.archive,"w:gz") as t:
            for n,data in self.source.items():
                m=tarfile.TarInfo("FrameBridge-"+self.ver+"/"+n);m.size=len(data);t.addfile(m,io.BytesIO(data))
        self.data={"version":self.ver,"status":"RELEASE_SOURCE_ASSEMBLED","archive":self.archive.name,
                   "bytes":self.archive.stat().st_size,"sha256":identify(self.archive)["sha256"],
                   "vendor_inputs":{"vendors":{"cp313":self.vendor},"release_profiles":[PORTABLE_RELEASE_PROFILE],"portable_release_inputs":self.spec},
                   "files":[{"path":n,"sha256":hashlib.sha256(data).hexdigest()} for n,data in self.source.items()]}
        self.manifest=self.root/"SOURCE-ARCHIVE-MANIFEST.json"
    def check(self,spec,profile=PORTABLE_RELEASE_PROFILE):
        self.manifest.write_text(json.dumps(self.data))
        return release_source(self.manifest,self.ver,profile,self.current,self.vendor,portable_inputs=spec)
    def test_portable_source_identity(self):
        self.assertEqual(self.check(self.spec)["runtime_sources"],self.spec["runtime_sources"])
    def test_wrong_runtime_source_bundle_identity(self):
        with self.assertRaisesRegex(RuntimeError,"Portable corresponding source identity"):
            self.check({"runtime_sources":{"filename":"changed.tar"}})
    def test_requires_current_key_and_runtime_spec(self):
        self.current.pop("source/packaging/portable-archive-keyring.gpg")
        with self.assertRaisesRegex(RuntimeError,"identity files missing"):self.check(self.spec)
    def test_modified_key_rejected(self):
        self.current["source/packaging/portable-archive-keyring.gpg"].write_bytes(b"changed")
        with self.assertRaisesRegex(RuntimeError,"Package source differs"):self.check(self.spec)
    def test_legacy_profiles_cannot_use_portable_manifest(self):
        with self.assertRaisesRegex(RuntimeError,"cp312 and cp313"):
            self.check(None,"debian13-amd64-py313")
if __name__=="__main__":unittest.main()
