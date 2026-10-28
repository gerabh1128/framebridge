#!/usr/bin/python3
"""Build the self-hosted FrameBridge website with the Python standard library."""
from pathlib import Path
import html,json,re,shutil,sys,runpy,hashlib

BASE=Path(__file__).resolve().parent
ROOT=BASE.parent
DIST=BASE/'dist'
META=json.loads((BASE/'release.json').read_text())
META.setdefault('git_url','/git/gerardo/framebridge')
META.setdefault('git_clone_url','https://framebridge.betancourt.one/git/gerardo/framebridge.git')
E=lambda value:html.escape(str(value),quote=True)
READY=META.get('state')=='ready'
CAT=runpy.run_path(str(BASE/'release_catalog.py'))
CAT['validate'](META,READY)
VIDEO=CAT['VIDEO'];POSTER=CAT['POSTER']
for name,expected in [(VIDEO,CAT['VIDEO_SHA256']),(POSTER,CAT['POSTER_SHA256'])]:
    asset=BASE/'assets'/name
    if asset.exists():
        assert hashlib.sha256(asset.read_bytes()).hexdigest()==expected,'Approved original media changed.'
    elif READY:
        raise ValueError('Approved original media is required before publication.')
for path in ['assets','docs','download','blog/2026-10-26-opening-framebridge','community','support']:
    (DIST/path).mkdir(parents=True,exist_ok=True)
for p in (BASE/'src').iterdir():
    if p.is_file():shutil.copyfile(p,DIST/'assets'/p.name)
for p in (BASE/'assets').iterdir():
    if p.is_file():shutil.copyfile(p,DIST/'assets'/p.name)
(DIST/'assets/release.json').write_text(json.dumps(META,indent=2)+'\n')

def brand():
    return '<a class="brand" href="/" aria-label="FrameBridge home"><span class="prompt" aria-hidden="true">&gt;</span>framebridge<span class="cursor" aria-hidden="true">_</span></a>'

def header(current):
    items=[('Docs','/docs/','docs'),('Development','/blog/','blog'),('Source',META['git_url'],'source'),('Support','/support/','support'),('Download','/download/','download')]
    links=[]
    for title,url,key in items:
        links.append('<a href="'+E(url)+'"'+(' aria-current="page"' if current==key else '')+(' class="nav-download"' if key=='download' else '')+'>'+E(title)+'</a>')
    return '<a class="skip" href="#content">Skip to content</a><header class="site-header"><div class="wrap header-inner">'+brand()+'<nav aria-label="Main navigation">'+''.join(links)+'</nav></div></header>'

def footer():
    return '''<footer class="site-footer"><div class="wrap"><div class="footer-main"><div>'''+brand()+'''<p class="micro">Your systems. A shared way to work.</p></div><div class="credits"><strong>Programming:</strong> ChatGPT<br><strong>Original concept and project coordination:</strong> Gerardo Betancourt</div><div class="footer-links"><a href="/docs/">Documentation</a><a href="/community/">Contribute</a><a href="/support/">Support</a><a href="'''+E(META['git_url'])+'''">Source code</a></div></div><div class="footer-bottom"><p>FrameBridge software is released under GNU GPLv3. Third-party components retain their own notices and licenses.</p><p>Independent community project.<br>Not an official OpenAI product.</p></div></div></footer>'''

def page(title,body,current='',light=False,description='FrameBridge connects your own ChatGPT account to Linux systems you control.'):
    return '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="description" content="'+E(description)+'"><meta name="robots" content="'+('index,follow' if READY else 'noindex,nofollow')+'"><meta name="color-scheme" content="'+('light dark' if light else 'dark')+'"><meta http-equiv="Content-Security-Policy" content="default-src \'self\'; script-src \'self\'; style-src \'self\'; img-src \'self\' data:; media-src \'self\'; connect-src \'self\'; base-uri \'none\'; object-src \'none\'; form-action \'self\'"><link rel="icon" href="/assets/favicon.svg" type="image/svg+xml"><link rel="stylesheet" href="/assets/site.css"><title>'+E(title)+' · FrameBridge</title><script src="/assets/site.js" defer></script></head><body'+(' class="docs"' if light else '')+'>'+header(current)+body+footer()+'</body></html>'

def code(text):
    return '<div class="codebox"><pre><code>'+E(text)+'</code></pre></div>'

release_label='Official release' if READY else 'Release preparation'
home='''<main id="content"><div class="wrap"><section class="hero"><div><div class="eyebrow">Home Linux networks · One administrator</div><h1>Your Linux network.<br>One place to administer it.</h1><p class="lead">Administer your own home Linux network through ChatGPT: inspect equipment, diagnose connections, manage services and run commands across enrolled computers. One administrator, one bridge.</p><div class="actions"><a class="button primary" href="/download/">Get FrameBridge <span aria-hidden="true">&nbsp;↗</span></a><a class="button" href="#preview">Watch the concept</a></div><p class="micro">GPL-3.0-only · Debian 13 · Ubuntu 24.04 · Linux Mint 22.x · amd64</p></div><div><div class="terminal" aria-label="Illustrative command workflow"><div class="terminal-top"><span>FRAMEBRIDGE / EXAMPLE WORKFLOW</span><span aria-hidden="true">— □ ×</span></div><div class="terminal-body"><div class="term-row"><p class="term-command"><span>$</span>fb connect</p><div class="term-note">Connect your own account through the guided terminal flow.</div></div><div class="term-row"><p class="term-command"><span>$</span>fb status</p><div class="term-note">See tools, connection and startup state in one place.</div></div><div class="term-row"><p class="term-command"><span>$</span>fb console</p><div class="term-note">Return to your work view: Bash and FBTERM activity.</div></div></div></div><p class="terminal-caption">Illustrative commands. This is not a screen capture.</p></div></section><div class="strip rule"><span>Local execution</span><span>Persistent terminals</span><span>Network administration</span><span>Central + nodes</span></div></div>
<section class="section rule" id="preview"><div class="wrap"><div class="section-head"><div><p class="section-label">01 / A conversation takes shape</p><h2>From a blinking cursor<br>to a connected workflow.</h2></div><p>A conceptual look at independent tasks, consoles and connected computers. The software runs your tools on your systems; the ChatGPT model remains hosted.</p></div><div class="video-shell"><video controls preload="metadata" poster="/assets/framebridge-preview-0.3.1-original-r4.jpg" aria-label="FrameBridge conceptual preview animation"><source src="/assets/framebridge-preview-0.3.1-original-r4.mp4" type="video/mp4"><p>Your browser cannot play this video. <a href="/assets/framebridge-preview-0.3.1-original-r4.mp4">Download the MP4 preview.</a></p></video></div><p class="video-caption"><strong>Conceptual animation — not a screen recording.</strong><span>Approved 0.3.1 concept preview with its original music. Current release packages and commands are documented separately.</span></p></div></section>
<section class="section rule"><div class="wrap workflow"><div><p class="section-label">02 / The bridge</p><h2>Start with one machine.<br>Extend when you need to.</h2><p class="lead">A standalone computer can provide local tools. A central installation can also connect approved nodes.</p><div class="architecture"><div class="architecture-title">Where work happens</div><div class="architecture-line"><span>Your ChatGPT account</span><span class="arrow" aria-hidden="true">→</span><span>FrameBridge</span><span class="arrow" aria-hidden="true">→</span><span>Your Linux systems</span></div></div><p class="note">No phone pairing is required. Initial account consent and credentials stay in the normal browser or hidden terminal prompts.</p></div><ol class="workflow-list"><li><span class="step-no">01</span><div><h3>Install and connect</h3><p>Install the package for your distribution, then use the guided text interface to connect your own account. Advanced role setup is optional for ordinary standalone use.</p></div></li><li><span class="step-no">02</span><div><h3>Choose the right tool</h3><p>Use FBEXEC for commands, persistent terminals for ongoing shell work, and graphical CUA when a compatible desktop is available.</p></div></li><li><span class="step-no">03</span><div><h3>Keep the context</h3><p>Return to a live terminal session without rebuilding its shell state between calls. Running processes do not survive an operating-system reboot.</p></div></li><li><span class="step-no">04</span><div><h3>Add approved nodes</h3><p>Use the central instance for ChatGPT transport and select enrolled nodes explicitly. Nodes do not need a second ChatGPT tunnel.</p></div></li></ol></div></section>
<section class="section rule"><div class="wrap"><p class="section-label">03 / A practical toolset</p><div class="feature-row"><h3>Execution</h3><p>Run authorized shell tasks on the selected machine and inspect their recorded results.</p><code>FBEXEC</code></div><div class="feature-row"><h3>Terminals</h3><p>Keep a real Bash session across calls. Inspect text, send input and return to ongoing work.</p><code>FBTERM / fb tty</code></div><div class="feature-row"><h3>Desktop and text</h3><p>Use compatible graphical and text access as auxiliary tools for your own equipment. Commands, diagnostics and network administration remain the main workflow.</p><code>CUA / CUA-TTY</code></div><div class="feature-row"><h3>People and agents</h3><p>The same command-line interface serves human operators and authorized ChatGPT agents, with discoverable usage guidance.</p><code>fb / framebridge</code></div><p class="note">Independent FBEXEC tasks have been used together in the laboratory when their objectives and changes did not overlap. This is observed practice, not an isolation guarantee. Use one controller at a time for each shared graphical desktop.</p></div></section>
<section class="section rule" id="download"><div class="wrap"><div class="release-panel"><div><p class="section-label">04 / Start working</p><h2>Free software.<br>Verifiable releases.</h2><p>Read the code, adapt it and contribute. Official downloads are identified by their published checksums and release signatures.</p><div class="actions"><a class="button primary" href="/download/">Download &amp; verify</a><a class="button" href="/docs/">Read the guide</a></div></div><ul class="release-facts"><li><span>Version</span><strong>'''+E(META['version'])+'''</strong></li><li><span>Platform</span><strong>'''+E(META['distribution'])+'''</strong></li><li><span>License</span><strong>GNU GPLv3 only</strong></li><li><span>Channel</span><strong>'''+E(release_label)+'''</strong></li></ul></div></div></section>
<section class="section rule"><div class="wrap community"><div><p class="section-label">05 / Built in the open</p><h2>A small project.<br>An open invitation.</h2><p>Good tools improve through use, careful fixes and clear documentation. Report a reproducible issue, improve the guide or propose a change in Git.</p><a class="text-link" href="/community/">How to contribute <span aria-hidden="true">→</span></a></div><div><p class="section-label">Development journal</p><a class="news-link" href="/blog/2026-10-26-opening-framebridge/"><time datetime="2026-10-26">26 October 2026 · Project notes</time><strong>Opening FrameBridge: a shared interface for people and agents.</strong><span>Read the development note →</span></a></div></div></section></main>'''
(DIST/'index.html').write_text(page('Administer your home Linux network',home))

download=CAT['download'](META)
(DIST/'download/index.html').write_text(page('Download and verify',download,'download'))

community='''<main id="content"><div class="page-header"><div class="wrap"><p class="section-label">Community / Contribution</p><h1>Make the bridge better.</h1><p class="lead">Useful bug reports, small patches and clear explanations are a good place to begin.</p></div></div><div class="wrap page-content"><article class="prose"><h2>Start with the existing behavior</h2><p>Read the versioned guide, check the installed version and look for an existing issue. A reproducible report should name the command, expected result, actual result and relevant environment.</p><p><strong>Remove credentials and personal data from logs before sharing them.</strong> Never include passwords, access keys, private keys, tokens or cookies.</p><h2>Propose a focused change</h2><p>Use an issue to explain the problem or a pull request to propose a patch. Include the reason for the change and proportionate validation. Keep unrelated edits separate so they can be reviewed.</p>'''+('<h3>Clone the project</h3>'+code('git clone '+META['git_clone_url']) if META.get('git_clone_url') else '')+'''<p><a class="button primary" href="'''+E(META['git_url'])+'''">Open the project in Git</a></p><h2>Official releases and free forks</h2><p>Maintainers review which changes become official FrameBridge releases. Published release checksums and signatures identify those artifacts.</p><p>GNU GPLv3 also allows private modifications and independently distributed forks, subject to its terms. Contributing changes upstream is welcome; it is not a condition for exercising those rights. Modified builds should be clearly marked and must not be presented as official project releases.</p><p><a href="https://www.gnu.org/licenses/gpl-3.0.html" rel="noreferrer">Read the GNU GPLv3 license.</a> Third-party components retain their existing notices and licenses.</p><h2>Using an agent to contribute</h2><p>The command interface and MCP guide are designed for both people and authorized agents. Normal operation uses installed commands and tools. Editing or rebuilding the application is a development task that should follow the owner’s instructions.</p><h2>Project credits</h2><p><strong>Programming:</strong> ChatGPT<br><strong>Original concept and project coordination:</strong> Gerardo Betancourt</p><p>FrameBridge is an independent project. The credit describes how the project was developed and does not imply endorsement by OpenAI.</p></article></div></main>'''
(DIST/'community/index.html').write_text(page('Contribute',community,'source'))

post='''<main id="content"><div class="page-header"><div class="wrap"><p class="section-label">Development journal / 26 October 2026</p><h1>Opening FrameBridge.</h1><p class="lead">A shared interface for people and agents, and a small invitation to improve it together.</p></div></div><div class="wrap page-content"><article class="prose"><p>FrameBridge began with a practical goal: use a conversation with ChatGPT to do useful work on Linux computers we control. Commands, persistent terminals and a compatible desktop should fit into the same working context.</p><h2>Make the interface easier to understand</h2><p>The current development work brings everyday commands into a shorter vocabulary, keeps status observation separate from diagnosis, and explains the effects of connection and startup controls. The long command <code>framebridge</code> and its short alias <code>fb</code> use the same interface.</p><p>The agent also needs a reliable starting point. A versioned guide and a brief MCP invitation are intended to help it discover how the installed software works, which computer receives an operation and what a command will change.</p><h2>Keep the evidence honest</h2><p>The preview video is a conceptual animation, not a recording of a live session. Real demonstrations and captures must remain distinguishable from illustrations. Independent tasks have been used together in the laboratory, but that experience does not establish automatic isolation or exclusive desktop control.</p><h2>Open source, with identifiable releases</h2><p>The project is being prepared for GNU GPLv3 distribution. Sources and package artifacts should be published together, with signatures and checksums identifying official releases. Community members remain free to adapt and redistribute the software under the license.</p><p>The first contribution can be small: a clearer explanation, a reproducible issue or a focused fix. Review gives those changes a path into a later official version.</p><p><a class="text-link" href="/community/">Read the contribution guide →</a></p></article></div></main>'''
(DIST/'blog/2026-10-26-opening-framebridge/index.html').write_text(page('Opening FrameBridge',post,'blog'))
support='''<main id="content"><div class="page-header"><div class="wrap"><p class="section-label">Support FrameBridge</p><h1>Help keep the bridge working.</h1><p class="lead">Voluntary support can help sustain a free software project: its hosting, maintenance, documentation and development.</p></div></div><div class="wrap page-content"><article class="prose"><h2>What support can help with</h2><ul><li><strong>Hosting and distribution:</strong> keeping the source code, documentation and release files available.</li><li><strong>Maintenance:</strong> investigating reported problems and keeping the software usable.</li><li><strong>Documentation:</strong> improving clear instructions for people and agents.</li><li><strong>Development:</strong> building and reviewing focused improvements.</li></ul><h2>Voluntary support and contact</h2><p><a class="button primary" href="https://www.paypal.com/donate/?business=gerardobh1128%40gmail.com&amp;currency_code=USD" rel="noreferrer">Support through PayPal</a></p><p>Project contact and PayPal account: <a href="mailto:gerardobh1128@gmail.com">gerardobh1128@gmail.com</a>.</p><p>Contributions are voluntary. FrameBridge remains free software under GNU GPLv3 only, whether or not you provide financial support.</p><h2>Support without a payment</h2><p>A useful bug report, a clearer explanation or a carefully scoped patch can be valuable contributions. You can also help by sharing honest demonstrations that distinguish real recordings from conceptual illustrations.</p><p><a class="text-link" href="/community/">Read the contribution guide →</a></p><h2>Review stays independent</h2><p>Financial support does not grant control over official releases, bypass code review or security checks, or imply a project endorsement. Official changes follow the same review and release process.</p><p><a class="text-link" href="'''+E(META['git_url'])+'''">Explore the source code →</a></p></article></div></main>'''
(DIST/'support/index.html').write_text(page('Support FrameBridge',support,'support'))

blog='''<main id="content"><div class="page-header"><div class="wrap"><p class="section-label">Development journal</p><h1>Notes from the bridge.</h1><p class="lead">Design decisions, release work and what we learn while using FrameBridge.</p></div></div><div class="wrap page-content narrow"><a class="news-link" href="/blog/2026-10-26-opening-framebridge/"><time datetime="2026-10-26">26 October 2026 · Project notes</time><strong>Opening FrameBridge: a shared interface for people and agents.</strong><span>Read the development note →</span></a></div></main>'''
(DIST/'blog/index.html').write_text(page('Development journal',blog,'blog'))

guide_path=ROOT/'repository/source/docs/COMMAND-GUIDE.json'
if not guide_path.exists():
 guide_path=ROOT/'source/docs/COMMAND-GUIDE.json'
guides={}
guide_data={}
if guide_path.exists():
    guide_data=json.loads(guide_path.read_text())
    guides=guide_data.get('topics',{})
    assert isinstance(guides,dict) and all(isinstance(v,dict) for v in guides.values())
    assert guide_data.get('version')==META['version'],'Guide and release versions must match.'
    (DIST/'assets/command-guide.json').write_text(json.dumps(guide_data,indent=2)+'\n')
order=[x for x in ['start','commands','connection','lifecycle','nodes','terminals','screens','diagnostics','security'] if x in guides]
order += [x for x in guides if x not in order]

def docs_nav(current):
    entries='<li><a href="/docs/"'+(' aria-current="page"' if current=='index' else '')+'>Guide overview</a></li>'
    for topic in order:
        entries+='<li><a href="/docs/'+E(topic)+'/"'+(' aria-current="page"' if current==topic else '')+'>'+E(guides[topic].get('title',topic.title()))+'</a></li>'
    return '<aside class="docs-nav" aria-label="Guide topics"><p>FrameBridge '+E(META['version'])+'</p><ul>'+entries+'</ul><p>More</p><ul><li><a href="/download/">Download &amp; verify</a></li><li><a href="/community/">Contributing</a></li><li><a href="/blog/">Development journal</a></li></ul></aside>'

def paragraphs(body):
    if isinstance(body,list):body='\n\n'.join(str(x) for x in body)
    return ''.join('<p>'+E(x).replace('\n','<br>')+'</p>' for x in str(body).split('\n\n') if x.strip())

for topic in order:
    guide=guides[topic]
    article='<article class="prose"><p class="section-label">FrameBridge '+E(guide.get('version',META['version']))+' / Command guide</p><h1>'+E(guide.get('title',topic))+'</h1>'
    for section in guide.get('sections',[]):
        article+='<h2>'+E(section.get('heading',''))+'</h2>'+paragraphs(section.get('body',''))
        for command in section.get('commands',[]):
            article+=code(command if isinstance(command,str) else json.dumps(command,indent=2))
    article+='<p class="aside-note">This page is generated from the same versioned guide used by the CLI and MCP help tool. Use <code>fb help '+E(topic)+'</code> for the local reference.</p></article>'
    dest=DIST/'docs'/topic;dest.mkdir(parents=True,exist_ok=True)
    (dest/'index.html').write_text(page(guide.get('title',topic),'<main id="content" class="wrap docs-layout">'+docs_nav(topic)+article+'</main>','docs',True))
hub='<article class="prose"><p class="section-label">FrameBridge '+E(META['version'])+' / Documentation</p><h1>The command guide.</h1><p>One reference for human operators and authorized ChatGPT agents. The commands <code>fb</code> and <code>framebridge</code> use the same interface.</p><p>Start with the normal connection flow. Use advanced setup only when you need to change roles or profile settings.</p>'+code('fb connect\nfb status\nfb help')+'<div class="topic-links">'
for topic in order:hub+='<a class="topic-link" href="/docs/'+E(topic)+'/"><strong>'+E(guides[topic].get('title',topic.title()))+'</strong><span>'+E(topic)+' →</span></a>'
hub+='</div>'
if not guides:hub+='<div class="release-notice">The shared command-guide export is being finalized. This staging page is not the complete published reference yet.</div>'
else:hub+='<p><a href="/assets/command-guide.json">Read the versioned guide data</a></p>'
hub+='</article>'
(DIST/'docs/index.html').write_text(page('Command guide','<main id="content" class="wrap docs-layout">'+docs_nav('index')+hub+'</main>','docs',True))
(DIST/'404.html').write_text(page('Page not found','<main id="content" class="wrap page-content prose"><p class="section-label">404 / Page not found</p><h1>This path ends here.</h1><p>Return to <a href="/">FrameBridge</a> or open the <a href="/docs/">command guide</a>.</p></main>'))
(DIST/'robots.txt').write_text('User-agent: *\n'+('Allow: /\n' if READY else 'Disallow: /\n'))
print(json.dumps({'web_root':str(DIST),'release_state':META['state'],'pages':len(list(DIST.rglob('*.html'))),'guide_topics':len(guides),'video_present':(DIST/'assets'/VIDEO).is_file(),'ready_metadata_validated':READY},indent=2))
