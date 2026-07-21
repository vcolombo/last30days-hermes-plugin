"""Structural enforcement of the run-mode seam (lib/run_mode.py).

The two-phase inject execution mode ("am I injected / plan-only, so I must
never touch a live credentialed backend?") was once re-derived inline as raw
`config.get("_inject_results") is not None` / `config.get("_plan_queries_only")
is True` checks at ~9 sites. Nothing forced a new isolation rule to be added at
every site, so three separate credential-isolation holes shipped — one per
layer a contributor forgot. `lib/run_mode.py` now owns that predicate; this test
keeps it owned.

Every module except `run_mode.py` must ask the question through the predicates
(`is_injected` / `is_plan_only` / `is_two_phase`), never by reading
`_inject_results` / `_plan_queries_only` as a truth value. This lint flags any
access of those keys — `config.get(key[, default])` OR a `config[key]` read —
**except** when it is directly the value of an assignment. That one exception is
the sole legitimate reader of the raw key: `inj = config.get("_inject_results")`
in `_injected_results`, which needs the injected *data*, not a bool. Config
*writes* (`config["_inject_results"] = …`) are store-context subscripts, not
reads, so they never match.

This catches the material re-scatter forms — a truthiness `if config.get(key):`
(wrong: an empty injection is falsy, which reopens the live-fallback hole a
predicate closes), a subscript comparison `config[key] is not None`, and the
two-arg `config.get(key, None)`. It does NOT catch indirection through an
aliased key variable (`k = "_inject_results"; config.get(k)`), which is beyond
static AST reach; that residue is accepted, not claimed away.

AST-based (not regex), mirroring tests/test_source_log_visibility.py.

Scope: the config-layer key access only. The three pre-config arg-layer gates
read the mode via `run_mode.planned_two_phase(args)`; the raw
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


def _is_banned_access(node: ast.AST) -> bool:
    """True for a read of a banned key: `<x>.get("_inject_results"[, ...])`
    (any arg count) or a `<x>["_inject_results"]` load subscript."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value in BANNED_KEYS
    ):
        return True
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.ctx, ast.Load)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value in BANNED_KEYS
    ):
        return True
    return False


def _violation_lines(tree: ast.AST) -> list[int]:
    """Banned key accesses used as anything other than a bare assignment value.

    The lone allowed form is `<name> = <config>.get("_inject_results")` — the
    data read in `_injected_results`. Everything else (comparison, truthiness
    test, boolean op, argument, return) is a re-scatter of the mode predicate.
    """
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child._run_mode_parent = parent  # type: ignore[attr-defined]
    hits: list[int] = []
    for node in ast.walk(tree):
        if not _is_banned_access(node):
            continue
        parent = getattr(node, "_run_mode_parent", None)
        allowed = isinstance(parent, ast.Assign) and parent.value is node
        if not allowed:
            hits.append(node.lineno)
    return sorted(hits)


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
            violations += [
                f"{path.relative_to(REPO_ROOT)}:{ln}"
                for ln in _violation_lines(tree)]
        if violations:
            self.fail(
                "Two-phase run-mode checks must go through lib/run_mode.py "
                "(is_injected / is_plan_only / is_two_phase), not a raw "
                "config.get(...) / config[...] read used as a truth value. "
                "The only allowed raw read is `inj = config.get('_inject_"
                "results')` for the injected data itself. Violations:\n  - "
                + "\n  - ".join(violations))

    def test_lint_catches_predicate_and_truthiness_forms(self):
        """Every re-scatter form must fire — guard against a no-op lint."""
        cases = {
            'if config.get("_inject_results") is not None:\n    pass\n': [1],
            'if config.get("_inject_results"):\n    pass\n': [1],  # truthiness
            'if config["_inject_results"] is not None:\n    pass\n': [1],  # subscript
            'if config.get("_plan_queries_only", None):\n    pass\n': [1],  # 2-arg
            'x = config.get("_inject_results") or {}\n': [1],  # boolop, not bare
            'return config.get("_plan_queries_only") is True\n': [1],
        }
        for src, expected in cases.items():
            self.assertEqual(
                expected, _violation_lines(ast.parse(src)),
                msg=f"lint missed: {src!r}")

    def test_legit_data_read_and_writes_are_not_flagged(self):
        """The one allowed data read + config writes must NOT match."""
        legit = (
            'inj = config.get("_inject_results")\n'          # the allowed read
            'data = config["_inject_results"]\n'             # subscript read, bound
            'config["_inject_results"] = load()\n'           # write (store)
            'config["_plan_queries_only"] = True\n'          # write (store)
            'config = {**config, "_plan_queries_only": True}\n')  # dict literal key
        self.assertEqual([], _violation_lines(ast.parse(legit)))


if __name__ == "__main__":
    unittest.main()
