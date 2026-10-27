# FrameBridge Original Expanded — requirements and acceptance criteria

PROJECT=FRAMEBRIDGE_ORIGINAL_EXPANDED
STATUS=PRODUCT_REQUIREMENTS_AND_ACCEPTANCE_CRITERIA
DATE=2026-10-26
CANDIDATE=COMMUNITY_PREVIEW7

This document defines the requested product. An acceptance criterion is a requirement, not evidence that it already passes. Release claims must cite the final artifact and the corresponding test report.

## Product and supported installation

| ID | Requirement | Acceptance evidence required |
|---|---|---|
| PKG-01 | Deliver one amd64 Debian package for Debian 13 with Python 3.13. | Install the actual release package on a fresh Debian 13 amd64 environment and run its installed entry points. |
| PKG-02 | Include the original execution, CUA, fleet and terminal components needed by the supported roles. | A manifest maps packaged files to captured original sources or explicitly reviewed adaptations. |
| PKG-03 | Declare dependencies used by the actual execution path. | A clean installation resolves dependencies; doctor checks commands, Python imports and graphics requirements. |
| PKG-04 | Provide all help, prompts, instructions and release documentation in English. | Review the installed CLI, generated help and shipped documents. |
| PKG-05 | Preserve the existing production installations. | Tests use separate state, transport endpoints and terminal sockets; record the scope and cleanup performed. |

## First use and roles

| ID | Requirement | Acceptance evidence required |
|---|---|---|
| CLI-01 | Provide the framebridge command and the fb alias with an understandable first-use entry. | Exercise both installed entry points without arguments and inspect their help. |
| CLI-02 | Provide automatic standalone defaults for a normal first connection and separate guided setup for central/node roles and advanced settings. | Test first connection without a saved profile or prior setup/start, plus each advanced role's configuration and runtime behavior. |
| CLI-03 | Keep the normal connection wizard concise and expose the configured role, node name, ports and state location through status. | Status agrees with the selected profile and running processes; the first connection keeps the required three-field order. |
| CLI-04 | Keep setup, start, stop, status and doctor behavior explicit. | Test lifecycle, repeated invocation, occupied ports and process cleanup without touching production. |
| CLI-05 | Identify operations requiring administrator privileges and keep authentication in the normal terminal. | Fresh Debian instructions match package installation and managed-service setup; the connection wizard does not request administrator credentials in chat or conceal privileged setup. |

Standalone runs the local tools independently. Central manages the fleet-facing entry point and node admission. Node joins an explicitly selected central instance. These descriptions do not authorize a role merely because a host was discovered on the network.

## ChatGPT connection

| ID | Requirement | Acceptance evidence required |
|---|---|---|
| GPT-01 | The user signs in to their own ChatGPT account in the browser and completes the required consent manually. | Record an authorized browser connection and real tool calls; never treat a local label as authenticated identity. |
| GPT-02 | Accept only a raw tunnel ID in the normal hidden tunnel field; retain complete supported commands in the advanced hidden foreground workflow. | Test normal ID validation and safe rejection of accidentally pasted commands. Advanced parser tests cover quoting and invalid commands; a command is parsed as data and never run by a shell. |
| GPT-03 | Read the MCP server access key at a hidden terminal prompt. The normal connection workflow stores only a host-bound encrypted credential for managed startup; advanced foreground mode remains nonpersistent. | Verify no plaintext key in profile configuration, persistent files, shell history, logs, command arguments or release artifacts. Verify encrypted storage and decryption through protected process-memory pipes. |
| GPT-04 | Do not save or repeat the complete pasted tunnel command. | Inspect successful and failing CLI output and resulting state. Only necessary public tunnel metadata may be retained. |
| GPT-05 | Run the normal connection through managed services with concise progress, explicit readiness, status and disconnection; retain foreground diagnostics through an explicit option. | Verify prompt return, survival of terminal closure, concurrent invocation, startup failure, interruption, process identity and cleanup. Pending readiness must not be called connected. |
| GPT-06 | Report local readiness separately from a real ChatGPT end-to-end success. | Status and documentation identify which account/browser and tool-call checks were actually performed for this release. |
| GPT-07 | Configure Linux boot startup and reconnection as part of the normal standalone/central connection, without prior manual start, a separate startup opt-in or repeated boot-time credential prompts. | Verify immediate managed startup and terminal closure; reboot a Debian 13 VM and verify text-mode tools and tunnel before graphical login. Setup must not force a routine reboot. |
| GPT-08 | Use systemd encrypted credentials for the normal saved MCP access key, with normal sudo authentication when setup requires it. | Verify systemd 256 or later, protected encrypted storage, no plaintext persistent profile copy, and documented root/full-image recovery limits. |
| GPT-09 | Keep disconnect, stop and disable behavior distinct and reversible. | Disconnect retains the local runtime and next-boot enablement; stop ends the current instance; disable stops it and removes only matching owned configuration, reversing changed settings with guards. |
| GPT-10 | Provide explicit transactional replacement and normal package removal without mandatory manual stop/disable steps. | Verify connect chatgpt --replace preserves prior managed settings on failure. Verify actual package removal stops/disables only recognized package-managed user units, preserves profiles/encrypted credentials/linger, and does not run removal cleanup on upgrade. Optional guarded autostart disable remains separate. |
| GPT-11 | Ask for the visible account/email label first, hidden tunnel input second and hidden MCP server access key third on a first unmanaged connection. | Inspect prompt order and hidden input; distinguish local account labeling from browser login and the MCP access key from an OpenAI API key. |
| GPT-12 | Check or resume a valid managed profile using saved encrypted settings without reentering the fields; reserve changes for explicit --replace. | Test repeated connect on running and stopped managed profiles, and safe rejection of corrupt or unowned configuration without a false connected result. |

An email entered during setup is a local label. It is not proof of the browser account, tunnel ownership or node identity. No browser cookies, saved sessions or account passwords belong in this package or its reports.

## Original execution and CUA behavior

| ID | Requirement | Acceptance evidence required |
|---|---|---|
| EXEC-01 | Preserve original FBEXEC behavior and its job, exit-code, stdout, stderr and hash evidence. | Run real commands through the packaged route and verify their backend evidence. |
| EXEC-02 | Use the original execution path without importing the experimental job-approval queue. | Source and runtime review identify the handler actually reached by each public tool. |
| CUA-01 | Preserve original X11 CUA where the supported desktop or private display is available. | Run actual observation and interaction tests against the intended display. |
| TTY-01 | Provide access to the original GERAMEG CUA-TTY backend, including the supported real-terminal mode. | Verify text input, Enter, actual shell output and terminal cleanup through the original RPC. |
| TTY-02 | Distinguish an isolated demo from control of a real persistent Bash terminal. | Tests and help explicitly identify the active mode; demo acknowledgements alone do not certify a real shell command. |
| TTY-03 | Separate terminal state for independent instances. | Test distinct sockets and sessions; never copy live socket paths, session IDs or process IDs from another machine. |
| AUTH-01 | Do not require a phone or Owner Device for the requested local execution or CUA workflow. | Exercise the workflow without phone enrollment or an Owner Device gate. |

The original GERAMEG documentation first certified a demo and later certified a real terminal. The real-terminal A/B certificate is the relevant reference for TTY-01; the earlier demo result is not interchangeable with it.

## Fleet admission and authenticated transport

| ID | Requirement | Acceptance evidence required |
|---|---|---|
| FLEET-01 | Central and node roles use authenticated transport. | Successful authenticated node traffic and rejected unauthenticated traffic are demonstrated. |
| FLEET-02 | A node's identity and intended central instance are reviewed and approved explicitly before admission. | Record the exact identity presented, the pending request and the resulting approval; no silent acceptance on discovery. |
| FLEET-03 | Keep route selection explicit and reject an unknown target without redirecting to another host. | Test local and enrolled-node routes plus an unknown target. |
| FLEET-04 | Include the original broker/outbound-agent workflow required by the selected roles. | Demonstrate actual node registration, online status, remote FBEXEC and CUA routing through the central instance. |

Transport authentication and approved node identity remain required. Removing a phone requirement does not remove either condition. The normal connection workflow saves the MCP access key only as a host-bound encrypted systemd credential; advanced foreground diagnostics keep it in process memory without persistence. Plaintext credentials must not be persisted in profile files or distributed evidence. Generated node identity and transport-authentication material have a separate purpose and may require protected persistent profile state; such state must never be included in a redistributable package or evidence bundle.

## Graphics and headless operation

The installer and control console use a text interface in a Linux TTY or terminal emulator. Graphical CUA is a backend selected automatically from a usable supported desktop; its availability does not change the installer or console into a graphical interface.

The historical R1 FBTERM engine opens a GNOME terminal and uses wmctrl; evidence for that path must identify it. The current persistent-terminal workflow must also work in text mode without a graphical desktop. Graphical X11 CUA needs a verified display, the correct Xauthority and its command-line tools.

Community Preview 4 and later product decision: headless mode is auto by default. The runtime uses a verified usable physical X11 session when available; otherwise it starts in text mode, reporting display_context.kind=text. Text mode provides FBEXEC, FBTERM and CUA-TTY without a monitor, graphical login or forced virtual desktop. Graphical CUA requests in adaptive mode re-check for a usable physical session when one appears, without restarting persistent FBTERM sessions; per-operation screen checks reject a session change during input. Explicit headless=yes requests the separate private X11 desktop, reported as virtual-x11 and physical=false. Explicit headless=no requires the verified physical session; enabling automatic startup backs up and converts that physical-only setting to auto for boot operation.

The retained xvfb, openbox, dbus-x11, xauth, x11-xkb-utils and fonts-dejavu-core dependencies support explicit private graphical operation alongside the original execution and CUA dependencies. Graphical capability checks must distinguish optional graphics from text-mode readiness. Prove adaptive text startup, later verified physical CUA, explicit private mode and safe refusal against the exact final runtime in Debian QA. These are acceptance requirements, not passing test results. See the current [connection lifecycle](../../docs/CONNECTION-STATUS.md) and [Debian tutorial](DEBIAN-TUTORIAL.md).

Node operation must use the configured local user's identity and runtime directory rather than requiring a hard-coded UID such as 1000.

## Required release evidence

| Area | Current status | Evidence needed before a passing claim |
|---|---|---|
| Final package installation on fresh Debian | See VALIDATION.md | Exact package hash, dependencies, installed entry points and environment details. |
| English CLI and tutorial | See VALIDATION.md | Review against actual installed help and behavior. |
| Three role configurations | See VALIDATION.md | Per-role configuration and lifecycle results. |
| Fleet identity approval and transport | See VALIDATION.md | Real central/node admission and routed tools. |
| Tunnel parser and memory-only key | See VALIDATION.md | Parser, failure, cleanup and secret-retention checks. |
| Original FBEXEC, X11 CUA and real CUA-TTY | See VALIDATION.md | Backend receipts and observed results for the final package. |
| Manual ChatGPT connection | See VALIDATION.md | Browser consent and real tool calls against this package. |
| Source fidelity and reproducible package | See VALIDATION.md | Reviewed source mapping and independently compared builds. |

The successful Original R1/R2 tests are historical reference evidence. They must not be reported as fresh Expanded package tests.

## Historical source documents

- FrameBridge C8 terminal-first onboarding R7, 2026-10-11: https://drive.google.com/file/d/1LuYF1vGw8FKaD4Izcpw4NnR0HRuws78s/view
- Terminal-first onboarding guide R1: https://drive.google.com/file/d/11MEm5eOVke4aSeIBc-024e1zNLke_y_s/view
- GERAMEG CUA-TTY demo-only live test, 2026-10-19: https://drive.google.com/file/d/1DixrNUYUhr6pM2wiXeILKmU2dmbE53_D/view
- GERAMEG real-terminal FBEXEC/CUA-TTY A/B certificate R3, 2026-10-19: https://drive.google.com/file/d/1VUzhCLJpV8DNQSOlfWYus-nzKAsloGlm/view

These are historical implementation references. They do not override the current requested product or authorize copying runtime identities or secrets.


## RC7 saved-image and lifecycle acceptance

The exact saved PNG must be retrievable by node/capture_id without taking a replacement capture or performing input. Presentation must preserve the original bytes, resolution and SHA-256, label saved images as historical, and fail explicitly for unknown or corrupt data. An offline MCP Apps viewer must be tested in the intended ChatGPT account; ImageContent serialization alone is insufficient proof of rendering.

A central must be usable and manually connectable before any worker is installed. A later separately installed node must join the running central through explicit authenticated approval and become available without reconnecting ChatGPT.

Persistent tty framebuffer access must be an explicit root opt-in for a named non-root user, limited to reading the real fb0 device. No installation-time broad grant is acceptable. Revocation and package removal must restore only the helper's own change, preserve unrelated changes, and report conflicts. Fresh installation, reboot/recovery/removal and password-backed sudo results must be recorded for the exact RC7 artifact; inherited RC6 evidence is historical context.
