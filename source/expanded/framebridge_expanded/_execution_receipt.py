"""Carry confirmed routing and execution state across tool wrappers."""
from __future__ import annotations

import re


ROUTE_FIELDS = (
    "requested_target", "resolved_target", "target", "node", "node_id",
    "hostname", "registered_label", "executed_hostname", "receiver_hostname",
    "transport", "transport_target",
)
RECEIPT_FIELDS = (*ROUTE_FIELDS, "request_id", "mcp_job_id", "job_id",
                  "mcp_script_sha256", "execution_state", "do_not_replay")
UNCERTAIN_EXECUTION = {"in_flight", "outcome_unknown"}
NOT_STARTED_EXECUTION = {"not_started", "expired_before_dispatch", "expired_before_start"}
UNFINISHED_EXECUTION = UNCERTAIN_EXECUTION | NOT_STARTED_EXECUTION


def execution_receipt(value):
    """Select public metadata from the trusted EXEC envelope, never its stdout."""
    receipt = {
        key: value[key] for key in RECEIPT_FIELDS
        if key in value and type(value[key]) in (str, bool, type(None))
    }
    if receipt.get("execution_state") in UNCERTAIN_EXECUTION:
        receipt["do_not_replay"] = True
    return receipt


def execution_error(value, fallback):
    code = value.get("error")
    return code if type(code) is str and re.fullmatch(r"[A-Z0-9_]{1,100}", code) else fallback
