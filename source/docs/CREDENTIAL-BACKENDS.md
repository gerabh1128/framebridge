# Protected connection credentials

The existing systemd 256+ backend uses owner-scoped encrypted credentials and
`systemd-creds decrypt --user`. Existing installation records without a backend
field retain that behavior. The systemd 255 backend preserves the same user
service and uses a separately installed, privileged credential helper. Node
startup without a ChatGPT connection does not need a runtime connection key.
New or rotated keys on both backends use an administrative, secret-free sudo
policy bootstrap so that key input cannot enter sudo I/O logs.

## Installation and authorization

The package supplies these root-owned files:

* `/usr/lib/framebridge-expanded/bin/framebridge-credential-helper`
* `/usr/lib/framebridge-expanded/expanded/framebridge_expanded/credential_helper.py`

The launcher executes the fixed system interpreter with `-I -S`; the helper uses
only the standard library and absolute systemd-creds/visudo paths. Merely
installing the package does not enable a connection or install a sudoers grant.

Connection setup first requires ordinary administrative sudo authorization.
The helper's `authorize` operation accepts no key and installs only literal,
owner/profile-specific commands tagged `NOLOG_INPUT`, `NOLOG_OUTPUT` and
`NOSETENV`. The frontend then queries the effective policy for the exact command
using command-specific `sudo -n -ll` in the C locale. It requires one effective
matching root rule, the exact command/arguments and explicit disabled I/O logging
and environment overrides before sending any key. Helper input is limited to
16384 bytes plus EOF with a five-second absolute deadline, so an open input pipe
cannot hold the shared registry lock indefinitely. Generic Defaults and absence
of logging flags are not accepted as evidence. Unknown formats fail closed.

On systemd 255, setup writes host-encrypted ciphertext and principal metadata under
`/var/lib/framebridge-expanded/credentials`, owned by root with private access.
It also installs one validated file in `/etc/sudoers.d` per credential version.
That file permits exactly the recorded owner's literal helper `decrypt` command
and fixed profile/cipher digest arguments, with `NOSETENV` and disabled I/O
logging. The same effective-policy check precedes runtime sudo decryption.
The one-use encryption bootstrap is consumed before the helper reads key input.
On systemd 256+, the bootstrap permits only the existing native encrypt command
with UID derived from the sudo caller and the fixed credential name; the
frontend revokes that temporary permission in `finally`. Owner-scoped ciphertext
and unprivileged `systemd-creds decrypt --user` remain unchanged. It does not permit
arbitrary systemd-creds commands, paths, shell execution, account selection,
provisioning, revocation or unrestricted root execution without authentication.
This narrow permission is necessary for unattended reconnection after reboot.

The owner must review this permission change before production setup is
activated. Building the package and running synthetic unit tests do not activate
the helper, create a host encryption key, alter sudoers or enable a service.

## Scope and recovery

The helper derives the caller from sudo's UID, username and primary group; it
does not accept a caller-selected UID. Its records bind the UID, username, home
identity, profile digest, ciphertext digest and randomized cryptographic name.
Only fixed root-owned paths are opened. Symlinks, unexpected hardlinks, writable
parents, broad metadata permissions and unrecognized arguments are rejected.

Provisioning keeps the old credential usable until the replacement connection
commits. Retired versions remain listed until revocation succeeds. Directory
fsync orders durable ciphertext registration before the decrypt grant, and
grant revocation before ciphertext removal. Failed output delivery rolls back a
new grant. After SIGKILL or power loss, administrative `prune` reconciles orphan
versions even if the owner never received their ciphertext digest. New setup
preserves known current versions while pruning orphans; explicit disable prunes
all versions for its owner/profile, including when no manifest was committed.
This reconciliation also runs after an upgrade to the systemd 256+ backend.
Uninstall uses administrative `purge-all` after the user workers have stopped.
Recognized root-owned staging files from interrupted publication are recovered.
Prune and purge also remove authorization bootstrap files left by interruption.

Administrative interface (run the installed launcher, never a user checkout):

* `protect PROFILE_SHA256`: read a bounded printable synthetic/runtime key from
  stdin and return only encrypted ciphertext on stdout. Requires and consumes
  its matching host authorization before reading input.
* `authorize PROFILE_SHA256 host|user`: secret-free administrative bootstrap for
  literal NOLOG encryption and cleanup commands, bound to the actual caller UID.
* `unauthorize PROFILE_SHA256 host|user`: remove a bootstrap authorization.
* `decrypt PROFILE_SHA256 CIPHER_SHA256`: return the registered owner's key on a
  pipe; this is the only operation granted passwordless sudo.
* `revoke PROFILE_SHA256 CIPHER_SHA256`: idempotently revoke one owner version.
* `prune PROFILE_SHA256 KEEP_DIGESTS`: remove other versions for this owner and
  profile; `KEEP_DIGESTS` is a comma-separated digest allowlist, or `-` for none.
* `purge-all`: root administrative removal of all helper-owned records/grants.

## Security limits

No plaintext key is intentionally stored in an environment variable, command
argument, log or disk file. Plaintext exists in process and pipe memory while
used; this is not a guarantee against memory inspection, swap, a compromised
owner session or root. Both credential backends trust the owner Unix principal.

UID reuse with another username or home is rejected. Recreating exactly the
same UID, username and home can recreate the same Unix principal; Linux does
not supply an account-generation identity here. Administrators must revoke or
purge the grants before deleting/recycling an account, and before changing its
identity/home in a way that would prevent ordinary owner cleanup. A randomized
credential name is not proof of an account's generation.

The effective-policy parser is validated against sudo 1.9's command-specific
verbose listing. A different sudo implementation or ambiguous policy output
fails before key input and needs its own compatibility work and evidence.

The helper backend is not a Wayland capture/input backend and does not remove
Python ABI, architecture, libc, PAM, user-systemd, cgroup, sudo or X11 capability
requirements. Synthetic tests validate interfaces and failure handling; a
support claim additionally requires clean-system installation, real systemd
credential operations, boot/lifecycle and feature-specific acceptance tests.
