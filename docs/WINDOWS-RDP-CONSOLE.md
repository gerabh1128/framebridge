# Windows private preview: RDP to console handoff

This runbook applies to the separately deployed **Windows private preview** when
its graphical CUA backend requires the signed-in physical console session. It
does not extend the supported Linux release matrix, add native RDP capture, or
change any desktop/session guard.

## Recognize the condition

A browser opened through Remmina/RDP can belong to a different session from the
physical console. FBEXEC may still work from a Windows service in session 0 while
graphical observation is unavailable. A connected node or a running browser is
not proof that its desktop can be captured.

The observed Windows preview checks that its interactive agent's session equals
`WTSGetActiveConsoleSessionId()`. An `INTERACTIVE_CONSOLE_SESSION_REQUIRED` result
is actionable evidence of that mismatch. `SCREEN_EXEC_FAILED` alone is a broader
failure and must not be diagnosed as RDP without checking the actual sessions.

Before changing anything:

1. Confirm the enrolled Windows node and the intended computer.
2. Identify the browser/application session and the owner's RDP session. Session
   IDs change; never reuse a historical ID or assume it is always 2.
3. Confirm that the owner has authorized transferring that session to the physical
   console. Explain that RDP will disconnect and the desktop can become visible
   and unlocked on the physical computer. The observed handoff kept applications
   open; save important work first. No files are deleted or rewritten by this
   procedure, so it does not require a new file backup.
4. Check whether the executing identity has the required session permissions.
   A service account is not the interactive owner merely because it can run
   FBEXEC. Do not add privileges, impersonate a user, retrieve a password, edit
   session guards, or start a second hidden desktop to make the command work.

## Perform the owner's normal handoff

Run the block **inside the owner's existing Windows RDP session**, in a normal
PowerShell terminal opened as administrator after normal Windows approval. Do
not run it from a service/session-0 FBEXEC job or another person's session.

Check the computer, signed-in account and desktop before executing. The script
uses its own current session ID; it does not select an arbitrary remote session.
Use the handoff only after the owner accepted the effects described above.

```powershell
# BEGIN HERE - run in the owner's existing Windows RDP session
$rdpSessionId = (Get-Process -Id $PID).SessionId
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if ($rdpSessionId -le 0) {
    Write-Output 'NO HANDOFF: use the existing interactive RDP session.'
} elseif (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Output 'NO HANDOFF: open PowerShell as administrator through normal Windows approval.'
} else {
    Write-Output ('Handing the current RDP session to the physical console: ' + $rdpSessionId)
    & "$env:SystemRoot\System32\tscon.exe" $rdpSessionId /dest:console
    Write-Output ('TSCON_EXIT_CODE=' + $LASTEXITCODE)
}
# END HERE
```

This is the console handoff command previously observed on the Windows preview;
it is not a guarantee for every Windows/session configuration. Microsoft documents
session ownership and Connect/Full Control requirements for `tscon`. Elevation by
itself does not prove that the selected session exists or the handoff succeeded.
Never put a password in command arguments or use a different account as a shortcut.

## Verify before graphical input

RDP disconnection is an expected effect, not proof of success. Verify all of:

- The command's actual result/exit code where available.
- The expected application remains in the selected Windows session.
- FrameBridge obtains a fresh, authentic complete screen capture of that console.
- The capture shows the intended browser/window before sending any clicks or keys.

If observation still fails, inspect the current session/desktop state and error.
Do not blindly repeat `tscon`, reinstall the product, restart services, weaken a
guard, or report that graphical CUA has been restored without a real capture.

## Common restricted-execution failure

A non-administrative service account can fail to enumerate or connect to an RDP
session. Observed messages included `No session exists for *` and
`SessionID ... not found`, with a nonzero `tscon` exit. They do not establish that
the browser disappeared; the service's session visibility can differ from the
owner's. Stop automated attempts and use the owner's normal elevated terminal as
above. Do not change service identity or access controls as routine troubleshooting.

## Return to remote use

Reconnect normally with Remmina/RDP when the owner wants remote control again.
The desktop/application session can move back to RDP; graphical CUA may then need
another explicitly authorized normal console handoff. Do not automate this on
login, enable autologin, or keep the physical desktop unlocked without the owner's
knowledge. Closing the browser, logging out, and transferring a session are
separate actions; this procedure requests only the transfer.

## Record the outcome

Record the target, current owner/session identity, executing identity/permission
boundary, command result, capture result, and whether the applications remained.
Separate a historical successful handoff from a new attempt. Keep operational
identities and detailed receipts in the owner's private technical memory, not in
public examples. A public runbook contains no machine-specific session ID, IP,
account name, credentials or browser session data.

Reference: [Microsoft `tscon` command documentation](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/tscon).
