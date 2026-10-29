# FrameBridge Original Expanded — RC7 image presentation and lifecycle

Date: 2026-10-24
Candidate: 0.3.0~expanded.20261024.7
Status: implementation candidate; release acceptance requires the external report.

## Connect the central first

It is valid to install and configure a central, start it, complete its manual ChatGPT connection, and install worker nodes later. The central does not require a worker to publish its local tools. A standalone profile can also connect directly. A node profile joins an approved central and does not publish its own ChatGPT MCP endpoint.

For a fleet, choose central at the initial setup. Start it and run `fb connect chatgpt`. Enter the tunnel information and runtime key at the hidden local prompts, then select the app and complete ChatGPT consent. The connection client remains in the foreground while the tunnel is active; lack of a new prompt is expected. Keep that terminal open. Browser login is separate from the optional account label.

When adding another machine later, install the same verified package there and choose node. Use the central's verified HTTPS broker URL and public CA certificate. The node submits enrollment, the central operator compares the identity and approves the matching request, and the node polls before starting. Section 8 of DEBIAN-TUTORIAL.md provides the exact commands. The central resolves the current approved node list for each request, so this workflow does not require reinstalling the central or reconnecting ChatGPT.

Changing a running central profile to another role is a different operation: stop that profile before editing setup. Installing every worker before connecting ChatGPT is an optional test order, not a product requirement.

## Display and reopen the same actual screen image

The existing actual-screen tools retain their contracts:

- `framebridge_screen_observe` captures a new verified active physical screen.
- `framebridge_screen_key` and `framebridge_screen_text` require a valid fresh input binding and return the resulting capture.
- `framebridge_screen_show(node, capture_id, authorized)` retrieves an existing saved capture and displays its original PNG.

Use the node and capture_id returned by observation when calling show. It reads the saved capture on that node, verifies the byte count, PNG dimensions and SHA-256, and returns the original ImageContent plus a structured result for the viewer. It does not run a new capture, open an execution console, send input or consume input authorization. An unavailable or corrupt saved capture produces an error rather than a substitute image.

The offline MCP Apps viewer is registered as `ui://framebridge/screen-v1.html` with MIME type `text/html;profile=mcp-app`. It receives the original PNG through tool-result metadata and validates integrity before showing it. It provides fit, native-size, fullscreen and exact-PNG download controls, and identifies the node, dimensions and capture hash. A reopened image is labelled saved/historical with control_available=false: viewing an old image does not make it a safe basis for input.

This UI requires a compatible ChatGPT client and refreshed RC7 app tools. Existing clients still receive the original PNG ImageContent. Tool/resource registration, direct SDK tests and capture backend success do not certify that ChatGPT rendered the image; an actual-account visual check is a separate release gate. The legacy graphical CUA JPEG preview remains capped at width 960 while reporting the true source geometry; that preview is separate from the native actual-screen PNG.

## Explicit read-only text framebuffer access

The package includes an inactive udev rule and a root helper. Installation does not enable a grant. The administrator can explicitly select an existing non-root user with:

```sh
fb screen-access status
sudo fb screen-access enable --user "$(id -un)"
fb screen-access status
```

The command must run from the intended normal user's login. Only the selected user receives a named read-only ACL for the actual `/dev/fb0` graphics framebuffer (major 29, minor 0). Other video devices, device ownership and group ownership are outside its scope. The helper preserves the current ACL mask; if read access would require widening that mask, it refuses the change. No broad video-group membership or passwordless sudo rule is installed.

The enabled policy under `/etc/framebridge-expanded/screen-access.json` contains the public user identity. Protected recovery data under `/var/lib/framebridge-expanded/screen-access` records the original access. The narrow udev rule reapplies the selected grant when fb0 is recreated. Persistent access lets any process of that Unix user read framebuffer pixels across logout and reboot, including another user's screen, so the administrator must assess that tradeoff.

Revoke on the same machine with:

```sh
sudo fb screen-access disable
fb screen-access status
```

Disable prevents regranting and restores only the access this helper owns. A conflicting external ACL change is reported instead of being overwritten; protected recovery data remains available for review. Package removal uses this same revocation and stops on unresolved conflict. Upgrade preserves the explicit policy. Normal per-console sudo enable/disable remains separate from framebuffer read access and still requires normal sudo authentication.

## Acceptance and scope

RC6 remains the frozen baseline. RC7 implementation tests, reproducible builds, installed-file verification, fresh installation, reboot, recovery, removal and the central-before-node workflow must be recorded for the exact RC7 artifact. Password-backed sudo must be checked interactively; root execution through a VM guest agent does not prove that the user entered a valid password. Source fixtures are not screenshots of a real screen. See VALIDATION.md and the external evidence for completed checks and remaining gates.
