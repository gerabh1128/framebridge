# FrameBridge 0.3.9 source and artifact identities

FrameBridge 0.3.9 was published on 11 November 2026 Eastern Time. The promotion completed at `2026-11-12T00:29:45Z`. Independent HTTPS verification from GERAMEG matched 52 public files against the signed stage and reviewed site files. Isolated APT checks over public HTTPS accepted 0.3.9 in all three suites. [Official download page](https://framebridge.betancourt.one/download/) · [Release catalog](https://framebridge.betancourt.one/assets/release.json)

## Frozen source

| Identity | Value |
| --- | --- |
| Version | `0.3.9` |
| Frozen source commit | `6774b904689f7fd2d6fd5e1e40b16698d276f85f` |
| Complete tracked Git tree | `a17c72cc055332e5c1491a72e3586a60fd1f1d49` |
| APT input manifest SHA-256 | `3e25003931c31ea0fe0ff10ea812322a0e76afcad9c2ac95eaa3059e87b6363a` |
| Release catalog SHA-256 | `66835ff83d8fe3d0b41e10af7db62532c936db4357c70ed5ab922a8679568af8` |
| Release-key fingerprint | `2B1F01BC806C0B33CB6CEDDE52859CEBC91B8808` |

The commit and tree identify the source revision. The corresponding-source archive has its own SHA-256. The official signed artifact channel is the FrameBridge download site and APT repository. [Frozen source](https://github.com/gerabh1128/framebridge/tree/6774b904689f7fd2d6fd5e1e40b16698d276f85f) · [Release notes](../RELEASE-NOTES.md#039) · [Review and release workflow](RELEASE-WORKFLOW.md)

## Build artifacts

The three Debian packages were built on GERAMEG. The package profile determines the distribution, system Python and APT suite. Verify the matching detached signature or signed APT metadata with the independently trusted release key before installation.

| Filename | APT suite | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| `FrameBridge-0.3.9-debian13-amd64-py313.deb` | `trixie` | 13,427,492 | `12dd5a5d6fa8f81554e1615b43d3327e4222c3c666f26b3d83458acc2f5dfd26` |
| `FrameBridge-0.3.9-ubuntu24.04-amd64-py312.deb` | `noble` | 13,427,632 | `611e1f631174df1a91a7398c30d15ac7ace374bf03f19acc49a948185e50d4bb` |
| `FrameBridge-0.3.9-mint22-amd64-py312.deb` | `mint22` | 13,427,636 | `d55b88c3529ba757ee7db5a2fc12519aff646f7ad25a7e11b95793d4a8d7f9c5` |
| `FrameBridge-0.3.9-source.tar.gz` | | 27,694,095 | `765ac402bd6aaa7688f024df9f842ae61c9b3156395b1edf0e2aae5a8075f41c` |

The APT input manifest records the files used to assemble repository metadata. Public APT verification requires matching the final signed indexes and the bytes served by the active repository for each suite.

## Verification status

| Check | Result |
| --- | --- |
| Focused regression tests | 279 of 279 passed |
| Full sandbox suite | 1,207 tests run, with 12 failures, 33 errors and two skips. The same failures and errors appeared on the sandbox baseline. |
| Native GERAMEG source tests | 102 of 102 passed as an unprivileged user on Debian 13 with Python 3.13. The suite covered the agent guide, screen transfer, broker and node behavior, TLS and package upgrades. |
| Release signatures and isolated local APT | Passed for the staged release and all three suites |
| Independent public HTTPS | All 52 files matched the reviewed bytes, totaling 218,369,200 bytes. Coverage included the catalog, complete package and source downloads, signatures and APT metadata. |
| Public signed APT | Isolated HTTPS verification accepted candidate 0.3.9 and the matching package identities in `trixie`, `noble` and `mint22`. |
| Live GERAMEG upgrade | The signed Debian package upgraded FrameBridge from 0.3.8 to 0.3.9. Transaction `82243c1cedd6bfe3` reached `COMPLETE`, the managed profile reached `READY`, the CLI reported 0.3.9 and a direct Fleet command completed. |

The live upgrade evidence applies to GERAMEG. Installation and upgrade on each distribution, reboot and rollout to enrolled nodes have separate acceptance records. [Claims and evidence boundaries](CLAIMS-AND-LIMITS.md)
