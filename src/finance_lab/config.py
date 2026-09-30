from __future__ import annotations

import json
import os
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path


@dataclass(frozen=True)
class Instrument:
    code: str
    exchange: str
    name: str
    kind: str

    @property
    def symbol(self) -> str:
        return f"{self.exchange}.{self.code}"

    @property
    def baostock_code(self) -> str:
        return self.symbol


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    data: Path
    raw: Path
    curated: Path
    outputs: Path
    database: Path

    def ensure(self) -> None:
        for directory in (self.data, self.raw, self.curated, self.outputs):
            directory.mkdir(parents=True, exist_ok=True)


def project_root() -> Path:
    configured = os.environ.get("FINANCE_LAB_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "pyproject.toml").is_file() and (
        source_root / "src" / "finance_lab" / "config.py"
    ).is_file():
        return source_root
    return Path.cwd().resolve()


def get_paths(root: Path | None = None) -> ProjectPaths:
    resolved = (root or project_root()).resolve()
    paths = ProjectPaths(
        root=resolved,
        data=resolved / "data",
        raw=resolved / "data" / "raw",
        curated=resolved / "data" / "curated",
        outputs=resolved / "outputs",
        database=resolved / "data" / "finance_lab.duckdb",
    )
    paths.ensure()
    return paths


def load_instruments(root: Path | None = None) -> list[Instrument]:
    config_path = (root or project_root()) / "config" / "instruments.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    instruments = [Instrument(**item) for item in payload]
    if not instruments:
        raise ValueError("config/instruments.json 至少需要一个标的")
    return instruments


def initialize_instruments(root: Path | None = None) -> tuple[Path, bool]:
    """Create the example configuration once; never replace user configuration."""
    target = (root or project_root()) / "config" / "instruments.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    example = files("finance_lab").joinpath("resources", "instruments.json").read_bytes()
    try:
        with target.open("xb") as stream:
            stream.write(example)
    except FileExistsError:
        return target, False
    return target, True
