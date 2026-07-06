"""REQ-080 interval 모델 마이그레이션 (1회용).

기존 스키마: (date, stock_code, badge_type, source) PK 4컬럼
신규 스키마: + active_from, active_until, id AUTOINCREMENT
            UNIQUE (date, stock_code, badge_type, source, active_from)

대표 결정 (2026-04-29 11:45 KST):
  - 옛 retire 정책 (source='retired_intraday_*' UPDATE-in-place) 폐기
  - production DB에 retire 흔적 0건 검증됨 → 모든 v1 행 active_until=NULL 마이그
  - source별 정책 분리:
      pipeline_chg     → 영구 활성 (EOD 종가 기반, 풀림 없음)
      kiwoom_ka10017   → 풀림 가능 (장중 응답 사라지면 active_until 설정)

가드:
  - 사전 백업 필수 (호출자가 스크립트 외부에서 cp .bak)
  - 행수 정합 검증 (v1 → 신규 동일)
  - dry-run 모드 지원 (--dry-run)
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


def run_migration(db_path: str, dry_run: bool = False) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    summary: dict = {
        "db_path": db_path,
        "dry_run": dry_run,
        "v1_total": 0,
        "v1_by_source": {},
        "migrated": 0,
        "active_count": 0,
        "retired_count": 0,
        "schema_changes": [],
    }

    # 1. 기존 schema 검증
    cols = conn.execute("PRAGMA table_info(stock_status_badges)").fetchall()
    col_names = [c["name"] for c in cols]
    expected_v1 = {
        "date",
        "stock_code",
        "badge_type",
        "source",
        "payload_json",
        "created_at",
    }
    if set(col_names) != expected_v1:
        # 이미 신규 스키마인지 확인 (멱등 처리)
        if "active_from" in col_names and "active_until" in col_names:
            summary["schema_changes"].append("ALREADY_MIGRATED")
            conn.close()
            return summary
        raise RuntimeError(
            f"unexpected schema: {col_names} (expected v1: {expected_v1})"
        )

    # 2. v1 통계
    summary["v1_total"] = conn.execute(
        "SELECT COUNT(*) FROM stock_status_badges"
    ).fetchone()[0]
    rows = conn.execute(
        "SELECT source, COUNT(*) AS cnt FROM stock_status_badges GROUP BY source"
    ).fetchall()
    summary["v1_by_source"] = {r["source"]: r["cnt"] for r in rows}

    if dry_run:
        conn.close()
        return summary

    # 3. 트랜잭션 시작 — rename + new table + insert + index
    cur = conn.cursor()
    try:
        cur.execute("BEGIN IMMEDIATE")

        # 3.1 rename
        cur.execute("ALTER TABLE stock_status_badges RENAME TO stock_status_badges_v1")
        summary["schema_changes"].append("RENAMED v1")

        # 3.2 기존 인덱스 정리 (v1로 따라옴 — drop 후 신규에서 재생성)
        for idx in (
            "idx_ssb_date_badge",
            "idx_ssb_stock",
        ):
            try:
                cur.execute(f"DROP INDEX IF EXISTS {idx}")
            except Exception:
                pass

        # 3.3 신규 테이블
        cur.execute(
            """CREATE TABLE stock_status_badges (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                date          TEXT NOT NULL,
                stock_code    TEXT NOT NULL,
                badge_type    TEXT NOT NULL,
                source        TEXT NOT NULL,
                payload_json  TEXT,
                active_from   TEXT NOT NULL,
                active_until  TEXT,
                created_at    TEXT NOT NULL,
                UNIQUE (date, stock_code, badge_type, source, active_from)
            )"""
        )
        summary["schema_changes"].append("CREATED interval")

        # 3.4 v1 → 신규 마이그 (모든 행 active_until=NULL — retire 흔적 0 검증됨)
        cur.execute(
            """INSERT INTO stock_status_badges
                 (date, stock_code, badge_type, source, payload_json,
                  active_from, active_until, created_at)
               SELECT date, stock_code, badge_type, source, payload_json,
                      created_at AS active_from,
                      NULL AS active_until,
                      created_at
               FROM stock_status_badges_v1"""
        )
        summary["migrated"] = cur.execute(
            "SELECT COUNT(*) FROM stock_status_badges"
        ).fetchone()[0]

        # 3.5 인덱스
        cur.execute(
            "CREATE INDEX idx_ssb_current ON stock_status_badges("
            "date, badge_type, active_until)"
        )
        cur.execute(
            "CREATE INDEX idx_ssb_stock ON stock_status_badges(stock_code, date DESC)"
        )
        cur.execute("CREATE INDEX idx_ssb_source ON stock_status_badges(source, date)")
        summary["schema_changes"].append("INDEXES created")

        # 3.6 정합 검증 — 행수 일치
        if summary["migrated"] != summary["v1_total"]:
            raise RuntimeError(
                f"row count mismatch: v1={summary['v1_total']} "
                f"new={summary['migrated']}"
            )

        # 3.7 활성·풀림 카운트
        summary["active_count"] = cur.execute(
            "SELECT COUNT(*) FROM stock_status_badges WHERE active_until IS NULL"
        ).fetchone()[0]
        summary["retired_count"] = cur.execute(
            "SELECT COUNT(*) FROM stock_status_badges WHERE active_until IS NOT NULL"
        ).fetchone()[0]

        cur.execute("COMMIT")
        summary["schema_changes"].append("COMMITTED")
    except Exception as e:
        cur.execute("ROLLBACK")
        summary["error"] = str(e)
        conn.close()
        raise

    conn.close()
    return summary


def main():
    ap = argparse.ArgumentParser()
    # --db default: config.py DB_PATH (env M1S_HOMEPAGE override 가능).
    # cron worktree 격리 (lead-meta §11.32) 정합 — 2026-05-28 cycle25 cron-isolation A1.
    from .config import DB_PATH as _DEFAULT_DB

    ap.add_argument(
        "--db",
        default=str(_DEFAULT_DB),
        help="대상 stocks.db 경로",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not Path(args.db).exists():
        print(f"DB not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    summary = run_migration(args.db, dry_run=args.dry_run)
    import json as _json

    print(_json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
