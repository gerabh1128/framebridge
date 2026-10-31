# FrameBridge 0.3.5

Reliability update for Debian 13 amd64/Python 3.13, Ubuntu 24.04 amd64/Python 3.12 and Linux Mint 22.x amd64/Python 3.12.

## Broker availability

TLS negotiation runs in each bounded request worker instead of blocking acceptance of all clients. Incomplete negotiations expire after five seconds. Up to 128 workers are admitted without waiting in the accept loop, and capacity is released on negotiation and worker-start failures. Certificates, TLS 1.2 minimum, authentication and enrollment are preserved. This fixes one stalled-client failure mode; the limit includes HTTP requests and long-polls and is not a general denial-of-service guarantee.

## Original activity viewer

The original automatic FBTERM read-only viewer validates the current same-user physical X11 session when opening. It uses that user's graphical bus and runtime even when the service originally started in text mode before login. Existing tmux sessions, jobs and shell environments are preserved. Explicit private virtual displays and headless execution retain their semantics. The activity viewer remains distinct from the interactive split work console; window detection retains its existing title-based limitation.

## Correct profile and version reporting

The CLI can remember an explicitly selected native local profile through a private reference containing only its path and identity. An explicit command-line state directory or environment setting retains priority. Validated upgrades can migrate one existing running profile; ambiguous or invalid preferences require an explicit selection instead of silently showing an empty default profile. No credentials are copied and no new pairing or connection is created by a status read.

Package upgrades invalidate only packaged Python bytecode before loading the new upgrade helper. The cleanup refuses unsafe package-directory traversal, does not follow links and never touches user profiles. This prevents reproducible equal-size/equal-timestamp files from loading a stale Python module version.

## Readiness with larger metrics

Authenticated polling evidence is scanned from the exact Prometheus metric family in bounded blocks, with line, total-byte and cooperative body-time limits. A metrics document larger than 256 KiB can therefore verify a live tunnel without retaining its whole body. Loopback-only transport, identity, HTTP status, metadata and timestamp freshness checks remain. The cooperative body budget is not a strict total HTTP deadline.

## Delivery and acceptance

The three packages share one matching corresponding-source archive and reviewed cp312/cp313 vendor inputs. Historical packages, signatures and dependency-source archives retain their original identities. GPL-3.0-only and third-party notices remain unchanged. Artifact hashes, signatures, upgrade/reboot acceptance and public availability are recorded separately; these notes do not substitute for those checks. Windows remains a separate private preview and is outside these Linux DEB packages.

Programming: ChatGPT. Original concept and project coordination: Gerardo Betancourt.
