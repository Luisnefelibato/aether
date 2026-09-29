import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "packages" / "caps" / "backup"))

from aether_caps_backup import BackupService


def test_backup_manifest_verify_and_explicit_restore(tmp_path: Path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "note.txt").write_text("valuable", encoding="utf-8")
    git = root / ".git"
    git.mkdir()
    (git / "config").write_text("secret", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "lib.js").write_text("skip", encoding="utf-8")
    service = BackupService(
        SimpleNamespace(
            root=root,
            output_path=tmp_path / "backups",
            sqlite_path=tmp_path / "backups" / "runs.db",
        ),
        root=root,
    )
    service.open()
    try:
        service.create_set("docs", [root])
        run = service.run("docs")
        assert service.verify(run["id"])["ok"]
        restore = tmp_path / "restore"
        assert service.restore(run["id"], restore)["restored"] == 1
        assert (restore / "note.txt").read_text(encoding="utf-8") == "valuable"
    finally:
        service.close()
