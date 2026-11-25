# Runtime layout migration and recovery

FrameBridge prepares four package-managed runtime links in the selected
profile. On Linux, the new package may migrate a dangling old role directory
when the remainder of its path exactly matches the packaged destination.
A custom link, active legacy directory, unexpected regular file, unsafe
directory or unavailable no-replace operation fails closed.

A private per-profile lock serializes all calls to the preparer. Each transaction
creates a private layout-migration directory containing a manifest and the
displaced entries. Linux renameat2 with RENAME_NOREPLACE refuses to replace
any destination. The original entry is retained before the new link is moved
into place. On failure, the preparer attempts restoration using no-replace
operations. Non-cooperating processes operating with the same Unix account
can still change the profile; this is not a sandbox against that account.

Completed transactions have one durable completion marker, committed or
rolled-back. An unfinished journal blocks subsequent preparation rather than
guessing that entries may be deleted. Do not remove the journal, the profile,
the stored command queue, or credentials to force startup. Stop the affected
profile using its documented control interface, preserve the journal and
manifest with owner-only permissions, compare each recorded old and new
target against the installed package layout and the actual profile entries,
and request a reviewed recovery or rollback plan. Never overwrite an
unexpected occupied pathname. The journal may contain local paths and must
not be placed in public artifacts or support messages.

Rollback to an older package may require restoring the saved legacy link
targets after validating the older installed payload. Preserve the exact
verified older package and the consistent job database before upgrading.
No rollback of unrelated system services or data is implied.

The source archive includes the layout migration regression tests. Real
system installation, update, service readiness and retained job output must
also be tested on each supported distribution before production promotion.
