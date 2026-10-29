# Historical FrameBridge 0.3.4 development notes

This record describes the earlier private I/O candidate. The final 0.3.4 source and current scope are documented in [the release notes](RELEASE-NOTES-0.3.4.md).

Version: `0.3.4~dev.20261029.3`.

This is a development candidate following the signed 0.3.3 release. It does not replace the signed tag, public downloads or stable APT channels. Build success is not installation, reboot, independent-user or multi-customer acceptance.

This I/O candidate is based on `0.3.4~dev.20261029.1`. The separate `.2` node-initialization experiment is not included.

## Changes

- `fb health` and `fb --json health` combine existing local status and prerequisite observations. The report names the receiving computer and profile, separates query success from health and connection intent, and leaves unknown values unknown. It does not create an absent profile, start a service, repair configuration or probe terminal/desktop control.
- Health exits 0 for a complete healthy or intentionally paused observation and 1 for degraded, unavailable or unconfigured state. JSON `result.ok` describes observation completeness; `result.health.healthy` describes health. A requested pause is not healthy when the observed tunnel still runs.
- Fleet read, EXEC, screen and CUA results include `requested_target`, `resolved_target`, `node_id`, `hostname` and `transport` from the confirmed route. Existing `target` semantics remain compatible. Upstream result fields cannot replace the confirmed identity. Errors after resolution retain that identity; unresolved targets never acquire an invented local identity.
- Installed documentation is assembled from the reviewed current source, including the corrected signed 0.3.3 publication record, support scope, first-connection instructions and canonical homepage. Shared CLI/MCP/manpage references are regenerated for this development version.
- Managed connection setup accepts administrator-owned `.config` and `.config/systemd` ancestors when they are directories without group/other write permission. The final `systemd/user` directory must still belong to the connecting user. Symbolic links, other-account ownership and unsafe permissions are rejected. Setup does not change existing directory ownership or permissions.
- Managed-worker state is persisted on transitions instead of repeatedly syncing the same state while idle. Connection liveness heartbeats use the account's validated `/run/user/<uid>` runtime directory when available; profiles without it retain a bounded durable fallback. Status still requires a fresh heartbeat bound to the same worker identity. Existing permissions and credentials are unchanged. These changes do not claim that all application or system disk activity stops.

## Native startup and the GERANAS deployment gap

Normal `fb connect` already installs the managed user service, protects the connection credential, enables startup without an interactive login and records connection intent. Installation of a package without connection setup cannot establish a new authenticated tunnel. APT preserves a manual profile's existing policy rather than silently enabling it.

The affected GERANAS deployment had been left as a manually started central profile without the native managed connection setup. External recovery services did not complete that setup and did not share the CLI's lifecycle controls. This candidate fixes the service-account directory compatibility issue needed for native setup; it does not add another external startup mechanism or fabricate managed installation records.

Source tests and package builds do not prove reconnection after a reboot. Acceptance must separately record a real changed boot ID, native service startup, connection availability and execution through the intended route. No reboot acceptance is claimed by these release notes.

## Scope

The three build profiles remain Debian 13 amd64/cp313, Ubuntu 24.04 amd64/cp312 and Linux Mint 22.x amd64/cp312. The new health command observes one selected **local profile**, not the fleet. Invoke it on the intended host and pass `--state-dir` when using a nondefault profile. Runtime readiness and capability availability are not fresh proofs that a command, screenshot or input operation succeeded.

The existing indicative console, connection intent, boot policy, user profiles, privileged-helper lifecycle and authentication workflow are preserved. Normal browser and operating-system authentication stay in the owner's normal interface. No new enrollment, transport, GUI or recurring approval mechanism is introduced.

The next product acceptance items are documented in [the operations roadmap](OPERATIONS-ROADMAP.md). Versioned repair workflows, a multi-host health dashboard, customer isolation and other MCP clients remain proposed work until separately implemented and tested.
