"""Tests for lint.py — boundary checks, conflict detection, AST hints."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lint import (
    run_lint,
    lint_changed_files,
    _check_boundaries,
    _check_conflicts,
    _extract_hints,
    _match_pattern,
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
        self.assertIsNone(result["hints"])

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
        # Use pipeline.py — an existing committed file
        result = run_lint(
            ["pipeline.py"], SAMPLE_BOUNDARIES, "py-agent", base_ref="HEAD~5"
        )
        self.assertFalse(result["blocked"])
        # hints may be None (file unchanged vs HEAD), or may contain
        # conflict info (if pipeline.py touched in recent commits)
        # Both outcomes are valid — just verify it runs cleanly

    def test_hints_from_nonexistent_file(self):
        result = run_lint(
            ["nonexistent.py"], SAMPLE_BOUNDARIES, "py-agent"
        )
        self.assertFalse(result["blocked"])
        # nonexistent file is not a real file, so no AST hints
        if result["hints"]:
            self.assertNotIn("ast", result["hints"])


class TestLintChangedFiles(unittest.TestCase):
    """The convenience wrapper that runs git diff --name-only."""

    def test_lint_changed_files_runs_without_error(self):
        # Tree may have dirty files from development — just verify it runs
        result = lint_changed_files(SAMPLE_BOUNDARIES, "py-agent")
        self.assertIn("blocked", result)
        self.assertIn("reason", result)


if __name__ == "__main__":
    unittest.main()
