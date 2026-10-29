import json
import unittest
from unittest.mock import patch
from framebridge_expanded import display_context as dc

class DisplayBindingTests(unittest.TestCase):
    def setUp(self):
        tools = patch.object(dc, "missing_graphical_commands", return_value=[])
        tools.start()
        self.addCleanup(tools.stop)

    def context(self, **changes):
        value = dict(kind="physical-x11", physical=True, session_id="c1", uid=1000,
                     leader=123, active_vt="tty7", seat="seat0", vtnr=7,
                     display=":0", authority="/private/user/.Xauthority")
        value.update(changes)
        return value

    def bound(self, **changes):
        context = self.context(**changes)
        context["profile"] = "/profile/a"
        return {"DISPLAY": context["display"], dc.BINDING: json.dumps(context)}

    def test_auto_prefers_verified_physical_over_inherited_display(self):
        env = {"DISPLAY": ":199", "XAUTHORITY": "/other/authority"}
        with patch.object(dc, "_physical", return_value=self.context()):
            dc.configure(env, "auto", "/profile/a")
        self.assertEqual(env["DISPLAY"], ":0")
        self.assertEqual(env["XDG_SESSION_TYPE"], "x11")
        self.assertEqual(env["GDK_BACKEND"], "x11")
        self.assertEqual(env["XAUTHORITY"], "/private/user/.Xauthority")
        self.assertTrue(dc.describe(env)["physical"])

    def test_explicit_virtual_does_not_resolve_physical(self):
        env = {}
        with patch.object(dc, "_physical") as physical:
            dc.configure(env, "yes", "/profile/a")
        physical.assert_not_called()
        dc.bind_virtual(env, "/profile/a", explicit=True)
        self.assertFalse(dc.describe(env)["physical"])
        self.assertEqual(dc.describe(env)["kind"], "virtual-x11")

    def test_auto_without_physical_session_is_text_not_virtual(self):
        env = {"DISPLAY": ":199"}
        with patch.object(dc, "_physical", side_effect=dc.DisplayContextError("NO_SESSION")):
            dc.configure(env, "auto", "/profile/a")
        self.assertEqual(dc.describe(env)["kind"], "text")
        self.assertFalse(dc.describe(env)["physical"])
        self.assertFalse(dc.describe(env)["explicit"])

    def test_no_headless_requires_verified_session(self):
        env = {"DISPLAY": ":199"}
        with patch.object(dc, "_physical", side_effect=dc.DisplayContextError("NO_SESSION")):
            dc.configure(env, "no", "/profile/a")
        self.assertEqual(dc.describe(env)["kind"], "unavailable")
        with self.assertRaisesRegex(dc.DisplayContextError, "CUA_DISPLAY_UNAVAILABLE"):
            dc.check(env)

    def test_changed_vt_session_or_user_is_rejected(self):
        for changed in (dict(active_vt="tty1", vtnr=1),
                        dict(session_id="c2"), dict(uid=1001), dict(leader=124)):
            with self.subTest(changed=changed):
                with patch.object(dc, "_physical", return_value=self.context(**changed)):
                    with self.assertRaisesRegex(dc.DisplayContextError, "CUA_FOREGROUND_SESSION_CHANGED"):
                        dc.check(self.bound())

    def test_input_display_must_match_physical_binding(self):
        env = self.bound()
        env["DISPLAY"] = ":199"
        with patch.object(dc, "_physical", return_value=self.context()):
            with self.assertRaisesRegex(dc.DisplayContextError, "CUA_DISPLAY_BINDING_MISMATCH"):
                dc.check(env)

    def test_virtual_display_cannot_silently_switch(self):
        env = {"DISPLAY": ":140"}
        dc.bind_virtual(env, "/profile/a", explicit=True)
        dc.check(env)
        env["DISPLAY"] = ":0"
        with self.assertRaisesRegex(dc.DisplayContextError, "VIRTUAL_DISPLAY_BINDING_MISMATCH"):
            dc.check(env)

    def test_public_metadata_omits_authority_reference(self):
        context = dc.describe(self.bound())
        self.assertNotIn("authority", context)
        self.assertTrue(context["physical"])

    def test_other_profile_binding_is_not_adopted(self):
        env = self.bound()
        with patch.object(dc, "_physical", return_value=self.context(session_id="c2")):
            dc.configure(env, "auto", "/profile/b")
        self.assertEqual(dc.describe(env)["session_id"], "c2")
        self.assertEqual(dc.describe(env)["profile"], "/profile/b")

    def test_invalid_binding_is_rejected(self):
        with self.assertRaisesRegex(dc.DisplayContextError, "DISPLAY_BINDING_INVALID"):
            dc.check({dc.BINDING: "invalid"})

if __name__ == "__main__":
    unittest.main()

class DisplayBindingAuditTests(unittest.TestCase):
    def test_headless_no_late_failure_refuses_virtual_fallback(self):
        env = {dc.BINDING: json.dumps({"kind": "physical-x11", "physical": True})}
        with self.assertRaisesRegex(dc.DisplayContextError, "NO_VIRTUAL_FALLBACK"):
            dc.private_display_required(env, "no", False)
        self.assertFalse(dc.private_display_required(env, "no", True))

    def test_auto_late_failure_does_not_start_virtual_desktop(self):
        env = {dc.BINDING: json.dumps({"kind": "physical-x11", "physical": True})}
        self.assertFalse(dc.private_display_required(env, "auto", False))

    def test_headless_no_ignores_inherited_virtual_binding(self):
        env = {dc.BINDING: json.dumps({"kind": "virtual-x11", "profile": "/fixture"}),
               "DISPLAY": ":199"}
        with patch.object(dc, "_physical", side_effect=dc.DisplayContextError("NO_SESSION")):
            dc.configure(env, "no", "/fixture")
        self.assertEqual(dc.describe(env)["kind"], "unavailable")

    def test_explicit_virtual_ignores_inherited_physical_binding(self):
        env = {dc.BINDING: json.dumps({"kind": "physical-x11", "profile": "/fixture"})}
        with patch.object(dc, "_physical") as physical:
            dc.configure(env, "yes", "/fixture")
        physical.assert_not_called()
        self.assertEqual(dc.describe(env)["kind"], "virtual-x11")
        self.assertTrue(dc.describe(env)["explicit"])

    def test_non_object_binding_is_invalid_and_can_be_reconfigured(self):
        env = {dc.BINDING: "[]"}
        with self.assertRaisesRegex(dc.DisplayContextError, "DISPLAY_BINDING_INVALID"):
            dc.check(env)
        self.assertEqual(dc.describe(env)["kind"], "unavailable")
        with patch.object(dc, "_physical", side_effect=dc.DisplayContextError("NO_SESSION")):
            dc.configure(env, "auto", "/fixture")
        self.assertEqual(dc.describe(env)["kind"], "text")
