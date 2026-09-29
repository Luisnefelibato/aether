from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import psutil


class HealthMonitor:
    def __init__(self, settings: Any = None) -> None:
        config = getattr(settings, "health", settings)
        self.db_path = Path(
            getattr(config, "sqlite_path", Path.cwd() / ".aether-health.sqlite3")
        ).resolve()
        self.cpu_alert = float(
            getattr(config, "cpu_alert_percent", getattr(config, "cpu_warn_percent", 90.0))
        )
        self.memory_alert = float(
            getattr(
                config,
                "memory_alert_percent",
                getattr(config, "memory_warn_percent", 90.0),
            )
        )
        self.disk_alert = float(getattr(config, "disk_alert_percent", 90.0))
        disks = getattr(config, "disks", None)
        self.disk_path = str(
            getattr(config, "disk_path", disks[0] if disks else self.db_path.anchor or ".")
        )
        self.cooldown_s = float(getattr(config, "alert_cooldown_s", 3600.0))
        self.disk_free_warn_gb = float(getattr(config, "disk_free_warn_gb", 15.0))
        self._conn: sqlite3.Connection | None = None

    def open(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS health_snapshots (
                id TEXT PRIMARY KEY, captured_at REAL NOT NULL,
                metrics TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS health_alerts (
                id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL,
                metric TEXT NOT NULL, value REAL NOT NULL,
                threshold REAL NOT NULL, created_at REAL NOT NULL
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def snapshot(self) -> dict[str, Any]:
        conn = self._db()
        disk = psutil.disk_usage(self.disk_path)
        metrics = {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "memory_percent": psutil.virtual_memory().percent,
            "disk_percent": disk.percent,
            "disk_free": disk.free,
            "disk_free_gb": round(disk.free / (1024**3), 2),
            "boot_time": psutil.boot_time(),
        }
        snapshot_id, now = str(uuid4()), time.time()
        conn.execute(
            "INSERT INTO health_snapshots VALUES(?,?,?)",
            (snapshot_id, now, json.dumps(metrics)),
        )
        alerts = []
        candidates = [
            ("cpu_percent", self.cpu_alert, metrics["cpu_percent"]),
            ("memory_percent", self.memory_alert, metrics["memory_percent"]),
            ("disk_percent", self.disk_alert, metrics["disk_percent"]),
        ]
        if self.disk_free_warn_gb > 0:
            candidates.append(
                ("disk_free_gb", self.disk_free_warn_gb, metrics["disk_free_gb"])
            )
        for metric, threshold, value in candidates:
            triggered = (
                value <= threshold if metric == "disk_free_gb" else value >= threshold
            )
            if not triggered:
                continue
            last = conn.execute(
                "SELECT created_at FROM health_alerts WHERE metric=? "
                "ORDER BY created_at DESC LIMIT 1",
                (metric,),
            ).fetchone()
            if last and now - float(last["created_at"]) < self.cooldown_s:
                continue
            alert = {
                "id": str(uuid4()),
                "snapshot_id": snapshot_id,
                "metric": metric,
                "value": value,
                "threshold": threshold,
                "created_at": now,
            }
            conn.execute(
                "INSERT INTO health_alerts VALUES(?,?,?,?,?,?)",
                tuple(alert.values()),
            )
            alerts.append(alert)
        conn.commit()
        return {
            "id": snapshot_id,
            "captured_at": now,
            "metrics": metrics,
            "alerts": alerts,
            "healthy": not alerts,
        }

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._db().execute(
            "SELECT * FROM health_snapshots ORDER BY captured_at DESC LIMIT ?",
            (max(1, min(limit, 1000)),),
        )
        return [
            {**dict(row), "metrics": json.loads(row["metrics"])}
            for row in rows
        ]

    def alerts(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._db().execute(
            "SELECT * FROM health_alerts ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 1000)),),
        )
        return [dict(row) for row in rows]

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("HealthMonitor.open() must be called first")
        return self._conn
