# FrameBridge 0.3.8 source and artifact identities

FrameBridge 0.3.8 was promoted on 11 November 2026. Independent HTTPS verification checked the live catalog, the complete three package downloads and the corresponding-source archive against the published SHA-256 values and release key. APT selected 0.3.8 with matching package hashes in each isolated HTTPS suite. [Official download page](https://framebridge.betancourt.one/download/) · [Release catalog](https://framebridge.betancourt.one/assets/release.json)

## Frozen source

| Identity | Value |
| --- | --- |
| Version | `0.3.8` |
| Frozen source commit | `2755eac9585658b30b4a8e4def7185b7c4ffbcb7` |
| Complete tracked Git tree | `8a0521dfab921a1395f53f7f96696d3e72c15860` |
| APT input manifest SHA-256 | `654c68d0e47f96edf32fd54e91ab7340f72fd8a2d09c166a8deffdf962c9b7c2` |
| Release catalog SHA-256 | `e4e49605874fb08ed4f22aee4c7f18076eac454a859d5550069b946697dd9c7a` |
| GERANAS promotion receipt SHA-256 | f37a7b0eee736a7e293728e6fed48a03c79a1eb23eca6a35bfa86066f33c7b6c |
| Public APT acceptance receipt SHA-256 | 85616893b942705ec734cfd7b475ff908db5f4a734c41b06542b687bdffe1950 |
| Release-key fingerprint | `2B1F01BC806C0B33CB6CEDDE52859CEBC91B8808` |

The commit and tree identify the source revision. The corresponding-source archive has its own SHA-256. The public GitHub source branch points to the frozen commit. The official signed artifact channel is the FrameBridge download site and APT repository. Git-host release tags have their own publication record. [Frozen source](https://github.com/gerabh1128/framebridge/tree/2755eac9585658b30b4a8e4def7185b7c4ffbcb7) [Release notes](../RELEASE-NOTES.md#038) · [Review and release workflow](RELEASE-WORKFLOW.md)

## Build artifacts

The three native Debian packages were built on GERAMEG. The package profile determines the distribution, system Python and APT suite. Compare the exact filename, byte size and SHA-256 with the verified official files. Verify the relevant detached signature or signed APT metadata with the independently trusted release key before installation.

| Filename | APT suite | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| `FrameBridge-0.3.8-debian13-amd64-py313.deb` | `trixie` | 13,423,956 | `0160f218787d81ed612a2c752880a1b702146721fc38fc68adf21e6d0d8d0592` |
| `FrameBridge-0.3.8-ubuntu24.04-amd64-py312.deb` | `noble` | 13,424,276 | `74569cb2f74fa9aa29408306c5a95573afe9f2f8fc9cb18623fb1546a02d06b0` |
| `FrameBridge-0.3.8-mint22-amd64-py312.deb` | `mint22` | 13,424,288 | `4ccdf362251ed42d9ccbac0306f3f5e04e706b65e32eb7709a2c706ef1905610` |
| `FrameBridge-0.3.8-source.tar.gz` | | 27,682,939 | `3ca4cb890d418fd111ad5a27ee9f894cdccf718b2ac25621b1a2406270132f9a` |

The APT input manifest records the files used to assemble repository metadata. Public APT verification requires matching the final signed indexes and the bytes served by the active repository for each suite.

## Verification status

| Check | Result |
| --- | --- |
| Focused regression tests | 260 of 260 passed |
| Full suite in the sandbox | 1,138 passed. The same 45 environment-related failures and errors were reproduced on the baseline. |
| Isolated GERAMEG QA | 133 of 133 passed |
| Release signatures and isolated local APT | Passed for the staged release and all three APT suites |
| Public HTTPS downloads | Complete package and source downloads match their SHA-256 and detached signatures |
| Public signed APT | Candidate 0.3.8 and exact package hashes verified in trixie, noble and mint22 |

Source tests cover deadline enforcement, receipt reconciliation and authenticated routing. GERAMEG QA applies to the isolated validation environment. Installation and upgrade on each named distribution and rollout to enrolled nodes have separate acceptance records. [Claims and evidence boundaries](CLAIMS-AND-LIMITS.md)
