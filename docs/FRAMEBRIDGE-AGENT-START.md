# Start FrameBridge with a coding assistant

This is a setup and verification workflow for an assistant that **already has authorized shell access to the target Linux computer**. A person can run the same commands by hand. For first setup, the owner uses that computer's normal terminal and completes the ChatGPT browser approval.

The currently tested connection is the manual ChatGPT workflow. A coding assistant can help prepare it through the CLI; this document does not claim drop-in integration with every AI client.

**FrameBridge 0.3.9+fbterm.15** has amd64 package profiles for Debian 13, Ubuntu 24.04 and Linux Mint 22.x. Before installation, verify the package profile, checksum and signature. Before connecting to an existing node, check the version actually installed there. [Official download page](https://framebridge.betancourt.one/download/) · [Current signed release record](RELEASE-SOURCE-0.3.9+fbterm.15.md)

## Before handing over

Give the assistant the computer you want to work on, the intended task, and the local path of the reviewed matching DEB when installing from a file. Use the [README's package matrix](../README.md#1-install-the-matching-package). For an existing installation, follow [cold package maintenance](PACKAGE-MAINTENANCE.md) and select [Normal updates](https://framebridge.betancourt.one/download/#normal-updates) for the current signed APT channel or the distribution's setup steps to migrate from the legacy channel with the rotated signing key.

For a pilot, a separate `--state-dir` isolates profile data while using the
installed package. To test a different DEB while the existing services must
stay available, use a separate computer or disposable VM.

Name both the computer receiving the assistant's tool call and the intended execution target. `LOCAL` means the current FrameBridge receiver, not a fixed computer called WORKSTATION or the computer displaying the chat. Discover enrolled nodes and use their confirmed aliases or IDs. A legacy connector can have a different default target; changing endpoints does not preserve that default automatically.

Select the computer with `target` in FBEXEC and CUA. The older `node` selector is an alias on the execution and screen tools that accept it. Pass one destination. For graphical input, observe with `framebridge_cua` and `capture_scope="WINDOW"`, then use the same target and the observed window ID and dimensions. The bound screen key and text tools require a visible TTY. Receipts identify the enrolled node by alias and UUID. `receiver_hostname` identifies the receiver, and `executed_hostname` gives the system name when reported by the executor. If the receipt reports `in_flight` or `outcome_unknown`, use `fb nodes execution` or `framebridge_exec_status` with the existing request or job ID before taking further action.

Do not put passwords, tunnel IDs, server access keys or browser codes in the prompt. The owner will enter them in the local hidden prompts. An MCP server access key is not an OpenAI API key.

## Copy this into your assistant

~~~text
Help me install and verify FrameBridge on this Linux computer.

Target computer: [the computer to which you already have authorized shell access]
Reviewed package: [absolute local path to the matching FrameBridge .deb when using one]
Scope: [fresh install, current APT update, legacy channel migration, or an isolated pilot]
Receiver and execution target: [identify both; do not assume LOCAL means WORKSTATION]

First establish where you are running. Inspect the distribution/version,
architecture and system Python. Match them to the package's release profile.
Read the project's README and the installed versioned help where available.
Reuse FrameBridge's existing CLI and package; do not invent a replacement
transport or rewrite the application to complete setup.
Confirm the receiver and selected target before the first remote operation.
Use an enrolled alias or ID for another computer and inspect the effective
target in the response. Do not fall back to a different computer on error.

Use my existing task authorization. Complete the authorized preparatory work
before asking for information or a decision that is actually missing.

Before package installation:
- Check the exact reviewed file's identity, version and architecture. Verify
  an official release through its signature/trusted key. An unsigned candidate
  must remain labelled a candidate; a checksum is not a release signature.
- Inspect the actual package-manager plan. Record package versions and relevant
  desktop selections before changes. Explain an unexpected upgrade/removal or
  desktop change rather than approving a different plan silently.
- For a configuration pilot with the installed package, use the agreed
  isolated state directory. To test a different package while existing
  services stay available, use a separate computer or disposable VM.
- Before replacing a package, back up the affected profiles, let active jobs
  finish and check their receipts. Close the tray and stop the affected
  runtimes and autostart supervisors from an independent terminal. Keep them
  stopped until the package transaction completes.

Treat optional virtual-display prerequisites as optional: install them only
when a virtual desktop is part of my task.

For a fresh installation from a local file, install the matching .deb through
APT. For an APT update or channel migration, use the official steps for the
matching distribution. Run the package transaction from an independent terminal.
If OS authentication is required, show the exact action and target terminal
once and let me type there. Keep that terminal available until post-install
verification and any required correction have finished. Never ask me to paste
credentials into chat, command arguments, environment variables or a log.
Do not weaken a service's restrictions or grant permanent passwordless root
as a workaround for a restricted execution channel.

Run fb connect interactively in that same normal terminal when connection is
part of the authorized scope. I will supply the hidden connection details and
complete the browser sign-in/approval. Installing a package or setting an
account label is not evidence that ChatGPT is connected.

For programmatic observation, use fb --json status and fb help commands --json.
JSON mode never prompts. If it returns INTERACTION_REQUIRED, use the stated
normal-terminal action; do not send a secret to a noninteractive tool.

Verify installed files, the selected release profile, fb status and fb doctor.
When connected, read FrameBridge's registered help tool before choosing a
small authorized terminal task. Prove a persistent Bash session by keeping
its session ID and checking a variable/working directory in later calls.
Close only the test session and test service you created.

If desktop access is in scope, first identify the actual X11 session and use
a new test window. A virtual display must be described as virtual. Terminal
text is not a screen capture, and a VM's desktop is not the owner's physical
display. Do not claim a screenshot or successful input unless it was observed.
Coordinate one controller at a time for a shared desktop. Preserve existing
console windows and their work unless changing them is part of the task.

Check actual command exit codes and operation results. An outer tool wrapper's
success flag is not proof that its inner command succeeded. Distinguish local
tool readiness, real connection state, boot policy and saved material.

Finish by comparing the relevant before/after package and desktop state,
confirming original service processes are still running where required, and
giving me a short result: exact version/profile, tests passed, what remains
unproved, and the commands to pause or remove this installation.
~~~

## The same short route without an assistant

After verifying and installing the matching `.deb`:

~~~bash
fb connect
fb status
fb doctor
fb console
~~~

Normal first connection needs no separate `fb setup` or `fb service start`. Advanced roles, enrollment, startup policy and credential management are documented in `fb help commands`.

## Keep undo specific

- `fb disconnect` pauses the connection until the next explicit connect, including after reboot; local tools and saved material remain.
- `fb service stop` stops current managed services without changing future startup policy.
- `fb service boot disable` changes future startup policy without stopping the current stack or forgetting its saved credential.
- `sudo dpkg --remove framebridge-original-expanded` removes the package without automatically removing dependencies. User profiles may remain, and administrative helper grants are revoked. An interactive reconnect can be needed after reinstalling.

Forgetting credentials is a separate destructive operation in the installed guide. Do not use it as routine cleanup or present package removal as a whole-machine restore. For a pilot, retain the original inventory and the exact profile path so the owner can distinguish the test installation from other work.
