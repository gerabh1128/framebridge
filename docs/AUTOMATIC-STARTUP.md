# Automatic startup: service, credentials and recovery

The normal `fb connect chatgpt` wizard configures Linux startup and reconnection as part of the connection workflow. No separate `autostart enable` command or startup opt-in is needed. See [connection setup and status](CONNECTION-STATUS.md) for the three-field workflow. This page describes the underlying service, advanced maintenance and reversal.

## Requirements and setup

Run the wizard as the Unix user who owns the profile, not as root. Use the same `--state-dir` selection for every command on a custom profile. This implementation requires systemd 256 or later; Debian 13 provides a suitable version. A missing normal profile is created as standalone with automatic desktop detection when setup commits. An existing central profile can use the same connection wizard. Nodes use their central's tunnel rather than connecting ChatGPT separately.

A first unmanaged connection asks for the visible account/email label, the hidden tunnel input and the hidden MCP server access key, in that order. A valid managed profile can be checked or resumed with the same command without reentering the fields. The label is not browser authentication; the access key is not an OpenAI API key. Administrator authentication stays in the normal terminal. The saved access key is encrypted on this machine.

Setup enables the managed service immediately, then reports current connection readiness separately from configuration. No routine reboot is required. Inspect the detailed service result with:

```sh
fb autostart status
```

A successful configuration is not proof that ChatGPT has linked the intended account. Complete browser consent and verify a small, safe real tool call.

## Service and desktop behavior

FrameBridge installs one profile-specific systemd user service and enables the owner's user manager at boot using systemd linger. This permits startup without a monitor or graphical login. It does not enable operating-system autologin or turn a user service into an unrestricted root service.

The default adaptive desktop setting detects a usable physical X11 session. If none is available, the local runtime starts FBEXEC, FBTERM and CUA-TTY in text mode. Graphical CUA requests re-check the physical session when it becomes available later, without restarting persistent FBTERM sessions. The selected display is checked around each operation to reject a session change during that operation.

For a legacy profile that strictly requires a physical desktop (`headless=no`), managed connection setup saves the original configuration and changes that setting to `headless=auto`. This lets boot-time text tools start before graphical login. If the profile explicitly requests a private desktop (`headless=yes`), that remains separate from the physical console: the display is reported as `virtual-x11` with `physical=false`. Adaptive mode does not create a private virtual desktop automatically.

Network failures use retry/backoff. A revoked or expired connection key needs manual replacement. Explicit `framebridge disconnect chatgpt` and `fb stop` leave automatic startup enabled for the next boot; they do not mean it has been disabled permanently.

## Encrypted connection credential

The durable runtime-key copy is a host-bound encrypted systemd credential associated with this machine's host encryption key and the owner UID. The worker supplies the encrypted credential to `systemd-creds decrypt --user` through a pipe and receives the decrypted key through another pipe in process memory. It does not write a plaintext credential file. The pinned tunnel client consumes the key from its own temporary process environment.

This protects the saved profile copy from plaintext exposure. Root access or a complete machine image that includes the host encryption key can recover it. The encrypted credential is not a portable backup for another machine. Do not publish a live profile, credential file or machine image as a project source archive.

To change the saved account label, tunnel or access key, explicitly reopen the three-field wizard:

```sh
fb connect chatgpt --replace
```

The update is transactional and does not require disabling startup first. Enter the replacement only at `MCP server access key (hidden):`.

The advanced `fb autostart enable` command remains available for maintenance and node-local runtime startup. Its own confirmation and hidden-key prompts are separate from the normal connection wizard and are not additional normal setup steps.

## Disable, reverse or remove

Run as the profile owner:

```sh
fb autostart disable
```

Disable stops the managed service, connection and local runtime. It removes only the matching FrameBridge-owned unit and encrypted credential. A desktop setting changed by setup is restored only if the profile has not subsequently changed. Linger is restored only when FrameBridge originally enabled it and the recorded enabled-unit set still permits that reversal; otherwise the shared user-manager setting remains in place. If owned files have changed externally, removal is refused for review.

The package can be removed normally without first running manual FrameBridge stop/disable commands:

```sh
sudo apt remove framebridge-original-expanded
```

The package-removal hook stops and disables only recognized FrameBridge-managed user services belonging to this package. It preserves the user's profile, encrypted credential files and linger setting. It does not perform this cleanup during a package upgrade. Purging the package likewise does not erase those user-owned profile and credential files.

If you also want to remove the matching managed unit and encrypted credential, the explicit `fb autostart disable` command above remains available while FrameBridge is installed. Its guarded cleanup is optional, not a prerequisite for normal package removal. If a matching managed service cannot be stopped or disabled safely, package removal pauses for review rather than deleting unrelated user configuration.
