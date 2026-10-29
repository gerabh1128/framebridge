"""Behavioral X11 text regressions; no display, libraries, or input devices.

The fixture is an independent US physical-key model. It deliberately places
the earlier LATAM slash before the US slash, matching the ASUS failure class.
"""
import ast
import ctypes as C
from dataclasses import replace
import inspect
from pathlib import Path
import sys
from types import SimpleNamespace
import textwrap
import unittest
from unittest.mock import Mock, patch

try:
    from framebridge_expanded import x11_text as subject
except ModuleNotFoundError as error:
    if error.name != "framebridge_expanded":
        raise
    import x11_text as subject


SHIFT, CAPS, NUM = 1, 2, 16


class KeyboardFixture:
    def __init__(self, *, held=(), locked=0):
        # Physical X11 evdev codes and the US unshifted/shifted characters.
        rows = [
            (10, "1", "!"), (11, "2", "@"), (12, "3", "#"),
            (13, "4", "$"), (14, "5", "%"), (15, "6", "^"),
            (16, "7", "&"), (17, "8", "*"), (18, "9", "("),
            (19, "0", ")"), (20, "-", "_"), (21, "=", "+"),
            (34, "[", "{"), (35, "]", "}"), (47, ";", ":"),
            (48, "'", '"'), (49, "`", "~"), (51, "\\", "|"),
            (59, ",", "<"), (60, ".", ">"), (61, "/", "?"),
            (65, " ", " "),
        ]
        for start, letters in [(24, "qwertyuiop"), (38, "asdfghjkl"), (52, "zxcvbnm")]:
            rows.extend((start + index, letter, letter.upper())
                        for index, letter in enumerate(letters))
        self.physical = {code: (lower, upper) for code, lower, upper in rows}
        self.modifier_keys = {SHIFT: 50}
        mappings = [subject.Mapping(16, 1, ord("/"), SHIFT, ord("/"))]
        for code, lower, upper in rows:
            mappings.append(subject.Mapping(code, 0, ord(lower), 0, ord(lower)))
            if lower != upper:
                mappings.append(subject.Mapping(code, 0, ord(upper), SHIFT, ord(upper)))
        self.mappings = tuple(mappings)
        self.pressed = set(held)
        self.initial = subject.Snapshot(0, 0, 0, 0, locked, 0, 0, locked,
                                        101, 1920, 1080, "fixture-map-v1", tuple(sorted(held)))
        self.events = []
        self.output = []
        self.snapshot_calls = 0
        self.sync_calls = 0
        self.sleeps = 0
        self.on_snapshot = None
        self.on_sleep = None
        self.fail_events = {}
        self.closed = False

    def lookup(self, code, mask, group):
        if group == 1 and code == 16:
            return ord("/") if mask & SHIFT else ord("7")
        if group != 0 or code not in self.physical:
            return 0
        lower, upper = self.physical[code]
        shifted = bool(mask & SHIFT)
        if lower.isalpha():
            shifted ^= bool(mask & CAPS)
        return ord(upper if shifted else lower)

    def snapshot(self):
        self.snapshot_calls += 1
        mods = self.initial.locked_mods
        if self.pressed & {50, 62}:
            mods |= SHIFT
        if 37 in self.pressed:
            mods |= 4
        value = replace(self.initial, mods=mods,
                        base_mods=mods & ~self.initial.locked_mods,
                        held=tuple(sorted(self.pressed & {37, 50, 62})))
        return self.on_snapshot(value, self.snapshot_calls) if self.on_snapshot else value

    def event(self, code, pressed):
        self.events.append((code, pressed))
        if pressed:
            self.pressed.add(code)
        else:
            self.pressed.discard(code)
        if pressed and code in self.physical:
            mask = self.initial.locked_mods | (SHIFT if self.pressed & {50, 62} else 0)
            value = self.lookup(code, mask, self.initial.group)
            self.output.append(chr(value) if value else "\ufffd")
        error = self.fail_events.get(len(self.events))
        if error:
            raise error

    def sync(self):
        self.sync_calls += 1

    def sleep(self, duration):
        self.sleeps += 1
        if self.on_sleep:
            self.on_sleep(self.sleeps)

    def close(self):
        self.closed = True


class TextBehaviorTests(unittest.TestCase):
    def run_text(self, text, keyboard=None, guard=None, **kwargs):
        keyboard = keyboard or KeyboardFixture()
        subject.type_text(text, backend=keyboard, before_input=guard or (lambda: None),
                          clock=lambda: 0, sleep=keyboard.sleep, **kwargs)
        return keyboard

    def assert_error(self, code, operation):
        with self.assertRaises(subject.TextError) as caught:
            operation()
        self.assertEqual(str(caught.exception), code)

    def test_slash_uses_current_us_key_even_with_earlier_latam_entry(self):
        keyboard = self.run_text("/")
        self.assertEqual(keyboard.events, [(61, True), (61, False)])
        self.assertEqual("".join(keyboard.output), "/")

    def test_original_shell_looking_text_is_only_exact_data(self):
        text = "exec tmux -S /run/user/1000/fbe-tty-probe/sudo-consoles/sudo/s-c451.sock attach-session -t fb-real-f86"
        keyboard = self.run_text(text)
        self.assertEqual("".join(keyboard.output), text)
        self.assertEqual(sum(event == (61, True) for event in keyboard.events), text.count("/"))

    def test_unsupported_last_character_does_not_even_release_held_modifiers(self):
        keyboard = KeyboardFixture(held=(37, 50))
        self.assert_error("TYPE_TEXT_UNREPRESENTABLE_IN_CURRENT_GROUP",
                          lambda: self.run_text("already representable/\U0001f642", keyboard))
        self.assertEqual(keyboard.events, [])
        self.assertEqual(keyboard.pressed, {37, 50})

    def test_nonzero_group_or_group_latch_is_rejected_before_events(self):
        cases = [dict(group=1, locked_group=1), dict(group=2, locked_group=2),
                 dict(base_group=1), dict(latched_group=1), dict(locked_group=1)]
        for changed in cases:
            with self.subTest(changed=changed):
                keyboard = KeyboardFixture(held=(50,))
                keyboard.initial = replace(keyboard.initial, **changed)
                self.assert_error("TYPE_KEYBOARD_GROUP_UNSUPPORTED", lambda: self.run_text("a", keyboard))
                self.assertEqual(keyboard.events, [])

    def test_latched_modifier_rejected_without_consuming_it(self):
        keyboard = KeyboardFixture()
        keyboard.initial = replace(keyboard.initial, latched_mods=SHIFT)
        self.assert_error("TYPE_LATCHED_MODIFIERS_UNSUPPORTED", lambda: self.run_text("A", keyboard))
        self.assertEqual(keyboard.events, [])

    def test_locked_control_alt_or_super_rejected_before_any_event(self):
        for mask in (4, 8, 64):
            with self.subTest(mask=mask):
                keyboard = KeyboardFixture(locked=mask)
                self.assert_error("TYPE_LOCKED_MODIFIERS_UNSUPPORTED",
                                  lambda: self.run_text("a/", keyboard))
                self.assertEqual(keyboard.events, [])
                self.assertEqual(keyboard.initial.locked_mods, mask)

    def test_mid_text_focus_change_stops_after_prefix_without_replay(self):
        keyboard = KeyboardFixture()
        keyboard.on_sleep = lambda count: setattr(keyboard, "initial", replace(keyboard.initial, window=999))
        self.assert_error("TYPE_ACTIVE_WINDOW_CHANGED", lambda: self.run_text("abc", keyboard))
        self.assertEqual(keyboard.events, [(38, True), (38, False)])
        self.assertEqual("".join(keyboard.output), "a")

    def test_mid_text_group_change_stops_and_marks_restoration_uncertain(self):
        keyboard = KeyboardFixture()
        keyboard.on_sleep = lambda count: setattr(keyboard, "initial", replace(keyboard.initial, group=1))
        self.assert_error("TYPE_MODIFIER_RESTORE_UNCERTAIN", lambda: self.run_text("ab", keyboard))
        self.assertEqual(keyboard.events, [(38, True), (38, False)])

    def test_mid_text_keymap_change_stops_after_prefix(self):
        keyboard = KeyboardFixture()
        keyboard.on_sleep = lambda count: setattr(keyboard, "initial", replace(keyboard.initial, keymap="changed"))
        self.assert_error("TYPE_KEYMAP_CHANGED", lambda: self.run_text("ab", keyboard))
        self.assertEqual("".join(keyboard.output), "a")
        self.assertEqual(keyboard.events, [(38, True), (38, False)])

    def test_mid_text_geometry_change_stops_after_prefix(self):
        keyboard = KeyboardFixture()
        keyboard.on_sleep = lambda count: setattr(keyboard, "initial", replace(keyboard.initial, width=800))
        self.assert_error("TYPE_GEOMETRY_CHANGED", lambda: self.run_text("ab", keyboard))
        self.assertEqual("".join(keyboard.output), "a")

    def test_guard_failure_before_first_mutation_has_zero_events(self):
        keyboard = KeyboardFixture(held=(37,))
        guard = Mock(side_effect=subject.TextError("AUTHORITY_EXPIRED"))
        self.assert_error("AUTHORITY_EXPIRED", lambda: self.run_text("a", keyboard, guard))
        self.assertEqual(keyboard.events, [])
        guard.assert_called_once_with()

    def test_guard_failure_mid_text_retains_prefix_without_replay(self):
        keyboard = KeyboardFixture()
        guard = Mock(side_effect=[None, subject.TextError("AUTHORITY_EXPIRED")])
        self.assert_error("AUTHORITY_EXPIRED", lambda: self.run_text("ab", keyboard, guard))
        self.assertEqual(keyboard.events, [(38, True), (38, False)])
        self.assertEqual("".join(keyboard.output), "a")

    def test_focus_change_during_permission_wait_emits_zero_events(self):
        keyboard = KeyboardFixture()

        def guard():
            keyboard.initial = replace(keyboard.initial, window=999)

        permission = Mock(side_effect=guard)
        self.assert_error("TYPE_ACTIVE_WINDOW_CHANGED", lambda: self.run_text("/", keyboard, permission))
        permission.assert_called_once_with()
        self.assertEqual(keyboard.events, [])

    def test_keymap_change_during_permission_wait_emits_zero_events(self):
        keyboard = KeyboardFixture()

        def guard():
            keyboard.initial = replace(keyboard.initial, keymap="changed-during-ack")

        permission = Mock(side_effect=guard)
        self.assert_error("TYPE_KEYMAP_CHANGED", lambda: self.run_text("/", keyboard, permission))
        permission.assert_called_once_with()
        self.assertEqual(keyboard.events, [])

    def test_failed_keydown_is_released_and_held_modifiers_restored(self):
        keyboard = KeyboardFixture(held=(37,))
        keyboard.fail_events[3] = subject.TextError("EVENT_FAILED")
        self.assert_error("EVENT_FAILED", lambda: self.run_text("A", keyboard))
        self.assertEqual(keyboard.events, [(37, False), (50, True), (38, True),
                                           (38, False), (50, False), (37, True)])
        self.assertEqual(keyboard.pressed, {37})

    def test_cleanup_continues_after_one_cleanup_event_fails(self):
        keyboard = KeyboardFixture(held=(37, 62))
        keyboard.fail_events[4] = subject.TextError("KEYDOWN_FAILED")
        keyboard.fail_events[5] = subject.TextError("RELEASE_FAILED")
        self.assert_error("TYPE_MODIFIER_RESTORE_UNCERTAIN", lambda: self.run_text("A", keyboard))
        self.assertEqual(keyboard.events[-4:], [(38, False), (50, False), (37, True), (62, True)])
        self.assertEqual(keyboard.pressed, {37, 62})

    def test_partial_modifier_clear_is_restored_after_event_failure(self):
        keyboard = KeyboardFixture(held=(37, 50))
        keyboard.fail_events[1] = subject.TextError("CLEAR_FAILED")
        self.assert_error("CLEAR_FAILED", lambda: self.run_text("a", keyboard))
        self.assertEqual(keyboard.events, [(37, False), (37, True)])
        self.assertEqual(keyboard.pressed, {37, 50})

    def test_caps_lock_uses_lookup_result_without_toggling_lock(self):
        keyboard = self.run_text("aA/", KeyboardFixture(locked=CAPS))
        self.assertEqual("".join(keyboard.output), "aA/")
        self.assertEqual(keyboard.events, [(50, True), (38, True), (38, False),
                                           (50, False), (38, True), (38, False),
                                           (61, True), (61, False)])
        self.assertEqual(keyboard.initial.locked_mods, CAPS)

    def test_num_lock_is_preserved_and_does_not_add_numlock_events(self):
        keyboard = self.run_text("1/A", KeyboardFixture(locked=NUM))
        self.assertEqual("".join(keyboard.output), "1/A")
        self.assertNotIn(77, [code for code, _ in keyboard.events])
        self.assertEqual(keyboard.initial.locked_mods, NUM)

    def test_held_modifiers_clear_once_and_restore_after_success(self):
        keyboard = self.run_text("a", KeyboardFixture(held=(37, 62)))
        self.assertEqual(keyboard.events, [(37, False), (62, False), (38, True),
                                           (38, False), (37, True), (62, True)])
        self.assertEqual(keyboard.pressed, {37, 62})

    def test_too_small_budget_rejects_before_events(self):
        keyboard = KeyboardFixture(held=(37,))
        self.assert_error("TYPE_BUDGET_TOO_SMALL", lambda: self.run_text("abc", keyboard, timeout=.02))
        self.assertEqual(keyboard.events, [])

    def test_expected_window_or_geometry_mismatch_has_zero_events(self):
        for kwargs, code in [({"expected_window": 202}, "TYPE_ACTIVE_WINDOW_CHANGED"),
                             ({"expected_geometry": (800, 600)}, "TYPE_GEOMETRY_CHANGED")]:
            with self.subTest(kwargs=kwargs):
                keyboard = KeyboardFixture()
                self.assert_error(code, lambda: self.run_text("a", keyboard, **kwargs))
                self.assertEqual(keyboard.events, [])

    def test_layout_binding_required_entry_never_becomes_typeable(self):
        keyboard = KeyboardFixture()
        keyboard.mappings += (subject.Mapping(120, 0, 0x01002603, 0, 0x2603, 1),)
        keyboard.physical[120] = ("\u2603", "\u2603")
        self.assert_error("TYPE_TEXT_UNREPRESENTABLE_IN_CURRENT_GROUP",
                          lambda: self.run_text("\u2603", keyboard))
        self.assertEqual(keyboard.events, [])


class NativeBoundaryTests(unittest.TestCase):
    def test_primary_header_abi_sizes_and_offsets(self):
        self.assertEqual(C.sizeof(subject.CharCode), 32)
        self.assertEqual({name: getattr(subject.CharCode, name).offset for name, _ in subject.CharCode._fields_},
                         {"key": 0, "code": 4, "symbol": 8, "group": 16,
                          "modmask": 20, "needs_binding": 24})
        self.assertEqual(C.sizeof(subject.XkbState), 18)
        self.assertEqual({name: getattr(subject.XkbState, name).offset for name, _ in subject.XkbState._fields_},
                         {"group": 0, "locked_group": 1, "base_group": 2,
                          "latched_group": 4, "mods": 6, "base_mods": 7,
                          "latched_mods": 8, "locked_mods": 9, "compat_state": 10,
                          "grab_mods": 11, "compat_grab_mods": 12, "lookup_mods": 13,
                          "compat_lookup_mods": 14, "ptr_buttons": 16})
        self.assertEqual(subject.XdoPrefix.charcodes.offset, 16)
        self.assertEqual(subject.XdoPrefix.charcodes_len.offset, 24)
        self.assertEqual([name for name, _ in subject.XdoPrefix._fields_],
                         ["xdpy", "display_name", "charcodes", "charcodes_len"])

    def test_only_native_input_binding_is_xtest_key_event(self):
        tree = ast.parse(textwrap.dedent(inspect.getsource(subject.NativeBackend._bind)))
        bindings = {call.args[1].value for call in ast.walk(tree)
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id == "bind"}
        self.assertEqual(bindings & {"xdo_enter_text_window", "xdo_send_keysequence_window",
                                      "XkbLockGroup", "XkbLatchGroup", "XChangeKeyboardMapping",
                                      "XkbSetMap", "XSetSelectionOwner", "XSendEvent"}, set())
        self.assertIn("XTestFakeKeyEvent", bindings)
        self.assertEqual({name for name in bindings if name.startswith("xdo_")},
                         {"xdo_version", "xdo_new", "xdo_free", "xdo_get_active_window"})

    def test_unknown_libxdo_abi_rejected_without_context_or_display(self):
        fake = SimpleNamespace(xdo_version=Mock(return_value=b"4.20260303.1"), xdo_new=Mock())
        with patch.object(subject, "trusted_library", return_value=fake), patch.object(subject.NativeBackend, "_bind"):
            with self.assertRaisesRegex(subject.TextError, "^TYPE_NATIVE_ABI_UNSUPPORTED$"):
                subject.NativeBackend()
        fake.xdo_new.assert_not_called()

    def test_untrusted_library_ancestor_is_rejected_before_load(self):
        for uid, mode in [(1000, 0o755), (0, 0o777), (0, 0o775)]:
            with self.subTest(uid=uid, mode=mode):
                leaf = Mock()
                leaf.lstat.return_value = SimpleNamespace(st_uid=0, st_mode=0o755)
                ancestor = Mock()
                ancestor.lstat.return_value = SimpleNamespace(st_uid=uid, st_mode=mode)
                leaf.parents = [ancestor]
                fake_path = Mock()
                fake_path.__truediv__ = Mock(return_value=fake_path)
                fake_path.resolve.return_value = leaf
                with patch.object(subject, "Path", return_value=fake_path), patch.object(subject.C, "CDLL") as loader:
                    with self.assertRaisesRegex(subject.TextError, "^TYPE_NATIVE_LIBRARY_UNTRUSTED$"):
                        subject.trusted_library("libX11.so.6")
                loader.assert_not_called()

    def test_missing_library_is_rejected_without_loader(self):
        fake_path = Mock()
        fake_path.__truediv__ = Mock(return_value=fake_path)
        fake_path.resolve.side_effect = FileNotFoundError("fixture missing")
        with patch.object(subject, "Path", return_value=fake_path), patch.object(subject.C, "CDLL") as loader:
            with self.assertRaisesRegex(subject.TextError, "^TYPE_NATIVE_LIBRARY_UNAVAILABLE$"):
                subject.trusted_library("libX11.so.6")
        loader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
