# FrameBridge 0.3.5 corresponding source

This archive supplies the project source and the reviewed cp313 and cp312 vendor
inputs for Debian 13, Ubuntu 24.04 and Linux Mint 22.x on amd64. Keep its matching
SOURCE-ARCHIVE-MANIFEST.json sidecar beside the archive. Signatures, publication
and actual OS lifecycle acceptance are recorded separately; assembly alone does
not certify them. Historical signed assets retain their immutable identities.

The offline builder needs Python 3, dpkg/dpkg-deb and the documented host build
prerequisites. It never fetches dependencies, installs packages or starts services.
Extract the archive beside its matching sidecar and run from the extracted root:

    python3 source/packaging/build_expanded.py --target debian13-amd64-py313 --public-release --source-manifest ../SOURCE-ARCHIVE-MANIFEST.json
    python3 source/packaging/build_expanded.py --target ubuntu24.04-amd64-py312 --vendor-dir source/target-vendors/cp312 --public-release --source-manifest ../SOURCE-ARCHIVE-MANIFEST.json
    python3 source/packaging/build_expanded.py --target mint22-amd64-py312 --vendor-dir source/target-vendors/cp312 --public-release --source-manifest ../SOURCE-ARCHIVE-MANIFEST.json

Public-release mode verifies the archive hash, every current project source file
and the selected reviewed vendor identity before assembling a package. It does
not assert an independent rebuild of third-party native wheels or tunnel-client.
--check-vendor validates inputs without staging a package.

Original FrameBridge material is GNU GPL version 3 only. Third-party material
retains its applicable terms. Read LICENSE, LICENSING.md, COPYRIGHT.md and the
source-delivery records. Distribute these separate, unchanged dependency assets
with the release; their historical names do not identify the installer version:

- FrameBridge-0.3.1-Community-Preview1-dependency-sources.tar.gz
  Bytes: 82606998
  SHA-256: 64540a85bd65eac3f394d8c0a967cacbc00e112676d57d644dcd1bb691c1926a
- FrameBridge-0.3.1-tunnel-client-dependency-sources.tar.gz
  Bytes: 26941745
  SHA-256: 6445d4f0e773aba833fe5c610fbae54102b92de2ce636438f769f21e890c2bd9

The exact own-source revision of the retained tunnel executable is not asserted.
Retain the R2 source-notice supplements, both dependency indices and original
notices. Private profiles, credentials, QA logs, build outputs, Git history and
unpublished blog drafts are excluded from the archive allowlist.
