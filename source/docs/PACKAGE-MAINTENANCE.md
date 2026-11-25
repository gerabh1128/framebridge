# Cold package maintenance

The safe upgrade policy deliberately refuses an active installation. Do not
run APT/dpkg for FrameBridge from a FrameBridge execution job. A delayed
post-install restart is not a safe drain: package files may disappear while
the old adapter or worker is still using them.

Before upgrading or downgrading, review existing job receipts and pending or
detached effects, back up the affected profiles, and stop only the applicable
FrameBridge profiles through their documented local lifecycle. Stop associated
autostart supervisors for the duration of maintenance. Do not start a profile
or reconnect it until the package transaction is complete. A rejected upgrade
does not authorize cancellation, replay, or deletion of the job history.

The new root-owned stable inode at /var/lib/framebridge-package-maintenance/state
has a persistent READY/MAINTENANCE marker and shared/exclusive flock leases.
The installed runtime, adapter, TTY, startup supervisor, background connection
worker, and supported original launch paths hold leases. Maintainer hooks
refuse active participants, inspect recognizable legacy processes, and set
MAINTENANCE before replacement. Configure completes the marker only after
successful configuration. No new deferred automatic restart is scheduled.
After the package transaction succeeds, restart only the intended profiles
using their normal local controls and verify a fresh harmless job. This is
a deliberate availability tradeoff rather than silently risking a lost receipt.

The marker persists across a reboot or interrupted install. A failed configure
or abort after entering maintenance leaves the gate closed; inspect the
package-manager state and logs, and finish the interrupted transaction or
reinstall a verified package with the profiles stopped. Never manually
delete, replace or edit the marker, scheduler database, or locks. Older
releases do not understand the new lease; the initial transition and a
downgrade require operator-enforced quiescence and no concurrent legacy starts.
A legacy downgrade target may not enforce the failed-upgrade maintainer hook.
Process inventory is a refusal check, not proof that arbitrary detached
commands or remote effects are absent. No automatic stop, kill, replay,
or release of an uncertain job is performed. The gate is cooperative
coordination, not a security boundary against a privileged local operator.

Acceptance required: isolated native upgrade/reinstall/downgrade tests,
busy-profile refusal, concurrent launch/upgrade, interrupted dpkg and
recovery, all historical records preserved, actual execution receipt after
restart, and all supported distribution profiles. A source unit test is not
certification of native package lifecycle.
