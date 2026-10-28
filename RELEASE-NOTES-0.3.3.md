# FrameBridge 0.3.3

FrameBridge administers Linux computers on a home network through a shared command-line interface for the human administrator and authorized agents. The language model remains hosted.

## Changes

- The desktop console reconnects to the existing official console window when the same tmux session contains other windows. It preserves the working shell and unrelated windows, and displays jobs from the selected profile. Recognized legacy desktop entries and console wrappers can be migrated to the packaged command with private backups.
- Forward package upgrades record active managed profiles before replacing files. A detached upgrade worker waits for DPKG to finish, then restarts only those profiles in independent user services. It preserves existing connection processes, profile settings, enrollment and boot policy, and records readiness separately from package installation. Stopped profiles remain stopped.
- The release provides separate amd64 builds and signed APT suites for Debian 13 (`trixie`), Ubuntu 24.04 (`noble`) and Linux Mint 22.x (`mint22`). Use the package for the distribution actually installed; keep its system Python.
- The public documentation uses the canonical website and Gitea repository under `framebridge.betancourt.one` and describes network administration as the primary workflow. Desktop tools remain auxiliary.

## Updating

After configuring the matching signed APT source, use the normal package manager to install or update `framebridge-original-expanded`. Inspect `fb status` after an upgrade. A successful APT command confirms package installation; the upgrade worker records runtime restart and readiness independently. An upgrade from a stopped profile does not connect it automatically.

## Compatibility

Physical desktop input requires a compatible active X11 session. Native Wayland control is not certified. The guarded x86-64 X11 text backend supports active keyboard group 0 and its documented modifier restrictions.

Legacy integrations can have a different default execution target from the current receiver. Preserve explicit target routing or use a compatibility adapter when migrating existing clients. Replacing an endpoint without checking its default target is not a compatibility guarantee.

## Validation

The release is being validated against the three distribution profiles. Build, signature, APT and live upgrade results are recorded in the accompanying release verification manifest; publication and installation must be verified against the actual signed artifacts.
