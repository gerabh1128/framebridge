# FrameBridge 0.3.5 source and artifact identities

The distributed Linux release and the current repository branch are separate identities. Reproduce the released build from its frozen corresponding-source archive and manifest, rather than an advancing branch. This record documents existing artifacts; it does not publish, rebuild, re-sign or relabel them.

## Frozen source

| Identity | Value |
| --- | --- |
| Version | 0.3.5 |
| Tested local source commit | `05d8663ac1abcfc1f6fdf7eb52c65c8910c8552a` |
| Public GitHub source commit | `5e9f0e478f17fa3309303559605e097eb4ecdc89` |
| Complete source Git tree, identical for both commits | `e8dce138b3a4138e3a87a204127d6036fd8ceb03` |
| Source review | [PR #2](https://github.com/gerabh1128/framebridge/pull/2) |
| Official release-key fingerprint | `2B1F01BC806C0B33CB6CEDDE52859CEBC91B8808` |

The differing commit IDs reflect different commit metadata; their complete tracked trees match. Source-archive generation has its own documented transformations and manifest, so a tarball checksum must not be confused with a Git tree ID. Documentation added after this frozen revision does not change the already distributed artifacts.

## Published files

Downloads and detached signatures are available from the [official download page](https://framebridge.betancourt.one/download/). Authenticate the release key independently, verify detached signatures, then compare SHA-256. The values below identify existing files; the unsigned table alone is not an authenticity guarantee.

| Filename | SHA-256 |
| --- | --- |
| `FrameBridge-0.3.5-source.tar.gz` | `b720d9be0da1b7499727f05f89a46fa49172d8d321a446cbc9f45d52d2ff736a` |
| `FrameBridge-0.3.5-source-manifest.json` | `b67143f2c26c8e6c790a030fd5347c8ce0dc198a7b358c079b8ee5484e00caa4` |
| `FrameBridge-0.3.5-debian13-amd64-py313.deb` | `45c2dbc13719939966aabecdf73ea3514083d3eaf881ba5a4cb3d2bd2a4df7d2` |
| `FrameBridge-0.3.5-ubuntu24.04-amd64-py312.deb` | `393c6d55d7ec1a90b56417a0f58e4ac91e62d82ae918fe8cba9aff690097bfd7` |
| `FrameBridge-0.3.5-mint22-amd64-py312.deb` | `c78d05dafe4ffb9f00f3f74c7b5dafd82145fa834068703c56099f319362d8ae` |

Signed APT metadata maps `trixie`, `noble` and `mint22` to the corresponding amd64 package. Archived release acceptance includes real guest upgrades/reboots and external download/APT verification; these are historical recorded results, not new tests executed by reading this file.

## Review provenance and validation limits

The source review documents 1,011 native cases with a GJS omission exercised separately, plus website and APT checks. The frozen tree does not include the previously documented `tools/run_tests.py` full-suite helper or its generated `source/qa` fixtures; see [focused source test instructions](../CONTRIBUTING.md#run-focused-source-tests). Reproducing that full historical run from a clean checkout remains separate work. The exact commit, environment, skips and logs remain part of that evidence. A PR description does not create a GitHub CI success status. At the integration review snapshot, GitHub reported no commit statuses and no check runs on the public source commit.

[PR #1](https://github.com/gerabh1128/framebridge/pull/1), commit `08ac951f4a19620decf87b34568dca520bb7f3e9`, proposed the bounded metrics fix. Its two changed files and complete patch are byte-identical in 0.3.5. Its commit is not an ancestor of the public 0.3.5 commit. It must not be applied a second time; after the 0.3.5 integration is approved and verified, it can be closed as incorporated with a cross-reference.

Artifact signatures, Git tag signatures, review approval and branch integration are distinct records. This record does not assert that a `v0.3.5` tag exists. A future tag must identify the frozen released source and needs its own approval; it must not label later documentation or code as the unchanged released source.

## Separate availability follow-up

The 0.3.5 broker moves incomplete TLS negotiation out of the accept loop and bounds concurrent request workers. After a completed handshake it restores the previous socket timeout, normally unlimited. Unauthenticated clients that complete TLS and send no HTTP can retain worker slots; saturation rejects additional requests. This residual case was reproduced on loopback with a worker limit reduced to two. The previous HTTP read path also lacked a deadline; the new global cap makes saturation explicit. This is an availability limitation, not evidence of authentication bypass or an attack on an installed machine.

A later reviewed change should evaluate HTTP header/body read deadlines compatible with long-poll, idle clients, worker recovery and normal enrollment. That hardening is outside the frozen 0.3.5 artifacts and this documentation-only follow-up.
