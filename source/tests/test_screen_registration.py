"""Exercise the actual portable screen registration callback and its UUID contract.

The bounded executor is a fixture. This is not VM, GUI, TTY or input acceptance.
"""
import ast
from pathlib import Path
import unittest

from framebridge_expanded.real_screen.screen_frontdoor import register_framebridge_screen_tools
from test_screen_frontdoor import Executor, Image, MCP, NODE_ID


PORTABLE = Path(__file__).resolve().parents[1] / "portable/front_door_mcp.py"


class PortableScreenRegistrationTests(unittest.TestCase):
    def registration(self, target="LOCAL", corrupt=None):
        fixture = Executor(hostname="fb-central-lab" if target == "LOCAL" else "fb-node-lab")
        calls, legacy_calls = [], []

        def expanded(alias, script, authorized, timeout_s=120, job_id="", sha256="", include_stdout=False):
            args = dict(script=script, authorized=authorized, timeout_s=timeout_s,
                        job_id=job_id, sha256=sha256, include_stdout=include_stdout, target=alias)
            calls.append(args)
            result = fixture(**args)
            if corrupt == "uuid":
                result.pop("node_id")
            elif corrupt == "hash":
                result["mcp_script_sha256"] = "0" * 64
            return result

        def legacy(**kwargs):
            # Model the real legacy LOCAL envelope which lacks the Fleet UUID.
            legacy_calls.append(kwargs)
            if kwargs["target"] != "LOCAL":
                remote = dict(kwargs)
                return expanded(remote.pop("target"), **remote)
            result = fixture(**kwargs)
            result.pop("node_id")
            return result

        tree = ast.parse(PORTABLE.read_text())
        definitions = [item for item in tree.body
                       if isinstance(item, ast.FunctionDef) and item.name == "_screen_exec_request"]
        registrations = [item for item in tree.body
                         if isinstance(item, ast.Assign) and any(
                             isinstance(name, ast.Name) and name.id == "_real_screen_client"
                             for name in item.targets)]
        self.assertEqual(len(registrations), 1)
        scope = dict(mcp=MCP(), Image=Image, expanded_execute_node=expanded, expanded_execute_screen_node=expanded,
                     framebridge_exec=legacy,
                     register_framebridge_screen_tools=register_framebridge_screen_tools,
                     resolve_profile_node=lambda node: node)
        # Load the definition and registration from production source rather
        # than recreating the adapter inside this test.
        module = ast.Module(body=definitions + registrations, type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), str(PORTABLE), "exec"), scope)
        return scope["mcp"], fixture, calls, legacy_calls, scope["_real_screen_client"]

    def assert_observe(self, target):
        mcp, fixture, calls, legacy_calls, _ = self.registration(target)
        response = mcp.tools["framebridge_screen_observe"](target, True)
        self.assertIsInstance(response, list)
        metadata, image = response
        self.assertTrue(metadata["ok"])
        self.assertEqual(metadata["requested_node"], target)
        self.assertEqual(metadata["node_id"], NODE_ID)
        self.assertEqual(metadata["hostname"], fixture.hostname)
        self.assertEqual(image.data, fixture.data)
        self.assertEqual(image.format, "png")
        if target == "LOCAL":
            self.assertFalse(legacy_calls)
        self.assertGreater(len(calls), 1)  # Manifest plus bound image chunks.
        self.assertTrue(all(item["target"] == target for item in calls))
        self.assertTrue(all(item["authorized"] is True and item["include_stdout"] is True
                            and item["timeout_s"] == 30 for item in calls))

    def test_actual_registration_preserves_local_fleet_uuid(self):
        self.assert_observe("LOCAL")

    def test_actual_registration_preserves_remote_alias_and_hostname(self):
        self.assert_observe("fb-node")

    def test_actual_registration_still_rejects_missing_uuid(self):
        mcp, _, _, _, _ = self.registration(corrupt="uuid")
        response = mcp.tools["framebridge_screen_observe"]("LOCAL", True)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "SCREEN_EXEC_NODE_ID_INVALID")

    def test_actual_registration_still_rejects_wrong_script_binding(self):
        mcp, _, _, _, _ = self.registration(corrupt="hash")
        response = mcp.tools["framebridge_screen_observe"]("LOCAL", True)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "SCREEN_EXEC_BINDING_INVALID")

    def test_unauthorized_observation_never_reaches_executor(self):
        mcp, _, calls, legacy_calls, _ = self.registration()
        response = mcp.tools["framebridge_screen_observe"]("LOCAL", False)
        self.assertFalse(response["ok"])
        self.assertEqual(response["error"], "AUTHORIZED_TRUE_REQUIRED")
        self.assertFalse(calls)
        self.assertFalse(legacy_calls)


if __name__ == "__main__":
    unittest.main()
