# FrameBridge 0.3.1 source archive

This archive preserves the repository layout needed by the offline builder.
From this directory, run python3 source/packaging/build_expanded.py to build
FrameBridge-0.3.1.deb. The builder writes source/build and source/release;
it does not install or start FrameBridge. Debian's dpkg-deb and the documented
build prerequisites must already be available.

Original FrameBridge material is GNU GPL version 3 only. Read LICENSE,
LICENSING.md, COPYRIGHT.md, LICENSE-STATUS.md and OFFICIAL-RELEASES.md for
scope, attribution, third-party terms and the official-release trust model.

The project source asset is accompanied by these separate dependency assets:

- FrameBridge-0.3.1-Community-Preview1-dependency-sources.tar.gz
  82,606,998 bytes
  SHA-256 64540a85bd65eac3f394d8c0a967cacbc00e112676d57d644dcd1bb691c1926a
- FrameBridge-0.3.1-tunnel-client-dependency-sources.tar.gz
  26,941,745 bytes
  SHA-256 6445d4f0e773aba833fe5c610fbae54102b92de2ce636438f769f21e890c2bd9

The Preview1 filename deliberately preserves the identity of an immutable
shared asset. Retain licensing/third-party/R2-SUPPLEMENTS.json and its referenced
source notices with the dependency sources. The source indices and
SOURCE-AVAILABILITY.md there record scope and provenance. Do not substitute
unrelated archives. The retained tunnel executable is not claimed to have been
independently rebuilt from an identified own-source revision.

The blog directory contains licensing policies and the post template.
Editorial article drafts are omitted because they are not source build inputs.

Only source/evidence/BASELINE.json is included from technical evidence. Runtime
profiles, private QA logs, generated build/release files and Git history are
outside the source archive allowlist.

A completed archive or builder run is not a declaration of public readiness.
Release checksums, signatures and publication status are recorded separately.
