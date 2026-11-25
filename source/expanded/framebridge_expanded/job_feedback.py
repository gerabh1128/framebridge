"""Private durable owner-to-agent review requests, outside the EXEC scheduler.

An acknowledgement is only receipt of the incident, never a resolution.
No scripts, stdout, credentials or free text are included.
"""
from pathlib import Path
import hashlib
import json
import os
import re
import tempfile
import time

from . import activity_data as data, owner_jobs as owner

SCHEMA = "framebridge.job-feedback.v1"
HASH = re.compile(r"[0-9a-f]{64}")
STATES = {"queued","in_flight","running","completed","not_started","expired_before_start","outcome_unknown"}
MAX_RECORDS = 2048

def root_for(profile):
    return data._database(profile).parent.parent

def _dir(root, create=False):
    root = owner._directory(Path(root))
    return owner._directory(root / "owner-feedback-v1", create)

def _record_id(job, digest, request):
    return hashlib.sha256((job+"\0"+digest+"\0"+request).encode("ascii")).hexdigest()

def _check_identity(job,digest,request):
    owner._identity(job,digest)
    data.require(isinstance(request,str) and data.JOB_ID.fullmatch(request)
                 and request not in (".",".."), "FEEDBACK_REQUEST_ID_UNVERIFIED")

def _read_record(directory, incident):
    data.require(isinstance(incident,str) and HASH.fullmatch(incident),"INVALID_INCIDENT_ID")
    value = json.loads(owner._read(directory/(incident+".json"),4096))
    data.require(type(value) is dict and set(value)=={
        "schema","incident_id","job_id","command_sha256","request_id","job_state_at_request",
        "requested_at","source"} and value["schema"]==SCHEMA
        and value["incident_id"]==incident and value["source"] in ("local-owner","watchdog")
        and value["job_state_at_request"] in STATES
        and data.timestamp(value["requested_at"]) is not None,"INVALID_INCIDENT_RECORD")
    _check_identity(value["job_id"],value["command_sha256"],value["request_id"])
    data.require(_record_id(value["job_id"],value["command_sha256"],value["request_id"])==incident,
                 "INCIDENT_IDENTITY_MISMATCH")
    return value

def _ack(directory,record):
    try:
        a=json.loads(owner._read(directory/(record["incident_id"]+".ack"),4096))
    except FileNotFoundError:
        return None
    data.require(type(a) is dict and set(a)=={"schema","incident_id","command_sha256","acknowledged_at","source"}
                 and a["schema"]==SCHEMA and a["incident_id"]==record["incident_id"]
                 and a["command_sha256"]==record["command_sha256"]
                 and a["source"]=="authorized-agent" and data.timestamp(a["acknowledged_at"]) is not None,
                 "INVALID_ACKNOWLEDGEMENT")
    return a

def _publish(directory,filename,value):
    with tempfile.NamedTemporaryFile(dir=directory,prefix=".feedback-",delete=False) as stream:
        path=Path(stream.name)
        stream.write(json.dumps(value,sort_keys=True,separators=(",",":")).encode("ascii"))
        stream.flush();os.fsync(stream.fileno())
    try:
        try:os.link(path,directory/filename,follow_symlinks=False)
        except FileExistsError:pass
        fd=os.open(directory,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:path.unlink(missing_ok=True)

def list_root(root):
    root=owner._directory(Path(root))
    try:directory=owner._directory(root/"owner-feedback-v1")
    except FileNotFoundError:
        return {"ok":True,"pending_count":0,"incidents":[],"truncated":False}
    records=[]
    entries=0
    with os.scandir(directory) as iterator:
        for entry in iterator:
            entries+=1
            data.require(entries<=2*MAX_RECORDS+100,"FEEDBACK_CAPACITY_EXCEEDED")
            if not re.fullmatch(r"[a-f0-9]{64}\.json",entry.name):continue
            item=_read_record(directory,entry.name[:-5])
            ack=_ack(directory,item)
            item.update(state="acknowledged" if ack else "pending",
                        acknowledged_at=ack["acknowledged_at"] if ack else None,
                        resolved=False)
            records.append(item)
    records.sort(key=lambda r:(r["state"]!="pending",-r["requested_at"],r["incident_id"]))
    return {"ok":True,"pending_count":sum(r["state"]=="pending" for r in records),
            "incidents":records[:100],"truncated":len(records)>100}

def record(root,job,digest,request_id,state,source):
    _check_identity(job,digest,request_id)
    data.require(state in STATES and source in ("local-owner","watchdog"),"INVALID_FEEDBACK_SOURCE_STATE")
    directory=_dir(root,True)
    incident=_record_id(job,digest,request_id)
    if not (directory/(incident+".json")).exists():
        value={"schema":SCHEMA,"incident_id":incident,"job_id":job,
               "command_sha256":digest,"request_id":request_id,"job_state_at_request":state,
               "requested_at":time.time(),"source":source}
        _publish(directory,incident+".json",value)
    stored=_read_record(directory,incident)
    ack=_ack(directory,stored)
    return dict(stored,state="acknowledged" if ack else "pending",
                acknowledged_at=ack["acknowledged_at"] if ack else None,resolved=False)

def request(profile,job,expected_digest):
    row=owner.row(profile,job)
    data.require(row["digest"]==expected_digest and row["state"] in STATES,"JOB_IDENTITY_CHANGED")
    return record(root_for(profile),job,expected_digest,row["request"],row["state"],"local-owner")

def acknowledge_root(root,incident,digest,*,authorized=False):
    data.require(authorized is True,"AGENT_ACKNOWLEDGEMENT_NOT_AUTHORIZED")
    directory=_dir(root)
    record=_read_record(directory,incident)
    data.require(isinstance(digest,str) and record["command_sha256"]==digest,"INCIDENT_DIGEST_MISMATCH")
    if _ack(directory,record) is None:
        _publish(directory,incident+".ack",{"schema":SCHEMA,"incident_id":incident,
            "command_sha256":digest,"acknowledged_at":time.time(),"source":"authorized-agent"})
    ack=_ack(directory,record)
    data.require(ack is not None,"INCIDENT_ACK_UNVERIFIED")
    return {"ok":True,"incident_id":incident,"state":"acknowledged","resolved":False,
            "job_id":record["job_id"],"command_sha256":digest,"acknowledged_at":ack["acknowledged_at"]}
