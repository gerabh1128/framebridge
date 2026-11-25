# FrameBridge 0.3.9+fbterm.18 portable corresponding source

One amd64 binary with a pinned private CPython runtime for Debian 13, Ubuntu 24.04 and Mint 22.x. Source assembly alone does not certify installation, signature or publication.

Retain the separate runtime source bundle, the two pinned upstream runtime binary inputs, the historical dependency source assets and this archive's matching manifest. See source/docs/APT-BOOTSTRAP-CANDIDATE.md and source/packaging/portable-release-inputs.json.

Build offline with:

    python3 source/packaging/build_expanded.py --target debian13-amd64-py313 --public-release --source-manifest ../SOURCE-ARCHIVE-MANIFEST.json --privacy-policy /path/outside/repository/private-identifiers.json --bundled-runtime /path/install.tar.gz /path/full.tar.zst --apt-bootstrap-mode stable --apt-bootstrap-key source/packaging/portable-archive-keyring.gpg --runtime-source-bundle /path/FrameBridge-CPython-3.13.18-20261110-runtime-sources.tar

The builder never signs, publishes or starts services. Original FrameBridge code is GPLv3-only. Third-party license notices and source availability are retained. See LICENSE and LICENSING.md.
