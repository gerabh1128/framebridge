# FrameBridge Original Expanded — RC7 validation scope

Date: 2026-10-24
Candidate: 0.3.0~expanded.20261024.7
Status: release acceptance is determined by the external report and evidence.

## Preserved baseline evidence

RC6 (0.3.0~expanded.20261023.6) is a separate frozen artifact. Its final source suite passed 314 tests and two builds produced identical package bytes. RC6 was installed on the isolated Debian CENTRAL and NODE VM filesystems. The real test account and manual tunnel verified status, local and remote EXEC, and authentic physical X11/framebuffer captures without new capture-created FBTERM jobs.

Those results do not certify RC7. RC6 still had an open ChatGPT image presentation/reuse gate, and the lab framebuffer read ACL was temporary. Three additional direct diagnostic checks passed; two terminal-key negative cases were skipped because the required auxiliary Bash window was not in front. Official CLOUD MT publication was not verified by that diagnostic phase.

## RC7 implementation scope

RC7 adds read-only retrieval of saved original PNGs, an offline MCP Apps viewer, and explicit opt-in persistent read-only fb0 access. Existing capture and input contracts remain separate from historical viewing. Central-first/manual-ChatGPT/later-node setup is documented and must be exercised in an installed environment.

The source suite covers setup, manual connection, TLS and routing, terminal identity and NoNewPrivs transitions, display context, actual-screen transport and input bindings. RC7 must also test saved-image byte integrity, authorization, unknown/corrupt capture handling, the SDK UI contract, and safe framebuffer access/revocation including external ACL conflicts.

Image fixtures are unit data, not evidence that a physical screen was captured. Terminal transcripts are text data, not screenshots. Unit registration of a UI resource does not prove actual ChatGPT rendering.

## Required installed-VM gates

1. Verify the exact RC7 .deb hash and install with APT in clean Debian 13 on separate CENTRAL and NODE VM filesystems.
2. Verify declared dependencies, installed commands, package identity and installed critical-file hashes.
3. Check start, stop, reboot, recovery and removal with preserved evidence. Removal must not silently leave an enabled framebuffer grant.
4. Verify the shared official console from normal graphical and text logins.
5. Verify managed-console NoNewPrivs on/off from actual processes and ordinary password-authenticated sudo. Root guest-agent execution and NOPASSWD are not substitutes for a password test.
6. In a physical LightDM X11 session, capture complete display pixels and test visible graphical input. Verify stale observations fail after relevant window/geometry/session changes.
7. Verify physical tty capture permission persists after reboot only when the explicit opt-in is enabled, and that revocation restores the access this helper owns.
8. Connect the CENTRAL manually to the intended ChatGPT account, then enroll and approve a newly installed NODE through verified TLS without reconnecting ChatGPT. Verify routed tools on the added node.
9. Complete actual-account observation, original-PNG display and reuse through the RC7 tools. Match the shown capture/node/hash to the backend evidence; do not replace an unavailable capture or infer rendering from a tool response alone.
10. Record user-entered manual-tunnel/account consent outcomes without passwords, runtime keys, cookies or other secrets. Verify the selected account separately from its optional email label.

A single host using two profile directories is not a two-machine installation test. Xvfb is explicitly virtual and cannot satisfy the physical display gate. Fresh isolated clones must not overwrite the existing working VM disks or interrupt their manual tunnel.

## Evidence and limitations

The external release report records exact package/source hashes, checks actually run, return codes, machine identities and unresolved gates. Package hashes remain outside packaged documentation to avoid circular builds. A prepared script, simulated APT run, active display-manager daemon or stale Xorg log is not a completed acceptance result.

Creating the isolated RC7 source tree does not deploy to production or change host network, kernel or GPU configuration. The package does not grant passwordless root access, auto-enable a framebuffer policy or connect silently to ChatGPT. Read the external acceptance status before calling this candidate a finished release.
