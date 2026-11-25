"""Every hand-maintained copy of the release version must move together (#13).

A release used to be reverse-engineered from the previous release commit: the
same version lives in packaging metadata, the runtime, the OpenAPI snapshot,
two test suites and the install/deploy docs. This test parses each of those
specific fields and reports every stale location in one failure, so bumping the
version no longer depends on remembering the checklist.

Deliberately excluded:

* ``console/package-lock.json`` carries dependency versions (for example
  ``1.0.10`` for ``@types/estree``), not the application version.
* ``console/package.json`` carries the console package's own version, which is
  not the application release version.
* ``docs/history.md`` and the older ``CHANGELOG.md`` entries record past
  releases and must keep their old numbers.

The tag examples in ``deploy/windows`` count as version sources too: they are
what a release bumps alongside the code, so they use the current version.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import backend

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_VERSION = r"\d+\.\d+\.\d+"


def _read(relative: str) -> str:
    path = REPO_ROOT / relative
    assert path.is_file(), f"{relative} is missing"
    return path.read_text(encoding="utf-8")


def _single_version(relative: str, pattern: str) -> str:
    matches = re.findall(pattern, _read(relative), flags=re.MULTILINE)
    assert len(matches) == 1, (
        f"{relative}: expected exactly one version matching {pattern!r}, got {matches}. "
        "If the surrounding text changed, update this test."
    )
    return matches[0]


def _all_versions(relative: str, pattern: str) -> list[str]:
    matches = re.findall(pattern, _read(relative), flags=re.MULTILINE)
    assert matches, (
        f"{relative}: no version matching {pattern!r}. "
        "If the surrounding text changed, update this test."
    )
    return matches


def _collect_tag_examples(sources: dict[str, str], relative: str) -> None:
    pattern = rf"-Tag v({RELEASE_VERSION})"
    for number, version in enumerate(_all_versions(relative, pattern), start=1):
        sources[f"{relative} -Tag example {number}"] = version


def _version_sources() -> dict[str, str]:
    """Return ``location -> version`` for every checked copy."""
    pyproject = tomllib.loads(_read("pyproject.toml"))
    uv_lock = tomllib.loads(_read("uv.lock"))
    openapi = json.loads(_read("openapi.json"))

    locked = [entry["version"] for entry in uv_lock["package"] if entry["name"] == "beebox"]
    assert len(locked) == 1, f"uv.lock: expected one beebox package, found {len(locked)}"

    changelog = _read("CHANGELOG.md")
    released = re.findall(
        rf"^## \[({RELEASE_VERSION})\] - \d{{4}}-\d{{2}}-\d{{2}}$", changelog, re.MULTILINE
    )
    assert released, "CHANGELOG.md: no dated `## [x.y.z]` release heading"
    latest = released[0]
    release_link = re.findall(
        rf"^\[{re.escape(latest)}\]: \S+/releases/tag/v({RELEASE_VERSION})$",
        changelog,
        re.MULTILINE,
    )
    assert release_link == [latest], (
        f"CHANGELOG.md: the [{latest}] link definition must use v{latest}, got {release_link}"
    )

    sources = {
        "pyproject.toml [project].version": pyproject["project"]["version"],
        "backend/__init__.py __version__": backend.__version__,
        "uv.lock beebox package version": locked[0],
        "openapi.json info.version": openapi["info"]["version"],
        "backend/tests/test_v1_api.py health version": _single_version(
            "backend/tests/test_v1_api.py", rf'body\["version"\] == "({RELEASE_VERSION})"'
        ),
        "console/e2e/console.spec.ts health version": _single_version(
            "console/e2e/console.spec.ts",
            rf'getByTestId\("health-version"\)\)\.toHaveText\("({RELEASE_VERSION})"\)',
        ),
        "CHANGELOG.md latest release heading": latest,
        "CHANGELOG.md [Unreleased] compare base": _single_version(
            "CHANGELOG.md", rf"^\[Unreleased\]: \S+/compare/v({RELEASE_VERSION})\.\.\.HEAD$"
        ),
        "CHANGELOG.md latest release link": release_link[0],
    }

    for relative in ("README.md", "README.en.md"):
        sources[f"{relative} version badge"] = _single_version(
            relative, rf"badge/version-({RELEASE_VERSION})-blue"
        )
        sources[f"{relative} release tag example"] = _single_version(
            relative, rf"`v({RELEASE_VERSION})`"
        )

    readme = "deploy/windows/README.md"
    _collect_tag_examples(sources, readme)
    sources[f"{readme} -Tag parameter example"] = _single_version(
        readme, rf"例如 `v({RELEASE_VERSION})`"
    )
    deploy_script = "deploy/windows/deploy.ps1"
    sources[f"{deploy_script} -Tag parameter help"] = _single_version(
        deploy_script, rf"for example v({RELEASE_VERSION})"
    )
    for relative in ("deploy/windows/deploy.ps1", "deploy/windows/install.ps1"):
        _collect_tag_examples(sources, relative)

    return sources


def test_every_version_source_matches_pyproject() -> None:
    sources = _version_sources()
    expected = sources["pyproject.toml [project].version"]

    assert re.fullmatch(RELEASE_VERSION, expected), (
        f"pyproject.toml version {expected!r} is not a plain x.y.z release version"
    )

    stale = {name: version for name, version in sources.items() if version != expected}
    assert stale == {}, (
        f"release version sources disagree with pyproject.toml ({expected}); update:\n"
        + "\n".join(f"  - {name}: {version}" for name, version in stale.items())
    )
