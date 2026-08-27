"""Every run, on disk, forever — charts included.

Two stores in one, because they answer different questions:

* **A directory per run** holds the truth: the report JSON, the exact PNGs the
  model was shown, and the raw text it replied with. That is what a replay reads
  and what an argument about a past call is settled with.
* **A SQLite index** holds one row per run so the console can list a thousand of
  them without opening a thousand files.

The index is derived and disposable — `rebuild_index` regenerates it from the
directories. The directories are not.

    data/runs/
      index.db
      EURUSD-20260827T101500-a1b2c3/
        report.json
        charts/H4.png  H1.png  M15.png
        agents/H4.raw.txt  H1.raw.txt  M15.raw.txt
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from ...domain.models import ChartBundle, VisionReport
from ...logging_utils import get_logger

log = get_logger("store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    symbol        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    as_of         TEXT,
    state         TEXT NOT NULL,
    regime        TEXT,
    confidence    REAL,
    long_score    REAL,
    short_score   REAL,
    provider      TEXT,
    model         TEXT,
    chart_version TEXT,
    prompt_version TEXT,
    config_digest TEXT,
    duration_ms   REAL,
    path          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_symbol_idx ON runs(symbol, created_at DESC);
CREATE INDEX IF NOT EXISTS runs_state_idx ON runs(state);
"""


class RunStoreImpl:
    """The `RunStore` port, over the filesystem plus an index."""

    def __init__(self, root: Path, index_path: Path | None = None) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = index_path or (root / "index.db")
        self._init_index()

    # ------------------------------------------------------------------ index

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.index_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_index(self) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(SCHEMA)
            connection.commit()

    # ------------------------------------------------------------------- save

    def save(self, report: VisionReport, bundles: dict[str, ChartBundle] | None = None) -> str:
        """Write the run and index it. Returns the directory path."""
        directory = self.root / report.metadata.run_id
        (directory / "charts").mkdir(parents=True, exist_ok=True)
        (directory / "agents").mkdir(parents=True, exist_ok=True)

        for timeframe, bundle in (bundles or {}).items():
            if bundle.image.data_b64:
                import base64

                path = directory / "charts" / f"{timeframe}.png"
                path.write_bytes(base64.b64decode(bundle.image.data_b64))
                report.charts[timeframe] = str(path.relative_to(self.root))

        for observation in report.observations:
            if observation.raw_text:
                (directory / "agents" / f"{observation.timeframe}.raw.txt").write_text(
                    observation.raw_text, encoding="utf-8"
                )

        # The stored report drops the base64 images: they are already on disk as
        # PNGs, and keeping both makes a 4 MB JSON file nobody can read.
        payload = report.model_dump(mode="json")
        (directory / "report.json").write_text(
            json.dumps(payload, indent=2, default=str), encoding="utf-8"
        )
        self._index(report, directory)
        log.info("saved run %s to %s", report.metadata.run_id, directory)
        return str(directory)

    def _index(self, report: VisionReport, directory: Path) -> None:
        prompt_version = next(
            (o.prompt_version for o in report.observations if o.prompt_version), ""
        )
        row = (
            report.metadata.run_id,
            report.symbol,
            report.metadata.created_at.isoformat(),
            report.metadata.as_of.isoformat() if report.metadata.as_of else None,
            report.current_state.value,
            report.market_regime,
            report.vision_confidence,
            report.long.score if report.long else None,
            report.short.score if report.short else None,
            report.metadata.provider,
            report.metadata.model,
            report.metadata.chart_version,
            prompt_version,
            report.metadata.config_digest,
            report.metadata.duration_ms,
            str(directory),
        )
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row
            )
            connection.commit()

    # ------------------------------------------------------------------- read

    def load(self, run_id: str) -> VisionReport | None:
        path = self.root / run_id / "report.json"
        if not path.exists():
            return None
        return VisionReport.model_validate_json(path.read_text(encoding="utf-8"))

    def chart_path(self, run_id: str, timeframe: str) -> Path | None:
        path = self.root / run_id / "charts" / f"{timeframe}.png"
        return path if path.exists() else None

    def list_runs(
        self,
        limit: int = 50,
        symbol: str | None = None,
        state: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM runs"
        clauses, parameters = [], []
        if symbol:
            clauses.append("symbol = ?")
            parameters.append(symbol.upper())
        if state:
            clauses.append("state = ?")
            parameters.append(state)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        parameters.append(limit)

        with closing(self._connect()) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    def symbols(self) -> list[str]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT DISTINCT symbol FROM runs ORDER BY symbol"
            ).fetchall()
        return [row["symbol"] for row in rows]

    def delete(self, run_id: str) -> bool:
        directory = self.root / run_id
        if directory.exists():
            for path in sorted(directory.rglob("*"), reverse=True):
                path.unlink() if path.is_file() else path.rmdir()
            directory.rmdir()
        with closing(self._connect()) as connection:
            cursor = connection.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
            connection.commit()
        return cursor.rowcount > 0

    def rebuild_index(self) -> int:
        """Re-derive the index from the run directories. The files are the truth."""
        with closing(self._connect()) as connection:
            connection.execute("DELETE FROM runs")
            connection.commit()
        count = 0
        for path in sorted(self.root.glob("*/report.json")):
            try:
                report = VisionReport.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception as exc:  # a partial write from a killed run
                log.warning("skipping unreadable run %s: %s", path.parent.name, exc)
                continue
            self._index(report, path.parent)
            count += 1
        log.info("rebuilt index from %d runs", count)
        return count

    def since(self, moment: datetime) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM runs WHERE created_at >= ? ORDER BY created_at",
                (moment.isoformat(),),
            ).fetchall()
        return [dict(row) for row in rows]
