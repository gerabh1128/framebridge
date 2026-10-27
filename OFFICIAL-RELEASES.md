# Official releases and independent variants

FrameBridge is free software under the applicable licenses. Everyone retains the right to make private changes, publish forks and redistribute lawful modified copies. Contributing changes back to this Git is welcome, not an additional license condition.

## What official means

An official FrameBridge release is built from an identified source revision accepted by the project maintainers. It has a version, source archive, installer, checksums and release signatures. The Debian APT repository publishes signed repository metadata. Official distribution starts at https://betancourt.one/framebridge/. Each release record identifies its signed installer, corresponding-source archive and verification material. A source mirror, when marked published in that record, is available read-only at https://betancourt.one/framebridge/git/framebridge.git. Source branch and tag identities are bound to the signed source archive by the release record; this policy requires release artifact signatures, not an individually signed Git tag.

Maintainers control write access, reviewed branches, release tags and signing operations. Public visitors and agents do not receive release-signing or direct protected-branch write access. External contributors propose changes through review.

Independent builds must not be represented as maintainer-approved or cryptographically signed official releases when they are not. Keep original attribution and clearly identify modifications as required by the applicable license.

## What signing protects

A valid signature authenticates the signed release or repository metadata against the independently trusted public key. SHA-256 detects byte changes; an unsigned checksum fetched through the same compromised channel is not sufficient proof of origin. A signed repository connects its package indexes to the package hashes.

Signing does not encrypt the software, prohibit source modification, or guarantee that a privileged administrator has not modified an installed machine. Keep signing credentials outside source control and web content; publish only the verification material.

## Ordinary agent use

An agent using FrameBridge should treat installed program files as the application, use its documented CLI and MCP tools, and keep task data separate. Routine task execution should not rewrite the application or create an unofficial replacement binary. When the owner explicitly requests development, use a separate checkout and present reviewable changes.

This is operational guidance for reliable use, not a restriction on a recipient's GPL rights or an additional owner-device/phone pairing mechanism.
