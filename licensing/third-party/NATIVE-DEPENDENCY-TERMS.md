# Native dependency terms — R2

This review records an allowed distribution route for the actual Preview1 native artifacts and their supplied source/notices. It does not replace upstream licensing or authorize publication.

## Actual native objects

| Component | Original terms | Route used for this GPLv3 combination |
|---|---|---|
| cryptography 50.0.1 | Apache-2.0 OR BSD-3-Clause | Apache-2.0 |
| pydantic_core 2.46.5 | MIT | MIT |
| rpds-py 2026.6.3 | MIT | MIT |
| cffi 2.1.1 | MIT-0 | MIT-0 |

## Upstream alternatives and additional terms

The exact original expression remains in the JSON matrix and source. MIT is selected where offered; Apache is used for self_cell rather than its GPL-2.0-only alternative. r-efi uses MIT rather than its LGPL alternative. Every Unicode AND condition remains applicable.

| Original crate expression | Documented route | Unique name/version entries |
|---|---|---:|
| (MIT OR Apache-2.0) AND Unicode-3.0 | MIT AND Unicode-3.0 | 2 |
| (MIT OR Apache-2.0) AND Unicode-DFS-2016 | MIT AND Unicode-DFS-2016 | 1 |
| Apache-2.0 | Apache-2.0 | 1 |
| Apache-2.0 OR GPL-2.0-only | Apache-2.0 | 1 |
| Apache-2.0 OR MIT | MIT | 9 |
| Apache-2.0 WITH LLVM-exception | Apache-2.0 WITH LLVM-exception | 3 |
| Apache-2.0 WITH LLVM-exception OR Apache-2.0 OR MIT | MIT | 2 |
| BSD-2-Clause OR Apache-2.0 OR MIT | MIT | 2 |
| BSD-3-Clause | BSD-3-Clause | 2 |
| MIT | MIT | 17 |
| MIT OR Apache-2.0 | MIT | 80 |
| MIT OR Apache-2.0 OR LGPL-2.1-or-later | MIT | 1 |
| Unicode-3.0 | Unicode-3.0 | 19 |
| Unlicense OR MIT | MIT | 2 |
| Zlib | Zlib | 1 |

## Concrete delivery controls

All four actual native objects match official wheel payloads. The sealed source archive supplies exact project sdists, 143 lockfile-verified crates and associated OpenSSL/libffi sources, upstream Git source, CA data and runtime-source supplements. All 135 indexed upstream notice texts match the legal payload. The full MPL text is delivered separately, and R2 adds the pinned upstream MIT notice for wit-bindgen-rt, whose registry source crate omitted a standalone notice. This gives 137 legal texts in the payload, not 137 members inside the unchanged archive.

Deliver R2-SUPPLEMENTS.json and both separate notice texts together with the sealed dependency-source archive, matching installer and complete project source. Preserve all upstream notices and identify actual modifications where required. The exact source URLs, hashes and notice mappings are in DEPENDENCY-SOURCES.json, NATIVE-DEPENDENCY-TERMS.json and R2-SUPPLEMENTS.json.

## Certifi decision and evidence limits

The unmodified certifi CA bundle matches its official wheel, sdist and pinned Git source; its original notice and full MPL are supplied. MPL source delivery does not require an independent bit-identical regeneration experiment or automatically require recovering every historical Mozilla generation revision. The unknown historical revision remains a provenance limitation, not a new automatic blocker. The documented source-delivery control is technically accepted for this unchanged material; preserve clear source access and MPL rights when distributing. For a qualifying GPL Larger Work, preserve the MPL option while applying the MPL secondary-license permission as needed.

Some lock/SBOM entries are build-only or target-specific. In particular, getrandom selects wasi/wit-bindgen for wasm32/wasi/p2, not as a Linux runtime dependency; the full source archive still receives its supplemental notice. An independent native-wheel rebuild was not performed, and that experiment is not itself required by GPL/MPL. This review finds no incompatible compulsory branch in the reviewed terms when the documented alternatives and AND conditions are respected. It is a focused technical review, not an integral legal opinion or public-release approval.
