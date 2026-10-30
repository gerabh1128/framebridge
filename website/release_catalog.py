"""Validated release catalog and download content for the original website."""
import html
import re
from urllib.parse import urlsplit

E = lambda value: html.escape(str(value), quote=True)
FINGERPRINT = "2B1F01BC806C0B33CB6CEDDE52859CEBC91B8808"
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

def asset(spec):
    require(isinstance(spec, dict), "Artifact metadata missing")
    require(re.fullmatch("[a-f0-9]{64}", spec.get("sha256", "")), "Exact artifact SHA-256 required")
    require(isinstance(spec.get("size_bytes"), int) and spec["size_bytes"] > 0, "Exact artifact size required")
    require(re.fullmatch("[A-Za-z0-9][A-Za-z0-9._+~-]*", spec.get("filename", "")), "Safe artifact filename required")
    project_url(spec.get("url", ""), "/downloads/")
    project_url(spec.get("signature_url", ""), "/downloads/")

def validate(meta, ready):
    require(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", meta.get("version", "")), "Release version must be numeric")
    require(meta.get("state") in ("preparing", "ready"), "Unknown release state")
    require(meta.get("site_url") == "https://framebridge.betancourt.one", "Canonical site origin required")
    require(meta.get("package_name") == "framebridge-original-expanded", "Unexpected binary package")
    require(meta.get("git_url") == "/git/gerardo/framebridge", "Use existing canonical Gitea repository")
    require(meta.get("git_clone_url") == "https://framebridge.betancourt.one/git/gerardo/framebridge.git",
            "Use existing canonical Gitea clone URL")
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
  sudo install -m 0644 "$key_file" /etc/apt/keyrings/framebridge-archive-keyring.asc
  printf '%s\\n' \\
    'Types: deb' \\
    'URIs: https://framebridge.betancourt.one/debian/' \\
    'Suites: """ + profile["suite"] + """' \\
    'Components: main' \\
    'Architectures: amd64' \\
    'Signed-By: /etc/apt/keyrings/framebridge-archive-keyring.asc' \\
    | sudo tee /etc/apt/sources.list.d/framebridge.sources >/dev/null
  sudo apt update
  sudo apt install framebridge-original-expanded
)"""

def download(meta):
    ready = meta["state"] == "ready"
    body = '<main id="content"><div class="page-header"><div class="wrap"><p class="section-label">Download / Verify / Install</p><h1>Choose your Linux package.</h1><p class="lead">One network administration tool. Three distribution profiles. Updates through signed APT repositories.</p></div></div><div class="wrap page-content"><article class="prose">'
    body += '<p>FrameBridge helps one administrator manage their own home Linux network: inventory, services, storage, diagnostics, commands and persistent terminals. Desktop access is an auxiliary tool.</p>'
    body += '<h2>Match the target computer</h2>' + code("cat /etc/os-release\ndpkg --print-architecture\npython3 --version")
    body += '<p>The supported profiles below use amd64 and the distribution system Python. Select one matching suite for each computer.</p>'
    if not ready:
        body += '<div class="release-notice" role="status"><strong>Release preparation.</strong> Version ' + E(meta["version"]) + ' is being prepared. Download links and installation commands appear after the exact artifacts and signatures are verified.</div>'
    body += '</article><div class="profiles">'
    for profile in meta["profiles"]:
        body += '<section class="profile-card" id="' + E(profile["profile_id"]) + '"><p class="section-label">amd64 · Python ' + E(profile["python"]) + '</p><h2>' + E(profile["label"]) + '</h2><p>APT suite: <code>' + E(profile["suite"]) + '</code></p>'
        if ready:
            body += '<p class="hash">SHA-256: ' + E(profile["sha256"]) + '</p><p class="micro">' + E(profile["filename"]) + ' · ' + format(profile["size_bytes"], ",") + ' bytes</p><div class="actions"><a class="button primary" href="' + E(profile["url"]) + '" download>Download .deb</a><a class="button" href="' + E(profile["signature_url"]) + '">Package signature</a></div><details><summary>Install the signed APT channel</summary>' + code(apt_commands(meta, profile)) + '</details>'
        else:
            body += '<p class="micro">Version ' + E(meta["version"]) + ' · package and signature pending.</p>'
        body += '</section>'
    body += '</div><article class="prose">'
    if ready:
        body += '<h2>Release verification and source</h2><p>Expected signing-key fingerprint:</p><p class="hash">' + FINGERPRINT + '</p><p><a href="' + E(meta["checksums_url"]) + '">SHA256SUMS</a> · <a href="' + E(meta["checksums_signature_url"]) + '">Checksum signature</a> · <a href="' + E(meta["signing_key_url"]) + '">Public key</a></p><p><a href="' + E(meta["source"]["url"]) + '">Corresponding FrameBridge source</a> · <a href="' + E(meta["source"]["signature_url"]) + '">Source signature</a></p><p class="hash">SHA-256: ' + E(meta["source"]["sha256"]) + '</p><ul>'
        for dep in meta["dependency_sources"]:
            body += '<li><a href="' + E(dep["url"]) + '">' + E(dep.get("label", dep["filename"])) + '</a> · <a href="' + E(dep["signature_url"]) + '">Signature</a><br><span class="hash">SHA-256: ' + E(dep["sha256"]) + '</span></li>'
        body += '</ul><p>Third-party components retain their original licenses and notices. Unchanged dependency archives retain their historical filenames.</p><h2>Normal updates</h2>' + code("sudo apt update\napt-cache policy framebridge-original-expanded\nsudo apt install --only-upgrade framebridge-original-expanded\nfb status")
        body += '<p>APT verifies the repository signature and package hashes. Keep the selected distribution suite and its scoped Signed-By key.</p>'
    body += '<h2>Connect and work</h2><p>After installation, connect from the normal terminal on that computer:</p>' + code("fb connect\nfb status\nfb console") + '<p>One central installation can connect enrolled Linux nodes. Nodes use the central connection. Enter passwords and connection keys only in the normal local prompts.</p><p><a href="/docs/">Read the shared command guide</a> · <a href="' + E(meta["git_url"]) + '">Browse the canonical source repository</a></p></article></div></main>'
    return body
