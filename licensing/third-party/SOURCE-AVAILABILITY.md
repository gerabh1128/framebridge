# Sources accompanying FrameBridge 0.3.4

The matching release set is intended to be delivered together through the project download area. This record identifies the required assets; it does not assert that external access has already been opened.

- FrameBridge-0.3.4-debian13-amd64-py313.deb, FrameBridge-0.3.4-ubuntu24.04-amd64-py312.deb and FrameBridge-0.3.4-mint22-amd64-py312.deb; Debian package version 0.3.4.
- FrameBridge-0.3.4-source.tar.gz and its matching SOURCE-ARCHIVE-MANIFEST.json: the corresponding FrameBridge implementation, reviewed cp313 and cp312 vendor inputs, build scripts, tests and legal records.
- FrameBridge-0.3.1-Community-Preview1-dependency-sources.tar.gz: immutable shared Python/Rust dependency source asset, 82,606,998 bytes, SHA-256 64540a85bd65eac3f394d8c0a967cacbc00e112676d57d644dcd1bb691c1926a. The historical filename is deliberately retained; verify bytes rather than inferring the current project version from that name.
- FrameBridge-0.3.1-tunnel-client-dependency-sources.tar.gz: exact source module archives for the 47 Go dependencies embedded in the retained OpenAI tunnel executable, plus retained notices and provenance records. Its release hash is recorded in the adjacent release checksum manifest and TUNNEL-DEPENDENCY-SOURCES.json.

The complete FrameBridge source also carries [R2-SUPPLEMENTS.json](R2-SUPPLEMENTS.json), the wit-bindgen-rt MIT supplement, the full MPL-2.0 text, Python/native notices and the [tunnel-client notices](tunnel-client/README.md). Do not omit those because a shared source archive has an older filename.

The shared asset contains five Python source distributions, 143 lockfile-verified crate archives, pinned OpenSSL/libffi source evidence, upstream Git archives, the exact Certifi CA bundle and identified Rust supplements. The R2 notice supplement does not silently alter that immutable asset.

The OpenAI executable identifies a development module without a VCS revision; its exact original own-source commit is not established. Its Apache-2.0 notice source and the verified Go dependency versions are documented separately, without claiming an independent rebuild or complete upstream provenance.

For the unchanged Certifi component, retain the exact source assets, original notice, full MPL-2.0 terms and recipient access instructions. The historical Mozilla certdata revision remains unidentified. See [NATIVE-DEPENDENCY-TERMS.md](NATIVE-DEPENDENCY-TERMS.md) and [docs/THIRD-PARTY-RELEASE-REVIEW.md](../../docs/THIRD-PARTY-RELEASE-REVIEW.md).

Before public distribution, verify every actual download and SHA-256, provide the matching installer and source together, and retain source access for distributed versions. Private preparation or a link to a planned path is not public delivery.
