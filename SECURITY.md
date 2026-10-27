# Security and responsible use

## Scope and release evidence

FrameBridge 0.3.1 is a separate community release from historical Original Expanded RC7. Debian 13 amd64 is the documented platform. Historical test counts do not certify every later build or deployment. Consult the matching release validation record for what was actually checked.

## Deliberate access

Use an appropriate account, confirm the receiving machine and selected node, and authorize the intended operation. Keep owner authentication in the normal terminal or interface. Never place passwords, private keys, tokens, cookies or runtime credentials in chat, issues, recordings or source control.

Agents should normally use the installed CLI/MCP interface without rewriting application files. Explicit development belongs in a separate source checkout and reviewable change set. This guidance preserves ordinary owner control and does not restrict free-software rights.

Graphical control shares the desktop's focus, keyboard and mouse. Use one controller per shared desktop at a time. That is an operating recommendation, not a claim of an automatic certified lock. Terminal transcripts are not authentic complete screen captures; label evidence accurately.

## Authentic releases

Use official signed artifacts and the signed APT repository described in [OFFICIAL-RELEASES.md](OFFICIAL-RELEASES.md). Verify the published signing-key fingerprint through a trusted channel. Hashes identify bytes, while authenticated signatures establish the signed artifact's origin. Neither guarantees that a privileged local user cannot change an installed system.

## Reporting a problem

Report the affected version, platform, command and observed behavior through the project Git. For a potential security issue, provide a minimal description without credentials or exploitation against systems you do not own. Do not post sensitive data to a public issue. The project does not claim a private reporting mailbox until one is explicitly published.

The source/privacy review is bounded and does not guarantee that every possible secret or vulnerability is absent. Preserve third-party notices during cleanup and keep historical release evidence separate from new builds.
