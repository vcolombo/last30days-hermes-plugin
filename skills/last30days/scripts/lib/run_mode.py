"""Run-mode predicates — the single seam for the two-phase inject execution mode.

Two-phase inject (the Hermes plugin path) runs the engine in an isolated mode:
X and web results are pre-fetched by the host and injected, so the engine must
never touch a live credentialed backend. "Am I in that mode?" was previously
re-derived inline at ~12 call sites in four different spellings; every site had
to reconstruct the same load-bearing nuances (below). They now live here once.

Load-bearing invariants — read before changing a predicate:

- Membership, not truthiness. ``config["_inject_results"]`` may be an empty dict
  or list: that is a *real zero-result injection* (the host fetched and got
  nothing), which must still count as injected so the engine does not fall
  through to a live backend. Hence ``is not None``, never ``if config.get(...)``.
- Empty ``--inject-results`` still means two-phase. An empty path string arrives
  as ``args.inject_results == ""``; ``"" is not None`` is True, so a plan-only
  host that passes an empty inject path is still treated as two-phase. Preserved
  by ``planned_two_phase``.

There is no stored ``_two_phase`` key: the umbrella derives from the two base
predicates, so it can never desync from them.
"""


def is_injected(config) -> bool:
    """True once pre-fetched results are loaded onto the config.

    An empty dict/list is a genuine zero-result injection and still counts —
    the engine must not fall through to a live backend on an empty hit.
    """
    return config.get("_inject_results") is not None


def is_plan_only(config) -> bool:
    """True during a ``--plan-queries`` run: the engine plans queries and stops
    before the fetch executor. Seeded on the config by ``pipeline.run``."""
    return config.get("_plan_queries_only") is True


def is_two_phase(config) -> bool:
    """Umbrella for either half of two-phase inject. This is the
    "never touch a live credentialed backend" invariant: when true, no live X
    backend is probed, hosted routing is skipped, and browser cookies are not
    read."""
    return is_injected(config) or is_plan_only(config)


def planned_two_phase(args) -> bool:
    """Pre-config twin of :func:`is_two_phase`, read from CLI args.

    The cookie-policy, hosted-routing, and diagnose gates run before
    ``config["_inject_results"]`` is written, so they cannot use the config
    predicates. An empty inject path (``args.inject_results == ""``) still
    counts as two-phase — see the module docstring.
    """
    return (getattr(args, "inject_results", None) is not None
            or bool(getattr(args, "plan_queries", False)))
