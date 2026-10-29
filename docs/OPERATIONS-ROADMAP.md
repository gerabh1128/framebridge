# Small Linux fleet operations: development scope

This document records proposed follow-up work after the signed 0.3.3 release. It is not a feature-availability statement, a release announcement or a promise of unattended administration. The initial scope remains owner-controlled Debian 13, Ubuntu 24.04 and Mint 22.x amd64 computers administered by one responsible person.

The `0.3.4` release source implements an initial **local-only** `health` summary and additive confirmed destination identity in Fleet results. It also consolidates the installed documentation, permits secure administrator-owned ancestors of the user's service directory and reduces durable idle status writes. These changes do not complete the wider acceptance items below. See [release notes](RELEASE-NOTES-0.3.4.md).

## Acceptance order

| Priority | Intended improvement | Acceptance evidence required |
| --- | --- | --- |
| 1 | Coherent installation and diagnosis | The package, CLI, MCP guide and website agree on version and supported profiles. An administrator outside the project completes installation, connection and a first harmless query using the documentation; record time and assistance needed. |
| 2 | Unambiguous execution destination | Show receiver and effective target separately. An explicit target never falls back to another machine. Cover unknown and duplicate names and differences from legacy defaults. |
| 3 | Unified read-only health | Reuse existing status, doctor and inventory tools. Report observation time, source and unavailable/error states. Distinguish disk capacity from free space, package presence from readiness and screenshot availability from input control. |
| 4 | Five versioned procedures | Begin with general diagnosis, a failed service, DNS diagnosis, APT diagnosis and a FrameBridge update. Record prerequisites, exact procedure version, proposed change, applicable authorization, post-check and a recovery route where supported. Read-only checks must not repair implicitly. |
| 5 | Jobs and technical history | A later conversation can identify the target, operation, result and remaining work without reconstructing the entire chat. Test disconnect/reconnect behavior and duplicate submissions. Export useful records without secrets. |
| 6 | Small external pilot | Use a separate installation per customer initially. Record installation effort, incidents, completed tasks, support time and willingness to renew before extending the platform. |

## Preserve the current operating model

- Keep `fb` / `framebridge` as the shared interface for the administrator and authorized agents.
- Retain the indicative console and existing work. A health view complements it.
- Use terminal and command tools for routine administration, with graphical CUA available for tasks that need a real desktop. Describe actual session and display limits.
- Keep normal OS/browser authentication in the owner's normal interface. Reuse task authorization; do not add repetitive approvals to harmless observations.
- Retain explicit connection intent and boot policy. An update must not start a profile that was intentionally stopped.
- Treat transport as a component. Another MCP client or platform is supported only after a documented interoperability test.

## Deferred expansion

Shared multi-customer hosting, broad Windows/macOS/DSM support, native Wayland control, a new tunnel/VPN and an autonomous always-on technician are separate projects. They need their own requirements and acceptance evidence. The initial commercial experiment is a scoped installation and maintenance service, not a claim that FrameBridge already supplies a full enterprise RMM platform.
