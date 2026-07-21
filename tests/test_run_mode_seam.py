"""Structural enforcement of the run-mode seam (lib/run_mode.py).

The two-phase inject execution mode ("am I injected / plan-only, so I must
never touch a live credentialed backend?") was once re-derived inline as raw
`config.get("_inject_results") is not None` / `config.get("_plan_queries_only")
is True` checks at ~9 sites. Nothing forced a new isolation rule to be added at
every site, so three separate credential-isolation holes shipped — one per
layer a contributor forgot. `lib/run_mode.py` now owns that predicate; this test
keeps it owned.

Every module except `run_mode.py` itself must ask the question through the
predicates (`is_injected` / `is_plan_only` / `is_two_phase`), never by spelling
`config.get("_inject_results"/"_plan_queries_only")` inside a comparison. The
legitimate data read (`inj = config.get("_inject_results")` in
`_injected_results`) and the config writes (`config["_inject_results"] = …`)
are untouched — they are not comparisons, so they never match.

AST-based (not regex), mirroring tests/test_source_log_visibility.py: robust
against multi-line calls, whitespace, and `is not None` vs `!= None` variants.

Scope: the config-layer predicate spelling only. The three pre-config arg-layer
gates read the mode via `run_mode.planned_two_phase(args)`; the raw
`args.inject_results is not None` load trigger is deliberately not linted (it
performs the actual injection load, not a mode check).
"""

import ast
import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "skills" / "last30days" / "scripts"
LIB_DIR = SCRIPTS / "lib"

BANNED_KEYS = {"_inject_results", "_plan_queries_only"}


def _is_banned_get(node: ast.AST) -> bool:
    """True for a `<x>.get("_inject_results"|"_plan_queries_only")` call."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value in BANNED_KEYS
    )


class _CompareFinder(ast.NodeVisitor):
    """Collect line numbers where a banned `.get(...)` is used in a comparison."""

    def __init__(self) -> None:
        self.hits: list[int] = []

    def visit_Compare(self, node: ast.Compare) -> None:
        for operand in (node.left, *node.comparators):
            if _is_banned_get(operand):
                self.hits.append(node.lineno)
                break
        self.generic_visit(node)


def _scanned_modules() -> list[pathlib.Path]:
    paths: list[pathlib.Path] = [SCRIPTS / "last30days.py"]
    for path in LIB_DIR.rglob("*.py"):
        if path.name in ("run_mode.py", "__init__.py"):
            continue  # run_mode.py IS the seam; __init__ is a bare marker
        if "vendor" in path.parts:
            continue
        paths.append(path)
    return sorted(paths)


class RunModeSeamTests(unittest.TestCase):
    def test_no_raw_mode_predicate_outside_run_mode(self):
        violations: list[str] = []
        for path in _scanned_modules():
            text = path.read_text(encoding="utf-8")
            try:
                tree = ast.parse(text, filename=str(path))
            except SyntaxError as exc:
                self.fail(f"Could not parse {path.name}: {exc}")
            finder = _CompareFinder()
            finder.visit(tree)
            violations += [
                f"{path.relative_to(REPO_ROOT)}:{ln}" for ln in finder.hits]
        if violations:
            self.fail(
                "Two-phase run-mode checks must go through lib/run_mode.py "
                "(is_injected / is_plan_only / is_two_phase), not a raw "
                "config.get(...) comparison. Consolidating these prevented "
                "three credential-isolation holes; this test keeps them "
                "consolidated. Violations:\n  - " + "\n  - ".join(violations))

    def test_lint_catches_a_planted_violation(self):
        """The scanner must actually fire — guard against a no-op lint."""
        planted = 'if config.get("_inject_results") is not None:\n    pass\n'
        finder = _CompareFinder()
        finder.visit(ast.parse(planted))
        self.assertEqual([1], finder.hits)

    def test_data_read_and_writes_are_not_flagged(self):
        """The legit data read and config writes must NOT match."""
        legit = (
            'inj = config.get("_inject_results")\n'
            'config["_inject_results"] = load()\n'
            'config["_plan_queries_only"] = True\n')
        finder = _CompareFinder()
        finder.visit(ast.parse(legit))
        self.assertEqual([], finder.hits)


if __name__ == "__main__":
    unittest.main()
