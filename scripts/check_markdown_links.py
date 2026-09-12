from __future__ import annotations

import re
import sys
from pathlib import Path

MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DOCUMENTS = (
    "README.md",
    "CONTRIBUTING.md",
    "docs/快速开始.md",
    "docs/quickstart.en.md",
)


def main(arguments: list[str]) -> int:
    using_defaults = not arguments
    if using_defaults:
        arguments = [str(PROJECT_ROOT / document) for document in DEFAULT_DOCUMENTS]

    broken: list[tuple[Path, str]] = []
    for argument in arguments:
        document = Path(argument)
        content = document.read_text(encoding="utf-8")
        for match in MARKDOWN_LINK.finditer(content):
            target = match.group(1).strip()
            normalized_target = target.casefold()
            if target.startswith("#") or normalized_target.startswith(
                ("http://", "https://", "mailto:")
            ):
                continue
            local_path = target.split("#", maxsplit=1)[0]
            if not (document.parent / local_path).exists():
                broken.append((document, target))

    for document, target in broken:
        print(f"{document}: broken local Markdown link: {target}")
    if using_defaults and not broken:
        print(f"Checked {len(DEFAULT_DOCUMENTS)} Markdown files: {', '.join(DEFAULT_DOCUMENTS)}")
    return 1 if broken else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main(sys.argv[1:]))
