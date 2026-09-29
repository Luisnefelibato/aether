import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "packages" / "caps" / "health"))

from aether_caps_health import HealthMonitor


def test_health_snapshot_and_alerts_use_temporary_database(tmp_path: Path):
    monitor = HealthMonitor(
        SimpleNamespace(
            sqlite_path=tmp_path / "health.db",
            disk_path=str(tmp_path),
            cpu_alert_percent=-1,
            memory_alert_percent=-1,
            disk_alert_percent=-1,
            disk_free_warn_gb=0,
            alert_cooldown_s=0,
        )
    )
    monitor.open()
    try:
        snapshot = monitor.snapshot()
        assert set(snapshot["metrics"]) >= {"cpu_percent", "memory_percent", "disk_percent"}
        assert len(snapshot["alerts"]) == 3
        assert monitor.recent(1)[0]["id"] == snapshot["id"]
    finally:
        monitor.close()
