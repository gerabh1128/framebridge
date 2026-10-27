> Development candidate: **0.3.2~compat.20261027.1** for Debian, Ubuntu and Linux Mint on amd64. See [candidate scope and validation](docs/LINUX-COMPATIBILITY-CANDIDATE.md). Build profiles are not proof of live production compatibility. The official signed 0.3.1 release described below remains a separate artifact.

# FrameBridge

**Bring your ChatGPT conversations to Linux computers you control.**

FrameBridge connects a hosted ChatGPT conversation to tools running on your own computers. Run a task, return to a persistent Bash terminal, inspect the real desktop, and select an enrolled node.

**Community Release 0.3.1** · **Debian 13 amd64** · **Python 3.13**

The installer and control console are text interfaces in a Linux TTY or desktop terminal. FrameBridge detects a usable graphical X11 session automatically and adds graphical CUA when available. Text mode provides FBEXEC, persistent terminals and CUA-TTY. Adaptive mode does not silently create a virtual desktop.

ChatGPT remains hosted. The tools execute on your own machines. Your own ChatGPT account and manual browser approval are required; a local account label does not authenticate you. No phone pairing is required.

## Install and connect

Verify the release checksums and signatures using the independently trusted project key before installing. See [Official releases](OFFICIAL-RELEASES.md) for the trust model and the project site for the signed release record.

On Debian 13 amd64, with the downloaded installer in the current directory:

~~~bash
sudo apt install ./FrameBridge-0.3.1.deb
fb connect
fb status
~~~

Normal first connection needs no separate setup or start command. The terminal asks for an optional account label, a hidden tunnel ID and a hidden MCP server access key. This access key is not an OpenAI API key. Complete any browser sign-in and approval separately.

Normal connection prepares managed Linux startup, stores the saved credential encrypted on this machine, reports progress and returns to the prompt. It does not force a reboot. Repeating the command resumes valid saved settings; explicit replacement is available through fb connect --replace.

## One command interface for people and agents

The executable names fb and framebridge run the same program.

| Command | Purpose |
| --- | --- |
| fb connect | Establish or resume the managed ChatGPT connection. |
| fb disconnect | Pause that connection until the next explicit connect, including after restart. Local tools and saved material remain. |
| fb status | Observe actual tools, connection and boot policy together. |
| fb status connection | Observe just the connection, including its intended state. |
| fb doctor | Diagnose prerequisites and problems; it does not repair or connect. |
| fb console | Open or reattach the text Bash/FBTERM work view. |
| fb help | Read the versioned English guide. |
| fb setup | Optional advanced role/profile configuration. |

Read fb help commands for specialized service, boot, node, framebuffer-access and terminal-session controls. Legacy spellings are described separately in fb help compatibility.

For an agent or script:

~~~bash
fb --json status
fb help commands --json
~~~

Explicit JSON output is versioned and does not request interactive input. When owner authentication is needed, it reports INTERACTION_REQUIRED with an action for a normal terminal. The MCP server also invites an agent to read its registered help tool. This supplies usage context; client compliance must not be assumed from the invitation alone.

## Services, terminals and desktops

Service execution, connection intent, boot policy and saved material are separate:

- fb service start and fb service stop control the current stack. The first start of a configured node can also prepare its managed startup; that initial provisioning is reported explicitly.
- fb service boot enable and fb service boot disable control future startup without stopping current work or forgetting a saved credential.
- fb service credentials forget --confirm is a separate destructive operation: it removes the managed connection setup and saved material while retaining the profile and user persistence.

Persistent Bash sessions retain shell state between calls while the session and service remain alive. They do not preserve running processes across an operating-system reboot. Changing a session's sudo mode recreates Bash and loses its internal shell state; consult the guide before doing so.

CLI terminal observation is text. Real CUA captures are separate, authentic screen evidence. Private virtual displays, when explicitly selected, are identified as virtual.

Independent FBEXEC tasks have been observed to work on one computer when their targets and changes do not overlap. This is not an isolation guarantee. Use one controller at a time for a shared graphical desktop.

## Source, licenses and official releases

Original FrameBridge material is licensed under **GNU GPL version 3 only (GPL-3.0-only)**. Third-party material retains its own notices and license terms. Read [LICENSING.md](LICENSING.md), [COPYRIGHT.md](COPYRIGHT.md) and [LICENSE-STATUS.md](LICENSE-STATUS.md).

The distribution source set contains:

- FrameBridge-0.3.1-source.tar.gz — FrameBridge implementation, builder, tests and legal records.
- FrameBridge-0.3.1-Community-Preview1-dependency-sources.tar.gz — the immutable shared Python/Rust dependency source asset; its historical name is retained deliberately.
- FrameBridge-0.3.1-tunnel-client-dependency-sources.tar.gz — retained source archives and notices for the tunnel executable's Go dependencies.

Exact sizes, hashes, scope and upstream provenance are recorded in licensing/third-party. The retained tunnel executable is not presented as independently rebuilt from an identified upstream source revision.

~~~bash
python3 source/packaging/build_expanded.py
python3 source/packaging/archive_sources.py
~~~

These commands assemble the Debian package and project source archive; they do not install or start FrameBridge. Build prerequisites must already be available. Rebuilding the project package does not claim an independent rebuild of every embedded native component.

Release signatures and publication readiness are recorded separately from the builder. A successful build alone is not proof that a release is signed or publicly available.

Everyone retains the applicable freedoms to modify and redistribute lawful copies. Contributing through the project Git is welcome; it is not an additional license condition. Maintainers review what enters the official repository and sign official releases. Independent variants must not be represented as maintainer-approved releases.

## Community and credits

Project home: https://framebridge.betancourt.one/

Programming: **ChatGPT**.  
Original concept and project coordination: **Gerardo Betancourt**.

FrameBridge is an independent community project. No endorsement by OpenAI is claimed.

Read [CONTRIBUTING.md](CONTRIBUTING.md) before proposing changes and [SECURITY.md](SECURITY.md) for the security process. The English Debian guide is available through fb help and the included documentation. Demonstration animations are conceptual illustrations, not recordings of tool execution.
