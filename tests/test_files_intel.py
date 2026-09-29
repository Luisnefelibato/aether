import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1] / "packages" / "caps" / "files_intel"))

from aether_caps_files_intel import FilesIntel


def test_index_duplicates_and_reversible_trash(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("same")
    (root / "b.txt").write_text("same")
    files = FilesIntel(
        SimpleNamespace(
            root=root,
            sqlite_path=tmp_path / "files.db",
            trash_path=tmp_path / "trash",
        ),
        root=root,
    )
    files.open()
    try:
        assert files.index()["indexed"] == 2
        assert any(hit.get("match") == "content" for hit in files.search("same"))
        assert len(files.duplicates()) == 1
        copied = files.copy("a.txt", "copy.txt")
        assert copied["ok"]
        trashed = files.trash("copy.txt")
        assert not (root / "copy.txt").exists()
        files.restore(trashed["trash_id"])
        assert (root / "copy.txt").read_text() == "same"
    finally:
        files.close()
