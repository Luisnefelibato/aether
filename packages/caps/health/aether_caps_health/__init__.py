"""Local psutil health snapshots and alerts."""

from .register import register_health
from .service import HealthMonitor

__all__ = ["HealthMonitor", "register_health"]
