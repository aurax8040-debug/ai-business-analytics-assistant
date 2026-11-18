"""数据库连接层：只读打开 ETL V1 库，并在 temp schema 建立筛选投影视图。"""
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from . import config


def file_md5(path: Path) -> str:
    h = hashlib.md5()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def connect() -> sqlite3.Connection:
    """每个请求一条独立连接；``mode=ro`` 保证不会写入 ETL 数据库。"""
    if not config.ETL_DB.exists():
        raise FileNotFoundError(f"ETL V1 数据库不存在: {config.ETL_DB}")
    conn = sqlite3.connect(config.db_uri(), uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-32000")       # 32 MB 页缓存，只读
    # TEMP VIEW 落在连接私有的 temp schema；主库 schema 不发生任何变化
    script = (config.SQL_DIR / "dashboard_views.sql").read_text(encoding="utf-8")
    conn.executescript(script)
    return conn


def view_catalog(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def health(conn: sqlite3.Connection) -> dict:
    catalog = view_catalog(conn)
    missing = [v for v in config.DELIVERED_VIEWS if v not in catalog]
    temp_views = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_temp_master WHERE type='view' ORDER BY name")]
    return {
        "database": str(config.ETL_DB),
        "database_md5": file_md5(config.ETL_DB),
        "open_mode": "sqlite3 uri mode=ro（只读）",
        "delivered_views_present": len(config.DELIVERED_VIEWS) - len(missing),
        "delivered_views_expected": len(config.DELIVERED_VIEWS),
        "delivered_views_missing": missing,
        "temp_filter_views": temp_views,
        "readonly": True,
    }
