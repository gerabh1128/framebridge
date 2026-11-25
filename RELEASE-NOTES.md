# 0.3.9+fbterm.18 — informational FBTERM

- Simplify the local job context menu to read-only details, verified output, and copy ID; no request, edit, or cancellation controls.
- Show the recorded claim-to-finish duration when available. Keep the persistent viewer, read-only VTE client, and existing execution safety and maintenance controls.
- Retain historical review and job records. No external review task or notification service.

# 0.3.9+fbterm.17 — persistent viewer correction

- Launch the profile-owned GTK/VTE viewer with distribution Python, keeping the private execution interpreter separate.
- Prevent private Python ABI search paths from leaking into the graphical process; preserve the desktop/profile binding and read-only attachment.
- Preserve job history, existing update preparation, signed stable APT source and package maintenance safeguards.
- Correct portable installation and diagnostic guide labels. Release acceptance and signing are separate gates.

# 0.3.9+fbterm.16 — unsigned portable release assembly

- One amd64 package with private CPython 3.13.18 and verified vendor inputs; excludes pip and ensurepip. Retains system GTK/GI and package-maintenance dependencies.
- Stable APT source and scoped public archive key are package-managed conffiles; signature verification, publication and acceptance of the exact artifact are external gates.
- Local owner preparation pauses new managed EXEC admissions and lets admitted jobs finish. An uncertain result remains a blocker. No automatic cancellation, restart or replay.
- Durable owner review requests and new unknown-outcome notifications use exact job identity and digest. Authorized acknowledgements do not constitute resolution. A functioning authenticated transport is needed for remote retrieval.
- Retains verified original FBTERM activity display and local owner controls. No claim of public availability or unsupported distribution compatibility is made by the source assembly.

# Private candidate 0.3.9+fbterm.15

Correct header visibility ownership; preserve the approved classic .14
viewer unchanged. Default header text updates cannot toggle tmux visibility.
Only a verified singular legacy attachment can request the legacy header.
Separately installed legacy keepers require an explicit operator migration
check. No automatic disabling of unrelated services or public release.

# Private candidate 0.3.9+fbterm.14

Classic FBTERM presentation approved in an isolated local preview: neutral
palette, terminal-first layout, collapsible recorded history, explicit
non-executed job labels and suppression of the redundant embedded header.
The Original execution engine, read-only attachment and job records are
retained. Approval of the visual prototype is not package acceptance or
authorization to publish. Native lifecycle and signature validation remain
separate release gates.

# Private candidate 0.3.9+fbterm.10

Enrollment recovery preserves the existing node identity and uses an individually approved pending request. Bounded node runtime failures apply to legacy managed services. Candidate only; no public release or deployment certification.

# FrameBridge release notes

## Cold package replacement — 0.3.9+fbterm.9 candidate

Active-profile upgrades are deliberately refused. Package maintenance excludes cooperating runtime participants, persists until successful configuration, and does not schedule a delayed restart. Older versions require operator-enforced quiescence. See [cold package maintenance](docs/PACKAGE-MAINTENANCE.md). This remains a private candidate until native lifecycle validation and approval.


## 0.3.9+fbterm.9 (corrective candidate)

### Persistent FBTERM activity viewer

The managed runtime now supervises the persistent **FrameBridge Expanded** activity viewer for both manual startup and configured autostart. The viewer is a read-only view of the original FBTERM session. It keeps the original execution pane and presents an English header with the local host, installed version, running jobs, queued jobs and unconfirmed outcomes.

While the runtime is active and a validated graphical desktop is available, closing the viewer causes it to reopen. Minimizing it is respected. The supervisor does not focus an existing window or create another viewer while a matching window or client is still present. It refreshes the graphical environment after sign-in so an already-running service can attach to the available desktop.

Without a validated desktop, the supervisor does not open a viewer or create a worker. An explicitly selected private virtual X11 display remains a supported separate context. If an established worker or session disappears, the viewer supervisor reports the missing runtime and waits for recovery; it does not replay a job or report a healthy session that has not been verified. Stopping the profile stops its viewer supervisor before the normal terminal cleanup.

### Missing-engine requests fail before dispatch

The job scheduler now checks that the original engine path exists as a readable file before preparing a worker lane or invoking the controller. This covers a stale runtime link or a missing engine after the adapter has already started. The request receives `FBTERM_ENGINE_UNAVAILABLE` with `execution_state: not_started`, instead of acquiring an uncertain outcome solely because the engine could not be invoked.

The start deadline retains precedence. An identical job submission returns its existing result and does not execute again. After the dependency has been repaired, a new job ID is required for a new attempt. Failures after invocation and unverified execution receipts retain the conservative `outcome_unknown` state and reservation.

### Existing migration and recovery protections

The transactional role-path migration introduced in `.7` is retained. It repairs supported dangling package links, preserves displaced entries in a private journal and rejects unexpected entries or unfinished migrations. Profiles, historical queues and credentials are not reset by this migration.

The local, interactive, offline reconciliation procedure introduced in `.8` is also retained. It requires exact job identity, quiescent execution locks, independent evidence and a durable review decision. It preserves the original uncertain outcome and does not permit the same job ID to be replayed. The new engine preflight does not reconstruct missing historical receipts or automatically reconcile old reservations.

### Validation scope

Artifact integrity and compatibility checks, source regression tests, native runtime tests and package-upgrade acceptance are recorded separately. A build for a named distribution does not by itself establish native installation, desktop, login or reboot acceptance. In particular, an extracted-profile migration test does not certify a complete `.4` to `.9` package upgrade, and CPython 3.12 artifact checks do not establish native Ubuntu 24.04 acceptance. Consult the acceptance record for the exact package hashes, tested environments and remaining gaps before promotion.

## 0.3.9+fbterm.8 (private candidate)

A missing original execution receipt can leave a permanent exclusive reservation.
This candidate adds a local, interactive, offline reconciliation procedure for
that case. It requires exact job identity, independent review of pending effects,
quiescent scheduler and engine locks, a consistent backup, and a durable decision.
The original state remains outcome_unknown, and the same job ID cannot be replayed.
Changed identity/evidence invalidates the decision before further dispatch.
The public release privacy gate and .7 layout migration are retained.
See [the recovery procedure](docs/JOB-RECONCILIATION.md). No signed publication or
native installation is claimed by this source revision.

## 0.3.9+fbterm.7

This revision retains the release privacy checks introduced in the previous
privacy candidate and repairs the incompatible role-based runtime layout
introduced by the prior public package. Existing profiles can retain stale
symbolic links after an upgrade. Migration now uses a per-profile lock, verified
directory descriptors, Linux no-replace renames and a private transaction
journal. Unexpected entries are not overwritten. A completed rollback or
migration retains its evidence; an unfinished journal stops preparation for
operator review. Historical queues, profiles and credentials are not deleted.

See the [layout recovery procedure](docs/LAYOUT-MIGRATION.md). Build and unit
test success does not establish native installation, upgrade, reboot or signed
publication acceptance.

## 0.3.9+fbterm.5

The public-release builders inspect the assembled source and both sections of
each DEB before writing the finished files. The release review can also check the
rendered website and use private, locally maintained identifiers. Findings
identify the affected section without printing the value that triggered them.
See [the publication checks](docs/PRIVACY-REVIEW.md#publication-checks).

## 0.3.9+fbterm.4

Bundled components use role-based paths for the workstation, fleet hub and terminal node. Tests use synthetic node identifiers and sample addresses. Fleet nodes are selected through their enrolled identities.

## Linux job updates

**0.3.9+fbterm.3** incorporates three revisions on top of 0.3.9.

- **fbterm.1** separates command dispatch from terminal input and preserves the recorded output bytes. Launch receipts distinguish a confirmed cancellation before execution from an uncertain outcome.
- **fbterm.2** records job and request identities. Independent resources can run concurrently. Conflicting requests wait for their resource and expose the blocking job. Repeating an identical submission retrieves the existing job. Reusing the identifier for different content returns a conflict.
- **fbterm.3** retrieves captured output at the adapter that ran the job. The adapter checks the full capture against the recorded SHA-256 before returning a bounded view. A verified empty result, a missing capture and an integrity mismatch have distinct responses.

The package acceptance tests exercised concurrent work, duplicate requests and remote output retrieval on Debian 13 and Linux Mint 22.3 amd64. The Ubuntu 24.04 amd64 package was built and checked against its manifest. Native execution and package upgrade on Ubuntu remain to be verified for this revision. The [download catalog](https://framebridge.betancourt.one/assets/release.json) identifies the version available through the signed distribution channel.

## 0.3.9

- Linux peers negotiate larger blocks when transferring original PNG captures.
  The receiver verifies the complete image, and older nodes retain their
  existing transfer format.
- The broker acknowledges authenticated orphaned results after a restart so a
  node can resume polling. The broker validates result and polling envelopes,
  and the result wait for a read begins after it enters the queue.
- The screen client preserves the Windows node's policy response and validates
  capture provenance when the Windows screen operation is authorized.
- Local TLS control uses the configured listener address. CLI commands report
  failed operations with nonzero exits, and package upgrades ignore processes
  that fail the managed-profile identity checks.

## 0.3.8

The broker discards a queued direct execution request once its dispatch deadline
passes. The node checks the deadline again before forwarding the request to its
executor. A response without a final result reports the available execution state
and preserves the requested and resolved node identities. A command that already
started remains subject to its execution timeout.

Use `fb nodes execution` or the read-only MCP tool `framebridge_exec_status`
to check an existing remote receipt by request or job ID. Receipts distinguish
the registered node label, the receiver's hostname and the hostname reported
by the node in its execution result.

## 0.3.7

The Linux package carries forward the 0.4.0~rc3 tray, automatic LAN role
selection, target routing, CUA and FBEXEC. The tray opens FrameBridge status,
FBConsole and FBSetup. The central and nodes use the same package per Linux
profile. This package excludes the administrative Site, its launcher and
its web assets.

Debian treats a move from a 0.4.0 release candidate to 0.3.7 as a downgrade.
The package snapshots active profiles from the three compatible candidates
for that transition. See the installation role guide for deployment and
rollback.

## 0.4.0~rc3

The Linux notification area can open FrameBridge status, FBConsole and FBSetup. The tray starts at graphical sign-in. It uses a StatusNotifier host when available and the GTK notification area on compatible X11 desktops. Closing the tray leaves FrameBridge services running.

## 0.3.6

Reliability update for authenticated node heartbeats during active work, fresh graphical status, verified visible WINDOW capture and coherent post-action metadata. The complete FBTERM audit is retained with less directory-scanning overhead. Original-PNG transfers use a 90-second budget. See [the 0.3.6 change notes](docs/RELEASE-NOTES-0.3.6.md). Package assembly, per-OS acceptance, signatures and publication are separate release records.

## 0.3.5

Reliability update for the existing three Linux profiles: concurrent bounded TLS handshakes, automatic read-only FBTERM viewer discovery after graphical login, persistent default profile selection, bounded streaming tunnel metrics and safe package bytecode invalidation. See [the complete 0.3.5 notes](docs/RELEASE-NOTES-0.3.5.md). Artifact signatures, native lifecycle acceptance and publication status belong to the external release record.

## 0.3.4

Native persistent startup improvements, lower idle disk writes, local health reporting and confirmed Fleet destinations. The release has three amd64 distribution profiles and matching source with both Python ABIs. See [the complete 0.3.4 notes](docs/RELEASE-NOTES-0.3.4.md); signatures and actual lifecycle results are recorded with the release artifacts.

## Historical private production pilot

This section records the behavior and validation of an earlier private pilot.
See the versioned sections above for subsequent public releases.

- Recover from classified transient broker failures with capped backoff and fresh signed nonces. Result acknowledgement retries retain the computed result and never reexecute a command. TLS, authentication and malformed-response failures remain terminal.
- Dispatch blocking MCP operations in bounded workers, preserve synchronous internal callbacks, and protect shared initialization. Concurrent generated local job IDs are distinct.
- Negotiate larger original-PNG chunks per capture while retaining legacy receivers. Apply a cooperative transfer budget and require fresh observation after uncertain input replies; performed claims require explicit matching backend confirmation.
- Bind local real-terminal capture transport to the current node identity.
- Keep privileged-helper downgrade revocation tied to the fixed 0.3.2 compatibility floor, allowing private candidates to return to 0.3.2 without revoking grants.

Validation: 721 integrated source tests passed with zero failures, errors or skips. Frozen 0.3.2 source and signed Debian artifact remained unchanged. Focused tests cover real vendored MCP dispatch, retry/no-replay behavior, frozen screen-receiver compatibility and isolated maintainer-script semantics.

Pilot limits: an already-running transport call could exceed the cooperative capture budget. A broker restart could reject an unknown in-flight result, and a lost poll reply could orphan a request. Recovery for those cases excluded command reconstruction and replay. The 0.3.9 section describes the later broker recovery behavior.

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
