# Signed distribution repositories

The canonical source is the existing GERANAS Gitea repository
`gerardo/framebridge`. Build all three reviewed DEBs from its release commit;
do not edit installed files to produce an update.

One HTTPS repository at `https://framebridge.betancourt.one/debian/` contains
three independent suites:

| Suite | Tested distribution profile |
| --- | --- |
| trixie | Debian 13, amd64, Python 3.13 |
| noble | Ubuntu 24.04, amd64, Python 3.12 |
| mint22 | Linux Mint 22.x, amd64, Python 3.12 |

Select exactly the suite matching the computer. These are three builds of one
binary package, `framebridge-original-expanded`, with unique pool filenames.
This table does not certify untested releases or architectures.

## Prepare and review

Run `python3 tools/build_apt_repository.py prepare --help` in the source
repository. Supply the exact three version-matched DEBs, the existing official
public key and a new output directory. The command copies the artifacts, builds
indexes and by-hash paths, and prints the immutable input manifest SHA-256.
It does not open a private keyring, sign, publish or install anything.

Review the manifest, package hashes and release identity before signing.
The public key must contain exactly one primary release key with fingerprint
`2B1F01BC806C0B33CB6CEDDE52859CEBC91B8808`. Official subkeys are allowed.

## Sign in the owner's normal terminal

Run `python3 tools/build_apt_repository.py sign --help`. Pass the reviewed
output directory and exact manifest SHA-256. The signer requires a non-root
interactive terminal and an owner-private existing signing home. Set
`GPG_TTY=$(tty)` in that normal terminal if the pinentry implementation needs it.
The owner's passphrase is entered only into normal pinentry; it is never an
argument, environment variable or chat message.

The signer writes `Release.gpg` and `InRelease` for each suite, verifies the
signatures against the public key and exact reviewed Release plaintext, then
clears the dedicated signing agent. Existing signatures or partial results
cause refusal; inspect a partial transaction instead of blindly rerunning it.

Run `verify` with the same pinned manifest hash after signing and again after
transport. Metadata expires after 30 days; publish a freshly signed Release
before expiry, without altering historical signed artifacts.

## Publish through GERANAS

Use the existing GERANAS web origin and the CLOUD HTTPS reverse proxy. Do not
create another Git backend or expose MCP/auth/broker services. Back up the
current public generation before activation. Preserve the existing signed
0.3.1 downloads and old repository metadata in an archive path.

Publish only reviewed manifest files, the six verified signature files and the
signing receipt. Public directories must be `0755` and files `0644`; a private
staging umask must not leak into the served generation. Never publish the
signing home, private receipts, account configuration or operational worktree.
Activate a complete verified generation, preserving the web unit's existing
read-only bind mounts, then run isolated APT update and exact-version download
for all three suites through the final HTTPS authority.

## Configure a client

Install the verified official public key as
`/etc/apt/keyrings/framebridge-archive-keyring.asc` with mode `0644`.
Its fingerprint must match the value above. Add a Deb822 source file:

```text
Types: deb
URIs: https://framebridge.betancourt.one/debian/
Suites: trixie
Components: main
Architectures: amd64
Signed-By: /etc/apt/keyrings/framebridge-archive-keyring.asc
```

Replace `trixie` with `noble` or `mint22` for those tested profiles. Then use
normal APT commands:

```sh
sudo apt update
apt-cache policy framebridge-original-expanded
sudo apt install --only-upgrade framebridge-original-expanded
```

Do not use trusted=yes or disable signature/validity checks. APT update/download
verification does not itself prove runtime restart or reboot persistence.
Confirm the actual installed version and ready connection after each upgrade.

## Tool checks

`python3 tools/test_build_apt_repository.py` creates disposable packages and a
disposable signing identity in a temporary directory. It tests actual isolated
APT acceptance for each suite and rejects altered packages/indexes/signatures,
unexpected versions, path traversal and bundled extra primary trust keys.
It never uses the official private signing key or changes system APT sources.
