# FrameBridge Original Expanded — R2 additions

Date: 2026-10-23
Package series: 0.3.0~expanded.20261023

> Historical R2 record. Community Preview 4 and later change the default adaptive desktop behavior: auto uses a verified physical X11 session when available and otherwise starts text-mode FBEXEC, FBTERM and CUA-TTY, without automatically creating Xvfb. Graphical CUA requests in adaptive mode re-check a usable physical session when one becomes available without restarting persistent FBTERM. Explicit headless=yes remains the private virtual-desktop option. The R2 fallback description below records its earlier behavior; use the current [Debian tutorial](DEBIAN-TUTORIAL.md) and [connection lifecycle](../../docs/CONNECTION-STATUS.md) for Preview 4.

This release integrates the accepted Original features in an isolated package tree. It does not install GPU drivers, kernels, a complete Cinnamon desktop, phone pairing, an Owner Device agent, or a custom privileged authentication service. Product commands and documentation are in English.

## Open the official console

After setup, run this as the normal logged-in user:

```sh
fb console
```

The menu entry is **FrameBridge Console**. With an active verified local X11 session, it opens the graphical terminal. From an authenticated text login, it attaches in the current terminal. Both access the same profile-scoped Bash and FBTERM console. Bash is on the left on a wide screen and above the monitor on a narrow screen. Closing a graphical client detaches it; it does not intentionally destroy the persistent session.

```sh
fb console --mode tty
fb console --status
```

Ctrl+B, then o changes panes. Ctrl+B, then z zooms or restores a pane. A remote interactive attachment requires an explicitly configured SSH destination; the package never guesses an SSH account or installs an SSH server. Broker-routed remote EXEC, CUA and managed terminal operations remain separate supported interfaces.

## Optional console after text login

Opt in explicitly, as the normal user:

```sh
fb console --enable-login
fb console --login-status
```

The hook runs only after an authenticated local tty1 login, outside SSH and tmux. It never enables automatic login. The selected existing Bash login file is preserved with an exact backup. Graphical logins and other VTs retain their ordinary startup behavior.

To restore the saved login file:

```sh
fb console --disable-login
```

Restoration refuses unknown later edits rather than overwriting them. Review those edits before attempting manual recovery.

## Managed CUA-TTY sudo capability

Start the profile and open a managed Bash console:

```sh
fb start
fb tty open --program bash --rows 30 --cols 100
```

Replace SESSION_ID below with the returned managed console ID. Replace worker-1 with the enrolled node alias when controlling a different node:

```sh
fb tty sudo status SESSION_ID --node LOCAL
fb tty sudo on SESSION_ID --node LOCAL
fb tty sudo off SESSION_ID --node LOCAL
fb tty sudo status SESSION_ID --node worker-1
```

The default managed console has NoNewPrivs=1. Enabling the capability creates a controlled replacement Bash with NoNewPrivs=0 through the normal user's systemd manager. The controller verifies the actual process and server flags. It retains the logical console ID, working directory and geometry. Shell-local variables and functions do not survive a Bash replacement. Busy, elevated, stopped or detached jobs prevent an unsafe transition.

This is permission to attempt ordinary sudo, not root authentication. The system's normal sudo policy and password prompt still apply. No password is embedded or handled by FrameBridge, and no NOPASSWD entry is created. A normal interactive Bash in the official console remains an ordinary user terminal; its Linux sudo behavior is not a managed CUA-TTY ID.

## Actual screen images and input

The physical screen interface captures complete real pixels:

```text
framebridge_cua(action="OBSERVE", authorized=true, capture_scope="node:worker-1")
framebridge_screen_observe(node="worker-1", authorized=true)
```

It supports the active owned Linux framebuffer TTY and a verified active X11 session. LightDM's empty TTY field and root session leader are handled by checking the active seat, numeric VT, session, user and private user authority context. Unsupported or unreadable screens fail explicitly; terminal text is never substituted for pixels.

The seven CUA-TTY MCP tools retain terminal-management operations, but observe returns the actual physical screen. TTY key and text actions require the capture_id and visible screen session ID returned by a real observation. A hidden managed terminal ID is not proof that it is visible. Switching VT, changing the visible pane, or reusing an action's capture invalidates the binding. Use the existing verified graphical CUA for X11 clicks and keyboard input.

The CLI terminal observation is different: `fb tty observe SESSION_ID` returns a **terminal transcript**, including text, cursor and process data. It is explicitly marked terminal_transcript, authentic_pixels=false and full_screen=false. It is not an image of the physical display.

## Physical and virtual graphical displays

In auto mode the runtime prefers a verified active physical X11 session. Its graphical adapter is bound to that display/session. The binding is checked before adapter dispatch, immediately before native X11 operations, and after execution. A changed foreground session invalidates the result. If input may already have been sent, the response states input_may_have_been_sent=true and do_not_replay=true; do not repeat that action automatically.

Historical R2 behavior: a private Xvfb display is labelled virtual-x11, with physical=false. Selecting headless yes requests such a virtual workspace explicitly. In that version, an auto fallback is also labelled virtual; it is never advertised as the active physical screen. Selecting headless no requires the physical session and refuses a virtual fallback even if the display becomes unavailable after initial validation. Community Preview 4 replaces the implicit auto fallback with text mode as described at the start of this document.

The node:ALIAS screen interface always resolves the actual active physical screen independently; it does not silently capture the private virtual workspace. Keep this distinction when reporting screenshots or test results.

## Network diagnosis

```sh
fb doctor
```

The report distinguishes kernel link/address/default-route data from the manager that owns the interface. A working ifupdown Ethernet connection can be unmanaged in NetworkManager; a Cinnamon applet based on NetworkManager may then show disconnected. A default route alone is not proof of Internet or DNS access. The diagnostic does not change interfaces, managers, routes, DNS, firewall or SSH.

## Release acceptance

Source and component checks do not certify a fresh installation. The release must also pass clean Debian APT/dpkg installation, actual boot, an authenticated console, physical GUI capture and input, ordinary password-authenticated sudo, and a separate central/node VM enrollment and routing check. The manual ChatGPT connection requires the user's own account consent and secret entry in the real terminal, never in chat.

The release report and external evidence identify which gates were actually completed. Do not describe a private Xvfb fixture or same-host profiles as an installed physical LightDM session or separate machines.
