"""Manifest sanity checks — fast, no Home Assistant dependency.

hassfest (CI) validates the manifest against HA's actual schema; this test
catches the specific mistakes that break installation before a push even
reaches CI (see docs/DECISIONS.md's "v0 prototype rejected wholesale" entry
for what an unvalidated manifest cost the first prototype).
"""

import json
from pathlib import Path

INTEGRATION_DIR = Path(__file__).parent.parent / "custom_components" / "occupancy_tracker"


def load_manifest() -> dict:
    with open(INTEGRATION_DIR / "manifest.json", encoding="utf-8") as f:
        return json.load(f)


def test_domain_matches_directory_name() -> None:
    manifest = load_manifest()
    assert manifest["domain"] == INTEGRATION_DIR.name


def test_required_fields_present() -> None:
    manifest = load_manifest()
    for field in ("domain", "name", "codeowners", "documentation", "integration_type", "iot_class"):
        assert field in manifest, f"missing required field: {field}"


def test_config_flow_enabled() -> None:
    manifest = load_manifest()
    assert manifest["config_flow"] is True
    assert (INTEGRATION_DIR / "config_flow.py").exists()


def test_codeowners_are_github_handles() -> None:
    manifest = load_manifest()
    assert manifest["codeowners"], "codeowners must not be empty"
    for owner in manifest["codeowners"]:
        assert owner.startswith("@"), f"codeowner {owner!r} must start with '@'"
