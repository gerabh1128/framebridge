"""Mirror the signed stable origin without signing, trusting a new key, or deleting historical payloads."""
import argparse, email.parser, email.utils, hashlib, os, re, shutil, subprocess, tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.request import urlopen

ORIGIN="https://framebridge.betancourt.one/debian/stable/"
FPR="69D0E9D83699F96AB986B06F3C24F85D892A517E"
KEY_SHA="e969ce8e22d0068c6b855b0d188e9afed6543875acbb7ff6a6a02cd9095acf1b"
MIRROR="https://raw.githubusercontent.com/gerabh1128/framebridge/apt-mirror/debian/stable/"
def need(x,msg):
    if not x:raise RuntimeError(msg)
def sha(p):
    with p.open("rb") as f:return hashlib.file_digest(f,"sha256").hexdigest()
def safe_path(name):
    p=PurePosixPath(name)
    need(not p.is_absolute() and ".." not in p.parts and re.fullmatch(r"[A-Za-z0-9._+~/-]+",name),"Unsafe repository path")
    return p
def get(root,name,limit=100*1024*1024):
    p=root/safe_path(name);p.parent.mkdir(parents=True,exist_ok=True)
    with urlopen(ORIGIN+name,timeout=40) as r,p.open("xb") as f:
        need(r.geturl()==ORIGIN+name,"Unexpected redirect")
        n=0
        while b:=r.read(1024*1024):
            n+=len(b);need(n<=limit,"Download exceeds limit");f.write(b)
    return p
def fields(path):return email.parser.Parser().parsestr(path.read_text())
def time_fields(m):
    a=email.utils.parsedate_to_datetime(m["Date"]);b=email.utils.parsedate_to_datetime(m["Valid-Until"])
    need(a.tzinfo is not None and b.tzinfo is not None and a<=datetime.now(timezone.utc)<b,"Invalid or expired Release")
    need(0<(b-a).total_seconds()<=14*86400+60,"Unexpected validity window")
    return a,b
def run(a):subprocess.run(a,check=True)
def main():
    a=argparse.ArgumentParser();a.add_argument("--mirror",type=Path,required=True);a.add_argument("--key",type=Path,required=True);args=a.parse_args()
    mirror=args.mirror.resolve();key=args.key.resolve()
    need(sha(key)==KEY_SHA,"Pinned public key changed")
    repo=mirror/"debian/stable"
    need(not repo.is_symlink(),"Unexpected stable path")
    with tempfile.TemporaryDirectory(prefix="fb-stable-mirror-") as tmp:
        work=Path(tmp)
        signed=get(work,"dists/stable/InRelease",1024*1024)
        release=work/"verified.Release"
        run(["gpgv","--keyring",str(key),"--output",str(release),str(signed)])
        m=fields(release);date,until=time_fields(m)
        need(all(m[k]==v for k,v in (("Origin","FrameBridge"),("Suite","stable"),("Codename","stable"),("Architectures","amd64"),("Components","main"),("Acquire-By-Hash","yes"))),"Unexpected stable Release identity")
        old=repo/"dists/stable/InRelease"
        if old.exists():
            prior=work/"previous.Release"
            run(["gpgv","--keyring",str(key),"--output",str(prior),str(old)])
            before=email.utils.parsedate_to_datetime(fields(prior)["Date"])
            need(date>=before,"Refusing date rollback")
            if date==before:need(old.read_bytes()==signed.read_bytes(),"Conflicting same-date Release")
        raw=get(work,"dists/stable/Release",1024*1024)
        sig=get(work,"dists/stable/Release.gpg",65536)
        need(raw.read_bytes()==release.read_bytes(),"Detached Release differs")
        run(["gpgv","--keyring",str(key),str(sig),str(raw)])
        checks={}
        for line in m["SHA256"].splitlines():
            if not line.strip():continue
            digest,size,name=line.split();safe_path(name);need(re.fullmatch("[a-f0-9]{64}",digest),"Invalid hash");checks[name]=(digest,int(size))
        entries=[]
        for n in ("main/binary-amd64/Packages","main/binary-amd64/Packages.gz"):
            p=get(work,"dists/stable/"+n,4*1024*1024)
            need((sha(p),p.stat().st_size)==checks[n],"Signed index hash mismatch")
            for alg in ("SHA256","SHA512"):
                h=hashlib.new(alg.lower(),p.read_bytes()).hexdigest()
                byhash=get(work,"dists/stable/main/binary-amd64/by-hash/"+alg+"/"+h,4*1024*1024)
                need(byhash.read_bytes()==p.read_bytes(),"By-hash index mismatch")
        import gzip
        pack=work/"dists/stable/main/binary-amd64/Packages"
        need(gzip.decompress((pack.parent/"Packages.gz").read_bytes())==pack.read_bytes(),"Compressed index differs")
        paragraphs=[email.parser.Parser().parsestr(x) for x in pack.read_text().split("\n\n") if x.strip()]
        need(len(paragraphs)==1,"Expected a single stable package")
        p=paragraphs[0]
        need(p["Package"]=="framebridge-original-expanded" and p["Architecture"]=="amd64" and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:\+fbterm\.[1-9][0-9]*)?",p["Version"]),"Unexpected package identity")
        oldpack=repo/"dists/stable/main/binary-amd64/Packages"
        if oldpack.exists():
            oldversion=fields(oldpack)["Version"]
            need(subprocess.run(["dpkg","--compare-versions",p["Version"],"ge",oldversion]).returncode==0,"Version rollback refused")
        path=p["Filename"]
        need(path.startswith("pool/main/f/framebridge-original-expanded/"),"Unexpected pool path")
        deb=get(work,path)
        need(sha(deb)==p["SHA256"] and deb.stat().st_size==int(p["Size"]),"Package digest mismatch")
        need(subprocess.check_output(["dpkg-deb","-f",str(deb),"Version"],text=True).strip()==p["Version"],"DEB version mismatch")
        for f in work.rglob("*"):
            if not f.is_file() or f.parent==work:continue
            rel=f.relative_to(work);dest=repo/rel
            need(not dest.is_symlink() and not any(q.is_symlink() for q in dest.parents if q!=mirror.parent),"Symlink refused")
            if str(rel).startswith("pool/") and dest.exists():need(sha(dest)==sha(f),"Immutable pool conflict")
        # Immutable payloads and by-hash first; current signed index last.
        files=[f for f in work.rglob("*") if f.is_file() and f.parent!=work]
        files.sort(key=lambda f:(str(f.relative_to(work))=="dists/stable/InRelease",str(f.relative_to(work))))
        for f in files:
            dest=repo/f.relative_to(work);dest.parent.mkdir(parents=True,exist_ok=True)
            if dest.exists() and sha(dest)==sha(f):continue
            shutil.copyfile(f,dest)
        shutil.copyfile(key,repo/"framebridge-archive-keyring.gpg")
        (mirror/"sources").mkdir(exist_ok=True)
        (mirror/"sources/framebridge-stable.sources").write_text("Types: deb\nURIs: "+MIRROR+"\nSuites: stable\nComponents: main\nArchitectures: amd64\nSigned-By: /etc/apt/keyrings/framebridge-stable-archive.gpg\n")
        (mirror/"README-stable.md").write_text("# FrameBridge portable stable mirror\n\nOne amd64 package for Debian 13, Ubuntu 24.04 and Linux Mint 22.x.\n\nThe official origin is "+ORIGIN+". The mirror copies verified signed indexes; it never generates signatures.\n\nVerify the independently trusted archive fingerprint "+FPR+". Use the [official installation and cold maintenance guide](https://framebridge.betancourt.one/download/). Do not enable unattended hot upgrades or overwrite an existing source without review.\n")
        print("VERIFIED_STABLE",p["Version"],p["SHA256"],"valid until",until.isoformat())
if __name__=="__main__":main()
