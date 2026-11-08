from __future__ import annotations

import re
import sys
from pathlib import Path

MARKDOWN_LINK = re.compile(r"!?\[[^\]]+\]\(([^)]+)\)")
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _repository_documents() -> list[Path]:
    ignored = {
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
    return sorted(
        document
        for document in PROJECT_ROOT.rglob("*.md")
        if not any(
            part in ignored or part.startswith(("package_staging_", "package_verify_"))
            for part in document.relative_to(PROJECT_ROOT).parts
        )
    )


def main(arguments: list[str]) -> int:
    using_defaults = not arguments
    if using_defaults:
        documents = _repository_documents()
    else:
        documents = [Path(argument) for argument in arguments]

    broken: list[tuple[Path, str]] = []
    for document in documents:
        content = document.read_text(encoding="utf-8")
        for match in MARKDOWN_LINK.finditer(content):
            target = match.group(1).strip()
            normalized_target = target.casefold()
            if target.startswith("#") or normalized_target.startswith(
                ("http://", "https://", "mailto:")
            ):
                continue
            local_path = target.split("#", maxsplit=1)[0].split(maxsplit=1)[0]
            local_path = local_path.strip("<>")
            if not (document.parent / local_path).exists():
                broken.append((document, target))

    for document, target in broken:
        print(f"{document}: broken local Markdown link: {target}")
    if using_defaults and not broken:
        relative_documents = [
            document.relative_to(PROJECT_ROOT).as_posix() for document in documents
        ]
        print(
            f"Checked {len(documents)} Markdown files: "
            + ", ".join(relative_documents)
        )
    return 1 if broken else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main(sys.argv[1:]))
