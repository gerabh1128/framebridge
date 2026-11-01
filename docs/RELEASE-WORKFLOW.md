# Reviewed source and release workflow

Maintain one traceable mapping from accepted source to released artifacts. A successful build, mergeability result or signature does not replace human approval of the change.

1. Develop in a separate branch and open a draft review. Identify the base and head commits, scope, affected files and focused validation. Keep credentials and installed runtime state outside source control.
2. Test the exact candidate head. Retain commands, environment, logs, failures, skips and source identity. Document native tests separately from GitHub CI. If no checks are configured, do not describe an empty CI result as success. A changed executable source invalidates earlier acceptance until the affected checks are repeated.
3. Present the exact reviewed head and proposed integration to the owner. Source integration needs explicit approval. Recheck both base and head before applying it; stop for renewed review if either has changed. No auto-merge or unreviewed release branch updates are implied.
4. Freeze the accepted release source commit and complete tree. Record any different mirror commit together with proof of complete tree equality. Build the corresponding-source archive, source manifest and exact distribution profiles in staging, with their inputs and hashes. Do not generate a release from an unreviewed advancing branch.
5. Verify the staged packages, source mapping, upgrade/recovery behavior, signatures and real isolated APT candidate selection for each supported suite. Keep recorded historical results distinct from a fresh run. Prepare rollback without overwriting historical artifacts.
6. Obtain the owner's separate approval for signing, release tags and distribution promotion as applicable. Signing authenticates reviewed bytes; it does not approve unreviewed changes. Publish the exact accepted artifacts and verify downloads, signatures and APT metadata from outside the origin.
7. Update the repository release-source record and official technical memory. Preserve immutable artifact identities and historical evidence. Reconcile mirrors and close superseded PRs explicitly, without applying the same patch twice.

Documentation can advance after a frozen release if its commit is clearly separate and the released source mapping remains intact. A later `main` tree is then not claimed to be byte-identical to the old source archive. Never rewrite a published version's package bytes or move an existing release tag to hide a change; use a new identifiable version when executable release content changes.

Branch protection, required reviews and required CI checks should be enabled through a separately reviewed repository configuration change. This document is a process rule; it does not claim those controls are already enforced by GitHub.
