# FrameBridge 0.3.9+fbterm.15 — signed Linux maintenance release

Published 24 November 2026.

[Official downloads](https://framebridge.betancourt.one/download/) · [Release catalog](https://framebridge.betancourt.one/assets/release.json)

## Exact signed packages

| Profile | Package | SHA-256 |
| --- | --- | --- |
| Debian 13 (amd64) | [Download](https://framebridge.betancourt.one/downloads/FrameBridge-0.3.9+fbterm.15-debian13-amd64-py313.deb) · [Signature](https://framebridge.betancourt.one/downloads/FrameBridge-0.3.9+fbterm.15-debian13-amd64-py313.deb.asc) | `55e14abd3cca623f6523c5a67bd41f2fceb05ac3b54828352493c6b17f7fa109` |
| Ubuntu 24.04 (amd64) | [Download](https://framebridge.betancourt.one/downloads/FrameBridge-0.3.9+fbterm.15-ubuntu24.04-amd64-py312.deb) · [Signature](https://framebridge.betancourt.one/downloads/FrameBridge-0.3.9+fbterm.15-ubuntu24.04-amd64-py312.deb.asc) | `8e2c2528d539eb38cd808a65fd3a7289c54d7a9243945bd440adee770fc13039` |
| Linux Mint 22.x (amd64) | [Download](https://framebridge.betancourt.one/downloads/FrameBridge-0.3.9+fbterm.15-mint22-amd64-py312.deb) · [Signature](https://framebridge.betancourt.one/downloads/FrameBridge-0.3.9+fbterm.15-mint22-amd64-py312.deb.asc) | `ab385a6034e343e5978167279b9cfb78d4f98a8aa7b992bd55455b1f0d674314` |

[Corresponding source](https://framebridge.betancourt.one/downloads/FrameBridge-0.3.9+fbterm.15-source.tar.gz) · [Source signature](https://framebridge.betancourt.one/downloads/FrameBridge-0.3.9+fbterm.15-source.tar.gz.asc) · [Source manifest](https://framebridge.betancourt.one/downloads/FrameBridge-0.3.9+fbterm.15-source-manifest.json)

[SHA256SUMS](https://framebridge.betancourt.one/downloads/fbterm15/SHA256SUMS) · [Checksum signature](https://framebridge.betancourt.one/downloads/fbterm15/SHA256SUMS.asc) · [Release public key](https://framebridge.betancourt.one/debian/fbterm15/framebridge-archive-keyring.asc)

Dependency corresponding-source archives and signatures are available through the official download catalog.

## Source identity

- Frozen source commit: `424f7b600d5ad855c78524842780019d7eee1559`.
- Frozen source tree: `b1b7d12ea8dbdbf1dc49fac6c0a69a7f02266f41`.
- Source archive SHA-256: `b00ec2b7f05a3c365b8e35fa8fbeaa85054a1797169c13954a3dd8da052693a9`.
- Signing fingerprint: `69D0E9D83699F96AB986B06F3C24F85D892A517E`.

The frozen source commit is identical on GitHub and Gitea. Later documentation and website deployment changes do not redefine the signed source archive.

## Signed APT and upgrade safety

The new signed channel is [debian/fbterm15/](https://framebridge.betancourt.one/debian/fbterm15/). Use trixie for Debian 13, noble for Ubuntu 24.04 and mint22 for Mint 22.x. The earlier APT channel and its signing key remain unchanged; clients do not switch automatically. Follow the new key-verification and installation instructions on the official download page.

Before upgrading an existing installation, back up affected profiles, drain jobs, close the tray and stop the applicable FrameBridge runtime from an independent terminal. See [cold package maintenance](PACKAGE-MAINTENANCE.md).

## Verified scope

- Exact signed package and corresponding-source SHA-256 checks, including public HTTPS read-back: PASS.
- Signed public APT metadata and candidate selection for all three suites: PASS.
- Native installation and FrameBridge execution on Debian 13, Ubuntu 24.04 and Mint 22.x: PASS.
- Mint native graphical lifecycle validation: PASS. Debian and Ubuntu full graphical regression remains pending.
- Reviewed repository tree, exact signed packages/source and rendered site privacy scans: PASS.

This record is not a complete hardware, desktop or reboot compatibility certification. The release addresses FBTERM header ownership, preserves the approved classic viewer and retains the explicit package-maintenance and job-history protections.
