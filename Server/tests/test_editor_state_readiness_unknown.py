import ast
import copy
import pathlib
import unittest
from typing import Any


SOURCE = pathlib.Path(__file__).parents[1] / "src/services/resources/editor_state.py"
NOW = 200_000


def load_actual_helper():
    module = ast.parse(SOURCE.read_text())
    function = next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_enrich_advice_and_staleness"
    )
    namespace = {"Any": Any, "_now_unix_ms": lambda: NOW}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace[function.name]


def idle_state():
    return {
        "observed_at_unix_ms": NOW,
        "compilation": {"is_compiling": False, "is_domain_reload_pending": False},
        "tests": {"is_running": False},
        "assets": {"refresh": {"is_refresh_in_progress": False}},
    }


def load_actual_response_schema():
    from pydantic import BaseModel

    module = ast.parse(SOURCE.read_text())
    classes = [node for node in module.body if isinstance(node, ast.ClassDef)]
    namespace = {"Any": Any, "BaseModel": BaseModel, "__name__": __name__}
    exec(compile(ast.Module(body=classes, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace["EditorStateData"]


class EditorReadinessUnknownTests(unittest.TestCase):
    def setUp(self):
        self.enrich = load_actual_helper()

    def test_absent_editor_safety_state_is_unknown_not_ready(self):
        result = self.enrich({"observed_at_unix_ms": NOW})
        self.assertIsNone(result["advice"]["ready_for_tools"])
        self.assertIn("incomplete_status", result["advice"]["blocking_reasons"])
        self.assertNotEqual(result["advice"]["recommended_next_action"], "none")

    def test_null_safety_sections_are_unknown_not_ready(self):
        result = self.enrich({"observed_at_unix_ms": NOW, "compilation": None, "tests": None, "assets": None})
        self.assertIsNone(result["advice"]["ready_for_tools"])

    def test_each_unknown_safety_value_prevents_ready(self):
        paths = [("compilation", "is_compiling"), ("compilation", "is_domain_reload_pending"),
                 ("tests", "is_running"), ("assets", "refresh", "is_refresh_in_progress")]
        for location in paths:
            for value in [None, 0, "false", [], {}]:
                with self.subTest(location=location, value=value):
                    state = idle_state()
                    parent = state
                    for field in location[:-1]:
                        parent = parent[field]
                    parent[location[-1]] = value
                    result = self.enrich(state)
                    self.assertIsNone(result["advice"]["ready_for_tools"])

    def test_each_missing_safety_value_prevents_ready(self):
        for section, field in [("compilation", "is_compiling"), ("compilation", "is_domain_reload_pending"), ("tests", "is_running")]:
            with self.subTest(section=section, field=field):
                state = idle_state()
                del state[section][field]
                self.assertIsNone(self.enrich(state)["advice"]["ready_for_tools"])
        state = idle_state()
        del state["assets"]["refresh"]["is_refresh_in_progress"]
        self.assertIsNone(self.enrich(state)["advice"]["ready_for_tools"])

    def test_malformed_safety_sections_remain_unknown_without_throwing(self):
        for section in ["compilation", "tests", "assets"]:
            for value in ["invalid", [False], True]:
                with self.subTest(section=section, value=value):
                    state = idle_state()
                    state[section] = value
                    self.assertIsNone(self.enrich(state)["advice"]["ready_for_tools"])
        state = idle_state()
        state["assets"]["refresh"] = "invalid"
        self.assertIsNone(self.enrich(state)["advice"]["ready_for_tools"])

    def test_complete_fresh_idle_state_keeps_its_ready_behavior(self):
        result = self.enrich(idle_state())
        self.assertIs(result["advice"]["ready_for_tools"], True)
        self.assertEqual(result["advice"]["blocking_reasons"], [])
        self.assertEqual(result["advice"]["recommended_retry_after_ms"], 0)
        self.assertEqual(result["staleness"], {"age_ms": 0, "is_stale": False})

    def test_known_busy_state_remains_blocked_even_with_other_unknown_fields(self):
        result = self.enrich({"observed_at_unix_ms": NOW, "compilation": {"is_compiling": True}})
        self.assertIs(result["advice"]["ready_for_tools"], False)
        self.assertIn("compiling", result["advice"]["blocking_reasons"])

    def test_each_known_busy_reason_is_preserved(self):
        for section, field, reason in [("compilation", "is_compiling", "compiling"),
                                       ("compilation", "is_domain_reload_pending", "domain_reload"),
                                       ("tests", "is_running", "running_tests")]:
            with self.subTest(reason=reason):
                state = idle_state()
                state[section][field] = True
                result = self.enrich(state)
                self.assertIs(result["advice"]["ready_for_tools"], False)
                self.assertIn(reason, result["advice"]["blocking_reasons"])
        state = idle_state()
        state["assets"]["refresh"]["is_refresh_in_progress"] = True
        result = self.enrich(state)
        self.assertIs(result["advice"]["ready_for_tools"], False)
        self.assertIn("asset_refresh", result["advice"]["blocking_reasons"])

    def test_stale_state_remains_blocked(self):
        state = idle_state()
        state["observed_at_unix_ms"] = NOW - 3000
        result = self.enrich(state)
        self.assertIs(result["advice"]["ready_for_tools"], False)
        self.assertIn("stale_status", result["advice"]["blocking_reasons"])

    def test_missing_sections_are_not_filled_with_false_safety_values(self):
        state = {"observed_at_unix_ms": NOW, "compilation": None}
        original = copy.deepcopy(state)
        self.enrich(state)
        self.assertEqual(state["compilation"], original["compilation"])
        self.assertNotIn("tests", state)
        self.assertNotIn("assets", state)

    def test_actual_response_schema_preserves_unknown_advice(self):
        state = {"schema_version": "unity-mcp/editor_state@2", "observed_at_unix_ms": NOW, "sequence": 0}
        result = load_actual_response_schema().model_validate(self.enrich(state)).model_dump()
        self.assertIsNone(result["advice"]["ready_for_tools"])
        self.assertIsNone(result["compilation"])
        self.assertIsNone(result["tests"])

    def test_actual_response_schema_preserves_healthy_advice(self):
        state = idle_state()
        state.update({"schema_version": "unity-mcp/editor_state@2", "sequence": 1})
        result = load_actual_response_schema().model_validate(self.enrich(state)).model_dump()
        self.assertIs(result["advice"]["ready_for_tools"], True)


if __name__ == "__main__":
    unittest.main()
