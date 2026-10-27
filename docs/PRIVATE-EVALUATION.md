# Community Preview 2 evaluation guide

Current package: `FrameBridge-0.3.1-Community-Preview2.deb`, version `0.3.1~community.20261026.2`. Preview 2 passed 376 unit tests; package/VM checks remain pending at this documentation freeze. This guide is the intended workflow, not a completed VM acceptance record.

## Supported starting point

Use an isolated Debian 13 amd64 VM or test computer and keep normal owner/admin credentials under the owner's control. Preserve production profiles.

Obtain the Preview 2 installer and its release checksum record. Follow the [Debian tutorial](../source/docs/DEBIAN-TUTORIAL.md) to verify the selected file before installing. RC7 and Preview 1 installer hashes do not apply to Preview 2. Source delivery uses the matching Preview 2 source archive, shared immutable Preview 1-named dependency asset and R2 supplements.

## One computer

Run setup, select standalone, review the node label and profile, run doctor, and start the runtime. Connect manually with `fb connect chatgpt`. The foreground connection needs the owner-supplied tunnel and runtime key each time it is established. Keep it open; a second local terminal can inspect `fb status`.

In the intended ChatGPT account, enable the intended app/tunnel, complete consent, make a small tool call, and inspect the backend result. An online tunnel or a remembered account label alone is not successful end-to-end acceptance.

## Central first, workers later

Configure and start a central, then connect ChatGPT manually. Workers can be added afterward without reconnecting that central. Follow the complete certificate and identity instructions in the Debian tutorial.

On a new worker, install the same verified package, choose node during setup, run doctor, and enroll before starting the node runtime:

```sh
fb setup
fb doctor
fb nodes enroll --central https://CENTRAL_ADDRESS:20878 --name worker-1
```

Use the actual verified HTTPS URL printed by the central, with the public CA checked through a trusted channel. CENTRAL_ADDRESS and worker-1 above are placeholders.

On the central, inspect the pending request and compare the node identity and public fingerprint:

```sh
fb nodes pending
fb nodes approve REQUEST_ID --node-id NODE_UUID
```

On the worker, poll for confirmation and start only after approval and pairing complete:

```sh
fb nodes poll
fb start
fb status
```

On the central, use `fb nodes list` to check current operational transport readiness. Registration alone does not establish that a worker is currently reachable.

## Suggested small evaluation

Use new demo-only files under the selected node user's home. Ask ChatGPT to create a short checklist, compute its SHA-256, and display the same file from the persistent console. For concurrent work, use a separate directory and an independent task in each conversation. Compare results and record interference if it occurs.

During graphical work, give one conversation control of the shared desktop. Use a full authentic screen capture, inspect it before input, and verify the visible outcome. Reopen the same terminal session when demonstrating continuity.

Do not include passwords, tokens, private keys, cookies, real account pages or unrelated personal files in recordings. Public test labels and synthetic documents are sufficient.

## End of evaluation

Stop the foreground tunnel with Ctrl+C when finished, then use the normal runtime stop command as appropriate for the test profile. Keep the owner's production installations and unrelated tasks outside the evaluation.
