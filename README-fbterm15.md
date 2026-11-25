# FrameBridge signed fbterm.15 APT mirror

This is the separately signed maintenance channel for Debian 13, Ubuntu 24.04 and Linux Mint 22.x (amd64). It does not replace the legacy APT channel or its trust key.

Mirror URI: `https://raw.githubusercontent.com/gerabh1128/framebridge/apt-mirror/debian/fbterm15/`

Signing fingerprint: `69D0E9D83699F96AB986B06F3C24F85D892A517E`

Verify the [public archive key](debian/fbterm15/framebridge-archive-keyring.asc) against the independently trusted fingerprint. Use the source definition for your distribution: [Debian](sources/framebridge15-trixie.sources), [Ubuntu](sources/framebridge15-noble.sources) or [Mint](sources/framebridge15-mint22.sources). Follow the [official cold-upgrade and signing-key instructions](https://framebridge.betancourt.one/download/). Existing installations are not migrated automatically.

The mirror refreshes signed indexes and verifies actual APT downloads independently of the legacy channel.
