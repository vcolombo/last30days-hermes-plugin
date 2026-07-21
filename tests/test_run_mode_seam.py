"""Structural enforcement of the run-mode seam (lib/run_mode.py).

The two-phase inject execution mode ("am I injected / plan-only, so I must
never touch a live credentialed backend?") was once re-derived inline as raw
`config.get("_inject_results") is not None` / `config.get("_plan_queries_only")
is True` checks at ~9 sites. Nothing forced a new isolation rule to be added at
every site, so three separate credential-isolation holes shipped — one per
layer a contributor forgot. `lib/run_mode.py` now owns that predicate; this test
keeps it owned.

Every module except `run_mode.py` must ask the question through the predicates
(`is_injected` / `is_plan_only` / `is_two_phase`), and the injected *data* is
read only through `run_mode.injected_results(config)`. So NO raw read of these
keys is legitimate outside `run_mode.py`: this lint flags any `config.get(key)`
(any arg count) or `config[key]` read, with no assignment exemption. An earlier
version exempted "the direct value of any assignment," but that blessed a
value-alias escape (`mode = config.get(key)` then `if mode is not None:`) which
re-scatters the predicate one hop away — so the exemption is gone.

This catches every direct re-scatter form: comparison, a truthiness
`if config.get(key):` (wrong — an empty injection is falsy, which reopens the
live-fallback hole a predicate closes), a subscript `config[key] is not None`,
the two-arg `config.get(key, None)`, and a bare `x = config.get(key)`. Config
*writes* (`config["_inject_results"] = …`) are store-context subscripts, not
reads, so they never match. It does NOT catch indirection through an aliased
key *string* (`k = "_inject_results"; config.get(k)`), which is beyond static
AST reach; that residue is accepted, not claimed away.

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
    """Every raw read of a banned key. No exemption: the only legitimate reader
    is `run_mode.injected_results`, which lives in the excluded module."""
    return sorted(
        node.lineno for node in ast.walk(tree) if _is_banned_access(node))


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
                "(is_injected / is_plan_only / is_two_phase), and the injected "
                "data through run_mode.injected_results(config). No raw "
                "config.get('_inject_results') / config['_inject_results'] read "
                "is allowed outside run_mode.py. Violations:\n  - "
                + "\n  - ".join(violations))

    def test_lint_catches_every_raw_read_form(self):
        """Every re-scatter form must fire — guard against a no-op lint.
        Includes the bare assignment, which an earlier exemption let through."""
        cases = {
            'if config.get("_inject_results") is not None:\n    pass\n': [1],
            'if config.get("_inject_results"):\n    pass\n': [1],  # truthiness
            'if config["_inject_results"] is not None:\n    pass\n': [1],  # subscript
            'if config.get("_plan_queries_only", None):\n    pass\n': [1],  # 2-arg
            'x = config.get("_inject_results") or {}\n': [1],  # boolop
            'mode = config.get("_inject_results")\n': [1],  # value-alias escape
            'data = config["_inject_results"]\n': [1],  # subscript read
            'return config.get("_plan_queries_only") is True\n': [1],
        }
        for src, expected in cases.items():
            self.assertEqual(
                expected, _violation_lines(ast.parse(src)),
                msg=f"lint missed: {src!r}")

    def test_writes_and_dict_keys_are_not_flagged(self):
        """Config writes (store subscripts) and dict-literal keys are not reads."""
        legit = (
            'config["_inject_results"] = load()\n'           # write (store)
            'config["_plan_queries_only"] = True\n'          # write (store)
            'config = {**config, "_plan_queries_only": True}\n')  # dict literal key
        self.assertEqual([], _violation_lines(ast.parse(legit)))


if __name__ == "__main__":
    unittest.main()
