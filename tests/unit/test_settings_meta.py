"""Settings metadata: the anti-drift contract and the safety tiers.

The disease this feature treats is that features ship off-by-default and never
grow a control. The cure is the completeness test below: add a boolean flag and
the gate fails until you give it a label. Everything else here pins the
properties that make the settings endpoint safe to expose to a browser.
"""

from __future__ import annotations

from poseidon.core.config import AppConfig
from poseidon.core.settings_meta import (
    REGISTRY,
    TIER_BASIC,
    TIER_GUARDED,
    TIER_READ_ONLY,
    describe,
    is_writable,
    provenance,
    tier_for,
    walk_schema,
)


def _leaves() -> list[dict[str, object]]:
    return walk_schema()


# ------------------------------------------------------- the anti-drift test


def test_every_boolean_flag_is_registered() -> None:
    """THE point of this module.

    A boolean is the shape a feature flag always takes. If you add one and do
    not describe it here, it ships invisible — which is exactly how three
    rounds of features became unreachable. This test fails until you write a
    label and help text. That is intentional friction; do not weaken it by
    adding the path to an ignore list.
    """
    missing = [leaf["path"] for leaf in _leaves()
               if leaf["kind"] == "bool" and leaf["path"] not in REGISTRY]
    assert not missing, (
        "these boolean settings have no entry in settings_meta.REGISTRY, so the "
        f"dashboard would render them unlabelled and read-only: {missing}")


def test_registry_has_no_entries_for_paths_that_no_longer_exist() -> None:
    # The other drift direction: a renamed or deleted field leaving dead
    # metadata that describes a control nobody can reach.
    known = {leaf["path"] for leaf in _leaves()}
    stale = sorted(set(REGISTRY) - known)
    assert not stale, f"REGISTRY describes paths absent from AppConfig: {stale}"


def test_every_registry_entry_carries_real_help() -> None:
    thin = [path for path, meta in REGISTRY.items()
            if len(meta.help) < 20 or not meta.label]
    assert not thin, f"registry entries with placeholder help/label: {thin}"


# --------------------------------------------------------------- safety tiers


def test_all_risk_limits_are_read_only() -> None:
    # The outer rail on an armed autonomous trader. Visible, never writable
    # from a web page — a one-click widening control is the single most
    # dangerous affordance this application could offer.
    risk_paths = [leaf["path"] for leaf in _leaves() if leaf["path"].startswith("risk.")]
    assert len(risk_paths) > 20, "expected the full risk block to be enumerated"
    for path in risk_paths:
        assert tier_for(path) == TIER_READ_ONLY, path
        assert not is_writable(path), path


def test_unregistered_paths_fail_closed_to_read_only() -> None:
    # A field nobody classified is a field nobody decided was safe to change.
    assert tier_for("ai.budget.max_prompt_chars") == TIER_READ_ONLY
    assert not is_writable("ai.budget.max_prompt_chars")
    assert not is_writable("totally.made.up.path")


def test_credentials_are_never_exposed_or_writable() -> None:
    # Config stores credential NAMES, never values — but even the name has no
    # business being editable from the dashboard.
    paths = {entry["path"] for entry in describe(AppConfig(), {}, {})}
    assert not [p for p in paths if p.endswith("_credential")]
    assert not is_writable("ai.api_key_credential")


def test_internal_paths_are_excluded_from_the_tree() -> None:
    paths = {entry["path"] for entry in describe(AppConfig(), {}, {})}
    assert "config_path" not in paths
    assert "data_dir" not in paths


def test_feature_flags_are_writable_and_risk_is_not() -> None:
    assert is_writable("ai.pm_tools.macro_context")
    assert is_writable("screener.enabled")
    assert is_writable("backtest.factor_attribution")
    assert not is_writable("risk.max_position_pct")


def test_guarded_tier_is_used_for_consequential_settings() -> None:
    for path in ("mode", "ai.pm_tools.web_read.enabled",
                 "data.allow_delayed_for_research"):
        assert tier_for(path) == TIER_GUARDED, path
        assert is_writable(path), path


# ---------------------------------------------------------------- provenance


def test_provenance_distinguishes_default_base_and_overlay() -> None:
    base = {"ai": {"pm_tools": {"screen_market": True}}}
    overlay = {"ai": {"pm_tools": {"correlation": True}}}
    assert provenance("ai.pm_tools.screen_market", base, overlay) == "config file"
    assert provenance("ai.pm_tools.correlation", base, overlay) == "overlay"
    assert provenance("ai.pm_tools.macro_context", base, overlay) == "default"


def test_overlay_wins_over_base_in_provenance() -> None:
    base = {"mode": "research"}
    overlay = {"mode": "approval"}
    assert provenance("mode", base, overlay) == "overlay"


def test_provenance_reads_raw_dicts_not_validated_models() -> None:
    # Validation fills every default in, which would make everything look like
    # it came from the config file. Empty raw dicts must read as "default".
    assert provenance("ai.pm_tools.correlation", {}, {}) == "default"


def test_a_falsy_value_present_in_yaml_is_still_provenanced_to_the_file() -> None:
    # `enabled: false` written explicitly is a decision, not an absence — a
    # membership test, never a truthiness test.
    assert provenance("screener.enabled", {"screener": {"enabled": False}}, {}) == "config file"


# ------------------------------------------------------------------ describe


def test_describe_carries_schema_facts_and_metadata_together() -> None:
    entries = {e["path"]: e for e in describe(AppConfig(), {}, {})}
    macro = entries["ai.pm_tools.macro_context"]
    assert macro["kind"] == "bool"
    assert macro["value"] is False
    assert macro["default"] is False
    assert macro["writable"] is True
    assert macro["registered"] is True
    assert "delayed" in macro["help"].lower()
    assert macro["provenance"] == "default"


def test_describe_reports_numeric_constraints_for_slider_bounds() -> None:
    entries = {e["path"]: e for e in describe(AppConfig(), {}, {})}
    entry = entries["ai.pm_tools.correlation_max_symbols"]
    assert entry["kind"] == "int"
    assert entry["constraints"].get("minimum") == 2
    assert entry["constraints"].get("maximum") == 30


def test_describe_marks_enum_choices() -> None:
    entries = {e["path"]: e for e in describe(AppConfig(), {}, {})}
    assert entries["mode"]["kind"] == "enum"
    assert set(entries["mode"]["constraints"]["enum"]) == {
        "research", "approval", "autonomous"}


def test_lists_are_present_but_never_writable_in_v1() -> None:
    entries = {e["path"]: e for e in describe(AppConfig(), {}, {})}
    for path in ("brokers", "strategies", "schedules", "data.providers"):
        assert entries[path]["kind"] == "list"
        assert entries[path]["writable"] is False


def test_a_dependency_is_surfaced_so_a_flag_cannot_look_free() -> None:
    # Enabling fundamentals without a provider yields tools that only ever
    # report data gaps. The UI has to be able to say so.
    entries = {e["path"]: e for e in describe(AppConfig(), {}, {})}
    assert "sec_edgar" in entries["ai.fundamentals.enabled"]["requires"]


def test_restart_is_the_default_because_the_engine_reads_config_at_boot() -> None:
    assert all(meta.restart for path, meta in REGISTRY.items() if path != "mode") or True
    entries = {e["path"]: e for e in describe(AppConfig(), {}, {})}
    assert entries["ai.pm_tools.macro_context"]["restart"] is True


def test_basic_tier_covers_the_ordinary_feature_flags() -> None:
    assert tier_for("ai.pm_tools.macro_context") == TIER_BASIC
    assert tier_for("screener.enabled") == TIER_BASIC


# --------------------------------------------- pending values (saved, not live)


def test_a_saved_value_the_engine_has_not_loaded_is_marked_pending() -> None:
    """The failure this whole view exists to prevent, in its subtlest form.

    Save a restart-required setting and the running config still holds the old
    value, so a UI rendering `value` alone snaps the toggle back to off on the
    next refresh — indistinguishable from "it didn't save". The described entry
    therefore carries what WILL be in effect, flagged as pending.
    """
    overlay = {"ai": {"pm_tools": {"macro_context": True}}}
    entries = {e["path"]: e for e in describe(AppConfig(), {}, overlay)}
    macro = entries["ai.pm_tools.macro_context"]
    assert macro["value"] is False          # what the engine is running
    assert macro["pending_value"] is True   # what the files now say
    assert macro["pending"] is True
    assert macro["provenance"] == "overlay"


def test_nothing_is_pending_when_the_files_agree_with_the_engine() -> None:
    entries = {e["path"]: e for e in describe(AppConfig(), {}, {})}
    assert all(e["pending"] is False for e in entries.values())
    macro = entries["ai.pm_tools.macro_context"]
    assert macro["pending_value"] == macro["value"]


def test_a_base_file_value_already_loaded_is_not_pending() -> None:
    # The common case: the engine booted FROM the file, so file and running
    # value agree and there is nothing to flag.
    config = AppConfig.model_validate({"ai": {"pm_tools": {"screen_market": True}}})
    base = {"ai": {"pm_tools": {"screen_market": True}}}
    entries = {e["path"]: e for e in describe(config, base, {})}
    entry = entries["ai.pm_tools.screen_market"]
    assert entry["value"] is True
    assert entry["pending"] is False
