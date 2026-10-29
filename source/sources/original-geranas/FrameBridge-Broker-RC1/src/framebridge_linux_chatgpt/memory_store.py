from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import tempfile
import time
import uuid
import zlib
from pathlib import Path
from typing import Any, Iterator

from .product_core import ProductError, redact
from .seed_capabilities import hardware_summary, list_capabilities, network_inspect, storage_inspect, system_status

FBAM_PROTOCOL = "FBAM/1"
FBM_PROTOCOL = "FBM/1"
FBM_SCHEMA = 1
CODEC_ZLIB = 1
HEADER = struct.Struct(">4sBBII32s")
MAGIC_MANIFEST = b"FBAM"
MAGIC_STATE = b"FBM1"
MAGIC_RECORD = b"FBMR"
MAGIC_INDEX = b"FBI1"
MAX_EVENT_RAW = 65536
MAX_QUERY_EVENTS = 100

READ_CAPABILITIES = tuple(x["name"] for x in list_capabilities())
CHANGE_CAPABILITIES = (
    "workspace.write_text",
    "package.install",
    "package.remove",
    "service.manage",
    "system.hostname",
    "system.timezone",
    "package.refresh",
    "package.upgrade",
    "system.file.write_text",
    "system.directory.ensure",
    "system.file.mode",
    "system.daemon_reload",
)
MEMORY_CAPABILITIES = ("memory.bootstrap", "memory.query", "memory.append", "memory.checkpoint", "memory.stats")

RECORD_TYPE_IDS = {
    "checkpoint": 1,
    "change": 2,
    "incident": 3,
    "decision": 4,
    "artifact": 5,
    "recovery": 6,
    "certification": 7,
    "note": 8,
}
RECORD_TYPE_NAMES = {v: k for k, v in RECORD_TYPE_IDS.items()}
SOURCE_IDS = {"system": 1, "agent": 2, "operator": 3, "import": 4}
SOURCE_NAMES = {v: k for k, v in SOURCE_IDS.items()}
FIELD_REGISTRY = {
    1: "schema", 2: "record_type", 3: "timestamp_ns", 4: "node_id",
    5: "identity", 6: "platform", 7: "hardware", 8: "storage", 9: "network",
    10: "software", 11: "capabilities", 12: "policy", 13: "connections",
    14: "plans", 15: "audit", 16: "changes", 17: "incidents", 18: "decisions",
    19: "artifacts", 20: "recovery", 21: "relationships", 22: "pending",
    23: "freshness", 24: "provenance",
}
DOMAIN_TO_FIELD = {name: fid for fid, name in FIELD_REGISTRY.items() if fid >= 5}
FORBIDDEN_KEY_RE = re.compile(r"(?i)(password|passwd|secret|token|private[_-]?key|otp|credential|client[_-]?secret|api[_-]?key)")


def _uint_head(major: int, value: int) -> bytes:
    if value < 0:
        raise ValueError("CBOR_UINT_NEGATIVE")
    if value < 24:
        return bytes([(major << 5) | value])
    if value <= 0xFF:
        return bytes([(major << 5) | 24, value])
    if value <= 0xFFFF:
        return bytes([(major << 5) | 25]) + struct.pack(">H", value)
    if value <= 0xFFFFFFFF:
        return bytes([(major << 5) | 26]) + struct.pack(">I", value)
    if value <= 0xFFFFFFFFFFFFFFFF:
        return bytes([(major << 5) | 27]) + struct.pack(">Q", value)
    raise ValueError("CBOR_INTEGER_TOO_LARGE")


def cbor_encode(value: Any) -> bytes:
    if value is None:
        return b"\xf6"
    if value is False:
        return b"\xf4"
    if value is True:
        return b"\xf5"
    if isinstance(value, int) and not isinstance(value, bool):
        return _uint_head(0, value) if value >= 0 else _uint_head(1, -1 - value)
    if isinstance(value, float):
        return b"\xfb" + struct.pack(">d", value)
    if isinstance(value, bytes):
        return _uint_head(2, len(value)) + value
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return _uint_head(3, len(raw)) + raw
    if isinstance(value, (list, tuple)):
        parts = [cbor_encode(x) for x in value]
        return _uint_head(4, len(parts)) + b"".join(parts)
    if isinstance(value, dict):
        items = []
        for key, item in value.items():
            ek = cbor_encode(key)
            items.append((len(ek), ek, cbor_encode(item)))
        items.sort(key=lambda x: (x[0], x[1]))
        return _uint_head(5, len(items)) + b"".join(ek + ev for _, ek, ev in items)
    raise TypeError("CBOR_TYPE_UNSUPPORTED:" + type(value).__name__)


def _read_ai(data: bytes, offset: int, ai: int) -> tuple[int, int]:
    if ai < 24:
        return ai, offset
    if ai == 24:
        if offset + 1 > len(data): raise ValueError("CBOR_TRUNCATED")
        return data[offset], offset + 1
    if ai == 25:
        if offset + 2 > len(data): raise ValueError("CBOR_TRUNCATED")
        return struct.unpack_from(">H", data, offset)[0], offset + 2
    if ai == 26:
        if offset + 4 > len(data): raise ValueError("CBOR_TRUNCATED")
        return struct.unpack_from(">I", data, offset)[0], offset + 4
    if ai == 27:
        if offset + 8 > len(data): raise ValueError("CBOR_TRUNCATED")
        return struct.unpack_from(">Q", data, offset)[0], offset + 8
    raise ValueError("CBOR_INDEFINITE_NOT_SUPPORTED")


def _decode_one(data: bytes, offset: int = 0) -> tuple[Any, int]:
    if offset >= len(data):
        raise ValueError("CBOR_TRUNCATED")
    first = data[offset]; offset += 1
    major, ai = first >> 5, first & 31
    if major in (0, 1):
        n, offset = _read_ai(data, offset, ai)
        return (n if major == 0 else -1 - n), offset
    if major in (2, 3):
        n, offset = _read_ai(data, offset, ai)
        end = offset + n
        if end > len(data): raise ValueError("CBOR_TRUNCATED")
        raw = data[offset:end]
        return (raw if major == 2 else raw.decode("utf-8")), end
    if major == 4:
        n, offset = _read_ai(data, offset, ai)
        out = []
        for _ in range(n):
            item, offset = _decode_one(data, offset); out.append(item)
        return out, offset
    if major == 5:
        n, offset = _read_ai(data, offset, ai)
        out = {}
        for _ in range(n):
            key, offset = _decode_one(data, offset)
            val, offset = _decode_one(data, offset)
            out[key] = val
        return out, offset
    if major == 7:
        if ai == 20: return False, offset
        if ai == 21: return True, offset
        if ai == 22: return None, offset
        if ai == 27:
            if offset + 8 > len(data): raise ValueError("CBOR_TRUNCATED")
            return struct.unpack_from(">d", data, offset)[0], offset + 8
    raise ValueError("CBOR_VALUE_UNSUPPORTED")


def cbor_decode(data: bytes) -> Any:
    value, offset = _decode_one(bytes(data), 0)
    if offset != len(data):
        raise ValueError("CBOR_TRAILING_DATA")
    return value


def _pack_container(magic: bytes, value: Any) -> bytes:
    raw = cbor_encode(value)
    comp = zlib.compress(raw, 9)
    digest = hashlib.sha256(raw).digest()
    return HEADER.pack(magic, FBM_SCHEMA, CODEC_ZLIB, len(raw), len(comp), digest) + comp


def _unpack_container(data: bytes, expected_magic: bytes) -> tuple[Any, dict[str, Any]]:
    if len(data) < HEADER.size:
        raise ProductError("MEMORY_CONTAINER_TRUNCATED")
    magic, schema, codec, raw_len, comp_len, digest = HEADER.unpack_from(data, 0)
    if magic != expected_magic: raise ProductError("MEMORY_MAGIC_INVALID")
    if schema != FBM_SCHEMA: raise ProductError("MEMORY_SCHEMA_UNSUPPORTED")
    if codec != CODEC_ZLIB: raise ProductError("MEMORY_CODEC_UNSUPPORTED")
    if len(data) != HEADER.size + comp_len: raise ProductError("MEMORY_CONTAINER_SIZE_INVALID")
    try: raw = zlib.decompress(data[HEADER.size:])
    except Exception as exc: raise ProductError("MEMORY_DECOMPRESS_FAILED") from exc
    if len(raw) != raw_len: raise ProductError("MEMORY_RAW_SIZE_INVALID")
    if hashlib.sha256(raw).digest() != digest: raise ProductError("MEMORY_DIGEST_MISMATCH")
    try: value = cbor_decode(raw)
    except Exception as exc: raise ProductError("MEMORY_CBOR_INVALID") from exc
    return value, {"raw_bytes": raw_len, "compressed_bytes": comp_len, "raw_sha256": digest.hex()}


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(prefix=".fbm-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    data = (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".fbm-config-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data); handle.flush(); os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)


def _uuid_bytes(value: str) -> bytes:
    try: return uuid.UUID(str(value)).bytes
    except Exception: return hashlib.sha256(str(value).encode("utf-8")).digest()[:16]


def _uuid_text(value: bytes) -> str:
    try: return str(uuid.UUID(bytes=bytes(value)))
    except Exception: return bytes(value).hex()


def _human(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {str(k): _human(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_human(v) for v in value]
    return value


def _safe_payload(value: Any, depth: int = 0) -> Any:
    if depth > 8: raise ProductError("MEMORY_PAYLOAD_DEPTH_LIMIT")
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            key = str(key)
            if len(key) > 80: raise ProductError("MEMORY_KEY_TOO_LONG")
            out[key] = "[REDACTED]" if FORBIDDEN_KEY_RE.search(key) else _safe_payload(item, depth + 1)
        return out
    if isinstance(value, list):
        if len(value) > 256: raise ProductError("MEMORY_LIST_TOO_LONG")
        return [_safe_payload(x, depth + 1) for x in value]
    if isinstance(value, str):
        return str(redact(value))[:4096]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:4096]


class MemoryStore:
    def __init__(self, seed):
        self.seed = seed
        self.root = Path(seed.state_dir) / "memory"
        self.state_path = self.root / "node.fbm"
        self.events_path = self.root / "events.fbm"
        self.index_path = self.root / "index.fbi"
        self.manifest_path = self.root / "manifest.fbam"

    def _identity_config(self) -> tuple[dict[str, Any], dict[str, Any]]:
        identity = self.seed.load_identity()
        config = self.seed.load_config()
        if not isinstance(identity, dict) or not isinstance(config, dict):
            raise ProductError("MEMORY_SEED_NOT_INITIALIZED")
        return identity, config

    def _plans(self) -> list[dict[str, Any]]:
        out = []
        plans_dir = Path(self.seed.state_dir) / "plans"
        if not plans_dir.is_dir(): return out
        import json
        for path in sorted(plans_dir.glob("*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                plan = value.get("plan") or {}
                out.append({1: path.stem, 2: value.get("state"), 3: value.get("plan_sha256"), 4: plan.get("title")})
            except Exception:
                out.append({1: path.stem, 2: "INVALID"})
        return out

    @staticmethod
    def _normalize_storage(value: dict[str, Any]) -> dict[int, Any]:
        devices = []
        for item in (value.get("blockdevices") or [])[:128]:
            if not isinstance(item, dict): continue
            devices.append({
                1: item.get("name"), 2: item.get("type"), 3: item.get("size"),
                4: item.get("fstype"), 5: item.get("mountpoints"), 6: item.get("rota"), 7: item.get("model"),
            })
        return {1: value.get("ok") is True, 2: devices}

    @staticmethod
    def _normalize_network(value: dict[str, Any]) -> dict[int, Any]:
        addresses = []
        for item in (value.get("addresses") or [])[:64]:
            if not isinstance(item, dict): continue
            infos = []
            for a in (item.get("addr_info") or [])[:32]:
                if not isinstance(a, dict): continue
                infos.append({1:a.get("family"),2:a.get("local"),3:a.get("prefixlen"),4:a.get("scope")})
            addresses.append({1:item.get("ifname"),2:item.get("operstate"),3:infos})
        routes = []
        for r in (value.get("routes") or [])[:128]:
            if not isinstance(r, dict): continue
            routes.append({1:r.get("dst"),2:r.get("gateway"),3:r.get("dev"),4:r.get("protocol"),5:r.get("metric")})
        return {1:value.get("ok") is True,2:addresses,3:routes}

    def _collect_state(self) -> dict[int, Any]:
        identity, config = self._identity_config()
        doctor = self.seed.doctor()
        sys_state = system_status({})
        hw = hardware_summary({})
        storage = storage_inspect({})
        network = network_inspect({})
        audit = self.seed.audit.verify()
        plans = self._plans()
        pending = [p for p in plans if p.get(2) not in ("ROLLED_BACK",)]
        memory_cfg = config.get("memory") or {}
        return {
            1: FBM_SCHEMA,
            2: 1,
            3: time.time_ns(),
            4: _uuid_bytes(identity.get("node_id", "")),
            5: {
                1: identity.get("hostname"),
                2: identity.get("node_id_short"),
                3: config.get("provider"),
                4: identity.get("node_host_id"),
                5: identity.get("node_public_key_sha256"),
            },
            6: {1: sys_state.get("os_id"), 2: (doctor.get("checks") or {}).get("os_version"), 3: sys_state.get("os_name"), 4: sys_state.get("kernel"), 5: sys_state.get("architecture"), 6: (doctor.get("checks") or {}).get("python")},
            7: {1: hw.get("cpu_model"), 2: hw.get("logical_cpus"), 3: hw.get("memory_total_kib"), 4: hw.get("architecture")},
            8: self._normalize_storage(storage),
            9: self._normalize_network(network),
            10: {1: config.get("seed_version"), 2: (doctor.get("checks") or {}).get("python")},
            11: {1: list(READ_CAPABILITIES), 2: list(CHANGE_CAPABILITIES), 3: list(MEMORY_CAPABILITIES)},
            12: {1: config.get("capability_profile"), 2: True, 3: "plan-required"},
            13: {1: config.get("connection_name"), 2: config.get("connection_mode"), 3: (config.get("pairing") or {}).get("state"), 4: (config.get("pairing") or {}).get("transport")},
            14: plans,
            15: {1: audit.get("ok") is True, 2: audit.get("entries"), 3: audit.get("last_hash")},
            21: [],
            22: pending,
            23: {1: time.time_ns(), 2: "live-collection"},
            24: {1: "framebridge-node", 2: memory_cfg.get("authority", "deployment-defined"), 3: "node-operational"},
        }

    def _manifest_object(self, state: dict[int, Any]) -> dict[int, Any]:
        identity, config = self._identity_config()
        return {
            1: 1,
            2: "FrameBridge",
            3: FBAM_PROTOCOL,
            4: _uuid_bytes(identity.get("node_id", "")),
            5: identity.get("node_id_short"),
            6: identity.get("hostname"),
            7: config.get("seed_version"),
            8: state.get(6),
            9: state.get(7),
            10: state.get(11),
            11: {1: config.get("capability_profile"), 2: True},
            12: {1: True, 2: "read_write", 3: FBM_PROTOCOL, 4: list(MEMORY_CAPABILITIES)},
            13: {1: (config.get("memory") or {}).get("authority", "deployment-defined"), 2: "node-operational"},
            14: time.time_ns(),
        }

    def _read_container(self, path: Path, magic: bytes) -> tuple[Any, dict[str, Any]]:
        try: data = path.read_bytes()
        except FileNotFoundError as exc: raise ProductError("MEMORY_FILE_MISSING") from exc
        return _unpack_container(data, magic)

    def _load_index(self) -> dict[int, Any]:
        if not self.index_path.is_file(): return {1:1,2:0,3:b"",4:[]}
        value, _ = self._read_container(self.index_path, MAGIC_INDEX)
        if not isinstance(value, dict): raise ProductError("MEMORY_INDEX_INVALID")
        return value

    def initialize(self) -> dict[str, Any]:
        _, config = self._identity_config()
        desired = {
            "required": True, "protocol": FBM_PROTOCOL, "manifest_protocol": FBAM_PROTOCOL,
            "mode": "read_write", "authority": (config.get("memory") or {}).get("authority", "deployment-defined"),
        }
        if config.get("memory") != desired:
            config["memory"] = desired
            _atomic_write_json(Path(self.seed.config_path), config)
        self.root.mkdir(parents=True, exist_ok=True); os.chmod(self.root, 0o700)
        if not self.events_path.exists():
            self.events_path.touch(mode=0o600); os.chmod(self.events_path, 0o600)
        if not self.index_path.exists():
            _atomic_write(self.index_path, _pack_container(MAGIC_INDEX, {1:1,2:0,3:b"",4:[]}))
        checkpoint = self.checkpoint()
        if self._load_index().get(2, 0) == 0:
            self._append_event_raw("checkpoint", {"action":"memory.initialize","protocol":FBM_PROTOCOL}, "system")
        return {"ok":True,"protocol":FBM_PROTOCOL,"manifest_protocol":FBAM_PROTOCOL,"required":True,"mode":"read_write","checkpoint":checkpoint,"verify":self.verify()}

    def checkpoint(self) -> dict[str, Any]:
        state = self._collect_state()
        manifest = self._manifest_object(state)
        state_blob = _pack_container(MAGIC_STATE, state)
        manifest_blob = _pack_container(MAGIC_MANIFEST, manifest)
        _atomic_write(self.state_path, state_blob)
        _atomic_write(self.manifest_path, manifest_blob)
        return {
            "ok": True,
            "state_sha256": hashlib.sha256(state_blob).hexdigest(),
            "manifest_sha256": hashlib.sha256(manifest_blob).hexdigest(),
            "state_bytes": len(state_blob),
            "manifest_bytes": len(manifest_blob),
        }

    def ready(self) -> bool:
        try: return self.verify().get("ok") is True
        except Exception: return False

    def require_ready(self) -> None:
        _, config = self._identity_config()
        required = bool((config.get("memory") or {}).get("required"))
        if required and not self.ready(): raise ProductError("MEMORY_REQUIRED_NOT_READY")

    def _append_event_raw(self, record_type: str, data: dict[str, Any], source: str) -> dict[str, Any]:
        if record_type not in RECORD_TYPE_IDS: raise ProductError("MEMORY_RECORD_TYPE_INVALID")
        if source not in SOURCE_IDS: raise ProductError("MEMORY_SOURCE_INVALID")
        identity, _ = self._identity_config()
        safe = _safe_payload(data)
        if not isinstance(safe, dict): raise ProductError("MEMORY_PAYLOAD_NOT_OBJECT")
        index = self._load_index()
        prev = index.get(3) or b""
        record = {1:FBM_SCHEMA,2:RECORD_TYPE_IDS[record_type],3:time.time_ns(),4:_uuid_bytes(identity.get("node_id","")),5:safe,6:SOURCE_IDS[source],7:prev}
        raw = cbor_encode(record)
        if len(raw) > MAX_EVENT_RAW: raise ProductError("MEMORY_EVENT_TOO_LARGE")
        block = _pack_container(MAGIC_RECORD, record)
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        offset = self.events_path.stat().st_size if self.events_path.exists() else 0
        with self.events_path.open("ab") as handle:
            handle.write(block); handle.flush(); os.fsync(handle.fileno())
        os.chmod(self.events_path, 0o600)
        record_hash = hashlib.sha256(raw).digest()
        offsets = list(index.get(4) or [])
        offsets.append(offset)
        offsets = offsets[-1024:]
        new_index = {1:1,2:int(index.get(2) or 0)+1,3:record_hash,4:offsets}
        _atomic_write(self.index_path, _pack_container(MAGIC_INDEX, new_index))
        return {"ok":True,"record_type":record_type,"event_number":new_index[2],"event_sha256":record_hash.hex(),"source":source}

    def append(self, record_type: str, data: dict[str, Any], source: str = "agent") -> dict[str, Any]:
        self.require_ready()
        return self._append_event_raw(record_type, data, source)

    def _iter_events(self) -> Iterator[tuple[dict[int, Any], bytes, int]]:
        if not self.events_path.exists(): return
        data = self.events_path.read_bytes(); off = 0; number = 0
        while off < len(data):
            if off + HEADER.size > len(data): raise ProductError("MEMORY_EVENT_STREAM_TRUNCATED")
            _, _, _, _, comp_len, _ = HEADER.unpack_from(data, off)
            end = off + HEADER.size + comp_len
            if end > len(data): raise ProductError("MEMORY_EVENT_STREAM_TRUNCATED")
            value, _ = _unpack_container(data[off:end], MAGIC_RECORD)
            if not isinstance(value, dict): raise ProductError("MEMORY_EVENT_INVALID")
            number += 1
            yield value, hashlib.sha256(cbor_encode(value)).digest(), off
            off = end

    def verify(self) -> dict[str, Any]:
        state, sm = self._read_container(self.state_path, MAGIC_STATE)
        manifest, mm = self._read_container(self.manifest_path, MAGIC_MANIFEST)
        index = self._load_index()
        prev = b""; count = 0; offsets = []
        for record, record_hash, offset in self._iter_events():
            if record.get(7, b"") != prev: raise ProductError("MEMORY_EVENT_CHAIN_INVALID")
            prev = record_hash; count += 1; offsets.append(offset)
        if int(index.get(2) or 0) != count: raise ProductError("MEMORY_INDEX_COUNT_MISMATCH")
        if (index.get(3) or b"") != prev: raise ProductError("MEMORY_INDEX_HASH_MISMATCH")
        identity, _ = self._identity_config()
        if state.get(4) != _uuid_bytes(identity.get("node_id","")): raise ProductError("MEMORY_NODE_ID_MISMATCH")
        if manifest.get(4) != state.get(4): raise ProductError("MEMORY_MANIFEST_NODE_MISMATCH")
        return {
            "ok":True,"protocol":FBM_PROTOCOL,"manifest_protocol":FBAM_PROTOCOL,"events":count,
            "event_head_sha256":prev.hex() if prev else "GENESIS",
            "state_raw_bytes":sm["raw_bytes"],"state_compressed_bytes":sm["compressed_bytes"],
            "manifest_raw_bytes":mm["raw_bytes"],"manifest_compressed_bytes":mm["compressed_bytes"],
        }

    def bootstrap(self) -> dict[str, Any]:
        self.require_ready()
        manifest, _ = self._read_container(self.manifest_path, MAGIC_MANIFEST)
        platform = manifest.get(8) or {}; hardware = manifest.get(9) or {}; caps = manifest.get(10) or {}; mem = manifest.get(12) or {}; authority = manifest.get(13) or {}
        return {
            "ok": True,
            "protocol": FBAM_PROTOCOL,
            "framebridge": {"product":"FrameBridge","seed_version":manifest.get(7)},
            "node": {"node_id":_uuid_text(manifest.get(4,b"")),"node_id_short":manifest.get(5),"hostname":manifest.get(6)},
            "os": {"id":platform.get(1),"version":platform.get(2),"name":platform.get(3),"kernel":platform.get(4),"architecture":platform.get(5)},
            "hardware": {"cpu_model":hardware.get(1),"logical_cpus":hardware.get(2),"memory_total_kib":hardware.get(3),"architecture":hardware.get(4)},
            "capabilities": {"read":caps.get(1) or [],"change":caps.get(2) or [],"memory":caps.get(3) or []},
            "policy": {"profile":(manifest.get(11) or {}).get(1),"change_requires_plan":True},
            "memory": {"required":mem.get(1) is True,"mode":mem.get(2),"protocol":mem.get(3),"capabilities":mem.get(4) or [],"rule":"READ_BEFORE_DEPENDENT_ACTION__WRITE_AFTER_MATERIAL_CHANGE"},
            "authority": {"mode":authority.get(1),"node_role":authority.get(2)},
            "generated_at_ns": manifest.get(14),
            "manifest_sha256": hashlib.sha256(self.manifest_path.read_bytes()).hexdigest(),
        }

    def query(self, domain: str = "summary", limit: int = 20) -> dict[str, Any]:
        self.require_ready()
        domain = str(domain).strip().lower()
        limit = max(1, min(int(limit), MAX_QUERY_EVENTS))
        state, _ = self._read_container(self.state_path, MAGIC_STATE)
        if domain == "summary":
            verify = self.verify()
            return {"ok":True,"domain":"summary","node_id":_uuid_text(state.get(4,b"")),"hostname":(state.get(5) or {}).get(1),"seed_version":(state.get(10) or {}).get(1),"audit":_human(state.get(15)),"pending":_human(state.get(22)),"memory":verify}
        if domain == "events":
            items = []
            for record, record_hash, _ in self._iter_events():
                items.append({"record_type":RECORD_TYPE_NAMES.get(record.get(2),"unknown"),"timestamp_ns":record.get(3),"source":SOURCE_NAMES.get(record.get(6),"unknown"),"data":_human(record.get(5)),"sha256":record_hash.hex()})
            return {"ok":True,"domain":"events","events":items[-limit:]}
        if domain not in DOMAIN_TO_FIELD: raise ProductError("MEMORY_QUERY_DOMAIN_INVALID")
        return {"ok":True,"domain":domain,"value":_human(state.get(DOMAIN_TO_FIELD[domain]))}

    def stats(self) -> dict[str, Any]:
        self.require_ready(); verify = self.verify()
        return {
            "ok":True,"protocol":FBM_PROTOCOL,
            "files": {
                "node.fbm":self.state_path.stat().st_size,
                "events.fbm":self.events_path.stat().st_size,
                "index.fbi":self.index_path.stat().st_size,
                "manifest.fbam":self.manifest_path.stat().st_size,
            },
            "events":verify.get("events"),"event_head_sha256":verify.get("event_head_sha256"),
            "state_compression_ratio":round(verify["state_compressed_bytes"] / max(1, verify["state_raw_bytes"]), 4),
        }
