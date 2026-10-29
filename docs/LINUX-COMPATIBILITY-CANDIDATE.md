# Debian, Ubuntu and Linux Mint compatibility candidate

This development candidate is based on signed 0.3.1 source commit
b23819fff1500cd43bb7c44dc9d866e3fb701672.
Candidate version: 0.3.2~compat.20261027.1.

## Scope

Only Debian, Ubuntu and Linux Mint are in this candidate's support plan.
Initial architecture: amd64/x86_64.

| Profile | Native interpreter ABI |
| --- | --- |
| Debian 13 | cp313 |
| Ubuntu 24.04 | cp312 |
| Mint 22.x (Ubuntu Noble base) | cp312 |
| Ubuntu 26.04 | cp314 |
| LMDE 7 (Debian Trixie base) | cp313 |

A build profile is not installation evidence. Results must distinguish static
and unit checks, interpreter imports, disposable OS installation, and a pilot
on a real machine. Mint and LMDE are separate targets.

## Changes and boundaries

Platform preflight uses explicit profiles and an installed manifest tied to the
Python ABI and native modules. Build checks reject wrong native ABI, CPU,
dependency versions and unsupported libc requirements.

Python 3.12/3.14 vendors preserve the pinned dependency versions. Only three
CPython-specific native distributions are replaced with matching official wheels.
Record their URLs and SHA-256 digests. The abi3 cryptography build is retained
when its requirements are satisfied.

The system Python stays intact. Standalone interpreters in the isolated build
workspace are build/test tools, not payload runtimes.

Protected user-service credentials retain the systemd >=256 route. Systemd 255
support requires a separately installed, narrowly scoped helper and explicit
owner provisioning. Missing backend must fail closed. Read its documentation
and tests before installing or provisioning it.

Physical desktop capture/control remains X11 based. A functioning CLI, virtual
display or Xwayland window does not establish physical Wayland desktop support.
ARM, Alpine and other distribution families are outside this candidate scope.

## Build and evidence

From the source root, use an explicit target and ABI-matched vendor:

    python3 source/packaging/build_expanded.py --target mint22-amd64-py312 --vendor-dir /absolute/path/to/vendor-cp312 --version '0.3.2~compat.20261027.1'

Use the Debian/Ubuntu/LMDE target IDs documented by the builder. Output files and
manifests are separated by target. Candidate files need their own checksums and
must not reuse the signed 0.3.1 download path or signatures.

Native wheel provenance and matching source must accompany the candidate.
Existing license/source notices refer to the same upstream versions; preserve
the new wheel identities with the new build evidence.

## Pilot on existing machines

Order: disposable Debian and Ubuntu 24.04 VMs, GERAMEG (Debian 13), then ASUS
(Mint 22.3). Exclude GERANAS from this initial pilot: it is the central router
and its 160 GB RAID contains a degraded member.

The original services observed today run outside this published package.
Publishing the 0.3.1 website did not install its .deb on these hosts. GERAMEG
also has older Alpha packages. Check package ownership, file paths, maintainer
scripts, dependency transactions, ports and state before installation.

A production pilot needs separate owner authorization after concrete results:

- Review exact candidate checksum and dependency simulation.
- Confirm a recoverable backup for every existing path it will change.
- Retain an independent management route and the original service.
- Install before provisioning a new connection or replacing an existing role.
- Explain and authorize permission, udev, sudoers or service changes.
- Prove rollback/removal in a disposable machine, including credential cleanup
  and preservation of unrelated owner files.
