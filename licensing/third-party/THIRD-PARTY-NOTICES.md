# Third-party notices and source availability

This private community-preview supplement preserves upstream source archives and legal notices for dependencies used by FrameBridge. Original FrameBridge code is covered separately by GPL-3.0-only; that project license does not replace dependency licenses or author attributions.

The four shipped native objects were matched byte-for-byte to official release wheels. Their pinned project source distributions are included, together with 143 Cargo.lock-verified crate archives, SBOM-pinned OpenSSL source, CFFI's pinned static-libffi build-source evidence, the exact certifi CA bundle and upstream source, and identified Rust compiler/runtime source supplements. Binary wheels remain in the private verification cache and are not in this source archive.

See [DEPENDENCY-SOURCES.json](DEPENDENCY-SOURCES.json) for exact versions, public HTTPS locations, hashes, source identities, notice mappings and limitations. Full legal texts are preserved verbatim under [licenses/](licenses/), indexed by SHA-256 to avoid duplicate copies. Upstream authors and copyright notices remain in those texts and source archives.

Path fields in [DEPENDENCY-SOURCES.json](DEPENDENCY-SOURCES.json) and [UPSTREAM-SOURCES-MANIFEST.json](UPSTREAM-SOURCES-MANIFEST.json) describe members relative to the top-level directory of the sealed dependency-source archive, not paths in this installed legal directory. For notices, an archive path `notices/texts/<sha>.txt` maps here to `licenses/<sha>.txt`. The upstream manifest describes the sealed archive and is not a checksum manifest for renamed installed copies.

The source supplement is `FrameBridge-0.3.1-Community-Preview1-dependency-sources.tar.gz`. Supply it beside the matching installer and complete FrameBridge source when distributing that candidate, with its SHA256SUMS and index. This document does not invent a future hosting address or assert that a source offer has already been published.

Some SBOM entries may be used only during compilation. Their inclusion in this source review is not a claim that they are separate runtime libraries. License alternatives, target/build classification and the certifi preferred-source question remain documented review items. No independently reproducible native rebuild or blanket legal-compliance conclusion is claimed.
