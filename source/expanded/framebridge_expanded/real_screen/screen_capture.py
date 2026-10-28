#!/usr/bin/env python3
"""Capture actual Linux VT or X11 root pixels, never rendered terminal text.

Active X11 sessions use XGetImage, and Wayland fails explicitly. The framebuffer
backend is never used as a substitute for an active graphical session.
"""
from __future__ import annotations

import array
import base64
import ctypes
import ctypes.util
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import pwd
import re
import socket
import stat
import struct
import subprocess
import sys
import threading
import uuid
import zlib

MAX_PIXELS = 4096 * 2160
MAX_FRAMEBUFFER_BYTES = 128 * 1024 * 1024
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_CHUNK_BYTES = 45000
CAPTURE_ID = re.compile(r"[0-9a-f]{32}\Z")
SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
DISPLAY_ID = re.compile(r":[0-9]{1,5}(?:\.[0-9]{1,3})?\Z")
_X11_LOCK = threading.RLock()
FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602


class CaptureError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def require(condition: bool, code: str) -> None:
    if not condition:
        raise CaptureError(code)


@dataclass(frozen=True)
class Foreground:
    active_vt: str
    session_id: str
    uid: int
    leader: int
    session_type: str
    tty: str
    seat: str
    vtnr: int
    display: str = ""


def choose_foreground(active_vt: str, sessions: list[dict], seat: str) -> Foreground:
    """Require an identifiable active text session on the actual foreground VT."""
    require(re.fullmatch(r"tty[1-9][0-9]*", active_vt) is not None, "ACTIVE_VT_UNVERIFIED")
    active = [s for s in sessions if s.get("Seat") == seat and s.get("Active") == "yes"
              and s.get("Remote") == "no"]
    require(not any(s.get("Type") in {"x11", "wayland"} for s in active),
            "GRAPHICAL_CAPTURE_REQUIRED")
    matching = [s for s in active if s.get("Type") == "tty" and s.get("TTY") == active_vt
                and s.get("VTNr") == active_vt[3:]]
    require(len(matching) == 1, "FOREGROUND_SESSION_UNVERIFIED")
    s = matching[0]
    try:
        result = Foreground(active_vt, s["Id"], int(s["User"]), int(s["Leader"]),
                            s["Type"], s["TTY"], s["Seat"], int(s["VTNr"]))
    except (KeyError, TypeError, ValueError):
        raise CaptureError("FOREGROUND_SESSION_INVALID") from None
    require(SESSION_ID.fullmatch(result.session_id) is not None and result.leader > 0,
            "FOREGROUND_SESSION_INVALID")
    return result


def choose_display_foreground(active_vt: str, sessions: list[dict], seat: str) -> Foreground:
    active = [s for s in sessions if s.get("Seat") == seat and s.get("Active") == "yes"
              and s.get("Remote") == "no"]
    require(len(active) == 1, "FOREGROUND_SESSION_UNVERIFIED")
    s = active[0]
    if s.get("Type") == "tty":
        return choose_foreground(active_vt, sessions, seat)
    require(s.get("Type") != "wayland", "WAYLAND_CAPTURE_UNSUPPORTED")
    require(s.get("Type") == "x11", "SCREEN_TYPE_UNSUPPORTED")
    # LightDM may leave TTY empty while supplying the actual numeric VT.
    # Missing metadata or an explicit contradictory TTY is still rejected.
    require(re.fullmatch(r"tty[1-9][0-9]*", active_vt) is not None
            and s.get("VTNr") == active_vt[3:] and s.get("TTY") in ("", active_vt),
            "GRAPHICAL_VT_UNVERIFIED")
    require(DISPLAY_ID.fullmatch(s.get("Display", "")) is not None, "X11_DISPLAY_UNVERIFIED")
    try:
        f = Foreground(active_vt, s["Id"], int(s["User"]), int(s["Leader"]), "x11",
                       s["TTY"], s["Seat"], int(s["VTNr"]), s["Display"])
    except (KeyError, ValueError, TypeError):
        raise CaptureError("FOREGROUND_SESSION_INVALID") from None
    require(f.uid == os.getuid() and f.leader > 0 and SESSION_ID.fullmatch(f.session_id) is not None,
            "X11_SESSION_IDENTITY_MISMATCH")
    return f


def foreground(seat: str = "seat0", *, allow_graphics: bool = False) -> Foreground:
    def run(args: list[str]) -> str:
        try:
            p = subprocess.run(args, check=False, capture_output=True, timeout=4,
                               text=True, env={**os.environ, "LC_ALL": "C"})
        except (OSError, subprocess.TimeoutExpired):
            raise CaptureError("SESSION_QUERY_FAILED") from None
        require(p.returncode == 0 and len(p.stdout) <= 65536, "SESSION_QUERY_FAILED")
        return p.stdout
    ids = []
    for line in run(["loginctl", "list-sessions", "--no-legend", "--no-pager"]).splitlines():
        if line.strip():
            sid = line.split()[0]
            require(SESSION_ID.fullmatch(sid) is not None, "SESSION_QUERY_INVALID")
            ids.append(sid)
    require(len(ids) <= 32, "SESSION_COUNT_EXCEEDED")
    fields = ["Id", "Active", "Type", "Seat", "VTNr", "TTY", "User", "Leader", "Remote", "Display"]
    sessions = []
    for sid in ids:
        args = ["loginctl", "show-session", sid, "--no-pager"]
        for key in fields:
            args += ["-p", key]
        sessions.append(dict(line.split("=", 1) for line in run(args).splitlines() if "=" in line))
    try:
        active_vt = Path("/sys/class/tty/tty0/active").read_text().strip()
    except OSError:
        raise CaptureError("ACTIVE_VT_UNAVAILABLE") from None
    return (choose_display_foreground(active_vt, sessions, seat) if allow_graphics
            else choose_foreground(active_vt, sessions, seat))


class _Fix(ctypes.Structure):
    _fields_ = [("id", ctypes.c_char * 16), ("smem_start", ctypes.c_ulong),
                ("smem_len", ctypes.c_uint32), ("type", ctypes.c_uint32),
                ("type_aux", ctypes.c_uint32), ("visual", ctypes.c_uint32),
                ("xpanstep", ctypes.c_uint16), ("ypanstep", ctypes.c_uint16),
                ("ywrapstep", ctypes.c_uint16), ("line_length", ctypes.c_uint32),
                ("mmio_start", ctypes.c_ulong), ("mmio_len", ctypes.c_uint32),
                ("accel", ctypes.c_uint32), ("capabilities", ctypes.c_uint16),
                ("reserved", ctypes.c_uint16 * 2)]


@dataclass(frozen=True)
class Geometry:
    width: int
    height: int
    virtual_width: int
    virtual_height: int
    xoffset: int
    yoffset: int
    stride: int
    bpp: int
    red: tuple[int, int]
    green: tuple[int, int]
    blue: tuple[int, int]
    memory_bytes: int
    driver: str = ""
    byteorder: str = sys.byteorder


def validate_geometry(g: Geometry) -> None:
    require(0 < g.width <= 4096 and 0 < g.height <= 2160 and
            g.width * g.height <= MAX_PIXELS, "SCREEN_SIZE_UNSUPPORTED")
    require(g.bpp in {16, 24, 32} and g.byteorder in {"little", "big"}, "PIXEL_FORMAT_UNSUPPORTED")
    pixel_bytes = g.bpp // 8
    require(g.virtual_width >= g.width and g.virtual_height >= g.height and
            0 <= g.xoffset <= g.virtual_width - g.width and
            0 <= g.yoffset <= g.virtual_height - g.height, "VIEWPORT_INVALID")
    require(g.stride >= g.virtual_width * pixel_bytes and
            0 < g.memory_bytes <= MAX_FRAMEBUFFER_BYTES and
            (g.yoffset + g.height) * g.stride <= g.memory_bytes, "FRAMEBUFFER_LAYOUT_INVALID")
    masks = []
    for offset, length in (g.red, g.green, g.blue):
        require(1 <= length <= 8 and 0 <= offset and offset + length <= g.bpp,
                "COLOR_LAYOUT_UNSUPPORTED")
        masks.append(((1 << length) - 1) << offset)
    require(not any(a & b for i, a in enumerate(masks) for b in masks[i + 1:]),
            "COLOR_LAYOUT_OVERLAP")


def read_geometry(fd: int) -> Geometry:
    var = array.array("I", [0] * 40)
    require(var.itemsize == 4, "HOST_ABI_UNSUPPORTED")
    fixed_buffer = bytearray(ctypes.sizeof(_Fix))
    try:
        fcntl.ioctl(fd, FBIOGET_VSCREENINFO, var, True)
        fcntl.ioctl(fd, FBIOGET_FSCREENINFO, fixed_buffer, True)
    except OSError:
        raise CaptureError("FRAMEBUFFER_INFO_UNAVAILABLE") from None
    fixed = _Fix.from_buffer_copy(fixed_buffer)
    require(fixed.type == 0 and fixed.visual == 2 and var[7] == 0 and var[20] == 0
            and var[34] == 0 and all(var[i] == 0 for i in (10, 13, 16)),
            "FRAMEBUFFER_FORMAT_UNSUPPORTED")
    g = Geometry(var[0], var[1], var[2], var[3], var[4], var[5], fixed.line_length,
                 var[6], (var[8], var[9]), (var[11], var[12]), (var[14], var[15]),
                 fixed.smem_len, bytes(fixed.id).decode("ascii", "replace"))
    validate_geometry(g)
    return g


def decode_framebuffer(raw: bytes, g: Geometry) -> bytes:
    """Losslessly select visible pixels and decode the kernel-reported RGB fields."""
    validate_geometry(g)
    end = (g.yoffset + g.height - 1) * g.stride + (g.xoffset + g.width) * (g.bpp // 8)
    require(len(raw) >= end, "FRAMEBUFFER_READ_SHORT")
    pixel_bytes = g.bpp // 8
    output = bytearray(g.width * g.height * 3)
    byte_fields = g.bpp in {24, 32} and all(n == 8 and off % 8 == 0
                                          for off, n in (g.red, g.green, g.blue))
    for y in range(g.height):
        start = (y + g.yoffset) * g.stride + g.xoffset * pixel_bytes
        row = raw[start:start + g.width * pixel_bytes]
        dst = y * g.width * 3
        if byte_fields:
            for component, (off, _) in enumerate((g.red, g.green, g.blue)):
                byte = off // 8 if g.byteorder == "little" else pixel_bytes - 1 - off // 8
                output[dst + component:dst + g.width * 3:3] = row[byte::pixel_bytes]
        else:
            for x in range(g.width):
                value = int.from_bytes(row[x * pixel_bytes:(x + 1) * pixel_bytes], g.byteorder)
                for component, (off, n) in enumerate((g.red, g.green, g.blue)):
                    mask = (1 << n) - 1
                    output[dst + x * 3 + component] = (((value >> off) & mask) * 255 + mask // 2) // mask
    return bytes(output)


def encode_png(rgb: bytes, width: int, height: int, *, prefer_indexed: bool = False) -> bytes:
    require(len(rgb) == width * height * 3 and width > 0 and height > 0, "PIXEL_LENGTH_INVALID")
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    # A palette changes only PNG storage, never pixels. Stop the palette attempt
    # as soon as the 257th exact RGB value appears, retaining true RGB for photos.
    palette = {}
    indexes = None
    if prefer_indexed:
        indexes = bytearray(width * height)
        for pixel, start in enumerate(range(0, len(rgb), 3)):
            color = rgb[start:start + 3]
            index = palette.get(color)
            if index is None:
                if len(palette) == 256:
                    indexes = None
                    break
                index = len(palette)
                palette[color] = index
            indexes[pixel] = index
    if indexes is not None:
        depth = 1 if len(palette) <= 2 else 2 if len(palette) <= 4 else 4 if len(palette) <= 16 else 8
        per_byte = 8 // depth
        compressor = zlib.compressobj(9)
        rows = []
        for y in range(height):
            source = indexes[y * width:(y + 1) * width]
            if depth == 8:
                packed = source
            else:
                packed = bytearray((width * depth + 7) // 8)
                for x, value in enumerate(source):
                    packed[x // per_byte] |= value << (8 - depth * ((x % per_byte) + 1))
            rows.append(compressor.compress(b"\0" + packed))
        rows.append(compressor.flush())
        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, depth, 3, 0, 0, 0))
                + chunk(b"PLTE", b"".join(palette)) + chunk(b"IDAT", b"".join(rows))
                + chunk(b"IEND", b""))
    # RGB pixels are stored exactly; no fonts, labels, scaling or other overlays.
    compressor = zlib.compressobj(9)
    rows = []
    for y in range(height):
        rows.append(compressor.compress(b"\0" + rgb[y * width * 3:(y + 1) * width * 3]))
    rows.append(compressor.flush())
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", b"".join(rows)) + chunk(b"IEND", b""))


def _directory(output_dir: Path | str) -> Path:
    path = Path(output_dir).absolute()
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    s = path.lstat()
    require(stat.S_ISDIR(s.st_mode) and not path.is_symlink() and s.st_uid == os.getuid()
            and s.st_mode & 0o077 == 0, "CAPTURE_DIRECTORY_UNTRUSTED")
    return path


def _write_new(path: Path, data: bytes) -> None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        raise CaptureError("CAPTURE_ARTIFACT_CREATE_FAILED") from None
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def require_unchanged(before: Foreground, after: Foreground) -> None:
    require(before == after, "FOREGROUND_CHANGED_DURING_CAPTURE")


class _XImage(ctypes.Structure):
    _fields_ = [("width", ctypes.c_int), ("height", ctypes.c_int), ("xoffset", ctypes.c_int),
                ("format", ctypes.c_int), ("data", ctypes.c_void_p), ("byte_order", ctypes.c_int),
                ("bitmap_unit", ctypes.c_int), ("bitmap_bit_order", ctypes.c_int),
                ("bitmap_pad", ctypes.c_int), ("depth", ctypes.c_int),
                ("bytes_per_line", ctypes.c_int), ("bits_per_pixel", ctypes.c_int),
                ("red_mask", ctypes.c_ulong), ("green_mask", ctypes.c_ulong),
                ("blue_mask", ctypes.c_ulong), ("obdata", ctypes.c_void_p),
                ("functions", ctypes.c_void_p * 6)]


def _mask_field(mask: int) -> tuple[int, int]:
    require(mask > 0, "X11_COLOR_MASK_INVALID")
    offset = (mask & -mask).bit_length() - 1
    shifted = mask >> offset
    require(shifted & (shifted + 1) == 0, "X11_COLOR_MASK_INVALID")
    return offset, shifted.bit_length()


def _x11_leader_environment(f: Foreground, display: str) -> dict[str, str]:
    """Read the user leader's two context fields, never a display manager's env."""
    try:
        leader = Path("/proc") / str(f.leader)
        owner = leader.stat().st_uid
        require(owner in {0, f.uid}, "X11_LEADER_IDENTITY_MISMATCH")
        if owner != f.uid:
            # LightDM's session leader belongs to root. It is not the desktop
            # user and its authentication context must not be read or reused.
            return {}
        environment = (leader / "environ").read_bytes()
        require(len(environment) <= 1024 * 1024, "X11_CONTEXT_TOO_LARGE")
        values = {}
        for item in environment.split(b"\0"):
            if b"=" not in item:
                continue
            name, value = item.split(b"=", 1)
            if name in {b"DISPLAY", b"XAUTHORITY"}:
                values[name.decode()] = value.decode("utf-8", "strict")
        require(values.get("DISPLAY", display) == display, "X11_DISPLAY_IDENTITY_MISMATCH")
        return values
    except (OSError, UnicodeDecodeError):
        return {}


def _x11_context(f: Foreground, display: str | None, xauthority: str | None) -> tuple[str, str | None]:
    require(f.session_type == "x11" and f.uid == os.getuid(), "X11_SESSION_IDENTITY_MISMATCH")
    display = display or f.display
    require(isinstance(display, str) and DISPLAY_ID.fullmatch(display) is not None
            and display == f.display, "X11_DISPLAY_IDENTITY_MISMATCH")
    if xauthority is None:
        # Use the matching process context or the user leader's whitelist.
        # Authentication bytes are never inspected, logged or persisted here.
        if os.environ.get("DISPLAY") == display:
            xauthority = os.environ.get("XAUTHORITY")
        if xauthority is None:
            xauthority = _x11_leader_environment(f, display).get("XAUTHORITY")
        if xauthority is None:
            # Resolve the verified user's canonical home, not a caller's HOME.
            # Xlib's normal fallback is made explicit and validated below.
            try:
                account = pwd.getpwuid(f.uid)
            except KeyError:
                raise CaptureError("X11_AUTHORITY_UNAVAILABLE") from None
            require(account.pw_uid == f.uid and Path(account.pw_dir).is_absolute(),
                    "X11_AUTHORITY_PATH_INVALID")
            xauthority = str(Path(account.pw_dir) / ".Xauthority")
    if xauthority is not None:
        require(isinstance(xauthority, str) and len(xauthority) <= 4096 and "\0" not in xauthority,
                "X11_AUTHORITY_PATH_INVALID")
        path = Path(xauthority)
        try:
            s = path.lstat()
        except OSError:
            raise CaptureError("X11_AUTHORITY_UNAVAILABLE") from None
        require(path.is_absolute() and stat.S_ISREG(s.st_mode) and s.st_uid == f.uid
                and s.st_mode & 0o077 == 0, "X11_AUTHORITY_UNTRUSTED")
    return display, xauthority


def capture_x11_pixels(f: Foreground, *, display: str | None = None,
                       xauthority: str | None = None) -> tuple[bytes, Geometry, dict]:
    display, xauthority = _x11_context(f, display, xauthority)
    library = ctypes.util.find_library("X11")
    require(library is not None, "X11_LIBRARY_UNAVAILABLE")
    with _X11_LOCK:
        try:
            x = ctypes.CDLL(library)
            x.XOpenDisplay.argtypes = [ctypes.c_char_p]; x.XOpenDisplay.restype = ctypes.c_void_p
            x.XCloseDisplay.argtypes = [ctypes.c_void_p]
            x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]; x.XDefaultRootWindow.restype = ctypes.c_ulong
            x.XGetGeometry.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
                                      ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                                      *([ctypes.POINTER(ctypes.c_uint)] * 4)]
            x.XGetGeometry.restype = ctypes.c_int
            x.XGetImage.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_int]
            x.XGetImage.restype = ctypes.POINTER(_XImage)
            x.XDestroyImage.argtypes = [ctypes.POINTER(_XImage)]
            x.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        except (OSError, AttributeError):
            raise CaptureError("X11_LIBRARY_UNAVAILABLE") from None
        old_authority = os.environ.get("XAUTHORITY")
        try:
            if xauthority is not None:
                os.environ["XAUTHORITY"] = xauthority
            else:
                os.environ.pop("XAUTHORITY", None)
            connection = x.XOpenDisplay(display.encode("ascii"))
        finally:
            if old_authority is None:
                os.environ.pop("XAUTHORITY", None)
            else:
                os.environ["XAUTHORITY"] = old_authority
        require(bool(connection), "X11_DISPLAY_UNREADABLE")
        image = None
        try:
            root = x.XDefaultRootWindow(connection)
            def dimensions() -> tuple[int, int, int]:
                rr = ctypes.c_ulong(); px = ctypes.c_int(); py = ctypes.c_int()
                width = ctypes.c_uint(); height = ctypes.c_uint(); border = ctypes.c_uint(); depth = ctypes.c_uint()
                ok = x.XGetGeometry(connection, root, ctypes.byref(rr), ctypes.byref(px), ctypes.byref(py),
                                    ctypes.byref(width), ctypes.byref(height), ctypes.byref(border), ctypes.byref(depth))
                require(bool(ok), "X11_GEOMETRY_UNAVAILABLE")
                require(0 < width.value <= 4096 and 0 < height.value <= 2160
                        and width.value * height.value <= MAX_PIXELS, "SCREEN_SIZE_UNSUPPORTED")
                return width.value, height.value, depth.value
            width, height, depth = dimensions()
            x.XSync(connection, 0)
            image = x.XGetImage(connection, root, 0, 0, width, height, ctypes.c_ulong(-1).value, 2)
            require(bool(image), "X11_IMAGE_UNAVAILABLE")
            info = image.contents
            require(info.width == width and info.height == height and info.xoffset == 0
                    and info.format == 2 and info.byte_order in {0, 1} and bool(info.data),
                    "X11_IMAGE_FORMAT_UNSUPPORTED")
            count = info.bytes_per_line * height
            g = Geometry(width, height, width, height, 0, 0, info.bytes_per_line, info.bits_per_pixel,
                         _mask_field(info.red_mask), _mask_field(info.green_mask), _mask_field(info.blue_mask),
                         count, "X11-XGetImage", "little" if info.byte_order == 0 else "big")
            validate_geometry(g)
            raw = ctypes.string_at(info.data, count)
            require(dimensions() == (width, height, depth), "GEOMETRY_CHANGED_DURING_CAPTURE")
            return decode_framebuffer(raw, g), g, {"display": display, "root_window": root, "depth": depth}
        finally:
            if image:
                x.XDestroyImage(image)
            x.XCloseDisplay(connection)


def capture_screen(output_dir: Path | str, *, device: str = "/dev/fb0", seat: str = "seat0",
                   display: str | None = None, xauthority: str | None = None) -> dict:
    """Capture immutable real screen pixels or fail; never substitute a text render."""
    output_dir = _directory(output_dir)
    before = foreground(seat, allow_graphics=True)
    started = datetime.now(timezone.utc).isoformat()
    if before.session_type == "x11":
        rgb, g, backend = capture_x11_pixels(before, display=display, xauthority=xauthority)
        require_unchanged(before, foreground(seat, allow_graphics=True))
        png = encode_png(rgb, g.width, g.height)
        source = "x11-root-XGetImage"
    else:
        png, g = _capture_framebuffer(before, device, seat)
        backend = {}
        source = "linux-framebuffer"
    require(len(png) <= MAX_IMAGE_BYTES, "CAPTURE_IMAGE_TOO_LARGE")
    require_unchanged(before, foreground(seat, allow_graphics=True))
    capture_id = uuid.uuid4().hex
    image_path = output_dir / (capture_id + ".png")
    manifest = {"version": 1, "capture_id": capture_id, "source": source,
                "screen_type": before.session_type, "node": socket.gethostname(), "uid": os.getuid(),
                "width": g.width, "height": g.height,
                "sha256": hashlib.sha256(png).hexdigest(), "bytes": len(png),
                "path": str(image_path), "timestamp": started,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "active_vt": before.active_vt, "session": asdict(before),
                "geometry": asdict(g), "backend": backend,
                "cursor": ("pixels-present-in-framebuffer" if before.session_type == "tty"
                           else "hardware-cursor-not-included-by-XGetImage"),
                "pixel_source_verified": True, "synthetic": False}
    _write_new(image_path, png)
    try:
        _write_new(output_dir / (capture_id + ".json"),
                   json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode())
    except BaseException:
        image_path.unlink(missing_ok=True)
        raise
    return manifest


def _capture_framebuffer(before: Foreground, device: str, seat: str) -> tuple[bytes, Geometry]:
    require(re.fullmatch(r"/dev/fb[0-9]+", device) is not None, "FRAMEBUFFER_DEVICE_INVALID")
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        raise CaptureError("FRAMEBUFFER_UNREADABLE") from None
    try:
        require(stat.S_ISCHR(os.fstat(fd).st_mode), "FRAMEBUFFER_DEVICE_INVALID")
        g = read_geometry(fd)
        count = (g.yoffset + g.height) * g.stride
        raw = os.pread(fd, count, 0)
        require(len(raw) == count, "FRAMEBUFFER_READ_SHORT")
        require(read_geometry(fd) == g, "GEOMETRY_CHANGED_DURING_CAPTURE")
        require_unchanged(before, foreground(seat, allow_graphics=True))
    except OSError:
        raise CaptureError("FRAMEBUFFER_READ_FAILED") from None
    finally:
        os.close(fd)
    png = encode_png(decode_framebuffer(raw, g), g.width, g.height, prefer_indexed=True)
    return png, g


def _read_private(path: Path, max_bytes: int) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            s = os.fstat(fd)
            require(stat.S_ISREG(s.st_mode) and s.st_uid == os.getuid() and s.st_mode & 0o077 == 0
                    and s.st_nlink == 1 and s.st_size <= max_bytes, "CAPTURE_ARTIFACT_UNTRUSTED")
            with os.fdopen(fd, "rb", closefd=False) as f:
                b = f.read(max_bytes + 1)
            require(len(b) == s.st_size, "CAPTURE_ARTIFACT_CHANGED")
            return b
        finally:
            os.close(fd)
    except OSError:
        raise CaptureError("CAPTURE_ARTIFACT_UNAVAILABLE") from None


def read_chunk(output_dir: Path | str, capture_id: str, index: int, *, chunk_bytes: int = 2100,
               expected_sha256: str | None = None) -> dict:
    require(isinstance(capture_id, str) and CAPTURE_ID.fullmatch(capture_id) is not None,
            "CAPTURE_ID_INVALID")
    require(type(index) is int and index >= 0 and type(chunk_bytes) is int
            and 1 <= chunk_bytes <= MAX_CHUNK_BYTES, "CHUNK_BOUNDS_INVALID")
    directory = _directory(output_dir)
    try:
        m = json.loads(_read_private(directory / (capture_id + ".json"), 16384))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise CaptureError("CAPTURE_MANIFEST_INVALID") from None
    require(isinstance(m, dict) and m.get("capture_id") == capture_id and m.get("synthetic") is False
            and m.get("pixel_source_verified") is True, "CAPTURE_MANIFEST_INVALID")
    png = _read_private(directory / (capture_id + ".png"), MAX_IMAGE_BYTES)
    digest = hashlib.sha256(png).hexdigest()
    require(digest == m.get("sha256") and len(png) == m.get("bytes"), "CAPTURE_SHA256_MISMATCH")
    require(expected_sha256 is None or expected_sha256 == digest, "EXPECTED_SHA256_MISMATCH")
    total = math.ceil(len(png) / chunk_bytes)
    require(index < total, "CHUNK_INDEX_OUT_OF_RANGE")
    data = png[index * chunk_bytes:(index + 1) * chunk_bytes]
    return {"capture_id": capture_id, "index": index, "total_chunks": total,
            "chunk_bytes": len(data), "bytes": len(png), "sha256": digest,
            "encoding": "base64", "data": base64.b64encode(data).decode("ascii"),
            "more": index + 1 < total}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir")
    args = parser.parse_args()
    try:
        print(json.dumps({"ok": True, "result": capture_screen(args.output_dir)}, sort_keys=True))
    except CaptureError as exc:
        print(json.dumps({"ok": False, "error": exc.code}, sort_keys=True))
        raise SystemExit(1)
