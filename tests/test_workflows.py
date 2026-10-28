"""Static checks on the GitHub Actions workflow definitions."""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def test_release_requires_tests_before_publishing():
    data = _load("release.yml")
    jobs = data["jobs"]
    assert "test" in jobs
    needs = jobs["release"]["needs"]
    assert "test" in needs
    assert "linux" in needs
    assert "windows" in needs


def test_ci_has_expected_jobs():
    data = _load("ci.yml")
    assert {"lint", "test", "gui", "package"}.issubset(set(data["jobs"]))


def test_ci_triggers_on_push_and_pr():
    data = _load("ci.yml")
    # PyYAML (YAML 1.1) parses the bare key ``on`` as boolean True.
    triggers = data.get("on") or data.get(True)
    assert triggers is not None
    assert "push" in triggers
    assert "pull_request" in triggers


def test_version_is_semi_semver():
    version = (WORKFLOWS.parents[1] / "VERSION").read_text(encoding="utf-8").strip()
    parts = version.split(".")
    assert len(parts) == 3
    assert all(p.isdigit() for p in parts)
