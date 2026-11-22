from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__

SECRET_KEY_RE = re.compile(r"(secret|token|password|passwd|api[_-]?key|private[_-]?key|credential|authorization|cookie)", re.I)
TOKEN_RE = re.compile(r"(?i)\\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{10,}|github_pat_[A-Za-z0-9_]{10,}|xox[baprs]-[A-Za-z0-9-]{10,})\\b")

class ProductError(RuntimeError):
    pass

class PolicyDenied(ProductError):
    pass

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))

def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

def redact(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return "[DEPTH_LIMIT]"
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            out[str(key)] = "[REDACTED]" if SECRET_KEY_RE.search(str(key)) else redact(item, depth + 1)
        return out
    if isinstance(value, list):
        return [redact(x, depth + 1) for x in value[:200]]
    if isinstance(value, tuple):
        return [redact(x, depth + 1) for x in value[:200]]
    if isinstance(value, str):
        clean = TOKEN_RE.sub("[REDACTED TOKEN]", value)
        return clean[:8192] + ("[TRUNCATED]" if len(clean) > 8192 else "")
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact(str(value), depth + 1)

@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    mode: str
    reason: str

class PolicyEngine:
    VALID = {"allow", "deny", "require_authorized"}
    def __init__(self, policy: dict[str, Any] | None = None):
        policy = policy or {}
        self.default = str(policy.get("default", "deny")).strip().lower()
        if self.default not in self.VALID:
            self.default = "deny"
        self.tools = {}
        for key, value in (policy.get("tools") or {}).items():
            mode = str(value).strip().lower()
            self.tools[str(key)] = mode if mode in self.VALID else "deny"
    def decide(self, tool: str, authorized: bool = False) -> PolicyDecision:
        mode = self.tools.get(tool, self.default)
        if mode == "allow":
            return PolicyDecision(True, mode, "POLICY_ALLOW")
        if mode == "require_authorized" and authorized is True:
            return PolicyDecision(True, mode, "AUTHORIZED_TRUE")
        if mode == "require_authorized":
            return PolicyDecision(False, mode, "AUTHORIZED_TRUE_REQUIRED")
        return PolicyDecision(False, mode, "POLICY_DENY")

class AuditLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
    def verify(self) -> dict[str, Any]:
        previous = "GENESIS"
        count = 0
        if not self.path.exists():
            return {"ok": True, "entries": 0, "last_hash": previous}
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for number, raw in enumerate(handle, 1):
                    raw = raw.strip()
                    if not raw:
                        continue
                    item = json.loads(raw)
                    event_hash = str(item.pop("event_hash"))
                    if item.get("prev_hash") != previous:
                        return {"ok": False, "entries": count, "line": number, "error": "PREV_HASH_MISMATCH"}
                    expected = sha256_text(previous + canonical_json(item))
                    if event_hash != expected:
                        return {"ok": False, "entries": count, "line": number, "error": "EVENT_HASH_MISMATCH"}
                    previous = event_hash
                    count += 1
        except Exception as exc:
            return {"ok": False, "entries": count, "error": type(exc).__name__}
        return {"ok": True, "entries": count, "last_hash": previous}
    def append(self, tool: str, status: str, request: Any = None, result: Any = None, principal: str = "chatgpt") -> dict[str, Any]:
        with self._lock:
            current = self.verify()
            if not current.get("ok"):
                raise ProductError("AUDIT_CHAIN_INVALID")
            previous = str(current.get("last_hash", "GENESIS"))
            event = {"event_id": uuid.uuid4().hex, "timestamp": utc_now(), "principal": str(principal), "tool": str(tool), "status": str(status), "request": redact(request), "result": redact(result), "prev_hash": previous}
            event_hash = sha256_text(previous + canonical_json(event))
            stored = dict(event)
            stored["event_hash"] = event_hash
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(canonical_json(stored) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return {"event_id": event["event_id"], "event_hash": event_hash}
    def status(self) -> dict[str, Any]:
        value = self.verify()
        value["path"] = str(self.path)
        return value

    def tail(self, limit: int = 20) -> dict[str, Any]:
        limit = max(1, min(int(limit), 100))
        if not self.path.exists():
            return {"ok": True, "events": []}
        events = []
        with self.path.open("r", encoding="utf-8") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    item = json.loads(raw)
                except Exception:
                    continue
                events.append({
                    "timestamp": item.get("timestamp"),
                    "tool": item.get("tool"),
                    "status": item.get("status"),
                    "event_hash": item.get("event_hash"),
                    "request": item.get("request"),
                    "result": item.get("result"),
                })
        return {"ok": True, "events": events[-limit:]}


class SafeFiles:
    def __init__(self, roots: list[str]):
        self.roots = [Path(x).expanduser().resolve(strict=False) for x in roots]
    def _resolve(self, value: str) -> Path:
        if not self.roots:
            raise ProductError("NO_ALLOWED_ROOTS")
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = self.roots[0] / candidate
        resolved = candidate.resolve(strict=False)
        for root in self.roots:
            try:
                if os.path.commonpath([str(root), str(resolved)]) == str(root):
                    return resolved
            except ValueError:
                pass
        raise ProductError("PATH_OUTSIDE_ALLOWED_ROOTS")
    def list_dir(self, path: str = ".", limit: int = 200) -> dict[str, Any]:
        target = self._resolve(path)
        if not target.is_dir():
            raise ProductError("DIRECTORY_NOT_FOUND")
        limit = max(1, min(int(limit), 500))
        items = []
        for entry in sorted(target.iterdir(), key=lambda p: p.name.lower())[:limit]:
            info = entry.lstat()
            items.append({"name": entry.name, "type": "symlink" if entry.is_symlink() else ("dir" if entry.is_dir() else "file"), "size": info.st_size})
        return {"path": str(target), "items": items, "count": len(items)}
    def read_text(self, path: str, max_bytes: int = 65536) -> dict[str, Any]:
        target = self._resolve(path)
        if not target.is_file():
            raise ProductError("FILE_NOT_FOUND")
        max_bytes = max(1, min(int(max_bytes), 262144))
        size = target.stat().st_size
        if size > max_bytes:
            raise ProductError("FILE_TOO_LARGE")
        data = target.read_bytes()
        if b"\\x00" in data:
            raise ProductError("BINARY_FILE_NOT_ALLOWED")
        text = data.decode("utf-8", errors="strict")
        return {"path": str(target), "size": len(data), "sha256": sha256_bytes(data), "text": text}
    def write_text(self, path: str, text: str, expected_sha256: str = "", max_bytes: int = 262144) -> dict[str, Any]:
        target = self._resolve(path)
        parent = target.parent
        if not parent.is_dir():
            raise ProductError("PARENT_DIRECTORY_NOT_FOUND")
        if target.exists() and target.is_symlink():
            raise ProductError("SYMLINK_WRITE_BLOCKED")
        data = str(text).encode("utf-8")
        if len(data) > int(max_bytes):
            raise ProductError("WRITE_TOO_LARGE")
        before_sha = ""
        if target.exists():
            before_sha = sha256_bytes(target.read_bytes())
            if expected_sha256 and before_sha != expected_sha256:
                raise ProductError("EXPECTED_SHA256_MISMATCH")
        elif expected_sha256:
            raise ProductError("EXPECTED_SHA256_WITHOUT_EXISTING_FILE")
        fd, tmp_name = tempfile.mkstemp(prefix=".framebridge-", dir=str(parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        return {"path": str(target), "size": len(data), "sha256": sha256_bytes(data), "previous_sha256": before_sha}

class MemoryIndex:
    ALLOWED_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml"}
    BLOCKED_NAMES = {"rclone.conf", "runtime.env", ".env"}
    BLOCKED_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
    def __init__(self, db_path: Path, sources: list[str]):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.sources = [Path(x).expanduser().resolve(strict=False) for x in sources]
        self._init_db()
    def _connect(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con
    def _init_db(self):
        with self._connect() as con:
            con.execute("CREATE TABLE IF NOT EXISTS documents (source_path TEXT PRIMARY KEY, title TEXT NOT NULL, sha256 TEXT NOT NULL, mtime REAL NOT NULL, body TEXT NOT NULL, indexed_at TEXT NOT NULL)")
            con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(source_path UNINDEXED, title, body)")
    def _safe_file(self, path: Path, root: Path) -> bool:
        try:
            resolved = path.resolve(strict=True)
            if os.path.commonpath([str(root), str(resolved)]) != str(root):
                return False
        except Exception:
            return False
        if path.is_symlink() or not path.is_file():
            return False
        if path.name in self.BLOCKED_NAMES or path.suffix.lower() in self.BLOCKED_SUFFIXES:
            return False
        if path.suffix.lower() not in self.ALLOWED_SUFFIXES:
            return False
        try:
            return path.stat().st_size <= 2_000_000
        except OSError:
            return False
    def refresh(self) -> dict[str, Any]:
        docs = []
        for root in self.sources:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if not self._safe_file(path, root):
                    continue
                try:
                    body = path.read_text(encoding="utf-8")
                except Exception:
                    continue
                data = body.encode("utf-8")
                lines = body.splitlines()
                title = lines[0].lstrip("# ").strip() if lines else path.name
                docs.append({"source_path": str(path.resolve()), "title": title[:500] or path.name, "sha256": sha256_bytes(data), "mtime": path.stat().st_mtime, "body": body, "indexed_at": utc_now()})
        with self._connect() as con:
            con.execute("DELETE FROM documents")
            con.execute("DELETE FROM docs_fts")
            for item in docs:
                con.execute("INSERT INTO documents(source_path,title,sha256,mtime,body,indexed_at) VALUES (?,?,?,?,?,?)", (item["source_path"], item["title"], item["sha256"], item["mtime"], item["body"], item["indexed_at"]))
                con.execute("INSERT INTO docs_fts(source_path,title,body) VALUES (?,?,?)", (item["source_path"], item["title"], item["body"]))
        return self.status()
    def status(self) -> dict[str, Any]:
        with self._connect() as con:
            row = con.execute("SELECT COUNT(*) AS n, MAX(indexed_at) AS updated FROM documents").fetchone()
        return {"db_path": str(self.db_path), "documents": int(row["n"]), "last_indexed_at": row["updated"], "sources": [str(x) for x in self.sources]}
    def search(self, query: str, limit: int = 8) -> dict[str, Any]:
        tokens = re.findall(r"[A-Za-z0-9_]{2,}", str(query))
        if not tokens:
            return {"query": str(query), "results": []}
        expression = " AND ".join('"' + token.replace('"', '') + '"' for token in tokens[:12])
        limit = max(1, min(int(limit), 20))
        with self._connect() as con:
            rows = con.execute("SELECT source_path,title,snippet(docs_fts,2,'[',']','...',24) AS snippet,bm25(docs_fts) AS rank FROM docs_fts WHERE docs_fts MATCH ? ORDER BY rank LIMIT ?", (expression, limit)).fetchall()
        return {"query": str(query), "results": [{"source_path": row["source_path"], "title": row["title"], "snippet": row["snippet"], "rank": row["rank"]} for row in rows]}
    def read(self, source_path: str, start_line: int = 1, max_lines: int = 120) -> dict[str, Any]:
        start_line = max(1, int(start_line))
        max_lines = max(1, min(int(max_lines), 400))
        with self._connect() as con:
            row = con.execute("SELECT source_path,title,sha256,body,indexed_at FROM documents WHERE source_path=?", (str(source_path),)).fetchone()
        if row is None:
            raise ProductError("MEMORY_DOCUMENT_NOT_FOUND")
        lines = row["body"].splitlines()
        selected = lines[start_line - 1:start_line - 1 + max_lines]
        return {"source_path": row["source_path"], "title": row["title"], "sha256": row["sha256"], "indexed_at": row["indexed_at"], "start_line": start_line, "end_line": start_line + len(selected) - 1 if selected else start_line - 1, "text": "\n".join(selected)}

DEFAULT_POLICY = {"default": "deny", "tools": {"framebridge_product_status": "allow", "framebridge_files_list": "allow", "framebridge_files_read": "allow", "framebridge_files_write": "require_authorized", "framebridge_memory_status": "allow", "framebridge_memory_refresh": "require_authorized", "framebridge_memory_search": "allow", "framebridge_memory_read": "allow", "framebridge_audit_verify": "allow", "framebridge_exec": "require_authorized", "framebridge_cua": "require_authorized"}}

def load_config(path: Path) -> dict[str, Any]:
    if path.is_file():
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ProductError("CONFIG_NOT_OBJECT")
        return value
    return {"node_name": os.uname().nodename, "state_dir": "~/.local/share/framebridge-linux-chatgpt", "allowed_roots": ["~/FrameBridge-Workspace"], "memory_sources": [], "policy": DEFAULT_POLICY}

class AlphaContext:
    def __init__(self, config_path: Path):
        self.config_path = Path(config_path).expanduser()
        self.config = load_config(self.config_path)
        self.node_name = str(self.config.get("node_name") or os.uname().nodename)
        self.state_dir = Path(str(self.config.get("state_dir") or "~/.local/share/framebridge-linux-chatgpt")).expanduser()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.policy = PolicyEngine(self.config.get("policy") or DEFAULT_POLICY)
        self.files = SafeFiles(list(self.config.get("allowed_roots") or []))
        self.memory = MemoryIndex(self.state_dir / "memory.sqlite3", list(self.config.get("memory_sources") or []))
        self.audit = AuditLog(self.state_dir / "audit.jsonl")
    @classmethod
    def from_environment(cls) -> "AlphaContext":
        raw = os.environ.get("FRAMEBRIDGE_ALPHA_CONFIG", "~/.config/framebridge-linux-chatgpt/config.json")
        return cls(Path(raw).expanduser())
    def status(self) -> dict[str, Any]:
        return {"ok": True, "version": __version__, "node_name": self.node_name, "config_path": str(self.config_path), "state_dir": str(self.state_dir), "allowed_roots": [str(x) for x in self.files.roots], "memory": self.memory.status(), "audit": self.audit.status(), "policy_default": self.policy.default}
    def guarded(self, tool: str, authorized: bool, request: dict[str, Any], fn):
        decision = self.policy.decide(tool, authorized)
        if not decision.allowed:
            result = {"ok": False, "error": decision.reason, "policy_mode": decision.mode}
            self.audit.append(tool, "DENY", request=request, result=result)
            return result
        try:
            value = fn()
            result = {"ok": True, "policy_mode": decision.mode, "result": value}
            self.audit.append(tool, "PASS", request=request, result=result)
            return result
        except Exception as exc:
            result = {"ok": False, "error": str(exc), "error_type": type(exc).__name__}
            self.audit.append(tool, "ERROR", request=request, result=result)
            return result
