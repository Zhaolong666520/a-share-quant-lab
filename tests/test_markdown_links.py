from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CHECKER = PROJECT_ROOT / "scripts" / "check_markdown_links.py"


def _run_checker(*documents: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), *(str(document) for document in documents)],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def test_checker_reports_source_and_target_for_missing_local_link(tmp_path: Path) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text("[Missing](docs/missing.md)\n", encoding="utf-8")

    result = _run_checker(guide)

    assert result.returncode == 1
    assert str(guide) in result.stdout
    assert "docs/missing.md" in result.stdout


def test_checker_ignores_remote_mailto_and_anchor_links(tmp_path: Path) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text(
        "\n".join(
            [
                "[Website](https://example.com/docs)",
                "[Email](mailto:maintainer@example.com)",
                "[Section](#next-step)",
            ]
        ),
        encoding="utf-8",
    )

    result = _run_checker(guide)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""


def test_checker_reports_missing_local_image(tmp_path: Path) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text("![Diagram](missing-image.png)\n", encoding="utf-8")

    result = _run_checker(guide)

    assert result.returncode == 1
    assert "missing-image.png" in result.stdout


def test_default_check_covers_every_repository_markdown_file() -> None:
    result = _run_checker()
    expected_documents = sorted(
        path
        for path in PROJECT_ROOT.rglob("*.md")
        if not any(
                part
                in {
                    ".git",
                    ".mypy_cache",
                    ".pytest_cache",
                    ".ruff_cache",
                    ".venv",
                    ".worktrees",
                    "build",
                    "dist",
                    "smoke-venv",
                }
            or part.startswith(("package_staging_", "package_verify_"))
            for part in path.relative_to(PROJECT_ROOT).parts
        )
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Checked {len(expected_documents)} Markdown files" in result.stdout
    assert "README.md" in result.stdout
    assert "CONTRIBUTING.md" in result.stdout
    assert "experiments/008_forward_paper_trading.md" in result.stdout
    assert ".pytest_cache" not in result.stdout
    assert "smoke-venv" not in result.stdout


def test_checker_resolves_a_local_file_before_its_fragment(tmp_path: Path) -> None:
    guide = tmp_path / "guide.md"
    reference = tmp_path / "reference.md"
    reference.write_text("# Details\n", encoding="utf-8")
    guide.write_text("[Reference](reference.md#details)\n", encoding="utf-8")

    result = _run_checker(guide)

    assert result.returncode == 0, result.stdout + result.stderr


def test_release_package_requires_checker_and_its_tests() -> None:
    package_script = (PROJECT_ROOT / "scripts" / "package_release.ps1").read_text(
        encoding="utf-8"
    )

    assert '"scripts\\check_markdown_links.py"' in package_script
    assert '"tests\\test_markdown_links.py"' in package_script
