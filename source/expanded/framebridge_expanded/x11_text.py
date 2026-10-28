"""X11 text using only mappings in the validated current keyboard group.

The 2016 libxdo character search is keycode-first across all groups. Its
sender then locks/restores groups around XTEST events. Here libxdo supplies
read-only mapping data; only XTestFakeKeyEvent emits input. No clipboard,
keyboard binding, group-lock or keyboard-map setter is used.
"""
from __future__ import annotations

import ctypes as C
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import json
import selectors
import signal
import subprocess
import sys
import time
from typing import Callable


class TextError(RuntimeError):
    """Fixed public error code, without text, clipboard or credential contents."""


@dataclass(frozen=True)
class Mapping:
    code: int
    group: int
    symbol: int
    modifiers: int
    character: int
    needs_binding: int = 0


@dataclass(frozen=True)
class Snapshot:
    group: int
    locked_group: int
    base_group: int
    latched_group: int
    mods: int
    base_mods: int
    latched_mods: int
    locked_mods: int
    window: int
    width: int
    height: int
    keymap: str
    held: tuple[int, ...]


@dataclass(frozen=True)
class Stroke:
    code: int
    modifiers: tuple[int, ...]
    mask: int
    character: int


def require(value, code):
    if not value:
        raise TextError(code)


def plan_text(text: str, mappings: tuple[Mapping, ...], snapshot: Snapshot,
              modifier_keys: dict[int, int], lookup: Callable[[int, int, int], int]) -> tuple[Stroke, ...]:
    """Plan the entire string before clearing modifiers or emitting any event."""
    require(type(text) is str and 0 < len(text) <= 4096 and "\0" not in text,
            "TYPE_TEXT_INVALID")
    # Group zero is the native context validated by this revision. Do not guess
    # XTEST device mappings for another group or consume one-shot accessibility
    # state. Unsupported contexts fail without changing that state.
    require(snapshot.group == snapshot.locked_group == 0 and snapshot.base_group == 0
            and snapshot.latched_group == 0, "TYPE_KEYBOARD_GROUP_UNSUPPORTED")
    require(snapshot.latched_mods == 0, "TYPE_LATCHED_MODIFIERS_UNSUPPORTED")
    # Symbol lookup alone cannot prove that locked Ctrl/Alt/Super will not
    # invoke an application shortcut. Only conventional Caps/Num lock bits
    # participate in this text backend; held modifiers are refused natively.
    require(not snapshot.locked_mods & ~(2 | 16), "TYPE_LOCKED_MODIFIERS_UNSUPPORTED")
    allowed = 0
    for mask in modifier_keys:
        allowed |= mask
    choices: dict[int, Stroke] = {}
    candidates = sorted(mappings, key=lambda item: (item.modifiers.bit_count(), item.code))
    for item in candidates:
        if item.group != snapshot.group or item.needs_binding or item.modifiers & ~allowed:
            continue
        require(8 <= item.code <= 255 and 0 <= item.modifiers <= 255,
                "TYPE_KEYMAP_INVALID")
        character = lookup(item.code, snapshot.locked_mods | item.modifiers, snapshot.group)
        if character and character not in choices:
            modifiers = tuple(modifier_keys[mask] for mask in sorted(modifier_keys)
                              if item.modifiers & mask)
            choices[character] = Stroke(item.code, modifiers, item.modifiers, character)
    require(all(ord(character) in choices for character in text),
            "TYPE_TEXT_UNREPRESENTABLE_IN_CURRENT_GROUP")
    return tuple(choices[ord(character)] for character in text)


def _same_context(current: Snapshot, initial: Snapshot, *, cleared: bool):
    require(current.window == initial.window, "TYPE_ACTIVE_WINDOW_CHANGED")
    require((current.width, current.height) == (initial.width, initial.height),
            "TYPE_GEOMETRY_CHANGED")
    require((current.group, current.locked_group, current.base_group, current.latched_group)
            == (initial.group, initial.locked_group, 0, 0), "TYPE_KEYBOARD_GROUP_CHANGED")
    require(current.latched_mods == 0 and current.locked_mods == initial.locked_mods,
            "TYPE_KEYBOARD_STATE_CHANGED")
    require(current.keymap == initial.keymap, "TYPE_KEYMAP_CHANGED")
    if cleared:
        require(current.mods == current.locked_mods, "TYPE_MODIFIERS_NOT_CLEARED")


def type_text(text: str, *, expected_window: int | None = None,
              expected_geometry: tuple[int, int] | None = None,
              before_input: Callable[[], None], timeout: float = 20,
              backend=None, clock=time.monotonic, sleep=time.sleep):
    """Emit a prevalidated plan and conservatively restore all attempted keys.

    before_input is the existing native-host display/authority guard. It is
    called immediately before every stroke and before the first state change.
    Caller ownership of uncertainty remains unchanged: after this callback has
    run, a failure must not invite replay or assert that nothing was performed.
    """
    owned = backend is None
    try:
        backend = NativeBackend() if owned else backend
    except (AttributeError, OSError) as error:
        raise TextError("TYPE_NATIVE_LIBRARY_UNAVAILABLE") from error
    initial = None
    released_held: list[int] = []
    down: list[int] = []
    attempted = False
    failure = None
    restore_failed = False
    try:
        require(type(timeout) in (int, float) and 0 < timeout <= 120, "TYPE_TIMEOUT_INVALID")
        deadline = clock() + timeout
        initial = backend.snapshot()
        require(initial.window > 0, "TYPE_ACTIVE_WINDOW_UNAVAILABLE")
        require(expected_window is None or initial.window == expected_window,
                "TYPE_ACTIVE_WINDOW_CHANGED")
        require(expected_geometry is None or (initial.width, initial.height) == expected_geometry,
                "TYPE_GEOMETRY_CHANGED")
        strokes = plan_text(text, backend.mappings, initial, backend.modifier_keys, backend.lookup)
        if hasattr(backend, "validate_strokes"):
            backend.validate_strokes(strokes, initial.locked_mods)
        require(clock() < deadline and len(strokes) * 0.01 < timeout,
                "TYPE_BUDGET_TOO_SMALL")
        # This validation is still before ANY release/press. In particular an
        # unmapped final character cannot cause a typed prefix or modifier clear.
        _same_context(backend.snapshot(), initial, cleared=False)
        if initial.held:
            before_input()
            attempted = True
            _same_context(backend.snapshot(), initial, cleared=False)
            for code in initial.held:
                released_held.append(code)  # A failed call may still have acted.
                backend.event(code, False)
            backend.sync()
        _same_context(backend.snapshot(), initial, cleared=True)
        for stroke in strokes:
            require(clock() < deadline, "TYPE_DEADLINE_EXCEEDED")
            _same_context(backend.snapshot(), initial, cleared=True)
            require(backend.lookup(stroke.code, initial.locked_mods | stroke.mask,
                                   initial.group) == stroke.character, "TYPE_KEYMAP_CHANGED")
            before_input()
            attempted = True
            # Permission can wait on the parent display guard. Revalidate
            # after that wait, at the actual event boundary as well.
            _same_context(backend.snapshot(), initial, cleared=True)
            require(backend.lookup(stroke.code, initial.locked_mods | stroke.mask,
                                   initial.group) == stroke.character, "TYPE_KEYMAP_CHANGED")
            for code in stroke.modifiers:
                down.append(code)
                backend.event(code, True)
            down.append(stroke.code)
            backend.event(stroke.code, True)
            backend.event(stroke.code, False)
            down.remove(stroke.code)
            for code in reversed(stroke.modifiers):
                backend.event(code, False)
                down.remove(code)
            backend.sync()
            sleep(0.01)
        _same_context(backend.snapshot(), initial, cleared=True)
    except BaseException as error:
        failure = error
    finally:
        # Releases and restoration are cleanup, not text replay. Attempt all of
        # them even if a prior event, guard or restore call failed.
        for code in reversed(down):
            try:
                backend.event(code, False)
            except BaseException:
                restore_failed = True
        for code in released_held:
            try:
                backend.event(code, True)
            except BaseException:
                restore_failed = True
        if attempted:
            try:
                backend.sync()
                final = backend.snapshot()
                require(initial is not None and final.held == initial.held
                        and final.group == initial.group and final.locked_mods == initial.locked_mods,
                        "TYPE_MODIFIER_RESTORE_UNCERTAIN")
            except BaseException:
                restore_failed = True
        if owned:
            try:
                backend.close()
            except BaseException:
                if attempted:
                    restore_failed = True
                elif failure is None:
                    failure = TextError("TYPE_NATIVE_CLOSE_FAILED")
    if restore_failed:
        raise TextError("TYPE_MODIFIER_RESTORE_UNCERTAIN") from failure
    if failure is not None:
        raise failure


def bounded_type_text(text: str, *, expected_window: int,
                      expected_geometry: tuple[int, int], before_input,
                      timeout: float = 20):
    """Bound native Xlib work by a child process and gate every input batch.

    The child never receives permission to emit until whole-text preflight has
    passed. An ACK is conservative evidence of possible input. A stalled X
    server can prevent cleanup; any such termination therefore remains uncertain
    after an ACK and must never invite replay.
    """
    require(type(timeout) in (int, float) and 0 < timeout <= 120, "TYPE_TIMEOUT_INVALID")
    require(type(text) is str and 0 < len(text) <= 4096 and "\0" not in text,
            "TYPE_TEXT_INVALID")
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    try:
        process = subprocess.Popen([sys.executable, "-I", "-S", "-B",
                                    str(Path(__file__).resolve()), "--worker"],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, bufsize=0)
    except BaseException:
        selector.close()
        raise
    buffer = b""
    complete = False
    try:
        request = json.dumps({"text": text, "window": expected_window,
                              "geometry": expected_geometry, "timeout": timeout},
                             ensure_ascii=True).encode("ascii") + b"\n"
        # The request is bounded below PIPE_BUF by ASCII length for normal
        # strings, but Unicode escaping can be larger: use a writable selector
        # rather than allowing pipe backpressure to block the native host.
        selector.register(process.stdin, selectors.EVENT_WRITE)
        offset = 0
        while offset < len(request):
            remaining = deadline - time.monotonic()
            require(remaining > 0 and selector.select(remaining), "TYPE_WORKER_TIMEOUT")
            offset += os.write(process.stdin.fileno(), request[offset:offset + 4096])
        selector.unregister(process.stdin)
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            require(remaining > 0 and selector.select(remaining), "TYPE_WORKER_TIMEOUT")
            chunk = os.read(process.stdout.fileno(), 4096)
            require(bool(chunk), "TYPE_WORKER_EXIT_UNCERTAIN")
            buffer += chunk
            require(len(buffer) <= 4096, "TYPE_WORKER_PROTOCOL_INVALID")
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if line == b"INPUT":
                    before_input()
                    require(os.write(process.stdin.fileno(), b"GO\n") == 3,
                            "TYPE_WORKER_PROTOCOL_INVALID")
                elif line == b"OK":
                    require(not buffer, "TYPE_WORKER_PROTOCOL_INVALID")
                    complete = True
                    return
                elif line.startswith(b"ERROR TYPE_"):
                    code = line[6:].decode("ascii", errors="strict")
                    require(len(code) <= 96 and all(c.isupper() or c == "_" for c in code),
                            "TYPE_WORKER_PROTOCOL_INVALID")
                    raise TextError(code)
                else:
                    raise TextError("TYPE_WORKER_PROTOCOL_INVALID")
    except (OSError, ValueError, UnicodeError) as error:
        raise TextError("TYPE_WORKER_IO_UNCERTAIN") from error
    finally:
        selector.close()
        if process.poll() is None:
            if not complete:
                process.terminate()  # child handler runs restoration finally
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=0.5)
        process.stdin.close()
        process.stdout.close()


def _worker():
    # SIGTERM must unwind normal Python paths, including a child waiting for
    # permission. If Xlib is stuck, the parent bounds cleanup and kills it.
    def stopped(_number, _frame):
        raise TextError("TYPE_WORKER_CANCELLED")
    signal.signal(signal.SIGTERM, stopped)
    def permission():
        sys.stdout.write("INPUT\n"); sys.stdout.flush()
        require(sys.stdin.buffer.readline(16) == b"GO\n", "TYPE_WORKER_CANCELLED")
    try:
        raw = sys.stdin.buffer.readline(65537)
        require(len(raw) <= 65536 and raw.endswith(b"\n"), "TYPE_WORKER_REQUEST_INVALID")
        request = json.loads(raw)
        type_text(request["text"], expected_window=request["window"],
                  expected_geometry=tuple(request["geometry"]),
                  before_input=permission, timeout=request["timeout"])
        print("OK", flush=True)
    except BaseException as error:
        code = str(error) if isinstance(error, TextError) else "TYPE_NATIVE_FAILURE"
        print("ERROR " + code, flush=True)


# Public 2016 libxdo ABI. Debian preserves extra fields AFTER this prefix;
# deliberately never describe, write or depend on the rest of xdo_t.
class CharCode(C.Structure):
    _fields_ = [("key", C.c_uint32), ("code", C.c_ubyte), ("symbol", C.c_ulong),
                ("group", C.c_int), ("modmask", C.c_int), ("needs_binding", C.c_int)]


class XdoPrefix(C.Structure):
    _fields_ = [("xdpy", C.c_void_p), ("display_name", C.c_void_p),
                ("charcodes", C.POINTER(CharCode)), ("charcodes_len", C.c_int)]


class XkbState(C.Structure):
    # Authoritative XKBstr.h, not the obsolete HTML manual's byte fields.
    _fields_ = [("group", C.c_ubyte), ("locked_group", C.c_ubyte),
                ("base_group", C.c_ushort), ("latched_group", C.c_ushort),
                ("mods", C.c_ubyte), ("base_mods", C.c_ubyte),
                ("latched_mods", C.c_ubyte), ("locked_mods", C.c_ubyte),
                ("compat_state", C.c_ubyte), ("grab_mods", C.c_ubyte),
                ("compat_grab_mods", C.c_ubyte), ("lookup_mods", C.c_ubyte),
                ("compat_lookup_mods", C.c_ubyte), ("ptr_buttons", C.c_ushort)]


class ModifierMap(C.Structure):
    _fields_ = [("max_keypermod", C.c_int), ("modifiermap", C.POINTER(C.c_ubyte))]


class SymMap(C.Structure):
    _fields_ = [("kt_index", C.c_ubyte * 4), ("group_info", C.c_ubyte),
                ("width", C.c_ubyte), ("offset", C.c_ushort)]


class ClientMapPrefix(C.Structure):
    _fields_ = [("size_types", C.c_ubyte), ("num_types", C.c_ubyte), ("types", C.c_void_p),
                ("size_syms", C.c_ushort), ("num_syms", C.c_ushort), ("syms", C.c_void_p),
                ("key_sym_map", C.POINTER(SymMap))]


class ServerMapPrefix(C.Structure):
    _fields_ = [("num_acts", C.c_ushort), ("size_acts", C.c_ushort),
                ("acts", C.POINTER(C.c_ubyte * 8)), ("behaviors", C.c_void_p),
                ("key_acts", C.POINTER(C.c_ushort))]


class KeyboardPrefix(C.Structure):
    _fields_ = [("display", C.c_void_p), ("flags", C.c_ushort), ("device", C.c_ushort),
                ("min_key_code", C.c_ubyte), ("max_key_code", C.c_ubyte), ("ctrls", C.c_void_p),
                ("server", C.POINTER(ServerMapPrefix)), ("map", C.POINTER(ClientMapPrefix))]


def validate_actions(actions, strokes, modifier_keys, locked_mods=0):
    """No group/latch/lock/redirect/pointer actions on any generated key."""
    modifier_masks = {code: mask for mask, code in modifier_keys.items()}
    character_codes = {stroke.code for stroke in strokes}
    codes = character_codes | {code for stroke in strokes for code in stroke.modifiers}
    for code in codes:
        require(code in actions, "TYPE_KEY_ACTIONS_UNAVAILABLE")
        for action in actions[code]:
            require(len(action) == 8, "TYPE_KEY_ACTIONS_INVALID")
            if action[0] == 0:  # NoAction
                require(code in character_codes, "TYPE_KEY_ACTION_UNSUPPORTED")
                continue
            require(code not in character_codes and code in modifier_masks,
                    "TYPE_KEY_ACTION_UNSUPPORTED")
            mask = modifier_masks[code]
            # XKB ClearLocks only unlocks action modifiers (X.Org table16.2).
            # Permit it only when those bits have no locked state to alter.
            # No latch/lock/group/virtual modifier side effects are allowed.
            require(action[0] == 1 and action[1] in (0, 1, 4, 5) and action[2] == mask
                    and action[3] == mask and not any(action[4:]),
                    "TYPE_KEY_ACTION_UNSUPPORTED")
            require(not (action[1] & 1 and locked_mods & mask),
                    "TYPE_KEY_ACTION_WOULD_CLEAR_LOCK")


def trusted_library(name: str):
    path = Path("/usr/lib/x86_64-linux-gnu") / name
    try:
        resolved = path.resolve(strict=True)
        for part in [resolved, *resolved.parents]:
            info = part.lstat()
            require(info.st_uid == 0 and not info.st_mode & 0o022,
                    "TYPE_NATIVE_LIBRARY_UNTRUSTED")
        require(resolved.is_file(), "TYPE_NATIVE_LIBRARY_UNTRUSTED")
        return C.CDLL(str(resolved))
    except (OSError, ValueError) as error:
        raise TextError("TYPE_NATIVE_LIBRARY_UNAVAILABLE") from error


class NativeBackend:
    """Read-only XKB/libxdo inspection plus direct XTEST key events only."""

    def __init__(self):
        self.context = None
        self.xdo = trusted_library("libxdo.so.3")
        self.x11 = trusted_library("libX11.so.6")
        self.xtst = trusted_library("libXtst.so.6")
        require(C.sizeof(C.c_void_p) == 8 and C.sizeof(C.c_ulong) == 8
                and C.sizeof(CharCode) == 32 and C.sizeof(XkbState) == 18
                and C.sizeof(SymMap) == 8 and KeyboardPrefix.server.offset == 24
                and KeyboardPrefix.map.offset == 32 and ServerMapPrefix.key_acts.offset == 24
                and ClientMapPrefix.key_sym_map.offset == 32,
                "TYPE_NATIVE_ABI_UNSUPPORTED")
        self._bind()
        require(self.xdo.xdo_version() == b"3.20160805.1", "TYPE_NATIVE_ABI_UNSUPPORTED")
        self.context = self.xdo.xdo_new(None)
        require(bool(self.context), "TYPE_DISPLAY_UNAVAILABLE")
        try:
            prefix = XdoPrefix.from_address(self.context)
            require(prefix.xdpy and prefix.charcodes and 0 < prefix.charcodes_len <= 65536,
                    "TYPE_NATIVE_KEYMAP_INVALID")
            self.display = prefix.xdpy
            self.mappings = tuple(Mapping(item.code, item.group, item.symbol, item.modmask,
                                          item.key, item.needs_binding)
                                  for item in prefix.charcodes[:prefix.charcodes_len])
            require(all(8 <= item.code <= 255 and 0 <= item.group <= 3
                        and 0 <= item.modifiers <= 255 and item.needs_binding in (0, 1)
                        for item in self.mappings), "TYPE_NATIVE_KEYMAP_INVALID")
            self.characters = {item.symbol: item.character for item in self.mappings}
            require(self.xtst.XTestQueryExtension(self.display, C.byref(C.c_int()),
                                                  C.byref(C.c_int()), C.byref(C.c_int()),
                                                  C.byref(C.c_int())) != 0, "TYPE_XTEST_UNAVAILABLE")
            self.modifier_keys, self.modifier_codes, self.locking_codes, self.group_codes = self._modifiers()
        except BaseException:
            self.close()
            raise

    def _bind(self):
        def bind(lib, name, result, *args):
            value = getattr(lib, name); value.restype = result; value.argtypes = list(args)
        bind(self.xdo, "xdo_version", C.c_char_p)
        bind(self.xdo, "xdo_new", C.c_void_p, C.c_char_p)
        bind(self.xdo, "xdo_free", None, C.c_void_p)
        bind(self.xdo, "xdo_get_active_window", C.c_int, C.c_void_p, C.POINTER(C.c_ulong))
        bind(self.x11, "XkbGetState", C.c_int, C.c_void_p, C.c_uint, C.POINTER(XkbState))
        bind(self.x11, "XkbLookupKeySym", C.c_int, C.c_void_p, C.c_ubyte, C.c_uint,
             C.POINTER(C.c_uint), C.POINTER(C.c_ulong))
        bind(self.x11, "XkbKeycodeToKeysym", C.c_ulong, C.c_void_p, C.c_ubyte, C.c_int, C.c_int)
        bind(self.x11, "XkbGetMap", C.POINTER(KeyboardPrefix), C.c_void_p, C.c_uint, C.c_uint)
        bind(self.x11, "XkbFreeKeyboard", None, C.POINTER(KeyboardPrefix), C.c_uint, C.c_int)
        bind(self.x11, "XQueryKeymap", C.c_int, C.c_void_p, C.c_void_p)
        bind(self.x11, "XGetModifierMapping", C.POINTER(ModifierMap), C.c_void_p)
        bind(self.x11, "XFreeModifiermap", C.c_int, C.POINTER(ModifierMap))
        bind(self.x11, "XDisplayKeycodes", C.c_int, C.c_void_p, C.POINTER(C.c_int), C.POINTER(C.c_int))
        bind(self.x11, "XGetKeyboardMapping", C.POINTER(C.c_ulong), C.c_void_p, C.c_ubyte,
             C.c_int, C.POINTER(C.c_int))
        bind(self.x11, "XFree", C.c_int, C.c_void_p)
        bind(self.x11, "XDefaultScreen", C.c_int, C.c_void_p)
        bind(self.x11, "XDisplayWidth", C.c_int, C.c_void_p, C.c_int)
        bind(self.x11, "XDisplayHeight", C.c_int, C.c_void_p, C.c_int)
        bind(self.x11, "XSync", C.c_int, C.c_void_p, C.c_int)
        bind(self.xtst, "XTestQueryExtension", C.c_int, C.c_void_p,
             C.POINTER(C.c_int), C.POINTER(C.c_int), C.POINTER(C.c_int), C.POINTER(C.c_int))
        bind(self.xtst, "XTestFakeKeyEvent", C.c_int, C.c_void_p, C.c_uint, C.c_int, C.c_ulong)

    def _modifier_data(self):
        pointer = self.x11.XGetModifierMapping(self.display)
        require(bool(pointer), "TYPE_MODIFIER_MAP_UNAVAILABLE")
        try:
            width = pointer.contents.max_keypermod
            require(0 < width <= 32, "TYPE_MODIFIER_MAP_INVALID")
            values = tuple(pointer.contents.modifiermap[:8 * width])
            require(all(code == 0 or 8 <= code <= 255 for code in values),
                    "TYPE_MODIFIER_MAP_INVALID")
            return width, values
        finally:
            self.x11.XFreeModifiermap(pointer)

    def _modifiers(self):
        width, values = self._modifier_data()
        safe, codes, locking, groups = {}, set(), set(), set()
        for index in range(8):
            for code in values[index * width:(index + 1) * width]:
                if not code:
                    continue
                codes.add(code)
                symbol = self.x11.XkbKeycodeToKeysym(self.display, code, 0, 0)
                if index == 0 and symbol in (0xFFE1, 0xFFE2):
                    safe.setdefault(1, code)
                elif symbol == 0xFE03:  # ISO_Level3_Shift, not a group switch.
                    safe.setdefault(1 << index, code)
                if symbol in (0xFFE5, 0xFFE6, 0xFF7F, 0xFF14):
                    locking.add(code)
                if symbol == 0xFF7E or 0xFE06 <= symbol <= 0xFE0F:
                    groups.add(code)
        code_masks = {code: sum(1 << index for index in range(8)
                               if code in values[index * width:(index + 1) * width])
                      for code in codes}
        safe = {mask: code for mask, code in safe.items() if code_masks[code] == mask}
        return safe, codes, locking, groups

    def _map_token(self):
        low, high, width = C.c_int(), C.c_int(), C.c_int()
        self.x11.XDisplayKeycodes(self.display, C.byref(low), C.byref(high))
        require(8 <= low.value <= high.value <= 255, "TYPE_NATIVE_KEYMAP_INVALID")
        pointer = self.x11.XGetKeyboardMapping(self.display, low.value,
                                              high.value - low.value + 1, C.byref(width))
        require(bool(pointer), "TYPE_NATIVE_KEYMAP_INVALID")
        try:
            require(0 < width.value <= 64, "TYPE_NATIVE_KEYMAP_INVALID")
            data = C.string_at(pointer, (high.value - low.value + 1) * width.value * C.sizeof(C.c_ulong))
        finally:
            self.x11.XFree(pointer)
        action_data = b"".join(bytes((code, len(actions))) + self.behaviors[code] + b"".join(actions)
                               for code, actions in self._actions().items())
        return hashlib.sha256(data + bytes(self._modifier_data()[1]) + action_data).hexdigest()

    def _actions(self):
        # KeySyms | KeyActions | KeyBehaviors, using the authoritative public
        # XKBstr.h LP64 prefixes only. All indices are bounded before slicing.
        pointer = self.x11.XkbGetMap(self.display, 2 | 16 | 32, 256)
        require(bool(pointer), "TYPE_KEY_ACTIONS_UNAVAILABLE")
        try:
            keyboard = pointer.contents
            require(8 <= keyboard.min_key_code <= keyboard.max_key_code <= 255
                    and keyboard.server and keyboard.map, "TYPE_KEY_ACTIONS_INVALID")
            server, client = keyboard.server.contents, keyboard.map.contents
            require(client.key_sym_map and 0 <= server.num_acts <= server.size_acts <= 65535
                    and (server.num_acts == 0 or server.acts), "TYPE_KEY_ACTIONS_INVALID")
            result, behaviors = {}, {}
            for code in range(keyboard.min_key_code, keyboard.max_key_code + 1):
                entry = client.key_sym_map[code]
                groups = entry.group_info & 0x0F
                require(groups <= 4 and entry.width <= 32, "TYPE_KEY_ACTIONS_INVALID")
                count = groups * entry.width
                offset = server.key_acts[code] if server.key_acts else 0
                require(not offset or (0 < count and offset + count <= server.num_acts),
                        "TYPE_KEY_ACTIONS_INVALID")
                result[code] = (tuple(bytes(server.acts[index]) for index in range(offset, offset + count))
                                if offset else (bytes(8),))
                behaviors[code] = (C.string_at(server.behaviors + 2 * code, 2)
                                   if server.behaviors else bytes(2))
            self.behaviors = behaviors
            return result
        finally:
            self.x11.XkbFreeKeyboard(pointer, 0, True)

    def validate_strokes(self, strokes, locked_mods=0):
        actions = self._actions()
        codes = {stroke.code for stroke in strokes} | {code for stroke in strokes for code in stroke.modifiers}
        require(all(self.behaviors.get(code) == bytes(2) for code in codes),
                "TYPE_KEY_BEHAVIOR_UNSUPPORTED")
        validate_actions(actions, strokes, self.modifier_keys, locked_mods)

    def snapshot(self):
        state, window = XkbState(), C.c_ulong()
        require(self.x11.XkbGetState(self.display, 256, C.byref(state)) == 0,
                "TYPE_KEYBOARD_STATE_UNAVAILABLE")
        require(self.xdo.xdo_get_active_window(self.context, C.byref(window)) == 0,
                "TYPE_ACTIVE_WINDOW_UNAVAILABLE")
        keys = C.create_string_buffer(32)
        self.x11.XQueryKeymap(self.display, keys)
        held = tuple(sorted(code for code in self.modifier_codes
                            if keys.raw[code // 8] & (1 << (code % 8))))
        # XKB actions (e.g. grp:alt_shift_toggle) can be attached even to
        # ordinary-looking modifier keysyms. No modifier release is safe to
        # infer from keysym alone. This revision therefore requires none held.
        require(not held, "TYPE_HELD_MODIFIERS_UNSUPPORTED")
        require(not state.base_mods or held, "TYPE_MODIFIER_STATE_UNSUPPORTED")
        screen = self.x11.XDefaultScreen(self.display)
        return Snapshot(state.group, state.locked_group, state.base_group, state.latched_group,
                        state.mods, state.base_mods, state.latched_mods, state.locked_mods,
                        window.value, self.x11.XDisplayWidth(self.display, screen),
                        self.x11.XDisplayHeight(self.display, screen), self._map_token(), held)

    def lookup(self, code, modifiers, group):
        used, symbol = C.c_uint(), C.c_ulong()
        if not self.x11.XkbLookupKeySym(self.display, code, modifiers | (group << 13),
                                       C.byref(used), C.byref(symbol)):
            return 0
        value = symbol.value
        if value in self.characters:
            return self.characters[value]
        if 32 <= value <= 126 or 160 <= value <= 255:
            return value
        if 0x01000000 <= value <= 0x0110FFFF:
            return value & 0xFFFFFF
        return 0

    def event(self, code, pressed):
        require(8 <= code <= 255, "TYPE_KEYCODE_INVALID")
        require(self.xtst.XTestFakeKeyEvent(self.display, code, bool(pressed), 0) != 0,
                "TYPE_XTEST_EVENT_UNCERTAIN")

    def sync(self):
        self.x11.XSync(self.display, 0)

    def close(self):
        if self.context:
            self.xdo.xdo_free(self.context)
            self.context = None


if __name__ == "__main__":
    if sys.argv[1:] == ["--worker"]:
        _worker()
