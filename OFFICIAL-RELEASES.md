# Official releases and independent variants

FrameBridge is free software under the applicable licenses. Everyone retains the right to make private changes, publish forks and redistribute lawful modified copies. Contributing changes back to this Git is welcome, not an additional license condition.

## What official means

An official FrameBridge release is built from an identified source revision accepted by the project maintainers. It has a version, source archive, installer, checksums and release signatures. The Debian APT repository publishes signed repository metadata. Official distribution starts at https://framebridge.betancourt.one/. The existing GERANAS Gitea repository is https://framebridge.betancourt.one/git/gerardo/framebridge. Each release record identifies the exact source revision, corresponding-source archive, three distribution package profiles, checksums and signatures. The signed APT repository uses independent trixie, noble and mint22 suites for the tested Debian 13, Ubuntu 24.04 and Linux Mint 22.x amd64 profiles. Release artifact signatures identify official downloads; individually signed Git tags are not required by this policy.

Maintainers control write access, reviewed branches, release tags and signing operations. Public visitors and agents do not receive release-signing or direct protected-branch write access. External contributors propose changes through review.

Independent builds must not be represented as maintainer-approved or cryptographically signed official releases when they are not. Keep original attribution and clearly identify modifications as required by the applicable license.

## What signing protects

A valid signature authenticates the signed release or repository metadata against the independently trusted public key. SHA-256 detects byte changes; an unsigned checksum fetched through the same compromised channel is not sufficient proof of origin. A signed repository connects its package indexes to the package hashes.

Signing does not encrypt the software, prohibit source modification, or guarantee that a privileged administrator has not modified an installed machine. Keep signing credentials outside source control and web content; publish only the verification material.

## Ordinary agent use

An agent using FrameBridge should treat installed program files as the application, use its documented CLI and MCP tools, and keep task data separate. Routine task execution should not rewrite the application or create an unofficial replacement binary. When the owner explicitly requests development, use a separate checkout and present reviewable changes.

This is operational guidance for reliable use, not a restriction on a recipient's GPL rights or an additional owner-device/phone pairing mechanism.

See the [reviewed source and release workflow](docs/RELEASE-WORKFLOW.md) and the [0.3.5 source-to-artifact record](docs/RELEASE-SOURCE-0.3.5.md). A later documentation commit does not redefine the frozen source used for an existing signed package.
