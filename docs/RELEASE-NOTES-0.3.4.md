# FrameBridge 0.3.4

Release profiles: Debian 13 amd64/Python 3.13, Ubuntu 24.04 amd64/Python 3.12 and Linux Mint 22.x amd64/Python 3.12.

## Network administration and observation

- `fb health` and `fb --json health` summarize the selected local profile without starting, repairing or connecting it. Query completeness, health, connection intent and unknown observations remain distinct.
- Fleet command, terminal, screen and CUA results identify the confirmed receiving computer, node and transport. Result fields cannot replace the destination identity established by routing.
- Transient broker failures recover with bounded backoff and fresh signed nonces. Acknowledgement retries retain the computed result and do not reexecute commands. Authentication, TLS and malformed-response failures remain terminal.

## Persistent startup and idle writes

- Native managed startup accepts secure administrator-owned service-directory ancestors without changing their ownership or permissions. The final user-service directory remains private to its account.
- Node startup avoids redundant privileged setup when the required account settings already exist. Existing credential grants still follow the privileged cleanup lifecycle.
- The managed supervisor persists worker-state transitions instead of repeatedly syncing identical state. Connection heartbeats use the validated account runtime directory when available, with the same process identity and 15-second freshness checks. A bounded durable fallback remains for environments without a user runtime directory.
- APT upgrades preserve profiles and existing startup policy. Normal connection setup provisions the native managed service; installing a package alone cannot establish an authenticated ChatGPT connection.

## Console, compatibility and delivery

- The indicative console and persistent terminal remain available. Real-screen capture, terminal transcripts and explicitly selected virtual displays are identified separately.
- The same English command guide serves the CLI, MCP help, installed manual and tutorial. Other distributions, Wayland and CPU architectures are outside this release's validated scope.
- One corresponding-source archive includes the reviewed cp313 and cp312 vendor inputs for all three packages. Public-release assembly requires its matching manifest and checks the archive, current source and selected vendor hashes.
- Original material remains GPL-3.0-only. Preserve both immutable dependency-source assets, third-party notices and stated provenance limits. No new independent native-wheel or tunnel-client rebuild is claimed.

## Validation and release identity

Artifact hashes, signatures, signed APT metadata and actual distribution lifecycle results belong to the accompanying release record. Build assembly does not assert that those external checks passed or that a download has been published. Retain the source archive and its sidecar manifest together when rebuilding official release profiles.

Programming: ChatGPT. Original concept and project coordination: Gerardo Betancourt.
