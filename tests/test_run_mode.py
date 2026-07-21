"""Run-mode predicates + the seam invariant they exist to protect."""

from types import SimpleNamespace

from lib import run_mode


class TestIsInjected:
    def test_absent_key_is_false(self):
        assert run_mode.is_injected({}) is False

    def test_none_is_false(self):
        assert run_mode.is_injected({"_inject_results": None}) is False

    def test_empty_dict_is_injected(self):
        # A real zero-result injection: host fetched, got nothing. Still
        # injected, or the engine falls through to a live backend.
        assert run_mode.is_injected({"_inject_results": {}}) is True

    def test_empty_list_is_injected(self):
        assert run_mode.is_injected({"_inject_results": []}) is True

    def test_populated_is_injected(self):
        assert run_mode.is_injected({"_inject_results": {"x": {}}}) is True


class TestIsPlanOnly:
    def test_absent_is_false(self):
        assert run_mode.is_plan_only({}) is False

    def test_true_is_plan_only(self):
        assert run_mode.is_plan_only({"_plan_queries_only": True}) is True

    def test_false_is_false(self):
        assert run_mode.is_plan_only({"_plan_queries_only": False}) is False


class TestIsTwoPhase:
    def test_neither(self):
        assert run_mode.is_two_phase({}) is False

    def test_injected_only(self):
        assert run_mode.is_two_phase({"_inject_results": []}) is True

    def test_plan_only(self):
        assert run_mode.is_two_phase({"_plan_queries_only": True}) is True

    def test_both(self):
        assert run_mode.is_two_phase(
            {"_inject_results": {}, "_plan_queries_only": True}) is True


class TestPlannedTwoPhase:
    def test_neither(self):
        args = SimpleNamespace(inject_results=None, plan_queries=False)
        assert run_mode.planned_two_phase(args) is False

    def test_inject_path_given(self):
        args = SimpleNamespace(inject_results="/tmp/inject.json",
                               plan_queries=False)
        assert run_mode.planned_two_phase(args) is True

    def test_empty_inject_path_still_counts(self):
        # Empty --inject-results ("") is two-phase: "" is not None.
        args = SimpleNamespace(inject_results="", plan_queries=False)
        assert run_mode.planned_two_phase(args) is True

    def test_plan_queries_flag(self):
        args = SimpleNamespace(inject_results=None, plan_queries=True)
        assert run_mode.planned_two_phase(args) is True

    def test_missing_attrs_default_false(self):
        assert run_mode.planned_two_phase(SimpleNamespace()) is False


class TestSeamInvariant:
    """The whole point: a two-phase config never resolves a LIVE X backend.
    providers._resolve_x_backend must pass local_only=True to env.get_x_source
    (whose live leg spawns an authenticated `xurl whoami`)."""

    def _captured_local_only(self, monkeypatch, config):
        from lib import providers, env
        seen = {}
        monkeypatch.setattr(
            env, "get_x_source",
            lambda cfg, local_only=False: seen.__setitem__("v", local_only))
        # No backend pin, so _resolve_x_backend falls to the get_x_source path.
        config = {**config, env.X_BACKEND_PIN_VAR: ""}
        providers._resolve_x_backend(config)
        return seen.get("v")

    def test_injected_config_is_local_only(self, monkeypatch):
        assert self._captured_local_only(
            monkeypatch, {"_inject_results": []}) is True

    def test_plan_only_config_is_local_only(self, monkeypatch):
        assert self._captured_local_only(
            monkeypatch, {"_plan_queries_only": True}) is True

    def test_plain_config_is_not_local_only(self, monkeypatch):
        assert self._captured_local_only(monkeypatch, {}) is False
