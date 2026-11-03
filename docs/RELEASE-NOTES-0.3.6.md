# FrameBridge 0.3.6

Source integration candidate. Package assembly, target-OS acceptance, release signatures and publication are recorded separately; this text does not certify them.

## Reliability changes

- A negotiated, authenticated node heartbeat keeps liveness observable while one request is executing. Heartbeats do not dequeue another command. The central reports active work separately from offline status and preserves request expiry, signed nonces and no-replay behavior.
- `status` and `doctor` refresh the current graphical observation on a copy of the environment instead of treating a text-only boot observation as the present desktop state. Explicit virtual-display selection remains separate.
- Graphical WINDOW capture verifies a real ROOT capture and crops the stable visible window intersection. Client-side decoration geometry and partially off-screen windows retain explicit full-window and visible-source metadata. Input coordinates use the same verified visible mapping.
- Post-action capture metadata describes the new snapshot. Original action evidence stays in `action_capture`; a ROOT follow-up does not retain obsolete WINDOW fields.
- The FBTERM audit still checks every job. The scan avoids redundant directory/stat work without caching the result or silently limiting the audit to recent jobs.
- The central original-PNG transfer budget is 90 seconds. The default graphical follow-up is ROOT. Capture identity, chunk order, complete hash validation, window/geometry checks and uncertain-input handling remain enforced.

## Compatibility and limits

The public Linux profiles remain Debian 13 / Python 3.13, Ubuntu 24.04 / Python 3.12 and Linux Mint 22.x / Python 3.12 on amd64. The reviewed vendor inputs and SDK remain unchanged. Older nodes retain their existing behavior until upgraded; heartbeat capability negotiation preserves older endpoints.

Source tests and prior live pilot observations are separate from installation, graphical acceptance and reboot lifecycle on each distribution. Large or slow captures can still exceed a finite transfer deadline. A heartbeat is not proof that an executing command succeeded. Unknown or expired in-flight results are not reconstructed or replayed automatically.

Historical 0.3.5 signed artifacts, corresponding-source records and checksums keep their identities. This candidate must receive its own source archive, packages, checksums and release/repository signatures before public promotion.
