"""Local owner actions only; immutable original records, no automatic retry."""
from __future__ import annotations
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time
import uuid

from . import activity_data as data

SCHEMA="framebridge.owner-cancel.v1"
HASH=re.compile(r"[a-f0-9]{64}")
CANCEL_STATES=frozenset(("queued","in_flight","running"))
FINAL_STATES=frozenset(("completed","not_started","expired_before_start"))

def _read(path, maximum):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,"rb") as f:
        st=os.fstat(f.fileno())
        data.require(stat.S_ISREG(st.st_mode) and st.st_uid==os.getuid()
                     and st.st_nlink==1 and not st.st_mode&0o077 and st.st_size<=maximum,
                     "JOB_EVIDENCE_UNSAFE")
        result=f.read(maximum+1)
        data.require(len(result)<=maximum,"JOB_EVIDENCE_TOO_LARGE")
        return result

def _directory(path, create=False):
    if create:path.mkdir(mode=0o700,exist_ok=True)
    info=path.lstat()
    data.require(stat.S_ISDIR(info.st_mode) and info.st_uid==os.getuid()
                 and stat.S_IMODE(info.st_mode)==0o700,"OWNER_ACTION_DIRECTORY_UNSAFE")
    return path

def _identity(job,digest):
    data.require(isinstance(job,str) and data.JOB_ID.fullmatch(job)
                 and job not in (".","..") and isinstance(digest,str)
                 and HASH.fullmatch(digest),"JOB_IDENTITY_UNVERIFIED")

def row(p,job,with_script=False):
    data.require(isinstance(job,str) and data.JOB_ID.fullmatch(job) and job not in (".",".."),"JOB_ID_UNVERIFIED")
    db=data._database(p)
    with closing(sqlite3.connect(db.as_uri()+"?mode=ro",uri=True,timeout=.5)) as c:
        c.row_factory=sqlite3.Row
        c.execute("PRAGMA query_only=ON")
        fields="id,state,digest,request,slot,timeout,created,started,finished,result"
        if with_script:fields+=",script"
        r=c.execute("SELECT "+fields+" FROM jobs WHERE id=?",(job,)).fetchone()
        data.require(r is not None,"JOB_NOT_FOUND")
        v=dict(r)
    _identity(v["id"],v["digest"])
    return v

def request_path(queue,job):
    return Path(queue)/"owner-cancel-v1"/(job+".json")

def is_requested(queue,job,digest):
    try:
        _identity(job,digest)
        d=_directory(Path(queue)/"owner-cancel-v1")
        v=json.loads(_read(d/(job+".json"),4096))
        return (type(v) is dict and v.get("schema")==SCHEMA and v.get("job_id")==job
                and v.get("command_sha256")==digest)
    except (OSError,ValueError,data.ObservationError):return False

def request_cancel(p,job,expected_digest):
    r=row(p,job)
    data.require(r["digest"]==expected_digest and r["state"] in CANCEL_STATES,"JOB_NO_LONGER_CANCELLABLE")
    queue=data._database(p).parent
    d=_directory(queue/"owner-cancel-v1",True)
    path=d/(job+".json")
    value={"schema":SCHEMA,"job_id":job,"command_sha256":expected_digest,
           "requested_at":time.time(),"source":"local-owner-ui"}
    if path.exists():
        data.require(is_requested(queue,job,expected_digest),"CANCEL_REQUEST_CONFLICT")
        return {"state":"CANCELLATION_REQUESTED","job_id":job,"existing":True}
    with tempfile.NamedTemporaryFile(dir=d,prefix=".request-",delete=False) as f:
        temporary=Path(f.name)
        f.write(json.dumps(value,separators=(",",":")).encode());f.flush();os.fsync(f.fileno())
    try:
        try:os.link(temporary,path,follow_symlinks=False)
        except FileExistsError:
            data.require(is_requested(queue,job,expected_digest),"CANCEL_REQUEST_CONFLICT")
        finally:temporary.unlink(missing_ok=True)
        fd=os.open(d,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:temporary.unlink(missing_ok=True)
    data.require(is_requested(queue,job,expected_digest),"CANCEL_REQUEST_UNVERIFIED")
    return {"state":"CANCELLATION_REQUESTED","job_id":job}

def original_script(p,job):
    r=row(p,job,True)
    if r["state"]=="queued" and isinstance(r.get("script"),str):
        raw=r["script"].encode("utf-8")
    else:
        root=data._database(p).parent.parent
        slot=r["slot"]
        data.require(slot is None or type(slot) is int and 0<=slot<=8,"JOB_SLOT_UNVERIFIED")
        if slot:
            root=_directory(_directory(root/"lanes")/str(slot))
        jobs=_directory(root/"jobs")
        path=_directory(jobs/job)
        raw=_read(path/"command.bash",262144)
    data.require(0<len(raw)<=262144 and hashlib.sha256(raw).hexdigest()==r["digest"],"JOB_COMMAND_HASH_MISMATCH")
    return r,raw.decode("utf-8")

def details(p,job):
    r=row(p,job)
    try:receipt=json.loads(r["result"] or "{}")
    except (ValueError,TypeError):receipt={}
    r.pop("result",None)
    for k in ("exit_code","capture_status","owner_cancel_signal_sent","owner_cancel_before_exec","error"):
        v=receipt.get(k)
        if type(v) in (str,int,bool) and len(str(v))<=120:r[k]=data.safe_text(v) if isinstance(v,str) else v
    r["cancellation_requested"]=is_requested(data._database(p).parent,job,r["digest"])
    return r

def submit_edited(p,job,expected_digest,script,*,confirmed=False):
    # The editor never modifies an existing record or submits on an unresolved outcome.
    from . import config, runtime, jobs
    data.require(confirmed is True,"OWNER_CONFIRMATION_REQUIRED")
    old=row(p,job)
    data.require(old["digest"]==expected_digest and old["state"] in FINAL_STATES,"ORIGINAL_JOB_NOT_TERMINAL")
    data.require(isinstance(script,str) and 0<len(script.encode())<=262144,"SCRIPT_INVALID")
    data.require(config.state_path()==p.path,"PROFILE_BINDING_MISMATCH")
    data.check_profile(p)
    new="owner-edit-"+uuid.uuid4().hex
    request="owner-ui-"+uuid.uuid4().hex
    digest=hashlib.sha256(script.encode()).hexdigest()
    timeout=old["timeout"]
    data.require(type(timeout) is int and 1<=timeout<=600,"INVALID_TIMEOUT")
    envelope={"version":1,"mode":"EXEC","authorized":True,"job_id":request,
       "origin_request_id":request,"request_type":jobs.WIRE_TYPE,
       "script":jobs.WIRE_SCRIPT,"sha256":hashlib.sha256(jobs.WIRE_SCRIPT.encode()).hexdigest(),
       "timeout_s":timeout,"start_before":time.time()+timeout,
       "job_action":"submit","session_id":"local-owner","subject_job_id":new,
       "job_script":script,"job_sha256":digest,"resources":[]}
    # The same private Unix adapter socket and normal job queue are used.
    # Never replay an uncertain submission.
    try:result=runtime.adapter_request(envelope)
    except Exception:return {"state":"SUBMISSION_UNCONFIRMED","job_id":new,"do_not_replay":True}
    if result.get("job_id")!=new or result.get("command_sha256")!=digest or result.get("ok") is not True:
        return {"state":"SUBMISSION_NOT_VERIFIED","job_id":new,"do_not_replay":True}
    return {"state":result.get("execution_state","unverified"),"job_id":new}
