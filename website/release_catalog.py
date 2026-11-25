"""Validated release catalog and download content for the original website."""
import html
import copy
import hashlib
import json
import re
from urllib.parse import parse_qsl, urlsplit

E = lambda value: html.escape(str(value), quote=True)
FINGERPRINT = "69D0E9D83699F96AB986B06F3C24F85D892A517E"
PROFILES = (
    ("debian13-amd64-py313", "trixie", "Debian 13", "3.13"),
    ("ubuntu24.04-amd64-py312", "noble", "Ubuntu 24.04", "3.12"),
    ("mint22-amd64-py312", "mint22", "Linux Mint 22.x", "3.12"),
)
VIDEO = "framebridge-preview-0.3.1-original-r4.mp4"
POSTER = "framebridge-preview-0.3.1-original-r4.jpg"
VIDEO_SHA256 = "b2a8b0b302063edbe05d84fb6dadb7f303a6f0f1b6d79dc151287df4671adc5b"
POSTER_SHA256 = "2e867d4b6a22f81ecbefa293a88799b9e3c93f02aa7d1090c7f0a0dbfe44dc9f"

def require(condition, message):
    if not condition:
        raise ValueError(message)

def guide_digest(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()

def publication_guide(raw):
    """Correct a release-label erratum in a web copy, never in frozen sources."""
    original = json.loads(raw)
    if original.get("version") == "0.3.9+fbterm.16":
        require(original.get("guide_sha256")==guide_digest({k:v for k,v in original.items() if k!="guide_sha256"}),"Frozen guide digest mismatch")
        published=copy.deepcopy(original)
        corrections=[
            ("start","Install and connect","Install the verified portable amd64 DEB with private CPython 3.13.18 on Debian 13, Ubuntu 24.04 or Linux Mint 22.x. Keep system Python for GTK/GI and maintenance. The package configures the signed stable/main APT channel. Existing installations must prepare cold maintenance and review legacy sources explicitly. Connect in a normal interactive terminal; advanced setup remains optional."),
            ("diagnostics","Doctor","Use doctor to inspect prerequisites, the installed portable profile and local runtime. The .16 package contains its own CPython 3.13.18; system Python remains responsible for GTK/GI and system maintenance. Supported laboratory targets are Debian 13, Ubuntu 24.04 and Linux Mint 22.x on amd64. Other distributions are not certified by this release."),
        ]
        for topic,heading,body in corrections:
            value=published["topics"][topic]
            require(value.get("guide_sha256")==guide_digest({k:v for k,v in value.items() if k!="guide_sha256"}),"Frozen topic digest mismatch")
            rows=[r for r in value["sections"] if r.get("heading")==heading]
            require(len(rows)==1,"Expected one portable guide section")
            rows[0]["body"]=body
            value["guide_sha256"]=guide_digest({k:v for k,v in value.items() if k!="guide_sha256"})
        published["publication"]={"kind":"web-documentation-erratum","revision":"0.3.9+fbterm.16-web-r1",
            "frozen_guide_file_sha256":hashlib.sha256(raw).hexdigest(),"frozen_guide_sha256":original["guide_sha256"],
            "scope":"Portable installation and diagnostic profile labels; signed source and installed guide remain unchanged."}
        published["guide_sha256"]=guide_digest({k:v for k,v in published.items() if k!="guide_sha256"})
        return published
    if original.get("version") != "0.3.7":
        return original
    original_digest = original.get("guide_sha256")
    require(original_digest == guide_digest({k: v for k, v in original.items() if k != "guide_sha256"}),
            "Frozen guide export digest mismatch")
    published = copy.deepcopy(original)
    topic = published["topics"]["diagnostics"]
    require(topic.get("guide_sha256") == guide_digest({k: v for k, v in topic.items() if k != "guide_sha256"}),
            "Frozen diagnostics topic digest mismatch")
    matches = [section for section in topic["sections"] if section.get("heading") == "Doctor"]
    require(len(matches) == 1, "Expected one Doctor section for the publication erratum")
    before = "The published 0.3.3 scope is Debian 13 with cp313 and Ubuntu 24.04/Mint 22.x with cp312."
    after = "The 0.3.7 release profiles are Debian 13 with cp313 and Ubuntu 24.04/Mint 22.x with cp312."
    require(matches[0]["body"].count(before) == 1, "Frozen Doctor text changed; review the publication erratum")
    matches[0]["body"] = matches[0]["body"].replace(before, after, 1)
    topic["guide_sha256"] = guide_digest({k: v for k, v in topic.items() if k != "guide_sha256"})
    published["publication"] = {
        "kind": "web-documentation-erratum",
        "revision": "0.3.7-web-20261109-r1",
        "topic": "diagnostics",
        "section": "Doctor",
        "frozen_guide_file_sha256": hashlib.sha256(raw).hexdigest(),
        "frozen_guide_sha256": original_digest,
        "scope": "Release-profile label only; packaged CLI/MCP guide and release artifacts are unchanged.",
    }
    published["guide_sha256"] = guide_digest({k: v for k, v in published.items() if k != "guide_sha256"})
    return published

def project_url(value, prefix=None):
    require(isinstance(value, str), "Artifact URL must be text")
    url = urlsplit(value)
    require(not url.query and not url.fragment and not url.username and not url.password,
            "Artifact URL must not contain credentials, a query or a fragment")
    require((not url.scheme and not url.netloc and url.path.startswith("/") and not value.startswith("//"))
            or (url.scheme == "https" and url.netloc == "framebridge.betancourt.one"),
            "Only canonical HTTPS or root-relative project URLs are allowed")
    require(".." not in url.path.split("/"), "Artifact URL path traversal")
    require(re.fullmatch(r"/[A-Za-z0-9/._+~:-]*",url.path), "Unsafe artifact URL characters")
    if prefix:
        require(url.path.startswith(prefix), "Unexpected artifact route")
    return value

def paypal_donation_url(value):
    """Accept the public URL copied from PayPal's donation tools."""
    message = "Use the public PayPal donation URL without a payment account email."
    require(len(value) <= 2048 and not re.search(r"[\x00-\x20\x7f]", value), message)
    url = urlsplit(value)
    require(url.scheme == "https" and url.netloc in ("paypal.com", "www.paypal.com")
            and url.path in ("/donate", "/donate/") and not url.fragment, message)
    items = parse_qsl(url.query, keep_blank_values=True, strict_parsing=True)
    params = dict(items)
    require(len(params) == len(items), message)
    if set(params) == {"hosted_button_id"}:
        require(re.fullmatch(r"[A-Za-z0-9]{1,64}", params["hosted_button_id"]), message)
    else:
        require(set(params) == {"business", "currency_code", "item_name", "no_recurring"}, message)
        description = params["item_name"]
        require(re.fullmatch(r"[A-Z0-9]{13}", params["business"])
                and re.fullmatch(r"[A-Z]{3}", params["currency_code"])
                and params["no_recurring"] in ("0", "1")
                and 1 <= len(description) <= 127 and description.isprintable()
                and "@" not in description and "\ufffd" not in description, message)
    return value

def asset(spec):
    require(isinstance(spec, dict), "Artifact metadata missing")
    require(re.fullmatch("[a-f0-9]{64}", spec.get("sha256", "")), "Exact artifact SHA-256 required")
    require(isinstance(spec.get("size_bytes"), int) and spec["size_bytes"] > 0, "Exact artifact size required")
    require(re.fullmatch("[A-Za-z0-9][A-Za-z0-9._+~-]*", spec.get("filename", "")), "Safe artifact filename required")
    project_url(spec.get("url", ""), "/downloads/")
    project_url(spec.get("signature_url", ""), "/downloads/")

def validate(meta, ready):
    require(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:\+fbterm\.[1-9][0-9]*)?", meta.get("version", "")), "Invalid public release version")
    require(meta.get("state") in ("preparing", "ready"), "Unknown release state")
    require(meta.get("site_url") == "https://framebridge.betancourt.one", "Canonical site origin required")
    require(meta.get("package_name") == "framebridge-original-expanded", "Unexpected binary package")
    require(meta.get("git_url") == "/git/gerardo/framebridge", "Use existing canonical Gitea repository")
    require(meta.get("git_clone_url") == "https://framebridge.betancourt.one/git/gerardo/framebridge.git",
            "Use existing canonical Gitea clone URL")
    if meta.get("package_mode")=="portable":
        return validate_portable(meta,ready)
    expected = {x[0]: x[1:] for x in PROFILES}
    profiles = meta.get("profiles", [])
    require(isinstance(profiles, list) and len(profiles) == 3, "Exactly three profiles required")
    require(len({p.get("profile_id") for p in profiles}) == 3, "Duplicate profile")
    for profile in profiles:
        identity = profile.get("profile_id")
        require(identity in expected, "Unexpected profile")
        suite, label, python = expected[identity]
        require((profile.get("suite"), profile.get("label"), profile.get("python"),
                 profile.get("architecture")) == (suite, label, python, "amd64"),
                "Profile identity does not match its tested distribution")
        if ready:
            asset(profile)
    if ready:
        require(meta.get("signing_fingerprint") == FINGERPRINT, "Unexpected official signing fingerprint")
        asset(meta.get("source"))
        deps = meta.get("dependency_sources", [])
        require(isinstance(deps, list) and len(deps) >= 2, "Dependency corresponding-source metadata required")
        for dependency in deps:
            asset(dependency)
        for key, prefix in (("signing_key_url", "/debian/"), ("checksums_url", "/downloads/"),
                            ("checksums_signature_url", "/downloads/")):
            project_url(meta.get(key, ""), prefix)

def code(text):
    return '<div class="codebox"><pre><code>' + E(text) + '</code></pre></div>'

def validate_portable(meta,ready):
    profiles=meta.get("profiles")
    require(isinstance(profiles,list) and len(profiles)==1,"One portable profile required")
    profile=profiles[0]
    require((profile.get("profile_id"),profile.get("suite"),profile.get("architecture"),profile.get("python"))==
            ("linux-amd64-cp313","stable","amd64","3.13.18"),"Unexpected portable profile")
    require(meta.get("apt_repository_url")=="https://framebridge.betancourt.one/debian/stable/","Unexpected permanent APT origin")
    if not ready:return
    require(meta.get("signing_fingerprint")==FINGERPRINT,"Unexpected official signing fingerprint")
    asset(profile)
    for key,prefix in (("signing_key_url","/debian/"),("checksums_url","/downloads/"),("checksums_signature_url","/downloads/")):
        project_url(meta.get(key,""),prefix)
    for spec in [meta.get("source"),*meta.get("dependency_sources",[])]:
        require(isinstance(spec,dict) and spec.get("verification")=="signed-checksums","Source must use signed checksums")
        asset(dict(spec,signature_url=meta["checksums_signature_url"]))
    require(len(meta.get("dependency_sources",[]))==3,"Expected all three corresponding dependency assets")
    project_url(meta["source_manifest"]["url"],"/downloads/")

def portable_install_commands(meta):
    validate(meta,True)
    p=meta["profiles"][0]
    base=meta["site_url"]
    return """sudo apt install ca-certificates curl gnupg
(
  set -eu
  mkdir -p framebridge-verified
  cd framebridge-verified
  curl -fsSL '"""+base+meta["signing_key_url"]+"""' -o framebridge-stable-archive.gpg
  curl -fsSL '"""+base+meta["checksums_url"]+"""' -o SHA256SUMS
  curl -fsSL '"""+base+meta["checksums_signature_url"]+"""' -o SHA256SUMS.asc
  curl -fsSL '"""+base+p["url"]+"""' -o '"""+p["filename"]+"""'
  info="$(gpg --batch --show-keys --with-colons framebridge-stable-archive.gpg 2>/dev/null)"
  test "$(printf '%s\n' "$info" | awk -F: '$1=="pub"{n++} END{print n+0}')" = 1
  test "$(printf '%s\n' "$info" | awk -F: '$1=="fpr"{print $10;exit}')" = '"""+FINGERPRINT+"""'
  gpgv --keyring "$PWD/framebridge-stable-archive.gpg" SHA256SUMS.asc SHA256SUMS
  printf '%s  %s\n' '"""+p["sha256"]+"""' '"""+p["filename"]+"""' | sha256sum -c -
  grep -Fx '"""+p["sha256"]+"  "+p["filename"]+"""' SHA256SUMS
  sudo apt install './"""+p["filename"]+"""'
)"""

def portable_download(meta,support=""):
    validate(meta,meta["state"]=="ready")
    p=meta["profiles"][0]
    body='<main id="content"><div class="page-header"><div class="wrap"><p class="section-label">Download / Verify / Install</p><h1>One Linux package.</h1><p class="lead">Debian 13, Ubuntu 24.04 and Linux Mint 22.x — one amd64 binary and one signed APT channel.</p></div></div><div class="wrap page-content"><article class="prose">'
    body+='<div class="release-notice"><strong>FrameBridge '+E(meta["version"])+'</strong> contains private Python 3.13.18 and an informational FBTERM history with job details, recorded duration and verified output. System Python remains unchanged. This exact release passed native Mint 22.3 graphical testing; support for other platforms follows the documented compatibility scope.</div>'
    if support:body+='</article>'+support+'<article class="prose">'
    if meta["state"]!="ready":return body+'<p>Release preparation: download and installation are pending verification.</p></article></div></main>'
    body+='</article><div class="profiles"><section class="profile-card" id="linux-amd64-cp313"><p class="section-label">amd64 · Private CPython 3.13.18</p><h2>FrameBridge for Linux</h2><p>Permanent APT suite: <code>stable</code></p><p class="hash">SHA-256: '+E(p["sha256"])+'</p><p>'+E(p["filename"])+' · '+format(p["size_bytes"],",")+' bytes</p><div class="actions"><a class="button primary" href="'+E(p["url"])+'" download>Download .deb</a><a class="button" href="'+E(p["signature_url"])+'">Package signature</a></div></section></div><article class="prose">'
    body+='<h2>Verify and install</h2><p>Expected archive signing fingerprint:</p><p class="hash">'+FINGERPRINT+'</p><p><a href="'+E(meta["signing_key_url"])+'">Public key</a> · <a href="'+E(meta["checksums_url"])+'">SHA256SUMS</a> · <a href="'+E(meta["checksums_signature_url"])+'">Checksum signature</a></p><p>Verify the initial DEB before installing. For an existing installation, first review jobs and detached effects, back up profiles, stop affected services and supervisors normally, and run these steps from an independent terminal. The package refuses active replacement.</p>'+code(portable_install_commands(meta))
    body+='<h2 id="normal-updates">Updates from your normal Linux updater</h2><p>The verified DEB installs the scoped key and permanent source. Do not silently replace your earlier sources; review and disable only obsolete FrameBridge channels after verifying the new channel. To update, first prepare admission using the tray or <code>fb --state-dir PROFILE update prepare</code>. Existing jobs may finish; unknown outcomes remain blockers. Review other profiles, TTY/CUA and external effects, back up state and stop the affected services. Use the normal updater or:</p>'+code("sudo apt update\napt-cache policy framebridge-original-expanded\nsudo apt install --only-upgrade framebridge-original-expanded")
    body+='<p>After verifying the package, start intended services, explicitly resume accepting jobs and check a fresh harmless receipt. This is not an unattended hot upgrade. Job inspection never submits or repeats the original command.</p><h2>Corresponding source and verification</h2><p>All source archives below are covered by the signed SHA256SUMS. Verify that signature and compare each downloaded file hash; individual source detached signatures are not asserted.</p><ul>'
    for item in [meta["source"],*meta["dependency_sources"]]:
        body+='<li><a href="'+E(item["url"])+'">'+E(item.get("label",item["filename"]))+'</a><br><span class="hash">SHA-256: '+E(item["sha256"])+'</span></li>'
    body+='</ul><p><a href="'+E(meta["source_manifest"]["url"])+'">Source reconstruction manifest</a> · <a href="'+E(meta["source_manifest"]["url"].rsplit("/",1)[0]+"/RUNTIME-SOURCES-MANIFEST.json")+'">Runtime source inventory</a>. License: GNU GPLv3 only; third-party notices remain applicable.</p><p><a href="https://github.com/gerabh1128/framebridge/tree/apt-mirror">APT mirror and historical channels</a> · <a href="/docs/">Command guide</a></p></article></div></main>'
    return body

def apt_commands(meta, profile):
    require(meta.get("state") == "ready", "Do not offer installation commands for an unsigned preparation")
    validate(meta, True)
    key_url = meta["site_url"] + meta["signing_key_url"] if meta["signing_key_url"].startswith("/") else meta["signing_key_url"]
    return """sudo apt install ca-certificates curl gnupg
(
  set -eu
  key_file="$(mktemp)"
  trap 'rm -f "$key_file"' EXIT
  curl -fsSL '""" + key_url + """' -o "$key_file"
  key_info="$(gpg --batch --show-keys --with-colons "$key_file" 2>/dev/null)"
  actual="$(printf '%s\\n' "$key_info" | awk -F: '$1=="fpr" {print $10; exit}')"
  primary_count="$(printf '%s\\n' "$key_info" | awk -F: '$1=="pub" {n++} END {print n+0}')"
  test "$primary_count" -eq 1
  test "$actual" = '""" + FINGERPRINT + """'
  sudo install -d -m 0755 /etc/apt/keyrings
  sudo install -m 0644 "$key_file" /etc/apt/keyrings/framebridge-fbterm15-archive-keyring.asc
  printf '%s\\n' \\
    'Types: deb' \\
    'URIs: https://framebridge.betancourt.one/debian/fbterm15/' \\
    'Suites: """ + profile["suite"] + """' \\
    'Components: main' \\
    'Architectures: amd64' \\
    'Signed-By: /etc/apt/keyrings/framebridge-fbterm15-archive-keyring.asc' \\
    | sudo tee /etc/apt/sources.list.d/framebridge-fbterm15.sources >/dev/null
  sudo apt update
  sudo apt install framebridge-original-expanded
)"""

def download(meta, *, support=""):
    if meta.get("package_mode")=="portable":
        return portable_download(meta,support)
    ready = meta["state"] == "ready"
    body = '<main id="content"><div class="page-header"><div class="wrap"><p class="section-label">Download / Verify / Install</p><h1>Choose your Linux package.</h1><p class="lead">One network administration tool. Three distribution profiles. Updates through signed APT repositories.</p></div></div><div class="wrap page-content"><article class="prose">'
    body += '<p>FrameBridge helps one administrator manage their own home Linux network: inventory, services, storage, diagnostics, commands and persistent terminals. Desktop access is an auxiliary tool.</p>'
    if ready:
        body += '<div class="release-notice" role="note"><strong>0.3.9+fbterm.15 — signing-key rotation and maintenance.</strong> The new signed APT channel is separate from the previous channel. Existing installations are not switched automatically. Verify the new fingerprint below and use the matching distribution suite. Before upgrading an existing installation, back up the profile, drain jobs, close the tray and stop the applicable FrameBridge runtime from an independent terminal. Active-profile package replacement is refused. Native installation, execution, isolated cold-boot checks and direct cold upgrades from .4 passed on Debian 13, Ubuntu 24.04 and Linux Mint 22.3. X11 viewer and tray lifecycle checks passed on XFCE (Debian and Ubuntu) and Cinnamon (Mint). Mint may briefly report a busy FBTERM controller during graphical startup; wait until the viewer settles before submitting new work. Historical builds are not recommended for new installations. These are scoped laboratory results, not universal compatibility certification. <a href="https://github.com/gerabh1128/framebridge/blob/424f7b600d5ad855c78524842780019d7eee1559/docs/PACKAGE-MAINTENANCE.md">Read cold package maintenance.</a></div>'
    if support:
        body += '</article>' + support + '<article class="prose">'
    body += '<h2>Match the target computer</h2>' + code("cat /etc/os-release\ndpkg --print-architecture\npython3 --version")
    body += '<p>The supported profiles below use amd64 and the distribution system Python. Select one matching suite for each computer.</p>'
    if not ready:
        body += '<div class="release-notice" role="status"><strong>Release preparation.</strong> Version ' + E(meta["version"]) + ' is being prepared. Download links and installation commands appear after the exact artifacts and signatures are verified.</div>'
    body += '</article><div class="profiles">'
    for profile in meta["profiles"]:
        body += '<section class="profile-card" id="' + E(profile["profile_id"]) + '"><p class="section-label">amd64 · Python ' + E(profile["python"]) + '</p><h2>' + E(profile["label"]) + '</h2><p>APT suite: <code>' + E(profile["suite"]) + '</code></p>'
        if ready:
            body += '<p class="hash">SHA-256: ' + E(profile["sha256"]) + '</p><p class="micro">' + E(profile["filename"]) + ' · ' + format(profile["size_bytes"], ",") + ' bytes</p><div class="actions"><a class="button primary" href="' + E(profile["url"]) + '" download>Download .deb</a><a class="button" href="' + E(profile["signature_url"]) + '">Package signature</a></div><details><summary>Configure the new signed APT channel (fresh install or stopped runtime)</summary>' + code(apt_commands(meta, profile)) + '</details>'
        else:
            body += '<p class="micro">Version ' + E(meta["version"]) + ' · package and signature pending.</p>'
        body += '</section>'
    body += '</div><article class="prose">'
    if ready:
        body += '<h2>Release verification and source</h2><p>Expected signing-key fingerprint:</p><p class="hash">' + FINGERPRINT + '</p><p><a href="' + E(meta["checksums_url"]) + '">SHA256SUMS</a> · <a href="' + E(meta["checksums_signature_url"]) + '">Checksum signature</a> · <a href="' + E(meta["signing_key_url"]) + '">Public key</a></p><p><a href="' + E(meta["source"]["url"]) + '">Corresponding FrameBridge source</a> · <a href="' + E(meta["source"]["signature_url"]) + '">Source signature</a></p><p class="hash">SHA-256: ' + E(meta["source"]["sha256"]) + '</p><ul>'
        for dep in meta["dependency_sources"]:
            body += '<li><a href="' + E(dep["url"]) + '">' + E(dep.get("label", dep["filename"])) + '</a> · <a href="' + E(dep["signature_url"]) + '">Signature</a><br><span class="hash">SHA-256: ' + E(dep["sha256"]) + '</span></li>'
        body += '</ul><p>Third-party components retain their original licenses and notices. Unchanged dependency archives retain their historical filenames.</p><h2 id="normal-updates">Normal updates</h2>'
        body += '<p>Use these steps from an independent terminal after configuring the current signed APT channel for your distribution. To move from the legacy channel, follow the matching setup above with the rotated signing key and the affected runtime stopped.</p>'
        body += code("sudo apt update\napt-cache policy framebridge-original-expanded")
        body += (
            '<p><strong>Cold upgrade required.</strong> Check that APT selects the intended version. '
            'Back up the affected profiles, let active jobs finish and check their receipts, close the tray, '
            'and stop the affected runtimes and autostart supervisors from the independent terminal. '
            'Keep them stopped until the package transaction completes. '
            '<a href="https://github.com/gerabh1128/framebridge/blob/424f7b600d5ad855c78524842780019d7eee1559/docs/PACKAGE-MAINTENANCE.md">'
            'Read cold package maintenance.</a></p>'
        )
        body += code("sudo apt install --only-upgrade framebridge-original-expanded")
        body += (
            '<p>After installation, resume only the intended profiles according to their prior connection intent. '
            'Check <code>fb status</code> and, for a resumed connection, verify a harmless job with a fresh receipt.</p>'
        )
        body += '<p>APT verifies the repository signature and package hashes. Keep the selected distribution suite and its scoped Signed-By key.</p>'
    body += '<h2>Connect and work</h2><p>After installation, connect from the normal terminal on that computer:</p>' + code("fb connect\nfb status\nfb console") + '<p>One central installation can connect enrolled Linux nodes. Nodes use the central connection. Enter passwords and connection keys only in the normal local prompts.</p><p><strong>Tried FrameBridge on Linux?</strong> What worked for you? Where did you get stuck? Even one sentence helps. <a href="https://github.com/gerabh1128/framebridge/discussions/5">Share your first impression on GitHub →</a></p><p><a href="/docs/">Read the shared command guide</a> · <a href="' + E(meta["git_url"]) + '">Browse the canonical source repository</a></p></article></div></main>'
    return body
