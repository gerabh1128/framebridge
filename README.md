# FrameBridge Linux APT mirror

> **Current signed portable channel:** [stable/main](README-stable.md) — one amd64 package for Debian 13, Ubuntu 24.04 and Linux Mint 22.x.

- [Official installation, verification and cold-maintenance guide](https://framebridge.betancourt.one/download/)
- [Current stable source definition](sources/framebridge-stable.sources)
- [Stable archive public key](debian/stable/framebridge-archive-keyring.gpg)
- [Current signed mirror maintenance](https://github.com/gerabh1128/framebridge/actions/workflows/refresh-stable-apt-mirror.yml)

Mirror URI: `https://raw.githubusercontent.com/gerabh1128/framebridge/apt-mirror/debian/stable/`

Verify the independently trusted archive fingerprint `69D0 E9D8 3699 F96A B986 B06F 3C24 F85D 892A 517E`. For existing installations, use the documented cold-maintenance procedure and review obsolete APT sources before enabling stable. The mirror checks signed metadata daily and reuses verified existing packages instead of transferring them from the origin again.

## Historical channels — not recommended for new installations

The [fbterm15 channel](README-fbterm15.md) and the following original distribution suites remain available for historical recovery. Their maintenance workflows are manual-only. They do not redirect to stable. Historical signed indexes may expire; do not disable signature or expiry verification.

| Historical distribution | Historical suite | Source definition |
| --- | --- | --- |
| Debian 13 amd64 | `trixie` | [framebridge-trixie.sources](sources/framebridge-trixie.sources) |
| Ubuntu 24.04 amd64 | `noble` | [framebridge-noble.sources](sources/framebridge-noble.sources) |
| Linux Mint 22.x amd64 | `mint22` | [framebridge-mint22.sources](sources/framebridge-mint22.sources) |

Historical mirror URI: `https://raw.githubusercontent.com/gerabh1128/framebridge/apt-mirror/debian/`

Historical workflows: [original suites](https://github.com/gerabh1128/framebridge/actions/workflows/refresh-apt-mirror.yml) and [fbterm15](https://github.com/gerabh1128/framebridge/actions/workflows/refresh-apt15-mirror.yml). These remain separately signed snapshots; a mirror cannot extend signed validity periods. The old archive key and package bytes are preserved.

[Project source and license](https://github.com/gerabh1128/framebridge)
