from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


WORKBENCH_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class AppSettings:
    workbench_root: Path = WORKBENCH_ROOT
    database_path: Path | None = None
    host: str = "127.0.0.1"
    port: int = 8765

    @property
    def db_path(self) -> Path:
        return self.database_path or self.workbench_root / "data" / "market.duckdb"

    @property
    def ui_root(self) -> Path:
        return self.workbench_root / "ui_mockups" / "v2"
