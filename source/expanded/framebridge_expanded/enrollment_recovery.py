"""Owner-confirmed offline recovery using the existing signed pairing protocol.

Never grants authorization locally. A broker operator must approve the new
request. No old credential, private key, or pairing response is exported.
"""
from contextlib import ExitStack, closing
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import time

from . import autostart, broker, lifecycle, node, runtime
from .config import load_config
from . import job_reconciliation as guards
from .package_maintenance import guarded


def _need(condition, code):
    if not condition:
        raise node.NodeError(code)


def _legacy_node_directory(path, stack):
    # Older releases could create this parent directory with mode 0755.
    # Never accept writable, foreign-owned or symbolic directories. Hold
    # the verified inode open through the owner's explicit confirmation.
    guards._directory(path)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    stack.callback(os.close, fd)
    initial = os.fstat(fd)
    mode = stat.S_IMODE(initial.st_mode)
    _need(initial.st_uid == os.getuid() and mode in (0o700, 0o755),
          "RECOVERY_UNTRUSTED_NODE_DIRECTORY_MODE")
    _check_node_directory(path, fd, initial)
    return fd, initial, mode


def _check_node_directory(path, fd, initial):
    actual = os.fstat(fd)
    linked = path.lstat()
    _need(stat.S_ISDIR(linked.st_mode)
          and (linked.st_dev, linked.st_ino, linked.st_uid, linked.st_mode)
              == (actual.st_dev, actual.st_ino, actual.st_uid, actual.st_mode)
          and (actual.st_dev, actual.st_ino, actual.st_uid, actual.st_mode)
              == (initial.st_dev, initial.st_ino, initial.st_uid, initial.st_mode),
          "RECOVERY_NODE_DIRECTORY_CHANGED")


def _queue_snapshot(db):
    _need(tuple(db.execute("PRAGMA quick_check").fetchone()) == ("ok",),
          "RECOVERY_QUEUE_INTEGRITY_FAILED")
    _need(db.execute(
        "SELECT COUNT(*) FROM jobs WHERE state NOT IN "
        "('completed','not_started','expired_before_start')"
    ).fetchone()[0] == 0, "RECOVERY_WORK_NOT_QUIESCENT")
    return db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]


def _check_stopped(profile, unit):
    properties = autostart._systemctl("show", unit.name,
                                      "--property=ActiveState,MainPID",
                                      check=False)
    _need(properties.returncode == 0, "RECOVERY_SERVICE_STATE_UNAVAILABLE")
    values = dict(line.split("=", 1) for line in
                  properties.stdout.decode(errors="replace").splitlines() if "=" in line)
    _need(values.get("ActiveState") == "inactive" and values.get("MainPID") == "0",
          "RECOVERY_STOP_MANAGED_NODE_FIRST")
    _need(autostart.manual_stop_requested(profile),
          "RECOVERY_EXPLICIT_SERVICE_STOP_REQUIRED")
    _need(lifecycle._runtime_stopped(runtime.status(profile)),
          "RECOVERY_RUNTIME_NOT_QUIESCENT")


@guarded
def recover_enrollment(profile, *, expected_node_id, expected_fingerprint,
                       expected_broker, confirm):
    """Recover a previously paired, stopped node through a new signed request.

    The operator must first establish that the selected central has lost the
    registration and that the prior central/standby cannot dispatch work.
    A failed or interrupted submission must be reviewed, not replayed.
    """
    _need(os.getuid() != 0 and os.getuid() == os.geteuid(),
          "RECOVERY_RUN_AS_PROFILE_OWNER")
    profile = Path(profile).expanduser().absolute()
    _need(profile.resolve(strict=True) == profile, "RECOVERY_NONCANONICAL_PROFILE")
    guards._directory(profile, private=True)
    config = load_config(profile)
    _need(config["role"] == "node", "RECOVERY_NODE_ROLE_REQUIRED")

    def recover(_state):
        state, directory, unit, installation = lifecycle._owned_setup(profile)
        _need(installation is not None and installation.get("node") is True,
              "RECOVERY_MANAGED_NODE_REQUIRED")
        _check_stopped(state, unit)
        with ExitStack() as stack:
            locks = [stack.enter_context(guards._offline_lock(
                directory / "worker.lock", create=True)),
                stack.enter_context(guards._offline_lock(
                    state / "manager.lock", create=True))]
            locks.append(stack.enter_context(node._operation_lock(state)))
            state, current, ctx, identity = node._context(state)
            url = broker.validate_endpoint(current.get("broker_url", ""))
            _need(broker.validate_endpoint(expected_broker) == url,
                  "RECOVERY_BROKER_MISMATCH")
            saved_ca = current.get("tls_ca") or ""
            env_ca = os.environ.get("FRAMEBRIDGE_TLS_CA") or ""
            _need(not env_ca or (saved_ca and Path(env_ca).resolve() == Path(saved_ca).resolve()),
                  "RECOVERY_TLS_TRUST_OVERRIDE")
            ca_digest = guards._sha(Path(saved_ca)) if saved_ca else None
            _need(identity["node_id"] == expected_node_id == current.get("node_id")
                  and identity["node_public_key_sha256"] == expected_fingerprint
                  and identity["hostname"] == current.get("name"),
                  "RECOVERY_IDENTITY_MISMATCH")
            node_dir_fd, node_dir_stat, node_dir_mode = _legacy_node_directory(ctx.state_dir, stack)
            keydir = ctx.state_dir / "node-identity"
            guards._directory(keydir, private=True)
            guards._file_info((keydir / "node_ed25519").lstat())
            public = node._original("node_identity").read_ed25519_public_key(
                keydir / "node_ed25519.pub")
            _need(public["public_key_sha256"] == expected_fingerprint
                  and public["host_id"] == identity["node_host_id"]
                  and public["public_jwk"] == identity["node_public_jwk"],
                  "RECOVERY_PUBLIC_IDENTITY_MISMATCH")
            store = node._original("pairing").PairingStore(ctx.state_dir)
            old = store.load()
            _need(old.get("state") == "paired"
                  and old.get("broker_confirm_required") is not True
                  and old.get("node_id") == expected_node_id
                  and old.get("broker_base_url", "").rstrip("/") == url,
                  "RECOVERY_CONFIRMED_ASSOCIATION_REQUIRED")
            # Keep only metadata and hashes, never a second copy of the saved
            # authorization. A completed previous recovery is not replayed.
            journal = ctx.state_dir / "enrollment-recovery.json"
            previous = node._recovery_record(ctx, identity, url)
            _need(previous is None or (previous["state"] == "confirmed"
                  and previous.get("new_pair_id") == old.get("pair_id")),
                  "RECOVERY_PREVIOUS_ATTEMPT_REVIEW_REQUIRED")
            identity_path = ctx.identity_path
            config_path = state / "config.json"
            pairing_path = store.path
            for path in (identity_path, config_path, pairing_path):
                guards._file_info(path.lstat())
            before = {p: guards._sha(p) for p in
                      (identity_path, config_path, pairing_path)}
            if previous is not None:
                before[journal] = guards._sha(journal)
            fbroot = state
            for part in ("home", ".local", "share", "framebridge", "fbterm"):
                fbroot /= part
                guards._directory(fbroot)
            guards._directory(fbroot, private=True)
            queue = fbroot / "queue-v2"
            guards._directory(queue, private=True)
            locks.append(stack.enter_context(guards._offline_lock(queue / "scheduler.lock")))
            roots = list(guards._roots(fbroot))
            for root in roots:
                for name in ("controller.lock", "worker.lock"):
                    locks.append(stack.enter_context(
                        guards._offline_lock(root / name, create=True)))
            dbpath = queue / "jobs.sqlite3"
            guards._file_info(dbpath.lstat())
            with closing(sqlite3.connect(dbpath.as_uri() + "?mode=ro", uri=True,
                                          timeout=5)) as db:
                count = _queue_snapshot(db)
                summary = {"node_id": expected_node_id,
                           "fingerprint": expected_fingerprint,
                           "broker": url, "historical_jobs": count,
                           "previous_association_sha256": before[pairing_path],
                           "node_directory_mode_before": format(node_dir_mode, "04o"),
                           "node_directory_mode_after": "0700"}
                _need(confirm(dict(summary)) is True, "RECOVERY_CONFIRMATION_REQUIRED")
                _check_stopped(state, unit)
                guards._check_lock_identities(locks)
                _need(list(guards._roots(fbroot)) == roots
                      and _queue_snapshot(db) == count
                      and all(guards._sha(p) == digest for p, digest in before.items())
                      and (not saved_ca or guards._sha(Path(saved_ca)) == ca_digest)
                      and (os.environ.get("FRAMEBRIDGE_TLS_CA") or "") == env_ca,
                      "RECOVERY_STATE_CHANGED")
                _need(isinstance(old.get("pair_id"), str) and old["pair_id"],
                      "RECOVERY_OLD_ASSOCIATION_ID_UNAVAILABLE")
                _check_node_directory(ctx.state_dir, node_dir_fd, node_dir_stat)
                if node_dir_mode == 0o755:
                    # Owner explicitly confirmed the single-directory
                    # privacy tightening; never change child files or
                    # broaden permissions on failure.
                    os.fchmod(node_dir_fd, 0o700)
                    os.fsync(node_dir_fd)
                guards._directory(ctx.state_dir, private=True)
                evidence = dict(summary, schema=1, state="submission_review_required",
                                created_at=time.time(), approval_required=True,
                                previous_pair_id=old["pair_id"])
                if previous is None:
                    guards._write_new(journal, guards._json_bytes(evidence))
                else:
                    audit = ctx.state_dir / "enrollment-recovery-history"
                    audit.mkdir(mode=0o700, exist_ok=True)
                    guards._directory(audit, private=True)
                    archive = audit / (before[journal] + ".json")
                    raw = guards._read_private(journal, 32768)
                    if archive.exists() or archive.is_symlink():
                        _need(guards._read_private(archive, 32768) == raw,
                              "RECOVERY_AUDIT_CONFLICT")
                    else:
                        guards._write_new(archive, raw)
                    guards._sync_dir(audit)
                    node._write_json(journal, evidence)
                guards._sync_dir(ctx.state_dir)
                try:
                    # Standard proof of possession and verified broker request.
                    # Only a validated pending response may atomically replace
                    # the previous association. No authorization is granted.
                    result = node._submit_enrollment(
                        state, current, ctx, identity, url, identity["hostname"],
                        save_config=False, recover_from=before[pairing_path])
                    _need(result.get("state") == "pair_requested",
                          "RECOVERY_NEW_REQUEST_NOT_PENDING")
                    _need(guards._sha(identity_path) == before[identity_path]
                          and guards._sha(config_path) == before[config_path]
                          and _queue_snapshot(db) == count,
                          "RECOVERY_INVARIANT_CHANGED")
                except Exception:
                    # Remote acceptance may have occurred before a local error.
                    # Never automatically retry or restore the old authorization.
                    raise node.NodeError("RECOVERY_SUBMISSION_REVIEW_REQUIRED") from None
                pending = store.load()
                _need(pending.get("state") == "pair_requested"
                      and pending.get("pair_id") != old["pair_id"],
                      "RECOVERY_NEW_REQUEST_NOT_PENDING")
                evidence.update(state="approval_required",
                                new_pair_id=pending["pair_id"])
                node._write_json(journal, evidence)
                guards._sync_dir(ctx.state_dir)
                return {"ok": True, "state": "approval_required", **summary,
                        "runtime_started": False}
    return autostart._configuration_action(profile, recover)
