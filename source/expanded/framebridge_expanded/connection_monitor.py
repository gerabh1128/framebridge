"""Private connection logs and evidence-based terminal status.

This module monitors the packaged tunnel client. A ready tunnel is not evidence
that the intended ChatGPT account has completed consent or made a tool call.
"""
from __future__ import annotations

import datetime
import fcntl
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import selectors
import stat
import threading
import time
import uuid
import unicodedata
from urllib.parse import urlsplit

MAX_LINE = 64 * 1024
MAX_LOG = 2 * 1024 * 1024
LOG_COPIES = 3
POLL_FRESH_SECONDS = 75
METRICS_SCAN_BYTES = 2 * 1024 * 1024
METRICS_LINE_BYTES = 16 * 1024
METRICS_BODY_SECONDS = 0.6
_POLL_METRIC = b"commands_poll_last_successful_timestamp_seconds"
_METRIC_NAME = re.compile(rb"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL = rb'[a-zA-Z_][a-zA-Z0-9_]*[ \t]*=[ \t]*"(?:[^"\\\r\n]|\\["\\n])*"'
_LABELS = rb'\{[ \t]*(?:' + _LABEL + rb'(?:[ \t]*,[ \t]*' + _LABEL + rb')*[ \t]*,?)?[ \t]*\}'
_POLL_SAMPLE = re.compile(_POLL_METRIC + rb'(?:' + _LABELS + rb')?[ \t]+([^ \t\r\n]+)(?:[ \t]+[+-]?\d+)?[ \t]*')
_METRIC_NUMBER = re.compile(rb'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?')
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")


class ConnectionMonitorError(RuntimeError):
    """A safe public error; details belong only in sanitized logs."""


def _private_directory(path):
    path = Path(path).absolute()
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise ConnectionMonitorError("The private connection directory is unsafe.")
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ConnectionMonitorError("The private connection directory is unsafe.")
    return path


def redact(text, secret):
    """Normalize terminal controls before redacting and persisting a record."""
    text = _ANSI.sub("", str(text))
    text = "".join(c for c in text if c in "\n\t" or not unicodedata.category(c).startswith("C"))
    if secret:
        text = text.replace(secret, "[REDACTED]")
        # A child may exit after emitting only a credential prefix.
        tail = text.rstrip("\r\n")
        suffix = text[len(tail):]
        for length in range(min(len(secret) - 1, len(tail)), 7, -1):
            if tail.endswith(secret[:length]):
                text = tail[:-length] + "[REDACTED]" + suffix
                break
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}", "[REDACTED]", text)
    # Preserve a useful proxy/server location without credential-bearing userinfo.
    text = re.sub(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s?#]*@", r"\1[REDACTED]@", text)
    # Conservative whole-tail redaction prevents partial Cookie/password leaks.
    text = re.sub(r"(?im)(\b(?:authorization|proxy-authorization|api[_-]?key|access[_-]?token|"
                  r"refresh[_-]?token|password|passwd|cookie|set-cookie)\b[\"']?\s*[:=]\s*).*$",
                  r"\1[REDACTED]", text)
    return text


class PrivateLog:
    """A bounded rotating log, protected against symlinks and shared files."""
    def __init__(self, state, secret, *, max_bytes=MAX_LOG):
        root = _private_directory(state)
        root = _private_directory(root / "logs")
        self.directory = _private_directory(root / "chatgpt")
        self.path = self.directory / "connection.log"
        self.secret = secret
        self.max_bytes = max_bytes
        self.directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self.lock_fd = self._open("connection.lock", os.O_RDWR | os.O_CREAT)
        except Exception:
            os.close(self.directory_fd)
            raise
        self.mutex = threading.Lock()

    def _open(self, name, flags):
        fd = os.open(name, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600, dir_fd=self.directory_fd)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            os.close(fd)
            raise ConnectionMonitorError("The private connection log is unsafe.")
        return fd

    def _validate_existing(self, name):
        try:
            fd = self._open(name, os.O_RDONLY)
        except FileNotFoundError:
            return False
        os.close(fd)
        return True

    def write(self, text):
        clean = redact(str(text), self.secret)
        # All callers provide a complete bounded logical record.
        if len(clean.encode("utf-8")) > MAX_LINE * 4:
            clean = "[Oversized log record omitted]"
        stamp = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        data = (stamp + " " + clean.rstrip("\r\n") + "\n").encode("utf-8", errors="replace")
        with self.mutex:
            deadline = time.monotonic() + 1.5
            while True:
                try:
                    fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ConnectionMonitorError("The private connection log is busy.") from None
                    time.sleep(0.02)
            try:
                fd = self._open("connection.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT)
                try:
                    size = os.fstat(fd).st_size
                finally:
                    os.close(fd)
                if size + len(data) > self.max_bytes:
                    existing = {i: self._validate_existing("connection.log" + ("." + str(i) if i else ""))
                                for i in range(LOG_COPIES)}
                    if existing[LOG_COPIES - 1]:
                        os.unlink("connection.log." + str(LOG_COPIES - 1), dir_fd=self.directory_fd)
                    for i in range(LOG_COPIES - 2, -1, -1):
                        if existing[i]:
                            os.rename("connection.log" + ("." + str(i) if i else ""),
                                      "connection.log." + str(i + 1),
                                      src_dir_fd=self.directory_fd, dst_dir_fd=self.directory_fd)
                fd = self._open("connection.log", os.O_WRONLY | os.O_APPEND | os.O_CREAT)
                try:
                    view = memoryview(data)
                    while view:
                        count = os.write(fd, view)
                        if count <= 0:
                            raise OSError("Short private log write")
                        view = view[count:]
                finally:
                    os.close(fd)
            finally:
                fcntl.flock(self.lock_fd, fcntl.LOCK_UN)

    def close(self):
        self.secret = None
        for name in ("lock_fd", "directory_fd"):
            fd = getattr(self, name, None)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)


class Attempt:
    def __init__(self, state):
        root = _private_directory(state)
        self.root = _private_directory(root / "connection-runtime")
        self.directory = _private_directory(self.root / uuid.uuid4().hex)
        self.health_file = self.directory / "health.url"

    def close(self):
        # Remove only this attempt's own known file, never a symlink or unknown data.
        try:
            info = self.health_file.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1:
                self.health_file.unlink()
        except FileNotFoundError:
            pass
        try:
            self.directory.rmdir()
        except OSError:
            pass


def health_endpoint(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or info.st_mode & 0o022 or not 0 < info.st_size <= 512):
            raise ConnectionMonitorError("The tunnel health endpoint is unsafe.")
        text = os.read(fd, 513).decode("ascii").strip()
    finally:
        os.close(fd)
    value = urlsplit(text)
    try:
        address = ipaddress.ip_address(value.hostname or "")
        port = value.port
    except ValueError:
        raise ConnectionMonitorError("The tunnel health endpoint is unsafe.") from None
    if (value.scheme != "http" or not address.is_loopback or not port or "%" in text
            or value.username or value.password or value.path not in ("", "/")
            or value.query or value.fragment):
        raise ConnectionMonitorError("The tunnel health endpoint is unsafe.")
    return str(address), port


def _get(endpoint, path, limit):
    # Direct literal-loopback HTTP: no DNS, inherited proxy, or redirects.
    connection = http.client.HTTPConnection(*endpoint, timeout=0.6)
    try:
        connection.request("GET", path, headers={"Connection": "close"})
        response = connection.getresponse()
        body = response.read(limit + 1)
        if len(body) > limit:
            raise ConnectionMonitorError("The tunnel health response is too large.")
        return response.status, body
    finally:
        connection.close()


def _poll_timestamp(response):
    """Scan a bounded Prometheus metric group without retaining the whole body.

    Prometheus text exposition groups all samples of a metric together. Once
    this group ends, unrelated metrics cannot improve the polling evidence.
    read1() and a body deadline also bound a response with slowly arriving lines.
    """
    deadline = time.monotonic() + METRICS_BODY_SECONDS
    pending = bytearray()
    scanned = 0
    recent = None

    def sample(line):
        nonlocal recent
        if len(line) > METRICS_LINE_BYTES:
            raise ConnectionMonitorError("A tunnel metric line is too large.")
        line = line.strip(b" \t\r")
        if not line or line.startswith(b"#"):
            return False
        name = _METRIC_NAME.match(line)
        if not name:
            raise ConnectionMonitorError("The tunnel metrics have an unexpected format.")
        if name.group() != _POLL_METRIC:
            return recent is not None
        match = _POLL_SAMPLE.fullmatch(line)
        if not match or not _METRIC_NUMBER.fullmatch(match.group(1)):
            raise ConnectionMonitorError("The polling metric has an unexpected format.")
        value = float(match.group(1))
        if not math.isfinite(value):
            raise ConnectionMonitorError("The polling metric is not finite.")
        recent = value if recent is None else max(recent, value)
        return False

    while True:
        if time.monotonic() >= deadline:
            raise ConnectionMonitorError("The tunnel metrics took too long.")
        chunk = response.read1(min(4096, METRICS_SCAN_BYTES - scanned + 1))
        if time.monotonic() >= deadline:
            raise ConnectionMonitorError("The tunnel metrics took too long.")
        if not chunk:
            if pending:
                sample(bytes(pending))
            return recent
        scanned += len(chunk)
        if scanned > METRICS_SCAN_BYTES:
            raise ConnectionMonitorError("The polling metric was not found within the scan limit.")
        pending.extend(chunk)
        while (ending := pending.find(b"\n")) >= 0:
            line = bytes(pending[:ending])
            del pending[:ending + 1]
            if sample(line):
                return recent
        if len(pending) > METRICS_LINE_BYTES:
            raise ConnectionMonitorError("A tunnel metric line is too large.")


def _get_poll_timestamp(endpoint):
    # Same direct literal-loopback contract as _get(): no proxy or redirect.
    connection = http.client.HTTPConnection(*endpoint, timeout=0.6)
    try:
        connection.request("GET", "/metrics", headers={"Connection": "close"})
        with connection.getresponse() as response:
            return response.status, _poll_timestamp(response) if response.status == 200 else None
    finally:
        connection.close()


def probe(health_file, expected_tunnel, expected_mcp, started, *, now=None):
    """Return a public state and private detail from the pinned client contract."""
    now = time.time() if now is None else now
    try:
        endpoint = health_endpoint(health_file)
    except FileNotFoundError:
        return "starting", "Waiting for the tunnel health endpoint."
    except (OSError, ValueError, ConnectionMonitorError):
        return "checking", "The tunnel health endpoint is not available or valid."
    try:
        code, body = _get(endpoint, "/readyz", 8192)
        message = body.decode("utf-8", errors="replace").strip()
        if code != 200 or message != "ready":
            return "checking", "MCP readiness is pending: " + message
        code, body = _get(endpoint, "/api/status", 65536)
        if code != 200:
            return "checking", "Tunnel status is not available."
        status = json.loads(body)
        if not isinstance(status, dict):
            return "checking", "Tunnel status has an unexpected format."
        if (status.get("control_plane_tunnel_id") != expected_tunnel
                or status.get("mcp_server_url") != expected_mcp):
            return "checking", "Tunnel status does not match this profile."
        created = datetime.datetime.fromisoformat(status["started_at"].replace("Z", "+00:00")).timestamp()
        if not started - 5 <= created <= now + 5:
            return "checking", "Tunnel status belongs to a different startup."
        metadata = status.get("tunnel_metadata") or {}
        if not isinstance(metadata, dict):
            return "checking", "Tunnel metadata has an unexpected format."
        if status.get("tunnel_metadata_error"):
            detail = str(status["tunnel_metadata_error"])
            if re.search(r"\b(?:401|403|unauthorized|forbidden)\b", detail, re.I):
                return "rejected", "Tunnel access was rejected: " + detail
            return "access", "Tunnel metadata request failed: " + detail
        if metadata.get("ID") != expected_tunnel:
            return "access", "Waiting for authenticated tunnel metadata."
        code, recent = _get_poll_timestamp(endpoint)
        if code != 200:
            return "access", "Waiting for authenticated polling evidence."
        if recent is None:
            return "access", "Waiting for authenticated polling evidence."
        if recent < started - 5 or recent > now + 5 or now - recent > POLL_FRESH_SECONDS:
            return "access", "A successful authenticated poll has not been confirmed recently."
        return "ready", "Local MCP initialization, tunnel identity, metadata and recent authenticated polling verified."
    except (OSError, ValueError, KeyError, TypeError, http.client.HTTPException, ConnectionMonitorError):
        return "checking", "The tunnel health check is temporarily unavailable."


class Progress:
    def __init__(self, output):
        self.output = output
        self.tty = bool(getattr(output, "isatty", lambda: False)())
        self.state = None
        self.message = ""
        self.width = 0
        self.index = 0

    def change(self, state, message):
        if self.state == state and self.message == message:
            return
        self.clear()
        self.state, self.message = state, message
        self.output.write(message + "\n")
        self.output.flush()

    def clear(self):
        if self.tty and self.width:
            self.output.write("\r" + " " * self.width + "\r")
            self.width = 0

    def tick(self):
        if self.tty and self.state not in {"ready", "closed", "error", "rejected", "unverified"}:
            text = "Connecting " + "|/-\\"[self.index % 4] + "  Press Ctrl+C to cancel."
            self.index += 1
            self.width = max(self.width, len(text))
            self.output.write("\r" + text.ljust(self.width))
            self.output.flush()


def capture_output(stream, log, errors, stop):
    """Drain binary output without ever forwarding it to the terminal."""
    buffer = bytearray()
    dropping = False
    try:
        fd = stream.fileno()
        selector = selectors.DefaultSelector()
        selector.register(fd, selectors.EVENT_READ)
        while True:
            if not selector.select(timeout=0.1):
                if stop.is_set():
                    break
                continue
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            for part in chunk.splitlines(keepends=True):
                ending = part.endswith(b"\n") or part.endswith(b"\r")
                if not dropping:
                    if len(buffer) + len(part) > MAX_LINE:
                        buffer.clear()
                        dropping = True
                    else:
                        buffer.extend(part)
                if ending:
                    if dropping:
                        log.write("[Oversized client output line omitted]")
                    else:
                        log.write(buffer.decode("utf-8", errors="replace"))
                    buffer.clear()
                    dropping = False
        if buffer and not dropping:
            log.write(buffer.decode("utf-8", errors="replace"))
        elif dropping:
            log.write("[Oversized client output line omitted]")
    except Exception:
        errors.append("Connection output could not be written to its private log.")
    finally:
        if "selector" in locals():
            selector.close()
        if stop.is_set():
            # If the main thread timed out waiting for draining, this thread
            # remains the owner of its live stream/log until it finishes.
            try:
                stream.close()
            except Exception:
                errors.append("Connection output stream cleanup failed.")
            try:
                log.close()
            except Exception:
                errors.append("Private connection log cleanup failed.")
