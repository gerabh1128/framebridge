# Claims and evidence boundaries

**FrameBridge 0.3.9** has separate amd64 packages for Debian 13, Ubuntu 24.04 and Linux Mint 22.x. It includes the Linux tray and automatic central/node role selection. The administrative supervision Site is outside the package scope. The release record identifies the frozen source, signed artifacts and verification evidence. [Official download page](https://framebridge.betancourt.one/download/) · [0.3.9 source-to-artifact record](RELEASE-SOURCE-0.3.9.md)

## Current release scope

| Statement | Evidence and boundary |
| --- | --- |
| Distribution profiles | Debian 13 / CPython 3.13, Ubuntu 24.04 / CPython 3.12 and Mint 22.x / CPython 3.12, all amd64. Ubuntu 26.04, LMDE 7, other architectures and other distributions are outside this release's validated scope. |
| Signed APT updates | Isolated APT checks against the public HTTPS repository verified the 0.3.9 candidate and package identity in `trixie`, `noble` and `mint22`. Installation, managed-service restart and reboot require separate evidence. [0.3.9 source-to-artifact record](RELEASE-SOURCE-0.3.9.md) |
| Release identity | The frozen commit identifies the source. The signed corresponding-source archive, manifest, package signatures and release record identify the released bytes. [0.3.9 source-to-artifact record](RELEASE-SOURCE-0.3.9.md) |
| Source tests and live upgrade | All 279 focused tests and 102 native Debian 13 tests passed. The live GERAMEG upgrade reached managed-profile readiness and completed a direct Fleet command. The full sandbox suite and its baseline comparison are recorded separately. Results apply to the tested source and systems. [0.3.9 source-to-artifact record](RELEASE-SOURCE-0.3.9.md) · [Run focused source tests](../CONTRIBUTING.md#run-focused-source-tests) |
| Installed documentation | Read the guide matching the installed version. A newer repository README cannot update an older installed manual. Report contradictory release labels with the package version and file path. |

## Operation results

| Statement | Evidence and boundary |
| --- | --- |
| ChatGPT connection | Manual owner account consent; the model remains hosted while tools run on selected owner-controlled computers. Installing the package or assigning an account label does not prove connection. |
| Receiver and target | `LOCAL` denotes the current receiver. Another computer must be selected using a confirmed enrolled alias or ID. A legacy endpoint's default destination is not a routing guarantee for a replacement endpoint. |
| Direct execution receipt | The receipt distinguishes expiry before dispatch or execution from an in-flight request or an unknown result. Reconcile `in_flight` and `outcome_unknown` by request or job ID. The enrolled alias, node UUID, receiver hostname and available execution hostname identify different parts of the route. |
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
