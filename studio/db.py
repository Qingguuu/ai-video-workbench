"""SQLite 存储层：tasks / steps / publish_records 三表。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "studio.db"

_conn: sqlite3.Connection | None = None
# RLock（可重入）：update_task 等函数在持锁状态下会调用 connect()，
# connect() 也需拿锁——普通 Lock 会自死锁。
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id          TEXT PRIMARY KEY,
  type        TEXT NOT NULL DEFAULT 'gen',      -- gen=生成 / upload=手动上传
  topic       TEXT NOT NULL DEFAULT '',
  note        TEXT NOT NULL DEFAULT '',
  status      TEXT NOT NULL DEFAULT 'making',   -- making/ready/publishing/published/failed/interrupted
  episode_json TEXT NOT NULL DEFAULT '',
  video_path  TEXT NOT NULL DEFAULT '',
  title       TEXT NOT NULL DEFAULT '',
  tags        TEXT NOT NULL DEFAULT '[]',
  error       TEXT NOT NULL DEFAULT '',
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
  task_id     TEXT NOT NULL,
  step        TEXT NOT NULL,                    -- storyboard/image/voice/subtitle/video/publish
  status      TEXT NOT NULL DEFAULT 'pending',  -- pending/running/done/failed
  detail      TEXT NOT NULL DEFAULT '',
  error       TEXT NOT NULL DEFAULT '',
  started_at  REAL,
  finished_at REAL,
  PRIMARY KEY (task_id, step)
);
CREATE TABLE IF NOT EXISTS publish_records (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     TEXT NOT NULL,
  video_path  TEXT NOT NULL,
  title       TEXT NOT NULL,
  tags        TEXT NOT NULL DEFAULT '[]',
  result      TEXT NOT NULL,                    -- success/failed
  detail      TEXT NOT NULL DEFAULT '',
  published_at REAL NOT NULL
);
"""

GEN_STEPS = ["storyboard", "image", "voice", "subtitle", "video"]
ALL_STEPS = GEN_STEPS + ["publish"]


def connect() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.executescript(SCHEMA)
            _conn.commit()
        return _conn


def now() -> float:
    return time.time()


def new_task_id() -> str:
    return "ep" + uuid.uuid4().hex[:8]


def create_task(type_: str, topic: str, note: str, video_path: str = "") -> str:
    db = connect()
    tid = new_task_id()
    t = now()
    steps = GEN_STEPS if type_ == "gen" else []
    with _lock:
        db.execute(
            "INSERT INTO tasks (id, type, topic, note, status, video_path, title, tags, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (tid, type_, topic, note, "making", video_path, topic[:30], json.dumps([], ensure_ascii=False), t, t),
        )
        for s in steps:
            db.execute("INSERT INTO steps (task_id, step) VALUES (?,?)", (tid, s))
        db.commit()
    return tid


def get_task(tid: str) -> dict | None:
    row = connect().execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    return dict(row) if row else None


def list_tasks() -> list[dict]:
    rows = connect().execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def update_task(tid: str, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = now()
    cols = ", ".join(f"{k}=?" for k in fields)
    with _lock:
        connect().execute(f"UPDATE tasks SET {cols} WHERE id=?", (*fields.values(), tid))
        connect().commit()


def get_steps(tid: str) -> list[dict]:
    rows = connect().execute(
        "SELECT * FROM steps WHERE task_id=? ORDER BY rowid", (tid,)).fetchall()
    return [dict(r) for r in rows]


def step_status(tid: str, step: str) -> str | None:
    row = connect().execute(
        "SELECT status FROM steps WHERE task_id=? AND step=?", (tid, step)).fetchone()
    return row["status"] if row else None


def update_step(tid: str, step: str, status: str, detail: str = "", error: str = "",
                reset_time: bool = False) -> None:
    t = now()
    with _lock:
        if reset_time:
            connect().execute(
                "UPDATE steps SET status=?, detail=?, error=?, started_at=?, finished_at=NULL WHERE task_id=? AND step=?",
                (status, detail, error, t, tid, step))
        else:
            connect().execute(
                "UPDATE steps SET status=?, detail=?, error=?, finished_at=? WHERE task_id=? AND step=?",
                (status, detail, error, t, tid, step))
        connect().commit()


def add_publish_record(task_id: str, video_path: str, title: str, tags: list,
                       result: str, detail: str) -> None:
    with _lock:
        connect().execute(
            "INSERT INTO publish_records (task_id, video_path, title, tags, result, detail, published_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (task_id, video_path, title, json.dumps(tags, ensure_ascii=False), result, detail, now()))
        connect().commit()


def get_publish_records(task_id: str | None = None) -> list[dict]:
    if task_id:
        rows = connect().execute(
            "SELECT * FROM publish_records WHERE task_id=? ORDER BY published_at DESC", (task_id,)).fetchall()
    else:
        rows = connect().execute(
            "SELECT * FROM publish_records ORDER BY published_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_making_task() -> dict | None:
    """当前正在制作的任务（用于单任务并发控制）。"""
    row = connect().execute(
        "SELECT * FROM tasks WHERE status='making' ORDER BY created_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def tags_of(task: dict) -> list:
    try:
        return json.loads(task.get("tags") or "[]")
    except (TypeError, ValueError):
        return []
