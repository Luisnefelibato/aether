"""Scrub legacy Aether naming from SQLite memory."""
from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "data" / "aether.db"


def scrub_text(text: str) -> str:
    text = re.sub(r"(?i)\bAETHER\b", "ADAM", text)
    text = re.sub(r"(?i)\bAether\b", "Adam", text)
    text = re.sub(r"(?i)\baether\b", "adam", text)
    return text


def main() -> None:
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT id, role, content FROM episodes").fetchall()
    changed = 0
    for eid, role, content in rows:
        new = scrub_text(content or "")
        if new != content:
            con.execute("UPDATE episodes SET content=? WHERE id=?", (new, eid))
            changed += 1

    now = time.time()
    prefs = {
        "assistant_name": "ADAM",
        "assistant_never_alias": "aether",
        "product_name": "ADAM",
    }
    for k, v in prefs.items():
        con.execute(
            "INSERT INTO preferences(key, value, ts) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, ts=excluded.ts",
            (k, v, now),
        )
    con.commit()
    print(f"scrubbed_episodes={changed} prefs_set=ADAM")
    con.close()


if __name__ == "__main__":
    main()
