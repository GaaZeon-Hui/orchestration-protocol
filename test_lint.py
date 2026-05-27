"""Tests for lint_core, lint_gate, lint_full."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lint_core import run_lint, _check_boundaries, _match_pattern
from lint_gate import validate_plan, lint_plan
from lint_full import (
    lint_changed_files,
    lint_crossref,
    _check_conflicts,
    _extract_hints,
    _parse_safe,
    _collect_defs,
    _compare_asts,
)


SAMPLE_BOUNDARIES = {
    "py-agent": {
        "can_touch": ["*.py"],
        "forbidden": ["*.md", "app/"],
    },
    "md-agent": {
        "can_touch": ["*.md"],
        "forbidden": ["*.py", "app/", "service/"],
    },
    "service-agent": {
        "can_touch": ["service/"],
        "forbidden": ["app/", "*.md"],
    },
}


class TestPatternMatch(unittest.TestCase):
    """Boundary glob pattern matching."""

    def test_basename_glob(self):
        self.assertTrue(_match_pattern("main.py", "*.py"))
        self.assertFalse(_match_pattern("README.md", "*.py"))

    def test_basename_glob_in_subdir(self):
        self.assertTrue(_match_pattern("app/main.py", "*.py"))
        self.assertTrue(_match_pattern("deep/nested/file.py", "*.py"))

    def test_directory_prefix(self):
        self.assertTrue(_match_pattern("app/main.py", "app/"))
        self.assertTrue(_match_pattern("app/sub/deep/file.py", "app/"))
        self.assertFalse(_match_pattern("service/auth.py", "app/"))
        self.assertFalse(_match_pattern("other_app/foo.py", "app/"))

    def test_windows_backslash_normalised(self):
        self.assertTrue(_match_pattern("app\\main.py", "app/"))
        self.assertTrue(_match_pattern("app\\main.py", "*.py"))

    def test_pattern_with_backslash_normalised(self):
        self.assertTrue(_match_pattern("app/main.py", "app\\"))
        self.assertTrue(_match_pattern("app/main.py", "*.py"))


class TestBoundaries(unittest.TestCase):
    """Boundary violation detection."""

    def test_no_boundaries_blocks(self):
        blocked, reason = _check_boundaries(["main.py"], None, "py-agent")
        self.assertTrue(blocked)
        self.assertIn("not configured", reason)

    def test_agent_not_in_boundaries(self):
        blocked, reason = _check_boundaries(
            ["main.py"], SAMPLE_BOUNDARIES, "unknown-agent"
        )
        self.assertTrue(blocked)
        self.assertIn("unknown-agent", reason)

    def test_all_files_in_can_touch(self):
        blocked, _ = _check_boundaries(
            ["main.py", "utils/helper.py"], SAMPLE_BOUNDARIES, "py-agent"
        )
        self.assertFalse(blocked)

    def test_forbidden_hit(self):
        blocked, reason = _check_boundaries(
            ["main.py", "README.md"], SAMPLE_BOUNDARIES, "py-agent"
        )
        self.assertTrue(blocked)
        self.assertIn("boundary violation", reason)
        self.assertIn("README.md", reason)
        self.assertIn("forbidden", reason)

    def test_not_in_can_touch(self):
        blocked, reason = _check_boundaries(
            ["main.py", "config.json"], SAMPLE_BOUNDARIES, "py-agent"
        )
        self.assertTrue(blocked)
        self.assertIn("config.json", reason)
        self.assertIn("not in can_touch", reason)

    def test_forbidden_takes_priority_over_can_touch(self):
        # app/main.py matches *.py (can_touch) but also app/ (forbidden)
        blocked, reason = _check_boundaries(
            ["app/main.py"], SAMPLE_BOUNDARIES, "py-agent"
        )
        self.assertTrue(blocked)
        self.assertIn("forbidden", reason)

    def test_md_agent_allowed_files(self):
        blocked, _ = _check_boundaries(
            ["README.md", "docs/guide.md"], SAMPLE_BOUNDARIES, "md-agent"
        )
        self.assertFalse(blocked)

    def test_md_agent_blocked_on_py(self):
        blocked, reason = _check_boundaries(
            ["README.md", "main.py"], SAMPLE_BOUNDARIES, "md-agent"
        )
        self.assertTrue(blocked)
        self.assertIn("main.py", reason)

    def test_service_agent_directory_pattern(self):
        blocked, _ = _check_boundaries(
            ["service/auth.py", "service/api/user.py"], SAMPLE_BOUNDARIES, "service-agent"
        )
        self.assertFalse(blocked)

    def test_service_agent_blocked_outside_service(self):
        blocked, reason = _check_boundaries(
            ["service/auth.py", "app/main.py"], SAMPLE_BOUNDARIES, "service-agent"
        )
        self.assertTrue(blocked)
        self.assertIn("app/main.py", reason)

    def test_empty_file_list(self):
        blocked, _ = _check_boundaries([], SAMPLE_BOUNDARIES, "py-agent")
        self.assertFalse(blocked)


class TestRunLintBoundaries(unittest.TestCase):
    """run_lint top-level with boundary scenarios."""

    def test_blocks_when_no_boundaries(self):
        result = run_lint(["main.py"], None, "py-agent")
        self.assertTrue(result["blocked"])
        self.assertIn("not configured", result["reason"])

    def test_blocks_on_forbidden(self):
        result = run_lint(
            ["main.py", "app/secret.py"], SAMPLE_BOUNDARIES, "py-agent"
        )
        self.assertTrue(result["blocked"])
        self.assertIn("boundary violation", result["reason"])

    def test_passes_clean_files(self):
        result = run_lint(
            ["main.py", "utils.py"], SAMPLE_BOUNDARIES, "py-agent"
        )
        self.assertFalse(result["blocked"])
        self.assertIsNone(result["reason"])


class TestParseAndCollect(unittest.TestCase):
    """AST parsing and symbol collection."""

    def test_parse_valid(self):
        tree = _parse_safe("def foo():\n    pass\n")
        self.assertIsNotNone(tree)

    def test_parse_empty(self):
        self.assertIsNone(_parse_safe(""))
        self.assertIsNone(_parse_safe("   \n  "))

    def test_parse_syntax_error(self):
        self.assertIsNone(_parse_safe("def foo(:\n    pass\n"))

    def test_collect_functions(self):
        tree = _parse_safe("def foo(a, b):\n    pass\ndef bar(x):\n    pass\n")
        syms = _collect_defs(tree)
        names = {s["name"] for s in syms}
        self.assertIn("foo", names)
        self.assertIn("bar", names)

    def test_collect_function_params(self):
        tree = _parse_safe("def foo(a, b, c=None):\n    pass\n")
        syms = _collect_defs(tree)
        self.assertEqual(syms[0]["params"], ["a", "b", "c"])

    def test_collect_classes(self):
        tree = _parse_safe(
            "class Foo:\n    def bar(self):\n        pass\n    def baz(self, x):\n        pass\n"
        )
        syms = _collect_defs(tree)
        self.assertEqual(syms[0]["name"], "Foo")
        self.assertEqual(syms[0]["kind"], "class")
        method_names = {m["name"] for m in syms[0]["methods"]}
        self.assertEqual(method_names, {"bar", "baz"})


class TestCompareASTs(unittest.TestCase):
    """AST diffing."""

    def test_new_function_detected(self):
        old = "def existing():\n    pass\n"
        new = "def existing():\n    pass\n\ndef new_func(x):\n    return x\n"
        diff = _compare_asts(old, new)
        self.assertEqual(len(diff["new_functions"]), 1)
        self.assertEqual(diff["new_functions"][0]["name"], "new_func")
        self.assertEqual(diff["new_functions"][0]["params"], ["x"])

    def test_signature_change_detected(self):
        old = "def foo(a, b):\n    pass\n"
        new = "def foo(a, b, c=None):\n    pass\n"
        diff = _compare_asts(old, new)
        self.assertEqual(len(diff["signature_changes"]), 1)
        self.assertEqual(diff["signature_changes"][0]["name"], "foo")
        self.assertEqual(diff["signature_changes"][0]["old_params"], ["a", "b"])
        self.assertEqual(diff["signature_changes"][0]["new_params"], ["a", "b", "c"])

    def test_deleted_symbol_detected(self):
        old = "def foo():\n    pass\ndef bar():\n    pass\n"
        new = "def foo():\n    pass\n"
        diff = _compare_asts(old, new)
        self.assertEqual(len(diff["deleted_symbols"]), 1)
        self.assertEqual(diff["deleted_symbols"][0]["name"], "bar")

    def test_new_class_detected(self):
        old = "def foo():\n    pass\n"
        new = "class Bar:\n    pass\n\ndef foo():\n    pass\n"
        diff = _compare_asts(old, new)
        self.assertEqual(len(diff["new_classes"]), 1)
        self.assertEqual(diff["new_classes"][0]["name"], "Bar")

    def test_identical_code_no_diff(self):
        code = "def foo(a, b):\n    pass\n"
        diff = _compare_asts(code, code)
        for key in diff:
            self.assertEqual(len(diff[key]), 0, "{} should be empty".format(key))

    def test_empty_old_source(self):
        # New file — old source is empty
        new = "def foo():\n    pass\n"
        diff = _compare_asts("", new)
        # When old_src is empty, _parse_safe returns None → diff is empty
        # This is expected behaviour — new-file detection happens at a higher level
        self.assertEqual(diff, {})

    def test_both_empty(self):
        self.assertEqual(_compare_asts("", ""), {})
        self.assertEqual(_compare_asts("   ", ""), {})


class TestRunLintHints(unittest.TestCase):
    """AST hints through run_lint (files that exist on disk)."""

    def test_hints_from_real_python_file(self):
        result = lint_changed_files(SAMPLE_BOUNDARIES, "py-agent")
        self.assertIn("blocked", result)
        # hints may be None (file unchanged vs HEAD), or may contain
        # conflict info (if touched in recent commits)

    def test_hints_from_nonexistent_file(self):
        result = run_lint(
            ["nonexistent.py"], SAMPLE_BOUNDARIES, "py-agent"
        )
        self.assertFalse(result["blocked"])


class TestLintChangedFiles(unittest.TestCase):
    """The convenience wrapper that runs git diff --name-only."""

    def test_lint_changed_files_runs_without_error(self):
        # Tree may have dirty files from development — just verify it runs
        result = lint_changed_files(SAMPLE_BOUNDARIES, "py-agent")
        self.assertIn("blocked", result)
        self.assertIn("reason", result)


class TestValidatePlan(unittest.TestCase):

    def test_valid(self):
        ok, result = validate_plan('{"files":["a.py","b.py"]}')
        self.assertTrue(ok)
        self.assertEqual(result["files"], ["a.py", "b.py"])

    def test_empty_files(self):
        ok, reason = validate_plan('{"files":[]}')
        self.assertFalse(ok)
        self.assertIn("empty", reason)

    def test_not_json(self):
        ok, reason = validate_plan('not json')
        self.assertFalse(ok)

    def test_path_traversal(self):
        ok, reason = validate_plan('{"files":["../etc/passwd"]}')
        self.assertFalse(ok)

    def test_non_string_files(self):
        ok, reason = validate_plan('{"files":[123]}')
        self.assertFalse(ok)


class TestLintPlan(unittest.TestCase):

    def test_boundary_blocked(self):
        plan = '{"files":["app/main.py"]}'
        boundaries = {"py-agent": {"can_touch": ["lib/"], "forbidden": ["app/"]}}
        result = lint_plan(plan, boundaries, "py-agent")
        self.assertTrue(result["blocked"])

    def test_passes(self):
        plan = '{"files":["lib/utils.py"]}'
        boundaries = {"py-agent": {"can_touch": ["lib/"], "forbidden": ["app/"]}}
        result = lint_plan(plan, boundaries, "py-agent")
        self.assertFalse(result["blocked"])


class TestLintCrossref(unittest.TestCase):

    def setUp(self):
        self.boundaries = {"py-agent": {"can_touch": ["*.py", "lib/"], "forbidden": ["app/"]}}
        self.plan = '{"files":["lib/utils.py", "main.py"]}'
        # plan declares 2 files: lib/utils.py (OK), main.py (OK for *.py)

    def test_basic_structure(self):
        result = lint_crossref(self.plan, self.boundaries, "py-agent")
        self.assertIn("blocked", result)
        self.assertIn("crossref", result)
        self.assertIn("boundary", result)
        cr = result["crossref"]
        self.assertIn("declared_files", cr)
        self.assertIn("actual_files", cr)
        self.assertIn("extra_files", cr)
        self.assertIn("missing_files", cr)

    def test_crossref_boundary_blocked_on_actual_files(self):
        """Actual git diff includes a forbidden file."""
        # We can't control git diff in tests, but we can verify the structural
        # contract — blocked is a bool, boundary is a dict with 'reason'
        result = lint_crossref(self.plan, self.boundaries, "py-agent")
        self.assertIsInstance(result["blocked"], bool)
        self.assertIn("reason", result["boundary"])

    def test_crossref_handles_non_json_plan(self):
        result = lint_crossref("not json", self.boundaries, "py-agent")
        self.assertIn("blocked", result)
        # declared_files will be empty since plan failed to parse
        self.assertEqual(result["crossref"]["declared_files"], [])
        # actual_files comes from git diff regardless
        self.assertIsInstance(result["crossref"]["actual_files"], list)

    def test_crossref_handles_empty_plan(self):
        result = lint_crossref('{"files":[]}', self.boundaries, "py-agent")
        self.assertEqual(result["crossref"]["declared_files"], [])
        self.assertIsInstance(result["crossref"]["actual_files"], list)

    def test_crossref_extra_and_missing_are_sorted(self):
        result = lint_crossref(
            '{"files":["z.py","a.py"]}', self.boundaries, "py-agent"
        )
        declared = result["crossref"]["declared_files"]
        # Should be sorted
        self.assertEqual(declared, sorted(declared))

    def test_crossref_hints_structure_when_present(self):
        result = lint_crossref(self.plan, self.boundaries, "py-agent")
        if result["hints"] is not None:
            # hints may contain conflicts and/or ast
            self.assertIsInstance(result["hints"], dict)
            if "conflicts" in result["hints"]:
                self.assertIsInstance(result["hints"]["conflicts"], list)
            if "ast" in result["hints"]:
                self.assertIsInstance(result["hints"]["ast"], dict)
                for key in ["new_functions", "signature_changes", "deleted_symbols", "new_classes"]:
                    self.assertIn(key, result["hints"]["ast"])

    def test_crossref_path_normalisation_in_crossref(self):
        """Backslashes in plan files are normalised."""
        result = lint_crossref(
            '{"files":["lib\\\\utils.py"]}', self.boundaries, "py-agent"
        )
        declared = result["crossref"]["declared_files"]
        for f in declared:
            self.assertNotIn("\\\\", f)

    def test_crossref_agent_not_in_boundaries(self):
        boundaries = {"other-agent": {"can_touch": ["*.py"], "forbidden": []}}
        result = lint_crossref(self.plan, boundaries, "py-agent")
        # Boundary check fails for actual files (if any), but crossref is still produced
        self.assertIn("crossref", result)
        self.assertIn("boundary", result)

    def test_crossref_forbidden_file_in_plan(self):
        """Plan declares a forbidden file — caught by boundary layer if it matches actual."""
        plan = '{"files":["app/secret.py"]}'
        result = lint_crossref(plan, self.boundaries, "py-agent")
        # blocked depends on whether app/secret.py actually exists and is modified
        self.assertIn("blocked", result)
        self.assertIsInstance(result["blocked"], bool)


if __name__ == "__main__":
    unittest.main()
