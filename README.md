# FrameBridge Linux APT mirror

> Current signed maintenance channel: [FrameBridge 0.3.9+fbterm.15](README-fbterm15.md). The instructions below describe the legacy channel.

This branch serves the public FrameBridge Debian repository over HTTPS. It mirrors signed metadata and packages from the official origin. APT verifies the release using the FrameBridge archive key. The package name is `framebridge-original-expanded`.

| Distribution | Suite | Source definition |
| --- | --- | --- |
| Debian 13 amd64 | `trixie` | [framebridge-trixie.sources](sources/framebridge-trixie.sources) |
| Ubuntu 24.04 amd64 | `noble` | [framebridge-noble.sources](sources/framebridge-noble.sources) |
| Linux Mint 22.x amd64 | `mint22` | [framebridge-mint22.sources](sources/framebridge-mint22.sources) |

Mirror URI: `https://raw.githubusercontent.com/gerabh1128/framebridge/apt-mirror/debian/`

Use the definition for your own distribution. All three suites can carry the same package name and version with different Python and distribution dependencies. For an existing FrameBridge APT installation, use the existing trusted archive key and select the source definition corresponding to the installed distribution.

For a new installation, verify the archive key against the independently obtained FrameBridge fingerprint `2B1F 01BC 806C 0B33 CB6C EDDE 5285 9CEB C91B 8808`. Place the verified public key at `/etc/apt/keyrings/framebridge-archive-keyring.asc` with read access for APT, then enable the chosen source definition under `/etc/apt/sources.list.d/`. Run `sudo apt update`, inspect `apt-cache policy framebridge-original-expanded`, then install with `sudo apt install framebridge-original-expanded`.

[Official installation and downloads](https://framebridge.betancourt.one/download/) · [Project source and license](https://github.com/gerabh1128/framebridge) · [Archive public key](debian/framebridge-archive-keyring.asc)

## Refreshing the snapshot

[GitHub Actions](https://github.com/gerabh1128/framebridge/actions/workflows/refresh-apt-mirror.yml) checks the official origin daily at 08:27 UTC and on manual dispatch. It checks the signatures and downloads each suite through APT before publishing a changed snapshot. Each run records its result. The origin maintainer renews the signed indexes before their expiration date.

To refresh manually, use a clean checkout of this branch and run `python3 tools/refresh.py` with Python 3 and GnuPG. It verifies the origin signing key, both release signatures, the validity period, the package lists and every referenced package. It keeps immutable pool and by-hash paths for clients holding older indexes. Review the resulting diff, commit it and fast-forward this branch. The branch update makes the complete snapshot available under the same URI. The origin controls release signing and expiration. A mirror cannot extend the signed validity period.
