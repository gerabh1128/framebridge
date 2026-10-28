# FrameBridge website source

The original visual design and renderer are retained. The main workflow is
administration of a home Linux network by one human administrator. Compatible
desktop access is an auxiliary tool.

The canonical source repository is the existing GERANAS Gitea project
https://framebridge.betancourt.one/git/gerardo/framebridge. CLOUD provides the
existing HTTPS reverse proxy; GERANAS serves the website, downloads and APT
repository. Do not substitute a static Git mirror or another hosting backend.

Copy release.example.json to release.json, then fill it from the exact verified
release inventory. Its three profiles are Debian 13 amd64/Python 3.13 (trixie),
Ubuntu 24.04 amd64/Python 3.12 (noble), and Linux Mint 22.x amd64/Python 3.12
(mint22). A preparing catalog offers no new download or installation commands.
Use ready only after verifying all DEBs, corresponding source, dependency
source archives, signatures, checksums and the official public fingerprint.
Metadata describes release artifacts; it is not itself evidence that a public
hostname is operational.

Project media is delivered separately. Copy the approved original R4 MP4 and
JPEG into assets under their original filenames. The renderer verifies these
identities before a ready build:

- framebridge-preview-0.3.1-original-r4.mp4:
  b2a8b0b302063edbe05d84fb6dadb7f303a6f0f1b6d79dc151287df4671adc5b
- framebridge-preview-0.3.1-original-r4.jpg:
  2e867d4b6a22f81ecbefa293a88799b9e3c93f02aa7d1090c7f0a0dbfe44dc9f

The approved animation and original music remain unchanged. The video is
identified as a 0.3.1 conceptual demonstration; it does not claim to be a live
recording of the current release.

Run:

    python3 -I -S build_site.py
    python3 -I -S qa_site.py
    python3 -I -S test_release_catalog.py

The generator reads ../source/docs/COMMAND-GUIDE.json and requires its version
to match the release. QA checks local links/assets, loopback HTTP, the guarded
ready renderer with a disposable synthetic inventory, and syntax of all three
APT setup blocks. The test fixtures never sign, publish or install anything.
Perform final HTTPS, Gitea clone and isolated APT verification through the
actual public domain before reporting publication complete.

The website follows the project's GPL-3.0-only license. Third-party materials
retain their own licenses and notices. Support is voluntary; the contact and
PayPal account supplied by the project owner is gerardobh1128@gmail.com.
