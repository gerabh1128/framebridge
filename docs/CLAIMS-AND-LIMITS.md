# Claims and evidence boundaries

The latest signed release is **FrameBridge 0.3.3**, with separate amd64 packages for Debian 13, Ubuntu 24.04 and Linux Mint 22.x. The [official download page](https://framebridge.betancourt.one/download/) identifies the actual artifacts, signatures and corresponding source. A later default-branch documentation change does not rewrite the signed tag or an installed package.

## Current release scope

| Statement | Evidence and boundary |
| --- | --- |
| Distribution profiles | Debian 13 / CPython 3.13, Ubuntu 24.04 / CPython 3.12 and Mint 22.x / CPython 3.12, all amd64. Ubuntu 26.04, LMDE 7, other architectures and other distributions are outside this release's validated scope. |
| Signed APT updates | Actual update and managed-service lifecycle checks passed on Debian 13, Ubuntu 24.04 and Mint 22.3 VMs. This is not acceptance of every physical installation or every future package. |
| Release identity | The signed tag and release record identify released source and bytes. A source checkout, successful build or checksum alone does not establish an official signed release. |
| Source tests | Run `python3 tools/run_tests.py` from a source checkout. A test result applies to the source revision and environment actually tested; historical test counts are not current results. |
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
