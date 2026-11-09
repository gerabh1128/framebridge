# Claims and evidence boundaries

**FrameBridge 0.3.7** targets the same three release profiles, with separate amd64 packages for Debian 13, Ubuntu 24.04 and Linux Mint 22.x. It includes the Linux tray and automatic central/node role selection, but does not bundle the administrative supervision Site. The [official download page](https://framebridge.betancourt.one/download/) identifies the actual artifacts, signatures and corresponding source. A later default-branch documentation change does not rewrite frozen release source or an installed package.

## Current release scope

| Statement | Evidence and boundary |
| --- | --- |
| Distribution profiles | Debian 13 / CPython 3.13, Ubuntu 24.04 / CPython 3.12 and Mint 22.x / CPython 3.12, all amd64. Ubuntu 26.04, LMDE 7, other architectures and other distributions are outside this release's validated scope. |
| Signed APT updates | The [0.3.7 publication record](RELEASE-SOURCE-0.3.7.md) records isolated APT update and candidate-selection verification through public HTTPS on 9 November 2026 for trixie, noble and mint22: candidate 0.3.7 and signed index Size/SHA256 matching the verified artifacts. Complete package downloads were checked separately through the public downloads URLs, not performed by that APT test. This is distribution acceptance, not a new package installation, managed-service restart or reboot test. Earlier VM lifecycle results remain historical. |
| Release identity | The signed corresponding-source archive, manifest, package signatures and release record identify released source and bytes. The [v0.3.7 GitHub source release](https://github.com/gerabh1128/framebridge/releases/tag/v0.3.7) and its Git tag are separate records. A source checkout, successful build, Git tag or checksum alone does not establish an official signed binary release. |
| Source tests | See the directly runnable focused tests and their prerequisites in [CONTRIBUTING.md](../CONTRIBUTING.md#run-focused-source-tests). The historical 0.3.5 1,011-case result and the 0.3.7 source-release test report are not substitutes for a fresh run. A result applies to the exact source revision and environment tested; neither a source-test count nor a signed package proves live graphical, upgrade or reboot acceptance. |
| Installed documentation | Read the guide matching the installed version. A newer repository README cannot update an older installed manual. Report contradictory release labels with the package version and file path. |

## Operation results

| Statement | Evidence and boundary |
| --- | --- |
| ChatGPT connection | Manual owner account consent; the model remains hosted while tools run on selected owner-controlled computers. Installing the package or assigning an account label does not prove connection. |
| Receiver and target | `LOCAL` denotes the current receiver. Another computer must be selected using a confirmed enrolled alias or ID. A legacy endpoint's default destination is not a routing guarantee for a replacement endpoint. |
| Query and task success | A successful status query or outer tool wrapper does not prove readiness or successful inner execution. Read the target, exit code and operation result, then verify the intended effect. |
| Persistent console | Bash continuity lasts while the session survives; it does not promise survival across reboot. Preserve the existing console and unrelated work unless changing them is authorized. |
| Independent FBEXEC tasks | Repeated maintainer experience with non-overlapping work; no certified isolation or guarantee that every command can run concurrently. |
| Graphical control | Requires a compatible active X11 session and observed capture/input success. Native Wayland control is not certified. One controller per shared desktop is an operating recommendation, not an automatic lock. |
| Real images and demonstrations | Use complete authentic captures. Identify virtual displays and terminal text separately. Conceptual animation is illustration, not evidence of execution. |
| sudo | Actual privileges depend on the Unix account and normal authentication; an authorization flag does not grant root. |
| Recovery | Pause, stop, boot policy, revocation and removal have different effects. A package downgrade can revoke administrative helper grants; retaining a previous package is not proof of automatic rollback. |
| Dependency terms/source | Scoped technical terms review and exact source availability are recorded with delivery conditions; component terms and provenance limits remain in force. |

Earlier RC7 and Community Preview results remain historical records. They must not be substituted for the build, signature, installation or acceptance evidence of a later artifact. The [operations roadmap](OPERATIONS-ROADMAP.md) distinguishes proposed work from delivered capabilities.

## Retained control components

The retained development broker defaults to its existing 127.0.0.1 peer only. Repeatable `--control-peer IP_LITERAL` arguments add explicit peers. Invalid entries fail before Store creation/binding; checks use the real client address, not proxy headers. Retained RPC UID, host, client/socket and administrator identity guards remain unchanged. Normal Expanded handling uses the generic terminal entrypoint; `FRAMEBRIDGE_ENABLE_GERAMEG_CUA_TTY=0` is not an effective guard.

The classic lab-target map is empty, while the compatibility route remains available for explicit integrator configuration. Do not describe retained code as entirely inactive. Executable changes require an identifiable source revision and validation of the changed behavior.
