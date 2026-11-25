# Local reconciliation of a missing execution receipt

This is a private candidate feature for 0.3.9+fbterm.8. It does not establish
that any unknown command succeeded, failed, or never ran.

An outcome_unknown record normally retains its resource reservation. A
verified original DONE/result.env remains the preferred automatic recovery.
If the original job directory is absent, a permanent global reservation may
require a human decision. The new maintenance command records a separate
review decision and preserves the original job state, identity, receipt,
slot and do_not_replay semantics. An exact duplicate ID still returns the
historical unknown result and cannot execute again. An older scheduler
ignores the additional table and therefore holds the reservation again.

## Preconditions (do not perform automatically)

1. Identify the exact local profile and job ID, request ID and command
   SHA-256. Review the original authorization and effects independently.
   The absence of a receipt does not prove the absence of effects.
2. Obtain a consistent backup. Stop only the intended FrameBridge profile
   through its normal local service management and verify that its
   scheduler and all original FBTERM workers/controllers are quiescent.
   Do not stop unrelated virtual machines or services.
3. Review possible detached child processes and all external effects
   separately. If this cannot be done, leave the reservation held. Kernel
   locks check cooperating runtime components, not arbitrary commands
   or remote effects. The command refuses any queued/in-flight work,
   unexpired claim, original job directory or other unfinished engine job.
4. Prepare a private regular 0600 JSON evidence file as the profile owner,
   containing exactly these fields (replace example identities):

   ```json
   {
     "schema": 1,
     "job_id": "EXAMPLE-JOB",
     "request_id": "EXAMPLE-REQUEST",
     "command_sha256": "REPLACE_WITH_EXACT_64_LOWERCASE_HEX",
     "reviewed_local_processes": true,
     "reviewed_external_effects": true,
     "no_pending_effects": true,
     "accept_unknown": true,
     "review_reference": "PRIVATE-INCIDENT-REVIEW-ID"
   }
   ```

   Never put secrets, command contents, credentials, or private output in
   this file. The reference is a non-secret identifier of the operator's
   independent review; it is not an automatically verified assertion.

5. In an authenticated interactive terminal on the node, as the profile
   owner (not root), inspect the help:

   ```sh
   fb recovery reconcile --help
   ```

   Supply the explicit state directory, job identity, request ID, digest
   and evidence path. Review the exact identity and evidence hash displayed
   by the command. Enter the displayed confirmation only if the decision
   is authorized. JSON/noninteractive mode is deliberately refused. No MCP
   operation, force flag, or automatic reconciliation is added.

The transaction holds scheduler and controller/worker locks, validates the
database and immutable claim, takes a consistent predecision SQLite backup,
saves the evidence and decision in a private audit directory, and commits
only the matching additional reconciliation row. A failure before commit
leaves the reservation held; an interrupted command must be inspected,
not repeated blindly. The evidence files are not public release content.
A different or changed original identity, receipt hash, owner or evidence
hash never matches the release record. If an original job directory
reappears, the reservation becomes held again and ordinary authenticated
receipt recovery can inspect it. The scheduler persists invalidation before
further dispatch, so simply losing that evidence again cannot reactivate an
invalidated decision. This first procedure allows only one review per job;
an invalidated review requires further investigation, never a second
automatic release. The raw private receipt is not duplicated in the audit
table.

After local acceptance, start the same profile normally and test a new,
harmless job with a new ID. The retired job remains outcome_unknown with
do_not_replay=true and resource_reservation=released_after_review. A
reservation release does not authorize retrying the old operation with a
new ID. Reconcile the actual system effects separately.

Do not delete the database or edit it directly to repair a lock. Do not
fabricate engine markers. This initial command does not handle present but
incomplete original job directories. Preserve those for investigation.
A downgrade to an older scheduler is fail-closed for released unknown jobs.
