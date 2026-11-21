# Linux amd64 package with a private CPython

The packaging option `--bundled-runtime` builds one candidate DEB for Debian 13, Ubuntu 24.04 and Linux Mint 22.x. It uses the reviewed cp313 vendor tree. The CLI, adapter and MCP services share the private interpreter under `/usr/lib/framebridge-expanded/python/`. The system Python still runs the desktop tray and administrative maintenance hooks. APT resolves the existing desktop and system dependencies.

## Build from the repository root

Use an amd64 Debian-family build machine with Python 3.12 or 3.13, dpkg-deb with zstd support, GNU tar, zstd and the repository's existing build prerequisites. The runtime inputs come from [Astral python-build-standalone, release 20261110](https://github.com/astral-sh/python-build-standalone/releases/tag/20261110).

```bash
release=https://github.com/astral-sh/python-build-standalone/releases/download/20261110
base=cpython-3.13.18%2B20261110-x86_64-unknown-linux-gnu
curl --fail --location --output runtime.tar.gz "$release/$base-install_only.tar.gz" &&
curl --fail --location --output full.tar.zst "$release/$base-pgo%2Blto-full.tar.zst" &&
python3 source/packaging/build_expanded.py --target debian13-amd64-py313 \
  --bundled-runtime runtime.tar.gz full.tar.zst
```

The builder checks SHA-256 for both archives, records their hashes and copies upstream component notices and PYTHON.json into the DEB. It writes the DEB, release profile and payload manifest under `source/release/linux-amd64-cp313/`. Build each ordinary distribution package with its existing target configuration.

## Acceptance

Test in a disposable root or VM. Install the stable DEB for that distribution, upgrade to the candidate, confirm the CLI and its runtime profile, then reinstall the stable version there and check dpkg's state. Exercise the packaged MCP with `source/qa/verify_managed_jobs.py` using an isolated network, home, runtime directory and tmux sockets. The release version of any private CPython security update must be tracked by the package maintainer.

Publication requires the corresponding sources, a review of upstream redistribution materials, package acceptance and the project's signed release process. The builder only supports this option in candidate mode.
