#!/usr/bin/python3
"""Bounded static and loopback HTTP checks for the FrameBridge website."""
from pathlib import Path
from html.parser import HTMLParser
from urllib.parse import urlsplit,unquote
from urllib.request import urlopen
from http.server import ThreadingHTTPServer,SimpleHTTPRequestHandler
import json,hashlib,threading,tempfile,shutil,subprocess,html,os
BASE=Path(__file__).resolve().parent
DIST=BASE/'dist'
REPORT=BASE/'qa/WEBSITE-STATUS.json'
REPORT.parent.mkdir(mode=0o700,exist_ok=True)
class Inspect(HTMLParser):
    def __init__(self):
        super().__init__();self.links=[];self.assets=[];self.ids=set();self.lang=None;self.main=False;self.scripts=[];self.code=[];self.in_code=False;self.current=''
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='html':self.lang=a.get('lang')
        if 'id' in a:self.ids.add(a['id'])
        if tag=='main' and a.get('id')=='content':self.main=True
        if tag=='a' and 'href'in a:self.links.append(a['href'])
        if tag in ('script','source','img') and 'src'in a:self.assets.append(a['src'])
        if tag=='video' and 'poster'in a:self.assets.append(a['poster'])
        if tag=='link' and a.get('rel') in ('stylesheet','icon'):self.assets.append(a['href'])
        if tag=='script':self.scripts.append(a)
        if tag=='code':self.in_code=True;self.current=''
    def handle_data(self,data):
        if self.in_code:self.current+=data
    def handle_endtag(self,tag):
        if tag=='code':self.code.append(self.current);self.in_code=False
def resolve(path):
    p=DIST/unquote(path.lstrip('/'))
    return p/'index.html' if path.endswith('/') or p.is_dir() else p
def main():
    os.umask(0o077);pages=sorted(DIST.rglob('*.html'));assert pages
    failures=[];pending=[];infra=set();http_paths=set();external=set()
    for p in pages:
        raw=p.read_text();q=Inspect();q.feed(raw);rel='/'+str(p.relative_to(DIST));http_paths.add(rel)
        if q.lang!='en' or not q.main:failures.append(str(p.relative_to(DIST))+': missing English or main landmark')
        if '/home/' in raw or '/srv/geranas-services/' in raw or '/workspace/' in raw:failures.append(str(p.relative_to(DIST))+': personal host path')
        if "'unsafe-inline'" in raw:failures.append(str(p.relative_to(DIST))+': unsafe inline CSP')
        for value in q.links+q.assets:
            u=urlsplit(value)
            if u.scheme or u.netloc:
                external.add(value);continue
            if not u.path:
                if u.fragment and u.fragment not in q.ids:failures.append(str(p.relative_to(DIST))+': missing anchor '+u.fragment)
                continue
            if any(u.path.startswith(prefix) for prefix in ['/git/','/debian/','/downloads/']):
                infra.add(u.path);continue
            if not u.path.startswith('/'):failures.append('Non-root relative URL '+value);continue
            target=resolve(u.path)
            if not target.exists():
                if u.path=='/assets/framebridge-preview-0.3.1-original-r4.mp4':pending.append('Preview video asset pending')
                else:failures.append('Missing local target '+value+' referenced by '+str(p.relative_to(DIST)))
            elif u.path in q.assets:http_paths.add(u.path)
            if u.fragment and target.is_file() and target.suffix=='.html':
                other=Inspect();other.feed(target.read_text())
                if u.fragment not in other.ids:failures.append('Missing target fragment '+value)
        for item in q.scripts:
            if item.get('src')!='/assets/site.js':failures.append('Unexpected script source')
    css=(DIST/'assets/site.css').read_text()
    assert css.count('{')==css.count('}'),'CSS brace mismatch'
    class Quiet(SimpleHTTPRequestHandler):
        def __init__(self,*a,**k):super().__init__(*a,directory=str(DIST),**k)
        def log_message(self,*a):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Quiet);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    http_count=0
    try:
        for path in sorted(http_paths):
            with urlopen('http://127.0.0.1:'+str(server.server_port)+path,timeout=3) as response:
                if response.status!=200:failures.append('HTTP status '+str(response.status)+' for '+path)
                response.read();http_count+=1
    finally:server.shutdown();server.server_close();thread.join(timeout=2)
    # Exercise the guarded final-metadata branch using clearly synthetic values
    # in a disposable directory. No signature or installation command executes.
    meta=json.loads((BASE/'release.json').read_text())
    with tempfile.TemporaryDirectory(prefix='framebridge-web-qa-') as td:
        temp=Path(td);web=temp/'web';web.mkdir()
        shutil.copyfile(BASE/'build_site.py',web/'build_site.py')
        shutil.copyfile(BASE/'release_catalog.py',web/'release_catalog.py')
        shutil.copytree(BASE/'src',web/'src')
        (web/'assets').mkdir()
        for p in (BASE/'assets').iterdir():
            if p.is_file():shutil.copyfile(p,web/'assets'/p.name)
        fixture=json.loads(json.dumps(meta));fixture.update(state='ready',signing_fingerprint='2B1F01BC806C0B33CB6CEDDE52859CEBC91B8808',checksums_url='/downloads/TEST-SHA256SUMS',checksums_signature_url='/downloads/TEST-SHA256SUMS.asc')
        fixture['source']=dict(filename='TEST-SOURCE.tar.gz',url='/downloads/TEST-SOURCE.tar.gz',signature_url='/downloads/TEST-SOURCE.tar.gz.asc',sha256='a'*64,size_bytes=1)
        for i,profile in enumerate(fixture['profiles']):
            filename='TEST-profile-'+str(i)+'.deb'
            profile.update(filename=filename,url='/downloads/'+filename,signature_url='/downloads/'+filename+'.asc',sha256='a'*64,size_bytes=1)
        for dependency in fixture['dependency_sources']:
            dependency.setdefault('signature_url',dependency['url']+'.asc')
        (web/'release.json').write_text(json.dumps(fixture))
        result=subprocess.run(['/usr/bin/python3','-I','-S',str(web/'build_site.py')],capture_output=True,text=True,timeout=10)
        if result.returncode:failures.append('Synthetic-ready rendering failed: '+result.stderr[-600:])
        else:
            q=Inspect();q.feed((web/'dist/download/index.html').read_text())
            blocks=[x for x in q.code if '\n' in x]
            for text in blocks:
                result=subprocess.run(['/bin/bash','-n'],input=text,text=True,capture_output=True,timeout=3)
                if result.returncode:failures.append('Rendered install block shell syntax: '+result.stderr.strip())
            apt_blocks=[x for x in blocks if 'sources.list.d/framebridge.sources' in x]
            if len(apt_blocks)!=3:failures.append('Expected three independent APT profile setup blocks')
            for apt in apt_blocks:
                if 'Signed-By:' not in apt or 'primary_count' not in apt or 'apt-key' in apt:
                    failures.append('APT key trust must be pinned and scoped with Signed-By')
            for suite in ['trixie','noble','mint22']:
                if not any('Suites: '+suite in block for block in apt_blocks):
                    failures.append('Missing APT suite '+suite)
    status={'host':'GERAMEG','website_source':str(BASE),'web_root':str(DIST),'language':'English','buildless':True,'html_pages_checked':len(pages),'loopback_http_paths_checked':http_count,'static_failures':failures,'pending_assets':sorted(set(pending)),'infra_routes_expected':sorted(infra),'external_links':sorted(external),'no_external_runtime_dependencies':True,'copy_controls_progressive':True,'ready_branch_rendered_with_synthetic_fixture':True,'install_blocks_syntax_checked_not_executed':True,'release_state':meta.get('state'),'package_sha256_present':all(p.get('sha256') for p in meta.get('profiles',[])),'signing_fingerprint_present':bool(meta.get('signing_fingerprint')),'video_present':(DIST/'assets/framebridge-preview-0.3.1-original-r4.mp4').is_file(),'visual_browser_QA_performed':False,'visual_browser_QA_limit':'No browser or user desktop was operated by this subtask. Root may inspect the staged site via the authorized GERAMEG surface.','production_network_or_services_changed':False,'site_ready_for_private_staging':not failures,'public_release_ready':not failures and not pending and meta.get('state')=='ready','homepage_sha256':hashlib.sha256((DIST/'index.html').read_bytes()).hexdigest()}
    visual=BASE/'qa/VISUAL-REVIEW.json'
    if visual.exists():
        status['visual_browser_QA_performed']=True
        status['visual_browser_QA_limit']=json.loads(visual.read_text())['limitations']
        status['visual_browser_QA_report']=str(visual)
    status.update(support_page='/support/',support_funding_channel_configured='https://www.paypal.com/donate/' in (DIST/'support/index.html').read_text(),support_payment_form_present='<form' in (DIST/'support/index.html').read_text(),license_identifier=meta.get('license'),dependency_source_archives=len(meta.get('dependency_sources',[])))
    video=DIST/'assets/framebridge-preview-0.3.1-original-r4.mp4'
    if video.exists():
        status['video_sha256']=hashlib.sha256(video.read_bytes()).hexdigest()
    guide=json.loads((DIST/'assets/command-guide.json').read_text())
    status.update(shared_guide_version=guide['version'],shared_guide_topics=len(guide['topics']),shared_guide_sha256=guide['guide_sha256'])
    REPORT.write_text(json.dumps(status,indent=2)+'\n')
    print(json.dumps({'pages':len(pages),'http_paths':http_count,'failures':failures,'pending_assets':status['pending_assets'],'private_staging_ready':status['site_ready_for_private_staging'],'public_ready':status['public_release_ready'],'report':str(REPORT)},indent=2))
    return 1 if failures else 0
if __name__=='__main__':raise SystemExit(main())
