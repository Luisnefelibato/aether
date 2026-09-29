"""Verified local ZIP backups."""

from .register import register_backup
from .service import BackupService

__all__ = ["BackupService", "register_backup"]
