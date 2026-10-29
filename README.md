# FrameBridge

> This GitHub repository currently hosts the project introduction and licensing information. The complete source, signed packages and release history are available in the canonical repository and downloads linked below. A full GitHub source mirror is not yet published.

**Administer your own Linux network from a connected conversation.**

FrameBridge connects tools on Linux computers you control to ChatGPT. A central gateway and enrolled nodes let one human administrator inspect equipment, diagnose connections, check services and run commands across their own network. ChatGPT remains hosted; the tools execute on the selected Linux computer.

FrameBridge 0.3.4 is available under **GPL-3.0-only** for original project material. Third-party components retain their own licenses and notices. This is an independent community project.

## What you can do

- Run commands on confirmed, enrolled computers and inspect their results.
- Return to persistent Bash terminals while those sessions remain alive.
- Observe connection, health and managed-startup state through the shared `fb` / `framebridge` command interface.
- Use optional graphical tools with a compatible active X11 desktop. Desktop control is auxiliary to network administration.

Bash session state does not survive a reboot. Managed service startup and a live shell session are different things. Native Wayland control, Windows, other distributions and other CPU architectures are outside this release's validated scope.

## Supported release profiles

| Distribution | Architecture | System Python | Signed APT suite |
| --- | --- | --- | --- |
| Debian 13 | amd64 / x86_64 | 3.13 | `trixie` |
| Ubuntu 24.04 | amd64 / x86_64 | 3.12 | `noble` |
| Linux Mint 22.x | amd64 / x86_64 | 3.12 | `mint22` |

The maintainer's 0.3.4 acceptance record covers signed APT upgrade/reinstall, managed-service lifecycle and reboot checks on Debian 13, Ubuntu 24.04 and Linux Mint 22.3 **virtual machines**. Physical-machine rollout and desktop acceptance are tracked separately. These VM results do not certify every physical installation.

## Get started

1. On the Linux computer to connect, choose the exact profile on the [official download page](https://framebridge.betancourt.one/download/). Follow its signature, checksum and installation instructions.
2. In that computer's normal interactive terminal, run `fb connect`. Enter connection details in its hidden local prompts and complete your own browser sign-in and approval.
3. Check the installed state and read the guide:

```bash
fb status
fb health
fb doctor
fb help
```

Start with a harmless task such as reporting the selected computer's hostname. Confirm the execution destination and exit code. Installing the package alone does not establish an authenticated connection.

The user supplies their own compatible ChatGPT account and any required access to its connection features. An AI account, subscription or model credit is not included. FrameBridge does not make the hosted model local. Read the installed guide for credential handling and the distinct effects of disconnecting, stopping services and changing startup policy.

## Project links and feedback

- [Website and existing concept video](https://framebridge.betancourt.one/#preview)
- [Download, verify and install](https://framebridge.betancourt.one/download/)
- [Documentation](https://framebridge.betancourt.one/docs/)
- [Canonical source repository](https://framebridge.betancourt.one/git/gerardo/framebridge)
- [Contribution guide](https://framebridge.betancourt.one/community/)
- [Voluntary support and project contact](https://framebridge.betancourt.one/support/)

The existing video is the approved 0.3.1 **conceptual animation**, with its original music. It is not a live recording of 0.3.4. Current release artifacts and the versioned command guide describe the software separately.

Useful reports identify the distribution, package version, command, confirmed target, expected result and actual result. Remove credentials and personal information before sharing logs. Read `LICENSING.md`, `COPYRIGHT.md`, `CONTRIBUTING.md` and `SECURITY.md` in the canonical repository before redistributing material or submitting changes.

Programming: **ChatGPT**. Original concept and project coordination: **Gerardo Betancourt**. These credits describe the collaboration; they do not claim exclusive ownership of AI output, third-party work, or OpenAI endorsement.
