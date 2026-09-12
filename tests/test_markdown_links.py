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


def test_checker_ignores_remote_mailto_anchor_and_image_links(tmp_path: Path) -> None:
    guide = tmp_path / "guide.md"
    guide.write_text(
        "\n".join(
            [
                "[Website](https://example.com/docs)",
                "[Email](mailto:maintainer@example.com)",
                "[Section](#next-step)",
                "![Diagram](missing-image.png)",
            ]
        ),
        encoding="utf-8",
    )

    result = _run_checker(guide)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == ""


def test_default_check_covers_repository_entrypoint_documents() -> None:
    result = _run_checker()

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Checked 4 Markdown files" in result.stdout
    assert "README.md" in result.stdout
    assert "CONTRIBUTING.md" in result.stdout
    assert "docs/快速开始.md" in result.stdout
    assert "docs/quickstart.en.md" in result.stdout


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
