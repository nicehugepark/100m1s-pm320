#!/usr/bin/env python3
"""cron worktree DB 스키마 검증 게이트 (DOC-20260617-FLR-001 §4 P0).

배경: 2026-06-16 cron 격리 worktree(`100m1s-homepage-cron`) 전환 시, 신규
worktree의 `data/stocks.db`에 `dailybars` 등 테이블이 누락된 채 초기화되어
2026-06-17 cron 의 build_daily 가 `no such table: dailybars` 크래시 → 라이브
0종목. 격리(isolation)는 race condition 을 봉쇄했으나 DB 스키마 동등성 검증을
누락한 설계 공백 (FLR-001 5 Whys 근본 원인).

본 게이트: cron build_daily 호출 **직전** 에 실행하여
  1) cron DB 의 필수 테이블 존재를 sqlite_master 로 검증
  2) 부재 시 메인 serving DB (schema SSOT) 의 CREATE 문을 추출해 빈 테이블 복구
     (데이터는 collect_dailybars 등 후속 stage 가 채움 — 스키마만 동등화)
  3) 복구/검증 결과를 알림 marker 에 기록
  4) 복구 불가(메인 DB 부재 등) 시 non-zero exit → cron 이 build SKIP
     (6시간 행/크래시 cascade 대신 즉시 graceful 차단).

설계 원칙:
  - 메인 serving DB = schema single source of truth. DDL 을 런타임 추출 →
    스키마가 미래에 변해도 하드코딩 없이 자동 동기화.
  - cron DB 경로 = config.py 의 M1S_HOMEPAGE 경유 DB_PATH 재사용 (단일 출처).
  - 필수 테이블 = build_daily 가 크래시할 수 있는 critical 집합으로 한정
    (전체 40개 전수 강제는 정상 cron false-positive SKIP 위험 → 핵심만).
  - read-only 검증 + 빈 테이블 CREATE 만. 기존 데이터 행은 절대 touch 안 함.

usage:
  M1S_HOMEPAGE=/Users/seongjinpark/company/100m1s-homepage-cron \\
    python3 -m scripts.news_pipeline.verify_cron_db_schema
  exit 0 = 검증 PASS 또는 복구 성공 (build 진행 가능)
  exit 2 = 복구 불가 (build SKIP 해야 함)

옵션:
  --check-only  복구 시도 없이 검증만 (exit 0=정상, 2=부재). dry-run 용.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# build_daily 가 의존하는 테이블 중, 부재 시 OperationalError 크래시를 유발하는
# critical 집합. FLR-001 의 dailybars(2470 _reconcile) 가 대표 사례.
# (build_daily.py 의 `FROM <table>` grep 결과 중 데이터 파이프라인 critical 만 선별.
#  llm_cache / *_usage / 로그성 테이블은 제외 — 부재해도 graceful 또는 _ensure 자동 생성.)
REQUIRED_TABLES = (
    "dailybars",  # FLR-001 크래시 진원지 (일봉 OHLC SSOT)
    "dailybars_adjustments",  # 권리락/분할 ratio 보정
    "daily_picks",  # 당일 카드 종목
    "stocks",  # 종목 마스터
    "index_dailybars",  # 지수 일봉 (시장경보 predicted 경로)
    "theme_daily_stats",  # 테마 집계
    "stock_themes",  # 종목-테마 매핑
    "disclosures",  # 공시
    "intraday_snapshot",  # 장중 스냅샷
)

# schema SSOT DB. M1S_SCHEMA_SOURCE_DB override 우선, 미설정 시 M1S_HOMEPAGE/pm320 레포 로컬.
# S5 자립화 (DOC-20260707-REQ-001): 옛 homepage 절대경로 → 자립 fallback.
DEFAULT_SCHEMA_SOURCE = (
    Path(os.environ.get("M1S_HOMEPAGE", str(Path(__file__).resolve().parents[2])))
    / "data"
    / "stocks.db"
)

# cron 운영 marker 표준 위치 (kiwoom_cron.sh 의 /tmp/100m1s-*.marker 패턴 정합).
ALARM_MARKER = Path("/tmp/100m1s-cron-db-schema.marker")  # noqa: S108


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str) -> None:
    print(f"[verify_cron_db_schema] {_now()} {msg}", flush=True)


def _existing_tables(db_path: Path) -> set[str]:
    """db_path 의 현재 테이블 집합 (read-only). 파일 부재 시 빈 set."""
    if not db_path.exists():
        return set()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()


def _ddl_for_table(source_db: Path, table: str) -> list[str]:
    """source_db 에서 table 의 CREATE TABLE + 관련 CREATE INDEX DDL 추출.

    sqlite_master.sql 원문을 그대로 반환 → 스키마 변경 자동 추종.
    table CREATE 1건 + 그 table 을 대상으로 한 INDEX CREATE N건.
    """
    conn = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    try:
        ddls: list[str] = []
        # CREATE TABLE (자동 생성 인덱스/PK 동반 제외 — sql IS NOT NULL 필터)
        trow = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name=? AND sql IS NOT NULL",
            (table,),
        ).fetchone()
        if trow and trow[0]:
            ddls.append(trow[0])
        # 해당 table 의 명시적 CREATE INDEX (sqlite_autoindex 제외 = sql NOT NULL)
        irows = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
            (table,),
        ).fetchall()
        ddls.extend(r[0] for r in irows if r[0])
        return ddls
    finally:
        conn.close()


def _write_marker(status: str, detail: str) -> None:
    try:
        ALARM_MARKER.write_text(
            f"{_now()} status={status} {detail}\n", encoding="utf-8"
        )
    except OSError:
        pass  # marker write 실패가 게이트를 깨면 안 됨


def verify_and_repair(cron_db: Path, source_db: Path, *, check_only: bool) -> int:
    """cron_db 필수 테이블 검증 + (check_only 아니면) 메인 DB 에서 복구.

    return: 0 = PASS/복구성공, 2 = 부재/복구불가 (cron build SKIP 트리거).
    """
    have = _existing_tables(cron_db)
    if not cron_db.exists():
        _log(f"FAIL: cron DB 파일 부재: {cron_db}")
        # 파일 자체 부재는 빈 DB 라도 connect 시 생성되나, 여기선 명시 FAIL 후
        # 복구 단계에서 CREATE 로 파일+테이블 동시 생성 시도.

    missing = [t for t in REQUIRED_TABLES if t not in have]

    if not missing:
        _log(
            f"PASS: cron DB 필수 테이블 {len(REQUIRED_TABLES)}종 전부 존재 ({cron_db})"
        )
        _write_marker("ok", f"tables={len(REQUIRED_TABLES)} db={cron_db}")
        return 0

    _log(f"DETECT: cron DB 필수 테이블 누락 {len(missing)}종: {missing} (db={cron_db})")

    if check_only:
        _log("CHECK-ONLY 모드 — 복구 미시도. build SKIP 권고 (exit 2).")
        _write_marker("missing_check_only", f"missing={','.join(missing)}")
        return 2

    # --- 복구: 메인 serving DB 의 DDL 로 빈 테이블 생성 ---
    if not source_db.exists():
        _log(
            f"FAIL: schema SSOT (메인 serving DB) 부재: {source_db} "
            f"→ 복구 불가. build SKIP (exit 2)."
        )
        _write_marker(
            "repair_failed_no_source",
            f"missing={','.join(missing)} source={source_db}",
        )
        return 2

    source_tables = _existing_tables(source_db)
    unrecoverable = [t for t in missing if t not in source_tables]
    if unrecoverable:
        _log(
            f"FAIL: 메인 serving DB 에도 부재하는 테이블 {unrecoverable} "
            f"→ 부분 복구 불가. build SKIP (exit 2)."
        )
        _write_marker(
            "repair_failed_source_incomplete",
            f"unrecoverable={','.join(unrecoverable)}",
        )
        return 2

    # cron DB connect (부재 시 생성). WAL — 기존 cron 동작 정합.
    cron_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cron_db)
    repaired: list[str] = []
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        for table in missing:
            ddls = _ddl_for_table(source_db, table)
            if not ddls:
                _log(f"WARN: {table} DDL 추출 0건 (메인 DB sql NULL) — skip")
                continue
            for ddl in ddls:
                # CREATE TABLE / CREATE INDEX 모두 IF NOT EXISTS 가 없을 수 있어
                # 원문 그대로 실행하되, 이미 있으면 (race) 무시.
                try:
                    conn.execute(ddl)
                except sqlite3.OperationalError as exc:
                    if "already exists" in str(exc):
                        continue
                    raise
            repaired.append(table)
        conn.commit()
    finally:
        conn.close()

    # 복구 후 재검증
    have2 = _existing_tables(cron_db)
    still_missing = [t for t in REQUIRED_TABLES if t not in have2]
    if still_missing:
        _log(f"FAIL: 복구 후에도 누락 잔존 {still_missing} → build SKIP (exit 2).")
        _write_marker("repair_partial", f"still_missing={','.join(still_missing)}")
        return 2

    _log(
        f"REPAIRED: 메인 serving DB DDL 로 빈 테이블 {len(repaired)}종 복구 완료 "
        f"{repaired} — 데이터는 후속 collect stage 가 적재. build 진행 (exit 0)."
    )
    _write_marker(
        "repaired",
        f"repaired={','.join(repaired)} source={source_db}",
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="cron worktree DB 스키마 검증·복구 게이트 (FLR-20260617-001)"
    )
    ap.add_argument(
        "--check-only",
        action="store_true",
        help="복구 미시도, 검증만 (exit 0=정상, 2=부재). dry-run.",
    )
    ap.add_argument(
        "--cron-db",
        default=None,
        help="검증 대상 cron DB 경로 (기본: M1S_HOMEPAGE/data/stocks.db).",
    )
    ap.add_argument(
        "--source-db",
        default=None,
        help="schema SSOT 메인 serving DB (기본: 100m1s-homepage/data/stocks.db "
        "또는 M1S_SCHEMA_SOURCE_DB).",
    )
    args = ap.parse_args()

    # cron DB 경로 결정: 명시 인자 > config.py DB_PATH (M1S_HOMEPAGE 경유)
    if args.cron_db:
        cron_db = Path(args.cron_db)
    else:
        try:
            # config 가 M1S_HOMEPAGE 미설정 시 RuntimeError — 게이트가 명확히 FAIL.
            from scripts.news_pipeline.config import DB_PATH

            cron_db = DB_PATH
        except Exception as exc:  # noqa: BLE001 — config import 실패 = 환경 결함
            _log(f"FAIL: cron DB 경로 결정 불가 ({type(exc).__name__}: {exc})")
            return 2

    source_db = Path(
        args.source_db
        or os.environ.get("M1S_SCHEMA_SOURCE_DB")
        or DEFAULT_SCHEMA_SOURCE
    )

    # 안전장치: cron DB 와 source DB 가 동일 파일이면 복구 무의미 (메인 worktree
    # ad-hoc 실행 등). 검증만 수행하고 PASS/FAIL 만 반환.
    if cron_db.resolve() == source_db.resolve():
        _log(
            f"NOTE: cron DB == source DB ({cron_db}) — 자기복구 무의미, "
            f"검증만 수행 (check-only 강제)."
        )
        return verify_and_repair(cron_db, source_db, check_only=True)

    return verify_and_repair(cron_db, source_db, check_only=args.check_only)


if __name__ == "__main__":
    sys.exit(main())
