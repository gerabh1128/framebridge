# Privacy review status — FrameBridge 0.3.1 preparation

This review describes the separate 0.3.1 source preparation. Historical RC7 and Community Preview inventories remain provenance records; final source manifests must describe the actual assembled release.

A bounded tracked-text scan examined 1,259 UTF-8 files for private-key banners and common credential-token shapes. Two candidates were reviewed as examples: a synthetic rejection-test input and the public PyJWT package metadata encode/decode example. Neither was validated as a secret. No matched credential values are retained in review reports. This is not a guarantee that every possible secret is absent.

A separate non-vendor path scan found no matches for the owner's known home-path and email patterns. Remaining laboratory names occur in retained source/provenance labels and implementation messages, not owner credentials. Matched private-network strings were generic RFC1918 network policy ranges or unit-test fixture addresses, not validated owner access endpoints. Keep public interface help generic and preserve legitimate third-party copyright/author notices.

CLASSIC_NODE_TARGETS is currently initialized to an empty dictionary. The compatibility routing function is conditionally reachable; this review does not dismiss it as wholly dead code. An empty implicit target map must not become an excuse to infer or restore personal infrastructure routes.

The normal Expanded implementation uses account-relative state and generic transport. Review final staged source and approved promotional content for unrelated personal information. Do not publish runtime profiles, SSH material, passwords, tokens, cookies, logs containing hidden inputs or unrelated owner data. Source filenames preserving historical origin are not a claim that a matching machine is an available public node.

No production services, profiles or owner credentials were accessed or changed by this text review. The final package, source archive and download delivery checks are separate release evidence. Subsequent implementation changes require a final artifact scan before public exposure.

## Role-based release layout

From 0.3.9+fbterm.4, the bundled workstation, hub and terminal-node snapshots use component roles in source and installed paths. Test fixtures use synthetic node identities and documentation addresses. The packaged command and fleet interfaces resolve targets through enrollment and local configuration.

Paths in historical provenance records now use the component role labels. Their content hashes record the historical bytes. The active snapshot and corresponding-source manifests cover the current files and hashes.

For a separate legacy CUA-TTY installation, configure `FRAMEBRIDGE_TERMINAL_NODE_HOST` for the terminal runtime and its local RPC wrapper. Both validate that host and retain the same-UID checks. The standard package selects targets through the fleet registry.

## Publication checks

From 0.3.9+fbterm.5, public-release builders check the assembled source archive and
both Debian package sections before the completed artifacts are placed in the
release directory. The checks cover common private-key and credential shapes,
private file paths, direct Drive links and personal email in package control
metadata or rendered website files. Third-party attribution stays intact.

Use `--privacy-policy` on the builders to include the private identifiers for a
specific deployment. Keep the policy in a private directory outside the checkout.
It is a JSON array of identifying strings such as host aliases, full local paths,
node IDs and private addresses. Include identifiers only and keep credential
values out of the policy. Test fixtures should use synthetic values.

Run the check again on the exact files selected for publication. Include a Git
archive of the reviewed commit to cover repository files outside the source
distribution. Pass the website output directory when including a website update.

```sh
git archive --format=tar HEAD > /staged/reviewed-source.tar
python3 source/packaging/release_privacy.py --policy /private/release-identifiers.json \
    /staged/reviewed-source.tar /staged/FrameBridge-source.tar.gz /staged/SOURCE-ARCHIVE-MANIFEST.json \
    /staged/debian.deb /staged/ubuntu.deb /staged/mint.deb /staged/website
```

A finding identifies the affected artifact section and entry number without
printing its matched value. Keep the report and exact file hashes with the
release review. Review media and sharing permissions separately, and recheck
the public release after promotion. Historical tags, packages and external
copies have their own retention and remediation decisions.
