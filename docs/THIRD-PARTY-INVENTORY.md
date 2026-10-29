# Python distribution inventory and Preview 2 source delivery

The following table records the 29 distributions and 33 legal files inventoried in RC7. Their component versions and original notices remain retained in the 930-file vendor subset unchanged between Community Preview 1 and Preview 2. The table is not a complete native-dependency or current legal-text count.

| Distribution | Version | Declared license | Historical legal-file count |
| --- | --- | --- | --- |
| annotated-types | 0.8.0 | MIT | 1 |
| anyio | 4.15.1 | MIT | 1 |
| attrs | 26.1.0 | MIT | 1 |
| certifi | 2026.7.22 | MPL-2.0 | 1 |
| cffi | 2.1.1 | MIT-0 | 1 |
| click | 8.5.0 | BSD-3-Clause | 1 |
| cryptography | 50.0.1 | Apache-2.0 OR BSD-3-Clause | 3 |
| h11 | 0.16.0 | MIT | 1 |
| httpcore | 1.0.9 | BSD-3-Clause | 1 |
| httpx | 0.28.1 | BSD-3-Clause | 1 |
| httpx-sse | 0.4.3 | MIT | 1 |
| idna | 3.20 | BSD-3-Clause | 1 |
| jsonschema | 4.26.0 | MIT | 1 |
| jsonschema-specifications | 2025.9.1 | MIT | 1 |
| mcp | 1.26.0 | MIT | 1 |
| pycparser | 3.0 | BSD-3-Clause | 1 |
| pydantic | 2.13.5 | MIT | 1 |
| pydantic_core | 2.46.5 | MIT | 1 |
| pydantic-settings | 2.15.0 | MIT | 1 |
| PyJWT | 2.14.0 | MIT | 2 |
| python-dotenv | 1.2.3 | BSD-3-Clause | 1 |
| python-multipart | 0.0.32 | Apache-2.0 | 1 |
| referencing | 0.37.0 | MIT | 1 |
| rpds-py | 2026.6.3 | MIT | 1 |
| sse-starlette | 3.4.11 | BSD-3-Clause | 2 |
| starlette | 1.7.0 | BSD-3-Clause | 1 |
| typing_extensions | 4.16.0 | PSF-2.0 | 1 |
| typing-inspection | 0.4.4 | MIT | 1 |
| uvicorn | 0.53.0 | BSD-3-Clause | 1 |

## Shared source asset and R2 supplements

The immutable `FrameBridge-0.3.1-Community-Preview1-dependency-sources.tar.gz` is 82,606,998 bytes, SHA-256 `64540a85bd65eac3f394d8c0a967cacbc00e112676d57d644dcd1bb691c1926a`. Its Preview 1 name is retained intentionally. It includes five Python source distributions, 143 lock-verified crate archives, OpenSSL/libffi source evidence, pinned Git sources, the exact Certifi bundle and identified Rust source supplements.

Its 135 legal texts are accompanied by full MPL-2.0 and the R2 MIT notice for wit-bindgen-rt 0.39.0, for 137 legal text files. Keep R2-SUPPLEMENTS.json and its referenced notice with the complete source delivery. This supplement does not silently change the sealed shared asset or original index.

Read [source availability](../licensing/third-party/SOURCE-AVAILABILITY.md), the [native terms matrix](../licensing/third-party/NATIVE-DEPENDENCY-TERMS.md) and [release review](THIRD-PARTY-RELEASE-REVIEW.md). The pinned terms were reconciled through documented permissive routes while preserving original alternatives, combined obligations and attribution.

Unchanged Certifi source availability is technically accepted with exact source assets, original notices, full MPL and clear recipient access. The original Mozilla revision remains unidentified; that is a provenance limit, not an automatic blocker. Four native objects match official wheels; independent rebuilding was not performed and is not imposed as a generic source-availability requirement.

At distribution, supply the matching Preview 2 installer and source, shared dependency asset, R2 supplements and notices, and verify access and download hashes. A project GPL choice does not relabel component terms or convert a private cache into public delivery.
