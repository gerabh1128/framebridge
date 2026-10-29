# FrameBridge Original Expanded — portability

PROJECT=FRAMEBRIDGE_ORIGINAL_EXPANDED
STATUS=EXPANDED_PORTABILITY_AND_VALIDATION_SCOPE_DOCUMENTED
DATE=2026-10-21

The supported target is Debian 13 amd64 with Python 3.13. Vendored native Python extensions require that ABI. Other platforms are not certified by this package.

Use the English first-use assistant with framebridge or fb. The full installation, role, manual ChatGPT connection and terminal workflow is in docs/DEBIAN-TUTORIAL.md; installed users can run framebridge tutorial.

The default ports are MCP 20865, fleet hub 20877 and broker 20878. Setup can select different ports. Standalone and central profiles expose local MCP; enrolled nodes are reached through their central instance.

Headless auto mode reuses a usable X11 display or creates a private Xvfb/Openbox/DBus environment for the original graphical tools. Original FBTERM still uses GNOME Terminal and wmctrl. Real CUA-TTY Bash/tmux sessions do not require X.

CUA-TTY real-terminal support is included in Expanded. It uses the configured user's identity and a private local socket; there is no fixed UID or phone requirement. Fleet access retains authenticated transport and explicit node-identity approval. Central setup can generate local TLS material; nodes receive only its public CA certificate and verify the published DER fingerprint.

ChatGPT connection is manual and runs in the foreground. A tunnel ID or supported tunnel-client command is parsed locally; the runtime API key is read with a hidden prompt and kept in process memory only.

Live profile state, keys, enrollment identities and runtime sessions are not release inputs. Do not copy them into an installer or source archive.

Consult docs/VALIDATION.md for actual test coverage. The historical R1 Front Door patch is provenance; it is not the Expanded implementation diff.
