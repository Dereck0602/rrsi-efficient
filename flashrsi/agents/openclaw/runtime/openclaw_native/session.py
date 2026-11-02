"""SQLite-backed session transcript, independent of host OpenClaw state."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class Session:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute(
                "create table if not exists messages (id integer primary key, role text not null, content text not null, meta text not null default '{}')"
            )
            db.commit()

    def append(self, role: str, content: Any, **meta: Any) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute(
                "insert into messages(role,content,meta) values(?,?,?)",
                (
                    role,
                    json.dumps(content, ensure_ascii=False),
                    json.dumps(meta, ensure_ascii=False),
                ),
            )
            db.commit()

    def messages(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                "select role,content,meta from messages order by id"
            ).fetchall()
        return [
            {"role": r, "content": json.loads(c), **json.loads(m)} for r, c, m in rows
        ]

    def clear(self) -> None:
        with sqlite3.connect(self.path) as db:
            db.execute("delete from messages")
            db.commit()
