"""Exercise packaged FastMCP, a paired loopback broker/node, real adapter and tmux.

Run with python3 verify_managed_jobs.py /path/to/extracted/usr/lib/framebridge-expanded
"""
import asyncio
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

prefix = Path(sys.argv[1]).resolve()
os.environ["FRAMEBRIDGE_EXPANDED_PREFIX"] = str(prefix)
sys.path[:0] = [str(prefix / "expanded"), str(prefix / "original/geranas/FrameBridge-Fleet-Hub/vendor")]
from framebridge_expanded import broker, config, node, runtime


def available_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def stop(child):
    child.terminate()
    try:
        child.wait(5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(5)


async def exercise(mcp, folder, producer, client):
    async def call(tool, **kwargs):
        res = await mcp.call_tool(tool, kwargs)
        return res[1] if isinstance(res, tuple) else json.loads(res[0].text)

    async def submit(job, session, resource, script):
        row = await call("framebridge_exec", target="test-node", authorized=True, script=script,
                         timeout_s=40, job_id=job, session_id=session, resources=[resource])
        assert row.get("ok") and row["job_id"] == job, row
        assert row["command_sha256"] == hashlib.sha256(script.encode()).hexdigest()
        return row

    async def status(job):
        return await call("framebridge_jobs", target="test-node", job_id=job, include_stdout=True)

    gate = folder / "release"
    def body(label, code):
        return f"touch '{folder / (label + str(code))}'; printf '{label}\\n'; while [ ! -f '{gate}' ]; do sleep 0.05; done; exit {code}"

    first, second = await asyncio.gather(submit("alpha","chat-a","write:usb",body("first",7)),
                                         submit("beta","chat-b","write:archive",body("second",75)))
    assert first["request_id"] != second["request_id"]
    try:
        for _ in range(10):
            rows = (await call("framebridge_jobs",target="test-node"))["jobs"]
            if sum(r["execution_state"] == "in_flight" for r in rows) == 2:
                break
        else:
            raise AssertionError(rows)
        for _ in range(50):
            if (folder / "first7").is_file() and (folder / "second75").is_file():
                break
            await asyncio.sleep(.1)
        else:
            raise AssertionError("both shells must run before releasing either gate")
        third = await submit("gamma","chat-c","write:usb","printf 'third\\n'")
        assert third["execution_state"] == "queued" and third["blocking_job_id"] == "alpha", third
        # Disconnect/reconnect is represented by a second call with the same stable ID.
        replay = await submit("alpha","chat-a","write:usb",body("first",7))
        assert replay["origin_request_id"] == first["origin_request_id"], replay
        collision = await call("framebridge_exec", target="test-node", authorized=True, job_id="alpha",
                               session_id="chat-a", resources=["write:usb"], script="false", timeout_s=40)
        assert collision.get("error") == "JOB_ID_CONFLICT", collision
    finally:
        gate.touch()
    for job,code,output in (("alpha","7","first"),("beta","75","second"),("gamma","0","third")):
        for _ in range(10):
            row = await status(job)
            if row.get("execution_state") == "completed":
                break
        assert row.get("exit_code") == code and row.get("stdout","").strip() == output, row
        assert row["capture_status"] == "complete" and row["engine_rc"] == 0
        by_id = await call("framebridge_exec_status", target="test-node", request_id=row["origin_request_id"],job_id=job)
        assert by_id.get("execution_state") == "completed" and by_id.get("exit_code")==code, by_id
        print(json.dumps({k:row[k] for k in ("job_id","session_id","execution_state","exit_code","worker_slot","started_at","finished_at","origin_request_id")}),flush=True)
    legacy_gate = folder / "release-legacy"
    legacy_ready = folder / "ready-legacy"
    legacy = asyncio.create_task(call("framebridge_exec", target="test-node", authorized=True,
        job_id="classic", timeout_s=40, script=f"touch '{legacy_ready}'; while [ ! -f '{legacy_gate}' ]; do sleep 0.1; done"))
    try:
        for _ in range(80):
            if legacy_ready.exists():
                break
            await asyncio.sleep(.1)
        assert legacy_ready.exists(), "legacy job did not enter shell"
        before = time.monotonic()
        current = await status("classic")
        assert current["execution_state"] == "in_flight", current
        blocked = await submit("delta","chat-d","write:other","printf 'fourth\\n'")
        assert blocked["blocking_job_id"] == "classic", blocked
        assert not legacy.done()
        print("PASS mixed clients; status and admission while classic EXEC active; seconds =",round(time.monotonic()-before,2),flush=True)
    finally:
        legacy_gate.touch()
    result = await legacy
    assert result.get("exit_code") == "0", result
    for _ in range(10):
        last = await status("delta")
        if last["execution_state"] == "completed":
            break
    assert last.get("stdout","").strip() == "fourth", last
    print("PASS concurrent jobs, resource conflict, duplicate, identity, receipts, stdout and command exit 75",flush=True)
    # These two profiles share only the authenticated adapter socket. Each has its own job tree.
    async def cli_status(job):
        argv = [sys.executable, "-B", "-m", "framebridge_expanded.cli", "--json", "nodes", "jobs",
                "--target", "LOCAL", "--job-id", job, "--include-stdout"]
        result = await asyncio.to_thread(subprocess.run, argv, env=client, capture_output=True,
                                         text=True, timeout=15)
        assert result.returncode == 0, (result.stdout[-800:], result.stderr[-800:])
        envelope = json.loads(result.stdout)
        return envelope.get("result", envelope)

    actual = await status("alpha")
    relative = Path("lanes") / str(actual["worker_slot"]) / "jobs/alpha/stdout.bin"
    producer_file = Path(producer["FBTERM_ROOT"]) / relative
    client_file = Path(client["FBTERM_ROOT"]) / relative
    assert producer_file.read_bytes() == b"first\n" and not client_file.exists()
    def assert_valid(row):
        assert row["stdout"] == "first\n" and row["stdout_integrity"] == "verified", row
        assert row["origin_request_id"] == first["origin_request_id"] and row["exit_code"] == "7"
    assert_valid(await cli_status("alpha"))
    client_file.parent.mkdir(parents=True, exist_ok=True)
    client_file.write_bytes(b"stale\n")
    assert_valid(await cli_status("alpha"))
    classic = await call("framebridge_exec", target="LOCAL", authorized=True, job_id="local-classic",
                         script="printf 'local-classic-ok\\n'", include_stdout=True)
    assert classic.get("stdout_integrity") == "verified" and classic.get("stdout") == "local-classic-ok", classic
    print("PASS CLI with missing and stale caller files; direct LOCAL MCP; producer-only stdout",flush=True)

    producer_file.write_bytes(b"wrong\n")
    for scenario, reason in (("changed", "STDOUT_INTEGRITY_MISMATCH"), ("missing", "STDOUT_NOT_FOUND")):
        if scenario == "missing":
            producer_file.unlink()
        for row in (await cli_status("alpha"), await status("alpha")):
            assert row["origin_request_id"] == first["origin_request_id"] and row["exit_code"] == "7"
            assert row["execution_state"] == "completed" and row.get("stdout_omitted_reason") == reason, row
            assert "stdout" not in row and row["stdout_integrity"] == "unverified", row
        print("PASS", scenario, reason, "same receipt, exit 7, no replacement output", flush=True)



with tempfile.TemporaryDirectory(prefix="fb-jobs-qa-") as temporary, contextlib.ExitStack() as cleanup:
    folder = Path(temporary)
    tmux_root = folder / "tmux"
    tmux_root.mkdir(mode=0o700)
    os.environ.pop("TMUX", None)
    os.environ.pop("TMUX_PANE", None)
    os.environ["TMUX_TMPDIR"] = str(tmux_root)
    port = available_port()
    central, requesting = folder / "central", folder / "node"
    url = f"http://127.0.0.1:{port}"
    for role, name, state in (("central","test-central",central),("node","test-node",requesting)):
        cfg = {"schema_version":1,"role":role,"name":name,"node_id":str(uuid.uuid4()),"headless":"yes",
               "ports":{"mcp":available_port(),"hub":available_port(),"broker":port}, "broker_url":url}
        config.save_config(state,cfg)
    os.environ["FRAMEBRIDGE_EXPANDED_STATE"] = str(central)
    broker.configure(state_dir=central)
    server = broker.make_server(port=port, state_dir=central)
    t = threading.Thread(target=server.serve_forever,daemon=True)
    t.start()
    cleanup.callback(server.server_close)
    cleanup.callback(server.shutdown)
    node_id = node.enroll(url,"test-node",requesting)["node_id"]
    pair_id = next(r["request_id"] for r in broker.list_pending() if r["node_id"]==node_id)
    broker.approve(pair_id,node_id)
    node.poll_enrollment(requesting)
    env = runtime.environment(requesting,prepare=True)
    env["TMUX_TMPDIR"] = str(tmux_root)
    cleanup.callback(shutil.rmtree,env["XDG_RUNTIME_DIR"],True)
    cleanup.callback(subprocess.run,["tmux","kill-server"],env=env,stderr=subprocess.DEVNULL)
    log = cleanup.enter_context((folder/"worker.log").open("w+"))
    for args in (["-m","framebridge_expanded.adapter","serve"],["-c","from framebridge_expanded import node; node.run()"]):
        p = subprocess.Popen([sys.executable,"-B",*args],env=env,stdout=log,stderr=log)
        cleanup.callback(stop,p)
    deadline=time.monotonic()+12
    while time.monotonic()<deadline:
        if (Path(env["FRAMEBRIDGE_TOOL_ROOT"])/"adapter.sock").is_socket() and any(n["ready"] for n in broker.list_confirmed_nodes()):
            break
        time.sleep(0.1)
    client = runtime.environment(central, prepare=True)
    cleanup.callback(shutil.rmtree, client["XDG_RUNTIME_DIR"], True)
    receiver = Path(client["FRAMEBRIDGE_TOOL_ROOT"])
    receiver.mkdir(parents=True, exist_ok=True)
    (receiver / "adapter.sock").symlink_to(Path(env["FRAMEBRIDGE_TOOL_ROOT"]) / "adapter.sock")
    os.environ["FRAMEBRIDGE_TOOL_ROOT"] = str(receiver)
    spec=importlib.util.spec_from_file_location("packaged_mcp",prefix/"portable/front_door_mcp.py")
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        asyncio.run(exercise(module.mcp,folder,env,client))
    except BaseException:
        log.flush()
        log.seek(0)
        print(log.read()[-1500:],file=sys.stderr)
        raise
