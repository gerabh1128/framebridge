"""XKB action/behavior validation with bounded in-memory public ABI tables."""
import ctypes as C
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

try:
    from framebridge_expanded import x11_text as subject
except ModuleNotFoundError as error:
    if error.name != "framebridge_expanded":
        raise
    import x11_text as subject
from test_x11_text import KeyboardFixture


NO_ACTION = bytes(8)
SHIFT_ACTION = bytes([1, 0, 1, 1, 0, 0, 0, 0])
SLASH = subject.Stroke(61, (), 0, ord("/"))
UPPERCASE_A = subject.Stroke(38, (50,), 1, ord("A"))


class ActionTableFixture:
    def __init__(self):
        self.symmap = (subject.SymMap * 256)()
        self.keyacts = (C.c_ushort * 256)()
        self.actions = ((C.c_ubyte * 8) * 8)()
        self.behaviors = (C.c_ubyte * 512)()
        self.symmap[50].group_info = 1
        self.symmap[50].width = 1
        self.keyacts[50] = 1
        self.actions[1][:] = SHIFT_ACTION
        self.symmap[61].group_info = 1
        self.symmap[61].width = 1
        self.keyacts[61] = 2
        self.actions[2][:] = NO_ACTION
        self.server = subject.ServerMapPrefix(3, 8, self.actions,
                                              C.addressof(self.behaviors), self.keyacts)
        self.client = subject.ClientMapPrefix()
        self.client.key_sym_map = self.symmap
        self.keyboard = subject.KeyboardPrefix()
        self.keyboard.min_key_code = 8
        self.keyboard.max_key_code = 61
        self.keyboard.server = C.pointer(self.server)
        self.keyboard.map = C.pointer(self.client)
        self.pointer = C.pointer(self.keyboard)
        self.backend = object.__new__(subject.NativeBackend)
        self.backend.display = 1
        self.backend.modifier_keys = {1: 50}
        self.backend.x11 = SimpleNamespace(XkbGetMap=Mock(return_value=self.pointer),
                                           XkbFreeKeyboard=Mock())

    def assert_freed(self, case):
        self.backend.x11.XkbFreeKeyboard.assert_called_once_with(self.pointer, 0, True)

    def validate_text_without_input(self, text):
        keyboard = KeyboardFixture()
        keyboard.validate_strokes = self.backend.validate_strokes
        subject.type_text(text, backend=keyboard, before_input=lambda: None,
                          clock=lambda: 0, sleep=lambda _: None)
        return keyboard


class ActionValidationTests(unittest.TestCase):
    def test_us_slash_and_shift_are_safe_and_read_tables_are_freed(self):
        fixture = ActionTableFixture()
        fixture.backend.validate_strokes((SLASH, UPPERCASE_A))
        fixture.backend.x11.XkbGetMap.assert_called_once_with(1, 2 | 16 | 32, 256)
        fixture.assert_freed(self)

    def test_modifier_map_flag_is_safe_for_exact_shift_mask(self):
        subject.validate_actions({38: (NO_ACTION,), 50: (bytes([1, 4, 1, 1, 0, 0, 0, 0]),)},
                                 (UPPERCASE_A,), {1: 50})

    def test_group_latch_lock_redirect_and_pointer_actions_reject_before_events(self):
        # XKB action types 2=LATCH_MODS, 3=LOCK_MODS, 4=SET_GROUP,
        # 5=LATCH_GROUP, 6=LOCK_GROUP, 7=MOVE_PTR, 17=REDIRECT_KEY.
        for action_type in (2, 3, 4, 5, 6, 7, 17):
            with self.subTest(action_type=action_type):
                fixture = ActionTableFixture()
                fixture.actions[2][0] = action_type
                keyboard = KeyboardFixture()
                keyboard.validate_strokes = fixture.backend.validate_strokes
                with self.assertRaisesRegex(subject.TextError, "^TYPE_KEY_ACTION_UNSUPPORTED$"):
                    subject.type_text("/", backend=keyboard, before_input=lambda: None,
                                      clock=lambda: 0, sleep=lambda _: None)
                self.assertEqual(keyboard.events, [])
                fixture.assert_freed(self)

    def test_character_key_may_not_set_modifiers(self):
        with self.assertRaisesRegex(subject.TextError, "^TYPE_KEY_ACTION_UNSUPPORTED$"):
            subject.validate_actions({61: (SHIFT_ACTION,)}, (SLASH,), {1: 50})

    def test_modifier_action_requires_exact_mask_and_no_extra_effects(self):
        actions = [bytes([1, 0, 4, 4, 0, 0, 0, 0]),
                   bytes([1, 2, 1, 1, 0, 0, 0, 0]),
                   bytes([1, 0, 1, 1, 1, 0, 0, 0]),
                   bytes([1, 0, 1, 1, 0, 1, 0, 0]), NO_ACTION]
        for action in actions:
            with self.subTest(action=action):
                with self.assertRaisesRegex(subject.TextError, "^TYPE_KEY_ACTION_UNSUPPORTED$"):
                    subject.validate_actions({38: (NO_ACTION,), 50: (action,)},
                                             (UPPERCASE_A,), {1: 50})

    def test_clearlocks_flags_one_and_five_preserve_unrelated_numlock(self):
        for flags in (1, 5):
            with self.subTest(flags=flags):
                fixture = ActionTableFixture()
                fixture.actions[1][1] = flags
                keyboard = KeyboardFixture(locked=16)
                keyboard.validate_strokes = fixture.backend.validate_strokes
                subject.type_text("A/", backend=keyboard, before_input=lambda: None,
                                  clock=lambda: 0, sleep=lambda _: None)
                self.assertEqual("".join(keyboard.output), "A/")
                self.assertEqual(keyboard.initial.locked_mods, 16)
                self.assertNotIn(77, [code for code, _ in keyboard.events])

    def test_clearlocks_cannot_clear_an_existing_lock_on_action_modifier(self):
        for flags in (1, 5):
            with self.subTest(flags=flags):
                fixture = ActionTableFixture()
                fixture.actions[1][1] = flags
                with self.assertRaisesRegex(subject.TextError, "^TYPE_KEY_ACTION_WOULD_CLEAR_LOCK$"):
                    fixture.backend.validate_strokes((UPPERCASE_A,), locked_mods=1)
                fixture.assert_freed(self)

    def test_action_missing_or_wrong_width_fails_closed(self):
        for actions, error in [({}, "TYPE_KEY_ACTIONS_UNAVAILABLE"),
                                ({61: (bytes(7),)}, "TYPE_KEY_ACTIONS_INVALID")]:
            with self.subTest(actions=actions):
                with self.assertRaisesRegex(subject.TextError, "^" + error + "$"):
                    subject.validate_actions(actions, (SLASH,), {1: 50})

    def test_all_groups_and_levels_are_checked_not_just_first_action(self):
        fixture = ActionTableFixture()
        fixture.symmap[61].group_info = 2
        fixture.symmap[61].width = 2
        fixture.server.num_acts = 6
        fixture.actions[5][0] = 6  # Unsafe fourth entry, reached by bounded read.
        with self.assertRaisesRegex(subject.TextError, "^TYPE_KEY_ACTION_UNSUPPORTED$"):
            fixture.backend.validate_strokes((SLASH,))
        fixture.assert_freed(self)

    def test_planned_character_or_modifier_nondefault_behavior_is_rejected(self):
        for code in (38, 50, 61):
            for behavior_byte in (0, 1):
                with self.subTest(code=code, behavior_byte=behavior_byte):
                    fixture = ActionTableFixture()
                    fixture.behaviors[2 * code + behavior_byte] = 1
                    keyboard = KeyboardFixture()
                    keyboard.validate_strokes = fixture.backend.validate_strokes
                    with self.assertRaisesRegex(subject.TextError, "^TYPE_KEY_BEHAVIOR_UNSUPPORTED$"):
                        subject.type_text("A/", backend=keyboard, before_input=lambda: None,
                                          clock=lambda: 0, sleep=lambda _: None)
                    self.assertEqual(keyboard.events, [])
                    fixture.assert_freed(self)

    def test_unplanned_behavior_does_not_reject_safe_plan(self):
        fixture = ActionTableFixture()
        fixture.behaviors[2 * 40] = 1
        fixture.backend.validate_strokes((SLASH,))
        fixture.assert_freed(self)

    def test_zero_action_offsets_and_missing_optional_tables_default_to_no_action(self):
        fixture = ActionTableFixture()
        fixture.server.key_acts = C.POINTER(C.c_ushort)()
        fixture.server.acts = C.POINTER(C.c_ubyte * 8)()
        fixture.server.num_acts = 0
        fixture.server.behaviors = None
        actions = fixture.backend._actions()
        self.assertEqual(actions[61], (NO_ACTION,))
        self.assertEqual(fixture.backend.behaviors[61], bytes(2))
        subject.validate_actions(actions, (SLASH,), {1: 50})
        fixture.assert_freed(self)

    def test_bad_action_offset_or_span_is_rejected_and_freed(self):
        cases = [(3, 1, 1), (2, 2, 1), (1, 0, 1), (65535, 1, 1)]
        for offset, width, groups in cases:
            with self.subTest(offset=offset, width=width, groups=groups):
                fixture = ActionTableFixture()
                fixture.keyacts[61] = offset
                fixture.symmap[61].width = width
                fixture.symmap[61].group_info = groups
                with self.assertRaisesRegex(subject.TextError, "^TYPE_KEY_ACTIONS_INVALID$"):
                    fixture.backend._actions()
                fixture.assert_freed(self)

    def test_invalid_dimensions_are_rejected_before_table_read_and_freed(self):
        for attribute, value in [("group_info", 5), ("width", 33)]:
            with self.subTest(attribute=attribute):
                fixture = ActionTableFixture()
                setattr(fixture.symmap[61], attribute, value)
                with self.assertRaisesRegex(subject.TextError, "^TYPE_KEY_ACTIONS_INVALID$"):
                    fixture.backend._actions()
                fixture.assert_freed(self)

    def test_invalid_keyboard_range_or_required_pointer_is_rejected_and_freed(self):
        for fault in ("range", "server", "client", "symmap", "actions", "count"):
            with self.subTest(fault=fault):
                fixture = ActionTableFixture()
                if fault == "range":
                    fixture.keyboard.min_key_code = 7
                elif fault == "server":
                    fixture.keyboard.server = C.POINTER(subject.ServerMapPrefix)()
                elif fault == "client":
                    fixture.keyboard.map = C.POINTER(subject.ClientMapPrefix)()
                elif fault == "symmap":
                    fixture.client.key_sym_map = C.POINTER(subject.SymMap)()
                elif fault == "actions":
                    fixture.server.acts = C.POINTER(C.c_ubyte * 8)()
                else:
                    fixture.server.num_acts = 9
                with self.assertRaisesRegex(subject.TextError, "^TYPE_KEY_ACTIONS_INVALID$"):
                    fixture.backend._actions()
                fixture.assert_freed(self)

    def test_null_keyboard_pointer_reports_unavailable_without_freeing_null(self):
        fixture = ActionTableFixture()
        fixture.backend.x11.XkbGetMap.return_value = C.POINTER(subject.KeyboardPrefix)()
        with self.assertRaisesRegex(subject.TextError, "^TYPE_KEY_ACTIONS_UNAVAILABLE$"):
            fixture.backend._actions()
        fixture.backend.x11.XkbFreeKeyboard.assert_not_called()

    def test_public_xkb_prefix_abi_offsets(self):
        self.assertEqual(C.sizeof(subject.SymMap), 8)
        self.assertEqual(subject.SymMap.offset.offset, 6)
        self.assertEqual(subject.ClientMapPrefix.key_sym_map.offset, 32)
        self.assertEqual(subject.ServerMapPrefix.acts.offset, 8)
        self.assertEqual(subject.ServerMapPrefix.behaviors.offset, 16)
        self.assertEqual(subject.ServerMapPrefix.key_acts.offset, 24)
        self.assertEqual(subject.KeyboardPrefix.server.offset, 24)
        self.assertEqual(subject.KeyboardPrefix.map.offset, 32)


if __name__ == "__main__":
    unittest.main()
