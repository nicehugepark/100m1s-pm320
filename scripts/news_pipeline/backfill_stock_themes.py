"""stock_theme_daily → stock_themes 백필 (DOC-20260422-FLR-008 후속).

배경:
  hot-fix 42c0b13은 **앞으로** 이시카와 판정 시 `stock_theme_daily`와
  `stock_themes`에 동시 기록한다. 그러나 hot-fix 머지 이전에 이미
  `stock_theme_daily`에만 기록된 과거 매칭분은 `stock_themes`에 없어
  build_daily.py가 UI JSON에 반영하지 못한다.

동작:
  - `stock_theme_daily`의 모든 (stock_code, theme_id) 쌍을 조회
  - `themes.is_active = 1` (canonical/활성)만 필터
  - 각 쌍에 대해 `stock_theme_daily`의 최대 date(= 최신 판정일)를 snap_date로
    `link_stock_theme(code, theme_name, snap_date, "backfill")` UPSERT
  - `link_stock_theme`은 `ON CONFLICT(stock_code, theme_id) DO UPDATE`로
    `date_last`/`source`만 갱신 → **idempotent**. 반복 실행해도 부작용 없음.

사용:
  # 영향 건수 미리 보기 (DB 쓰기 없음)
  python3 -m scripts.news_pipeline.backfill_stock_themes --dry-run

  # 실제 실행 (1회성)
  python3 -m scripts.news_pipeline.backfill_stock_themes

  # cron 금지. 타치코마가 수동 실행 후 결과 확인.

제약:
  - 본 스크립트는 단발성. 반복 실행 안전(idempotent)하지만 상시 스케줄 금지.
  - `themes.is_active = 0` 테마는 의도적으로 제외(가비지 매핑 유입 방지).
  - `link_stock_theme`이 내부적으로 비활성 테마를 재검증하므로 이중 안전.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable

from .db import connect
from .theme_normalizer import link_stock_theme


def _fetch_candidates(conn) -> list[dict]:
    """백필 대상 행 조회.

    `stock_theme_daily`의 (stock_code, theme_id) 쌍마다 최신 date를 계산하고,
    활성 테마만 남긴다. 이미 `stock_themes`에 존재하는 쌍도 포함(source를
    'backfill'로 갱신하고 date_last를 최신으로 당기는 것은 무해)하지만
    로그 혼선을 피하기 위해 결과 집합에는 전체를 싣고 실제 복구 건수는
    `link_stock_theme` 전/후 row 수 차이로 계산한다.
    """
    rows = conn.execute(
        """
        SELECT std.stock_code  AS stock_code,
               t.name          AS theme_name,
               MAX(std.date)   AS snap_date
        FROM stock_theme_daily std
        JOIN themes t ON std.theme_id = t.id
        WHERE t.is_active = 1
        GROUP BY std.stock_code, std.theme_id
        ORDER BY std.stock_code, t.name
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _count_missing(conn) -> tuple[int, int]:
    """현재 stock_themes에 없는 쌍 수 / 영향 종목 수."""
    missing = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT DISTINCT std.stock_code, std.theme_id
            FROM stock_theme_daily std
            JOIN themes t ON std.theme_id = t.id
            WHERE t.is_active = 1
            EXCEPT
            SELECT stock_code, theme_id FROM stock_themes
        )
        """
    ).fetchone()[0]
    impacted = conn.execute(
        """
        SELECT COUNT(DISTINCT stock_code) FROM (
            SELECT DISTINCT std.stock_code, std.theme_id
            FROM stock_theme_daily std
            JOIN themes t ON std.theme_id = t.id
            WHERE t.is_active = 1
            EXCEPT
            SELECT stock_code, theme_id FROM stock_themes
        )
        """
    ).fetchone()[0]
    return int(missing), int(impacted)


def run(dry_run: bool = False) -> dict:
    """백필 실행. dry_run=True면 쓰기 없이 집계만."""
    with connect() as conn:
        candidates = _fetch_candidates(conn)
        missing_before, impacted = _count_missing(conn)

    print(f"[backfill_stock_themes] 후보 (stock,theme) 쌍: {len(candidates)}")
    print(f"[backfill_stock_themes] stock_themes 미등록 쌍: {missing_before}")
    print(f"[backfill_stock_themes] 복구 대상 종목 수     : {impacted}")

    if dry_run:
        print("[backfill_stock_themes] --dry-run: DB 쓰기 생략")
        return {
            "candidates": len(candidates),
            "missing_before": missing_before,
            "impacted_stocks": impacted,
            "upserted": 0,
            "failed": 0,
            "dry_run": True,
        }

    upserted = 0
    failed = 0
    for row in candidates:
        code = row["stock_code"]
        name = row["theme_name"]
        snap = row["snap_date"]
        try:
            link_stock_theme(code, name, snap, "backfill")
            upserted += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  [FAIL] {code} / {name} @ {snap}: {exc}", file=sys.stderr)

    with connect() as conn:
        missing_after, _ = _count_missing(conn)

    print(
        f"[backfill_stock_themes] UPSERT 성공: {upserted} / 실패: {failed}\n"
        f"[backfill_stock_themes] 미등록 쌍 변화: {missing_before} → {missing_after}"
    )
    return {
        "candidates": len(candidates),
        "missing_before": missing_before,
        "missing_after": missing_after,
        "impacted_stocks": impacted,
        "upserted": upserted,
        "failed": failed,
        "dry_run": False,
    }


def _parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="backfill_stock_themes",
        description=(
            "stock_theme_daily의 과거 이시카와 판정을 stock_themes로 일괄 "
            "UPSERT한다. DOC-20260422-FLR-008 hot-fix(42c0b13) 후속."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="DB 쓰기 없이 영향 건수만 출력",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


if __name__ == "__main__":
    args = _parse_args()
    result = run(dry_run=args.dry_run)
    # 비영(0) 실패 시 종료코드 1 (cron 아닌 수동 실행 기준)
    sys.exit(1 if result.get("failed", 0) else 0)
