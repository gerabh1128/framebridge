# FrameBridge 0.3.7 source and artifact identities

FrameBridge 0.3.7 was built on 6 November 2026 and promoted to the public download and signed APT channels on 9 November 2026. The original artifacts, source archive and original build manifest retain their exact bytes. This record documents the promotion and its verification; it does not rewrite their historical build status, rebuild the release or move an existing Git tag.

## Frozen source

| Identity | Value |
| --- | --- |
| Version | 0.3.7 |
| Public GitHub source commit and v0.3.7 tag target | `c70d05c27ac898826aa3841539b4156ef1bd0fb9` |
| Canonical Gitea source commit | `664514640fc00d8b0ddf1d2e643742f07f67b150` |
| Complete tracked tree, identical in both commits | `a129e677f42f9c8a693c80a21a158618a9f9f1ad` |
| Official release-key fingerprint | `2B1F01BC806C0B33CB6CEDDE52859CEBC91B8808` |
| Original build date | 2026-11-06 |
| Public promotion date | 2026-11-09 |

The two commits have different identities and histories but the same complete tracked tree. Comparing their commit IDs alone does not establish different code. Later documentation commits can advance each branch normally without rewriting either history or changing the frozen v0.3.7 tag.

The publication verification compared all 2,354 non-generated files in the corresponding-source archive with their Git blob identities at the frozen source commit: no files were missing and no content differed. The two generated files, README-SOURCE-ARCHIVE.md and TARGET-VENDOR-PROVENANCE.json, were verified against the archive's own manifest. Thus the archive contains 2,356 manifested source files, with its generation transformations distinguished from the Git tree. A tarball SHA-256 is not a Git tree ID.

The original build manifest identifies a `COMPATIBILITY_CANDIDATE_BUILD` and private build kit. That original metadata remains unchanged. The later public promotion, signatures and acceptance checks are separately recorded here and in the signed distribution metadata; the build is not retroactively relabelled as having been publicly released on 6 November.

## Published files

Use the [official download page](https://framebridge.betancourt.one/download/) and [release catalog](https://framebridge.betancourt.one/assets/release.json). Authenticate the release key independently, verify the detached signatures, then compare SHA-256 and size. This unsigned documentation table alone is not proof of origin.

| Filename | Bytes | SHA-256 |
| --- | ---: | --- |
| `FrameBridge-0.3.7-debian13-amd64-py313.deb` | 13,411,616 | `8d29bb2aa7a79b56858a5e5b7971e15ab0d1bc4a665134712919b7f318adb985` |
| `FrameBridge-0.3.7-ubuntu24.04-amd64-py312.deb` | 13,412,996 | `bdc2534d8d9267854ab547432a38cf173b336d3e2b3d94a79d4ef2c87985fd35` |
| `FrameBridge-0.3.7-mint22-amd64-py312.deb` | 13,413,040 | `37a3fe69fb62b62bb65714751aa8bfd1c8cbd08bccfb128c3275fe089296345f` |
| `FrameBridge-0.3.7-source.tar.gz` | 27,661,262 | `6f5496e10e9c45290f973cd9e2d8bf2f82b1876cc1d0c29ec37d42d7ed3762c6` |
| `FrameBridge-0.3.7-source-manifest.json` | 455,392 | `45e67b46bda56c91295bf960e64f53a67d3d8c5120a9324016159953d441e21e` |

The final reviewed APT input manifest has SHA-256 `fffdc766f06764efa4a7ac29ab4d9db7c59ce535970c6c8b130af202700a36a7`. Signed repository metadata maps trixie, noble and mint22 to their exact Debian 13, Ubuntu 24.04 and Mint 22.x amd64 packages. The promotion preserved the 1,358 manifested payload files of each original DEB.

The shared dependency source archives retain their historical 0.3.1 filenames and identities; those filenames are not the current installer version. Existing 0.3.5 artifacts and historical release records remain unchanged.

## Publication verification and limits

The 9 November 2026 publication record contains the following distinct checks:

- Official signatures were produced through the normal owner signing workflow and verified against the fingerprint above. The repository, source, packages and verification material were checked before promotion.
- Isolated APT checks passed for all three suites at 15:48:30–15:48:31 UTC before activation, including candidate 0.3.7 and signature checks before and after testing.
- The public web/APT generation was activated at approximately 16:04–16:05 UTC. The activation checks recorded 57 local HTTP passes.
- Real isolated APT checks against the final public HTTPS authority passed for all three suites at 16:06:50–16:06:51 UTC, with candidate 0.3.7 and matching downloaded package bytes. Global APT configuration, sources, lists and dpkg state were not changed by those isolated checks.
- Website preparation produced 32 files, 18 HTML pages and 10 guide topics; 11 catalog/guide tests and 23 QA routes passed. The published release catalog SHA-256 is `e9d0acdd7083f58ccfe4a3119d5ffff71c535e53074cf35054f6ba05ba1707bc`.

These are recorded source, signature, website and repository-distribution checks. They do not constitute a new physical-desktop test, a new package installation/upgrade/reboot campaign, a new manual ChatGPT connection, or proof of Google indexing. The original 1,152-case source test report applies to the original tested source and environment; this publication does not claim that the full source test suite was rerun.

## Public documentation erratum

The frozen 0.3.7 command guide contains an obsolete 0.3.3 release-profile label in its Doctor paragraph. The published web copy corrects that label and identifies the change as `0.3.7-web-20261109-r1`. It records the original file and guide digests and recalculates the modified topic/export digests. The diagnostics page explains this documentary difference from the installed CLI/MCP reference.

The original guide file remains unchanged, SHA-256 `17dd398f1161e4e3455d8fbfe0a8419a7de6913faf8fb18e97f1d2c70694b2bc`. No DEB or corresponding-source archive was rebuilt or overwritten to make that text correction. The current film, historical concept films and their approved media identities are retained.

The release excludes the administrative supervision Site. Publication of the project website, documentation, source and packages does not authorize exposing private administrative interfaces or changing access controls.
