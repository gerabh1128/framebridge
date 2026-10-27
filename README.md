# FrameBridge

**Bring your ChatGPT conversations to Linux computers you control.**

Run a task on your own machine, return to the same Bash terminal, inspect an X11 desktop, or select an enrolled computer. ChatGPT remains hosted; FrameBridge runs the tools on your Linux computers.

> **Unsigned release candidate: `0.3.2`.** This source preparation promotes the compatibility candidate; it is not a signed public release. Final artifact validation and signing are recorded separately. The signed 0.3.1 release remains a different artifact. Read the [compatibility scope and history](docs/LINUX-COMPATIBILITY-CANDIDATE.md); use the exact artifact release record when choosing a package.

## Start here

Start on the Linux computer you want to connect, with a person at its terminal or an assistant already given shell access there. Your ChatGPT account and manual browser approval complete the connection. No phone pairing is required.

| Step | What you do | What to check |
| --- | --- | --- |
| **1. Install** | Choose the package matching your distribution and Python version, verify its release record, then install it with APT. | The package manager resolves dependencies for that computer. |
| **2. Connect** | Run `fb connect` in your normal terminal and enter the requested connection details there. Complete browser sign-in and approval yourself. | The command reports the real connection result and returns to the prompt. |
| **3. Verify** | Run `fb status`, `fb doctor`, then `fb console`. In ChatGPT, ask the connected FrameBridge help tool for a small terminal task. | Confirm the selected machine and the result of the task. |

### 1. Install the matching package

The compatibility candidate has separate native Python builds; it is not one interchangeable `.deb` for every Linux system.

| Distribution | Architecture | System Python | Candidate package profile |
| --- | --- | --- | --- |
| Debian 13 | amd64 / x86_64 | 3.13 | `debian13-amd64-py313` |
| Ubuntu 24.04 | amd64 / x86_64 | 3.12 | `ubuntu24.04-amd64-py312` |
| Linux Mint 22.x | amd64 / x86_64 | 3.12 | `mint22-amd64-py312` |

The preceding compatibility candidate `0.3.2~compat.20261027.3` passed installation, normal and optional virtual-runtime startup, actual MCP HTTP/SSE exchanges, terminal and cleanup tests on Debian 13, Ubuntu 24.04 and Mint 22.3 VMs. Mint also passed its Cinnamon window-capture test with its existing WebSocket library unchanged. This 0.3.2 source preparation preserves that runtime behavior; the exact final package bytes require their own artifact validation and release signatures. Ubuntu 26.04 and LMDE 7 builds remain experimental; other distributions, older releases and CPU architectures are outside this candidate's scope.

Verify official releases with the independently trusted project key as described in [Official releases](OFFICIAL-RELEASES.md). A candidate's recorded SHA-256 identifies its reviewed file; it is not a release signature. Candidate packages are for reviewed pilots.

For example, with the **Mint** candidate downloaded into the current directory:

~~~bash
sudo apt install ./FrameBridge-0.3.2-mint22-amd64-py312.deb
~~~

Use the corresponding profile filename on Debian or Ubuntu. Review APT's proposed changes for the actual computer. Installation places the software on disk; it does not establish a ChatGPT connection or replace the system Python.

### 2. Connect from your terminal

~~~bash
fb connect
~~~

First connection needs no separate setup or start command. Have your tunnel ID and MCP server access key ready from the manual connection setup described in `fb help`. Enter them in the hidden local prompts and complete browser approval yourself. The key is not an OpenAI API key; the optional account label does not authenticate you.

The managed connection saves an encrypted credential locally, prepares startup, reports progress and returns to the prompt without requiring a reboot. Repeating `fb connect` resumes valid saved settings. Read the guide before replacing them with `fb connect --replace`.

### 3. Verify and use it

~~~bash
fb status
fb doctor
fb console
~~~

Start with a harmless terminal task, such as reporting the hostname and current directory. Status distinguishes local tools, connection and startup policy. Doctor diagnoses prerequisites; it does not repair or connect.

The console works in a Linux TTY or desktop terminal. A local X11 session adds graphical CUA. Terminal text, real screen captures and explicitly selected virtual displays are identified separately. A VM test does not prove the owner's physical desktop or Wayland.

### Optional virtual desktop

Text mode and an existing X11 desktop work without a virtual desktop. For an explicitly selected virtual display, install its optional tools yourself:

~~~bash
sudo apt install xvfb openbox xauth
~~~

Openbox can register itself as the generic window manager when installed. Review that desktop choice when opting in. Normal FrameBridge installation does not request Xvfb or Openbox; `xauth` remains a shared X11 dependency. `fb doctor` reports virtual-display availability separately. Read `fb help commands` for the explicit virtual mode.

## Use it with an assistant

Give a coding assistant that already has shell access the [FrameBridge agent-start workflow](docs/FRAMEBRIDGE-AGENT-START.md). The same CLI is available for people and scripts:

~~~bash
fb --json status
fb help commands --json
~~~

JSON output is versioned and never prompts. `INTERACTION_REQUIRED` gives an action for the owner's normal terminal. The agent workflow helps an existing shell-based assistant install and diagnose FrameBridge; it does not claim every AI client is a tested replacement for ChatGPT.

`fb` and `framebridge` are names for the same program. Read `fb help` for the English guide and `fb help compatibility` for legacy commands.

## Pause, stop or remove

| Intended result | Command | What remains |
| --- | --- | --- |
| Pause the ChatGPT connection until the next explicit connect, including after restart | `fb disconnect` | Local tools and saved material. |
| Stop the current managed stack | `fb service stop` | Future startup policy and saved material. |
| Disable future managed startup | `fb service boot disable` | Current execution and saved material. |
| Remove the package without removing its dependencies automatically | `sudo dpkg --remove framebridge-original-expanded` | User-profile data can remain. |

Removal stops package-owned managed processes and revokes administrative helper grants. Reinstalling requires an interactive `fb connect` to reprovision what is needed. Removal/purge may retain profiles; forgetting saved connection material is a separate action in `fb help commands`.

These controls undo specific operations, not a whole computer snapshot. Bash state persists between calls while the session lives, not across reboots. Changing its sudo mode recreates Bash and loses its shell state.

## Source, licensing and contributions

Original FrameBridge material is **GNU GPL version 3 only (`GPL-3.0-only`)**. Third-party material keeps its own licenses and notices. Read [LICENSING.md](LICENSING.md), [COPYRIGHT.md](COPYRIGHT.md) and [LICENSE-STATUS.md](LICENSE-STATUS.md).

Versioned source archives include the FrameBridge implementation, builders, tests and legal records. The shared Python/Rust dependency source asset and the tunnel client's dependency sources retain their reviewed historical 0.3.1 filenames. Exact sizes, hashes, scope and upstream provenance are recorded in `licensing/third-party`. The retained tunnel executable is not presented as independently rebuilt from an identified upstream source revision. Building FrameBridge does not claim an independent rebuild of every embedded native component.

Use `python3 source/packaging/build_expanded.py --help` for package build profiles and `python3 source/packaging/archive_sources.py --help` for source archiving. Building does not install, start, sign or publish the product.

Read [CONTRIBUTING.md](CONTRIBUTING.md) to propose a change and [SECURITY.md](SECURITY.md) for security reports. Useful compatibility reports name the exact distribution, version, Python version, package profile and tests actually performed. Independent copies retain their applicable software freedoms; they must not be presented as maintainer-approved releases.

Project home and 0.3.2 publication origin: https://betancourt.one/framebridge/

Use the corresponding-source archive identified in each release record. When that record marks the source mirror published, its read-only HTTP Git endpoint is `https://betancourt.one/framebridge/git/framebridge.git`. Final 0.3.2 publication is pending; this source preparation does not claim that the new packages or mirror are already live.

Programming: **ChatGPT**.  
Original concept and project coordination: **Gerardo Betancourt**.

FrameBridge is an independent community project; no OpenAI endorsement is claimed. Demonstration animations are conceptual illustrations, not recordings of tool execution. Use one controller at a time for a shared graphical desktop. Independent FBEXEC tasks observed on different work targets are not an isolation guarantee.
