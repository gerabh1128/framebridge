# FrameBridge release notes

## Unreleased — private production pilot fixes

Private pilot validation remains in progress; these changes are not a public release.

- Recover from classified transient broker failures with capped backoff and fresh signed nonces. Result acknowledgement retries retain the computed result and never reexecute a command. TLS, authentication and malformed-response failures remain terminal.
- Dispatch blocking MCP operations in bounded workers, preserve synchronous internal callbacks, and protect shared initialization. Concurrent generated local job IDs are distinct.
- Negotiate larger original-PNG chunks per capture while retaining legacy receivers. Apply a cooperative transfer budget and require fresh observation after uncertain input replies; performed claims require explicit matching backend confirmation.
- Bind local real-terminal capture transport to the current node identity.
- Keep privileged-helper downgrade revocation tied to the fixed 0.3.2 compatibility floor, allowing private candidates to return to 0.3.2 without revoking grants.

Validation: 721 integrated source tests passed with zero failures, errors or skips. Frozen 0.3.2 source and signed Debian artifact remained unchanged. Focused tests cover real vendored MCP dispatch, retry/no-replay behavior, frozen screen-receiver compatibility and isolated maintainer-script semantics.

Known limits: the cooperative capture budget can be exceeded by an already-running lower transport call. A broker restart can reject an unknown in-flight result; a lost poll reply can orphan a request. These cases do not reconstruct or replay commands. Live fleet acceptance remains separate from source-test success.

## 0.3.1 — Community Release

Installer: FrameBridge-0.3.1.deb  
Debian package: framebridge-original-expanded  
Debian version: 0.3.1  
Project source: FrameBridge-0.3.1-source.tar.gz  
Target: Debian 13 amd64 with Python 3.13.

This is a separately identifiable revision of FrameBridge Original Expanded. It does not replace the historical RC7 archive or claim RC7 certification for newly changed behavior.

### A shared human and agent interface

- The same fb/framebridge command interface serves people and authorized agents.
- Normal connection uses fb connect; an ordinary initial connection needs no setup/start sequence. The existing connect chatgpt spelling remains compatible.
- fb status combines local tools, actual connection, connection intent and boot policy. Scoped connection/runtime/boot/console views use the same observation model.
- fb doctor remains a separate diagnostic operation. Query success is distinguished from a verified connection.
- Explicit --json output has a versioned envelope. --json and --non-interactive avoid prompts and report a required normal-terminal action when appropriate.
- fb help, the installed manual/tutorial and MCP agent guide share the versioned guide. The server supplies a concise learning invitation and a registered read-only help tool.

### Explicit lifecycle boundaries

- Canonical fb disconnect preserves an intentional pause through worker recovery and reboot until the next connect.
- Recovering a saved connection does not require restarting otherwise healthy local tools.
- Current service start/stop and future boot policy are separate. An initial node service setup is reported explicitly; it uses the central connection rather than creating another ChatGPT tunnel.
- Disabling future startup retains saved connection material. Forgetting it is a separately named, explicitly confirmed operation.
- Compatibility routes retain their documented historical effects. In particular, legacy autostart disable remains different from policy-only service boot disable.

### Console and terminal clarity

- Console reattachment preserves an existing zoom/focus choice instead of treating every attachment as initial layout.
- The dedicated console accepts the supported z/Ctrl+Z/Z zoom-key variants after its prefix.
- The horizontal work view follows the FBTERM-left/Bash-right arrangement; graphical and text layouts use the reviewed context and responsive rules.
- The launcher captures setup output instead of presenting technical READY JSON as the normal work screen.
- Real-screen CUA and CLI terminal transcripts remain distinct. Neither a terminal transcript nor a conceptual animation is presented as an authentic complete screen capture.
- Per-session sudo-mode changes document that Bash is recreated and internal shell state is lost.

### Distribution and provenance

- Original FrameBridge material remains GNU GPL version 3 only. Third-party notices, native dependency terms and source provenance remain separate.
- OFFICIAL-RELEASES.md explains maintainer-controlled branches, signed official artifacts and lawful independent variants.
- The release source set includes the project source plus the two dependency assets identified below.
- Signing and publication readiness belong to the external release record. Building the package does not assert that the repository is public, signed or ready to expose to the Internet.

### Corresponding source assets

| Asset | Bytes | SHA-256 |
| --- | ---: | --- |
| FrameBridge-0.3.1-Community-Preview1-dependency-sources.tar.gz | 82,606,998 | 64540a85bd65eac3f394d8c0a967cacbc00e112676d57d644dcd1bb691c1926a |
| FrameBridge-0.3.1-tunnel-client-dependency-sources.tar.gz | 26,941,745 | 6445d4f0e773aba833fe5c610fbae54102b92de2ce636438f769f21e890c2bd9 |

The historical Preview1 filename identifies an immutable shared dependency asset. It is not the version of the FrameBridge installer. The tunnel dependency archive identifies exact dependency module sources and retained notices; it is not proof of an independently reproduced tunnel executable or its exact own-source revision.

### Validation record

Release-specific check results, artifact hashes and signature status are recorded in the accompanying external release evidence. No new manual ChatGPT connection or node-creation campaign is implied by these notes. Focused checks cover the revised interfaces and packaging without claiming that every existing integration scenario was repeated.

### Credits

Programming: ChatGPT.  
Original concept and project coordination: Gerardo Betancourt.

This is an independent community project, not an OpenAI product.
