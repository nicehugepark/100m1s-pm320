#!/usr/bin/env python3
"""PM320 판정용 1분봉 processed snapshot builder.

Raw 수집 DB(`minutes.db`, `minutes_nxt.db`)는 수집 결과 보관층으로만 둔다. 카드 판정,
백필, 운영 파이프라인은 본 스크립트가 만든 단일 processed DB 만 읽는다.

merge policy:
- 정규분봉(`minutes.db`)을 먼저 적재한다.
- NXT/통합분봉(`minutes_nxt.db`)을 같은 `(code, dt)` 위에 덮는다.
- NXT 에 없는 정규장 봉은 정규분봉으로 보존한다.
- 장전/장후 봉은 NXT 원천만 들어온다.

usage:
  python3 scripts/pm320/build_judgement_minutes.py --start 2026-06-11 --end 2026-06-11
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RAW_REGULAR_DB = Path(
    os.environ.get(
        "M1S_PM320_RAW_MINUTES_DB",
        str(REPO_ROOT / "projects" / "pm320" / "data" / "minutes.db"),
    )
)
RAW_NXT_DB = Path(
    os.environ.get(
        "M1S_PM320_RAW_NXT_MINUTES_DB",
        str(REPO_ROOT / "projects" / "pm320" / "data" / "minutes_nxt.db"),
    )
)
OUT_DB = Path(
    os.environ.get(
        "M1S_PM320_JUDGE_MINUTES_DB",
        str(REPO_ROOT / "projects" / "pm320" / "data" / "processed" / "pm320_judge_minutes.db"),
    )
)
KST = timezone(timedelta(hours=9))


def log(msg: str) -> None:
    print(f"[build_judgement_minutes] {msg}", flush=True)


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=MEMORY;
        PRAGMA locking_mode=EXCLUSIVE;
        """
    )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS minute_bars (
          code TEXT NOT NULL,
          dt TEXT NOT NULL,
          open REAL,
          high REAL,
          low REAL,
          close REAL,
          volume REAL,
          session TEXT NOT NULL,
          source TEXT NOT NULL,
          source_priority INTEGER NOT NULL,
          processed_at TEXT NOT NULL,
          PRIMARY KEY (code, dt)
        );
        CREATE TABLE IF NOT EXISTS build_meta (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        """
    )


def _next_date(date_str: str) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    return (d + timedelta(days=1)).strftime("%Y-%m-%d")


def _dt_bounds(start: str, end: str) -> tuple[str, str]:
    return f"{start} 00:00", f"{_next_date(end)} 00:00"


def _copy_regular_sql(dst: sqlite3.Connection, start: str, end: str, processed_at: str) -> int:
    if not RAW_REGULAR_DB.exists() or RAW_REGULAR_DB.stat().st_size == 0:
        return 0
    lo, hi = _dt_bounds(start, end)
    dst.execute("ATTACH DATABASE ? AS regular_raw", (str(RAW_REGULAR_DB),))
    count = int(
        dst.execute(
            "SELECT COUNT(*) FROM regular_raw.minute_bars WHERE dt>=? AND dt<?",
            (lo, hi),
        ).fetchone()[0]
    )
    if count:
        dst.execute(
            """
            INSERT OR REPLACE INTO minute_bars
              (code, dt, open, high, low, close, volume, session, source, source_priority, processed_at)
            SELECT
              code,
              dt,
              open,
              high,
              low,
              close,
              volume,
              CASE
                WHEN substr(dt, 12, 5) BETWEEN '08:00' AND '08:50'
                  OR substr(dt, 12, 5) BETWEEN '15:40' AND '20:00'
                THEN 'extended'
                ELSE 'regular'
              END,
              'regular_raw',
              10,
              ?
            FROM regular_raw.minute_bars
            WHERE dt>=? AND dt<?
            """,
            (processed_at, lo, hi),
        )
    return count


def _copy_nxt_sql(dst: sqlite3.Connection, start: str, end: str, processed_at: str) -> int:
    if not RAW_NXT_DB.exists() or RAW_NXT_DB.stat().st_size == 0:
        return 0
    lo, hi = _dt_bounds(start, end)
    dst.execute("ATTACH DATABASE ? AS nxt_raw", (str(RAW_NXT_DB),))
    count = int(
        dst.execute(
            "SELECT COUNT(*) FROM nxt_raw.minute_bars WHERE dt>=? AND dt<?",
            (lo, hi),
        ).fetchone()[0]
    )
    if count:
        dst.execute(
            """
            INSERT OR REPLACE INTO minute_bars
              (code, dt, open, high, low, close, volume, session, source, source_priority, processed_at)
            SELECT
              code,
              dt,
              open,
              high,
              low,
              close,
              volume,
              session,
              'nxt_raw',
              20,
              ?
            FROM nxt_raw.minute_bars
            WHERE dt>=? AND dt<?
            """,
            (processed_at, lo, hi),
        )
    return count


def build_snapshot(start: str, end: str, out_db: Path = OUT_DB) -> dict[str, int | str]:
    processed_at = datetime.now(KST).isoformat(timespec="seconds")
    out_db.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{out_db.name}.tmp.", dir=str(out_db.parent))
    os.close(fd)
    tmp_path = Path(tmp_name)
    dst = sqlite3.connect(str(tmp_path))
    try:
        _init_schema(dst)
        regular_rows = _copy_regular_sql(dst, start, end, processed_at)
        nxt_rows = _copy_nxt_sql(dst, start, end, processed_at)
        meta_rows = {
            "processed_at": processed_at,
            "last_range": f"{start}~{end}",
            "raw_regular_db": str(RAW_REGULAR_DB),
            "raw_nxt_db": str(RAW_NXT_DB),
            "regular_rows_loaded": str(regular_rows),
            "nxt_rows_loaded": str(nxt_rows),
        }
        dst.executemany(
            "INSERT INTO build_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            list(meta_rows.items()),
        )
        dst.commit()
        lo, hi = _dt_bounds(start, end)
        total = dst.execute(
            "SELECT COUNT(*) FROM minute_bars WHERE dt>=? AND dt<?",
            (lo, hi),
        ).fetchone()[0]
        extended = dst.execute(
            "SELECT COUNT(*) FROM minute_bars WHERE dt>=? AND dt<? AND session='extended'",
            (lo, hi),
        ).fetchone()[0]
        dst.close()
        os.replace(tmp_path, out_db)
        return {
            "processed_at": processed_at,
            "regular_rows": int(regular_rows),
            "nxt_rows": int(nxt_rows),
            "total_rows": int(total),
            "extended_rows": int(extended),
            "out_db": str(out_db),
        }
    except Exception:
        try:
            dst.close()
        except Exception:
            pass
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    finally:
        if "dst" in locals():
            try:
                dst.close()
            except Exception:
                pass


def main() -> int:
    today = datetime.now(KST).strftime("%Y-%m-%d")
    ap = argparse.ArgumentParser(description="PM320 판정용 processed minute DB 생성")
    ap.add_argument("--start", default=today)
    ap.add_argument("--end", default=today)
    args = ap.parse_args()
    try:
        res = build_snapshot(args.start, args.end)
    except Exception as exc:
        log(f"FAIL: {type(exc).__name__}: {exc}")
        return 1
    log(
        f"DONE: {args.start}~{args.end} regular={res['regular_rows']} "
        f"nxt={res['nxt_rows']} total={res['total_rows']} ext={res['extended_rows']} "
        f"→ {res['out_db']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
