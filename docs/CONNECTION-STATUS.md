# Connection setup, status and private logs

## Connect in three steps

Run as the Unix user who will own this FrameBridge profile:

```sh
fb connect chatgpt
```

For a first unmanaged connection, the text wizard asks for these fields in order:

| Step | Input | Meaning |
| --- | --- | --- |
| 1 | ChatGPT account/email label | A local label for the intended account. It does not sign in or authenticate that account. |
| 2 | Tunnel ID, entered at a hidden prompt | The raw ID of the tunnel selected in your own ChatGPT account. The normal wizard accepts IDs only. |
| 3 | MCP server access key | Entered at `MCP server access key (hidden):`. This is the connection's access key, not an OpenAI API key. |

No earlier `fb setup`, `fb start` or separate automatic-startup command is required for a normal first standalone connection. If the profile does not exist, the successful setup creates one with local standalone defaults and automatic desktop detection. Existing central profiles use the same connection command. Nodes use their approved central connection and do not open a separate ChatGPT tunnel.

Sign in and complete consent through your own ChatGPT browser session. A local account label, configured service or online tunnel does not prove that the browser account has been linked. Finish the browser workflow and verify a small, safe real tool call.

## Setup progress and the result

The same workflow configures the profile's Linux services, encrypts the saved access key and enables automatic startup and reconnection. Administrator authentication takes place in the normal terminal when required. There is no separate automatic-startup opt-in in this normal workflow.

The command shows concise progress, checks current readiness and returns the shell prompt. It waits for up to 95 seconds for startup verification. `[OK] Connected.` is reported only for a verified current connection. A failure reports an error; if verification remains pending at the limit, the command reports the pending/retrying state and returns exit status 1 while the service continues in the background. Inspect current state with:

```sh
fb connection status
```

On a valid managed profile, repeating `fb connect chatgpt` checks or resumes the saved encrypted settings without asking for those fields again. A damaged or unowned managed setup produces an error, not a connected result.

After setup finishes, the terminal can be closed. The service starts immediately; a routine reboot is not part of this workflow. Later Linux boots do not repeat the connection prompts. Temporary failures use retry/backoff. An expired or revoked key requires replacement; retrying cannot make it valid.

Monitoring can take a polling interval to detect a loss. A running process or an earlier success message does not guarantee current availability. Configuration success, current local/tunnel readiness and a successful real ChatGPT tool call are separate results.

## Automatic tool selection

The installer and control console remain text interfaces in a Linux TTY or a terminal emulator. FrameBridge queries Linux for the available environment automatically.

| Available environment | Tools |
| --- | --- |
| Text console | FBEXEC, persistent FBTERM and CUA-TTY. |
| Usable physical X11 desktop | The text tools plus graphical CUA on that actual desktop. |
| Explicit private-desktop mode | The text tools plus CUA on a separate virtual X11 desktop, reported as `virtual-x11` with `physical=false`. |

A graphical CUA request re-checks a physical X11 desktop that becomes available later, without restarting persistent FBTERM sessions. Checks around the operation reject a desktop change during it. The default adaptive mode does not create a private desktop or enable operating-system autologin.

## Stop, replace a key or remove startup

To stop only this profile's managed ChatGPT connection and retain its local tools:

```sh
fb disconnect chatgpt
```

To stop its connection and local runtime now:

```sh
fb stop
```

Both leave automatic startup enabled for the next boot. To stop it and disable later startup:

```sh
fb autostart disable
```

To change the saved account label, tunnel or access key, run `fb connect chatgpt --replace` and complete the three fields. The update is transactional and does not require a prior disable. See [automatic startup details](AUTOMATIC-STARTUP.md) for service scope, encrypted-credential limits, guarded reversal and package removal.

Normal package removal is available through `sudo apt remove framebridge-original-expanded`; no manual stop/disable sequence is required first. The package hook stops and disables only its recognized managed user services and preserves profile data, encrypted credentials and linger. See the technical startup page for the separate optional cleanup.

## Advanced manual controls

Use the same global `--state-dir` selection for all commands on a custom profile. `fb setup` remains available for central/node roles and nondefault settings. `fb autostart status` exposes the service, worker, runtime and connection details. The explicit `fb autostart enable` maintenance command remains available; it is not a prerequisite for the normal connection wizard.

An attached diagnostic connection is an advanced nonpersistent workflow:

```sh
fb connect chatgpt --foreground
```

It is mutually exclusive with `--replace`, requires an already-running local runtime and does not configure encrypted automatic startup. It asks for its connection inputs separately. Keep that terminal open; Ctrl+C or closing it ends that foreground connection. Stop an existing connection before starting a diagnostic attempt on the same profile.

The normal tunnel prompt hides input and accepts only a raw ID. If a complete command is pasted accidentally, it is rejected without echoing it. The advanced foreground workflow retains support for a complete compatible tunnel-client command through its hidden prompt. That command is parsed as data and is never executed by a shell. FrameBridge uses its bundled client and rebuilds the MCP endpoint from the selected profile, so a pasted endpoint cannot redirect the connection to another server.

## Credential handling and private diagnostics

The normal wizard persists the access key only as a host-bound encrypted systemd credential. Its worker decrypts through pipes in process memory. The advanced foreground workflow keeps its key in process memory without creating a saved credential. In both cases the pinned tunnel client consumes the key through its own temporary process environment. Keys are not put in command arguments, plaintext profile files or the progress display. The complete pasted command is not saved or echoed. Public tunnel/account-label metadata may be saved.

Technical startup messages and client diagnostics go to the selected profile's private connection log. The default is `~/.local/share/framebridge-expanded/logs/chatgpt/connection.log`; a custom profile uses its own `logs/chatgpt/connection.log`. Known key values and recognized credential patterns are redacted before writing. Directories use mode `0700`; log files use `0600`. Retention is bounded to the current file plus `connection.log.1` and `connection.log.2`, with a 2 MiB limit per file.

When reporting a problem, include the FrameBridge version, current state and short error message. Review excerpts before sharing; omit keys, account passwords, cookies and complete credential-bearing commands. Do not publish a live profile or VM image.

See the [Debian tutorial](../source/docs/DEBIAN-TUTORIAL.md) for package installation and advanced fleet setup.
