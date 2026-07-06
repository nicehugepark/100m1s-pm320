"""5/6 영웅식 strict v3 — KOSPI100 + KIND 4종 (관리/환기/정리/불성실) + ETF 제외 보강.

대표 결정 P0 (2026-05-08 ~21:00 KST 본 사이클):
  Phase 1-B Path B 영웅식 5종 EXCLUDE 룰 정확 재현. 5/6 백필 거의 완벽화.

영웅식 spec (b763e9a + 16:00 캡처 영구):
  조건식: !A and B and C and D
    A: KOSPI100 (KODEX 153270 ETF 추종)
    B: [일] 1봉전 종가대비 0봉전 고가등락률 5% 이상 (high vs prev_close)
    C: [일] 거래대금 [백만원] 50,001 이상 9,999,999,990 이하 (= 500억~1경)
    D: [일] 0봉전 고가가 16봉중 신고가 (strict greater)

자동 제외 5종 (영웅식 검색식 내장):
  1. KOSPI100 (heroshik_strict_5_6.py 기존 구현 — kospi100_constituents 테이블 사용)
  2. ETF (stocks.market 미구분 시 종목명 휴리스틱: KODEX/TIGER/ARIRANG/HANARO/KOSEF/KBSTAR/SOL/PLUS/HK/ACE/RISE/KIWOOM/SAM/NH-Amundi)
  3. 관리종목 (KIND investwarn/adminissue.do)
  4. 투자환기종목 (KIND investwarn/hwangiissue.do)
  5. 정리매매종목 (KIND investwarn/delcompany.do)
  6. 불성실공시법인 (KIND investwarn/undisclosure.do)

본 모듈 = 5/6 단일 일자 only.
출력 source = 'heroshik_strict_5_6_v3' (기존 'heroshik_strict_5_6' 보존).

KIND fetch 실패 시 graceful — 해당 카테고리는 빈 set 으로 진행 + WARN.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import datetime

# DB_PATH SoT — config.py 의 DB_PATH 정합 (env M1S_HOMEPAGE override).
# cron worktree 격리 (lead-meta §11.32) 정합 — 2026-05-28 cycle25 cron-isolation A1 환경변수 일괄.
from .config import DB_PATH
from .exclude_categories_kind import collect_excluded_codes
from .heroshik_strict_5_6 import (
    KOSPI100_SOURCE_TAG,
    MAX_TRADE_AMOUNT_KRW,
    MIN_HIGH_CHANGE_PCT,
    MIN_TRADE_AMOUNT_KRW,
    ensure_kospi100_table,
    fetch_kospi100_constituents,
    seed_kospi100,
)

TARGET_DATE = "2026-05-06"
SOURCE_TAG = "heroshik_strict_5_6_v3"
HISTORY_LOOKBACK_DAYS = 16  # 16일 신고가 (D 조건)

# ETF EXCLUDE 룰 (2026-05-08 catch):
#   stocks 테이블에는 KOSPI/KOSDAQ 보통주만 적재 (~2663 row), ETF 자체가 부재.
#   따라서 strict SQL 의 `s.market IN ('KOSPI','KOSDAQ')` 만으로 ETF 자동 제외 보장.
#   prefix 휴리스틱은 false positive (예: 037030 파워넷 = 보통주) 위험 → 미사용.
#   안전망 테이블 stocks.market='ETF'/'ETN'/'ELW'/'SPAC' 발견 시에만 명시 제거.


def _ensure_v3_exclusion_table(conn: sqlite3.Connection) -> None:
    """KIND 4종 exclusion snapshot 테이블 (검증/감사용).

    daily_picks 와 별도 — pick 결과에 영향 X. 디버깅·재현을 위한 audit log.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS heroshik_v3_excluded (
          target_date  TEXT NOT NULL,
          stock_code   TEXT NOT NULL,
          stock_name   TEXT,
          category     TEXT NOT NULL,
          source       TEXT NOT NULL DEFAULT 'kind',
          snapshot_at  TEXT NOT NULL,
          PRIMARY KEY (target_date, stock_code, category, source)
        )
        """
    )
    conn.commit()


def _record_kind_exclusions(
    conn: sqlite3.Connection,
    target_date: str,
    kind_result: dict[str, dict],
) -> None:
    """KIND 4종 결과 audit log 적재 (idempotent re-run)."""
    snapshot_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00")
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM heroshik_v3_excluded WHERE target_date=? AND source='kind'",
        (target_date,),
    )
    for tag, info in kind_result.items():
        for code in info["codes"]:
            row = cur.execute(
                "SELECT name FROM stocks WHERE code=?", (code,)
            ).fetchone()
            name = row[0] if row else ""
            cur.execute(
                """INSERT OR REPLACE INTO heroshik_v3_excluded
                   (target_date, stock_code, stock_name, category, source, snapshot_at)
                   VALUES (?, ?, ?, ?, 'kind', ?)""",
                (target_date, code, name, tag, snapshot_at),
            )
    conn.commit()


def _get_etf_codes(conn: sqlite3.Connection) -> set[str]:
    """ETF/ETN/ELW/SPAC code set (안전망).

    stocks 테이블 자체가 KOSPI/KOSDAQ 보통주만 포함 (~2663 row, ETF 부재) →
    `s.market IN ('KOSPI','KOSDAQ')` 만으로 ETF 자동 제외. 본 함수는 향후 stocks
    스키마 확장 (ETF 적재) 대비 안전망. prefix 휴리스틱은 false positive 위험으로 제거.
    """
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT code FROM stocks WHERE market IN ('ETF','ETN','ELW','SPAC')"
    ).fetchall()
    return {r[0] for r in rows}


def apply_strict_filter_v3(
    conn: sqlite3.Connection,
    extra_excluded_codes: set[str],
) -> list[tuple]:
    """5/6 영웅식 strict satisfier 종목 list 산출 (v3 EXCLUDE 4종 + ETF 보강).

    extra_excluded_codes: KIND 4종 + ETF 휴리스틱 union
    """
    excluded_list = sorted(extra_excluded_codes)
    placeholders = ",".join("?" * len(excluded_list)) if excluded_list else "''"
    extra_filter = f"AND s.code NOT IN ({placeholders})" if excluded_list else ""

    # Fix B Phase 3 (DOC-20260514-REQ-002 §3.3 + DOC-20260515-DSN-001 §1.4):
    #   prev_close subquery에 권리락 ratio 보정 적용 (동형 cascade).
    sql = f"""
    WITH base AS (
      SELECT
        db.code,
        db.high,
        db.close,
        (SELECT close FROM dailybars
           WHERE code=db.code AND date<? AND close IS NOT NULL
           ORDER BY date DESC LIMIT 1) AS raw_prev_close,
        (SELECT date FROM dailybars
           WHERE code=db.code AND date<? AND close IS NOT NULL
           ORDER BY date DESC LIMIT 1) AS prev_date,
        (SELECT MAX(high) FROM (
            SELECT high FROM dailybars
              WHERE code=db.code AND date<? AND high IS NOT NULL
              ORDER BY date DESC LIMIT 15
         )) AS hist_max_high_15
      FROM dailybars db
      WHERE db.date=? AND db.high IS NOT NULL
    ),
    base_adj AS (
      SELECT
        b.code, b.high, b.close, b.raw_prev_close, b.prev_date,
        (b.raw_prev_close * COALESCE((
          SELECT ratio FROM dailybars_adjustments adj
          WHERE adj.code = b.code
            AND adj.date > b.prev_date
            AND adj.date <= ?
          ORDER BY adj.date DESC LIMIT 1
        ), 1.0)) AS prev_close,
        b.hist_max_high_15
      FROM base b
    ),
    amt AS (
      SELECT stock_code, MAX(trade_amount) AS trade_amount, MAX(change_pct) AS change_pct
      FROM daily_picks WHERE date=? GROUP BY stock_code
    ),
    db_amt AS (
      SELECT code, trade_amount FROM dailybars WHERE date=? AND trade_amount IS NOT NULL
    )
    SELECT
      b.code,
      s.name,
      s.market,
      COALESCE(a.trade_amount, da.trade_amount) AS trade_amount,
      b.high,
      b.prev_close,
      ROUND((b.high - b.prev_close) * 100.0 / b.prev_close, 2) AS chg_high_pct,
      b.hist_max_high_15
    FROM base_adj b
    JOIN stocks s ON s.code = b.code
    LEFT JOIN amt a ON a.stock_code = b.code
    LEFT JOIN db_amt da ON da.code = b.code
    WHERE
      -- !A: KOSPI100 종목 제외
      s.code NOT IN (SELECT code FROM kospi100_constituents)
      -- 시장: KOSPI/KOSDAQ
      AND s.market IN ('KOSPI', 'KOSDAQ')
      -- v3 신규: KIND 4종 + ETF 휴리스틱 제외
      {extra_filter}
      -- B: high 기준 등락률 5% 이상 (권리락 보정 적용)
      AND b.prev_close IS NOT NULL AND b.prev_close > 0
      AND ((b.high - b.prev_close) * 100.0 / b.prev_close) >= ?
      -- C: 거래대금 500억 이상 + 1경 cap
      AND COALESCE(a.trade_amount, da.trade_amount) >= ?
      AND COALESCE(a.trade_amount, da.trade_amount) <= ?
      -- D: 16일 신고가 strict
      AND b.hist_max_high_15 IS NOT NULL
      AND b.high > b.hist_max_high_15
    ORDER BY trade_amount DESC
    """
    params: list = [
        TARGET_DATE,
        TARGET_DATE,
        TARGET_DATE,
        TARGET_DATE,
        TARGET_DATE,  # base_adj adj.date <= ?
        TARGET_DATE,
        TARGET_DATE,
    ]
    params.extend(excluded_list)
    params.extend([MIN_HIGH_CHANGE_PCT, MIN_TRADE_AMOUNT_KRW, MAX_TRADE_AMOUNT_KRW])
    rows = conn.execute(sql, params).fetchall()
    return rows


def upsert_daily_picks_v3(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """v3 source 별로 daily_picks UPSERT (기존 v2 / heroshik_strict_5_6 / kiwoom 보존)."""
    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00")
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM daily_picks WHERE date=? AND source=?",
        (TARGET_DATE, SOURCE_TAG),
    )

    inserted = 0
    for rank, row in enumerate(rows, start=1):
        code, name, market, trade_amount, high, prev_close, chg_high_pct, _ = row
        cur.execute(
            """INSERT INTO daily_picks
               (date, stock_code, rank, trade_amount, change_pct, price, high_price, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(date, stock_code, source) DO UPDATE SET
                 rank=excluded.rank,
                 trade_amount=excluded.trade_amount,
                 change_pct=excluded.change_pct,
                 high_price=excluded.high_price,
                 created_at=excluded.created_at""",
            (
                TARGET_DATE,
                code,
                rank,
                trade_amount,
                chg_high_pct,
                high,
                high,
                SOURCE_TAG,
                now_iso,
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def main():
    print(f"[heroshik_strict_5_6_v3] start target={TARGET_DATE}")
    print(f"[heroshik_strict_5_6_v3] DB={DB_PATH}")

    # Step 0: 사전 백업 (destructive 가드)
    backup_path = (
        DB_PATH.parent
        / f".backup-heroshik-v3-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    )
    shutil.copy2(DB_PATH, backup_path)
    print(f"[step0] backup: {backup_path}")

    # Step 1: KOSPI100 fetch (재사용)
    print("[step1] fetching KOSPI100 constituents (KODEX 153270)...")
    constituents = fetch_kospi100_constituents()
    print(f"[step1] KOSPI100 count={len(constituents)} (source={KOSPI100_SOURCE_TAG})")

    if len(constituents) < 80:
        print(f"[step1] FATAL: only {len(constituents)} constituents fetched. Abort.")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    ensure_kospi100_table(conn)
    seeded = seed_kospi100(conn, constituents)
    print(f"[step2] seeded kospi100_constituents: {seeded} rows")

    # Step 3: KIND 4종 EXCLUDE fetch
    print("[step3] fetching KIND 4-category exclusion lists...")
    kind_result = collect_excluded_codes(conn, TARGET_DATE)
    for tag, info in kind_result.items():
        print(
            f"  {tag}: fetched={info['fetched']} before_date={info['before_date']} "
            f"matched={info['matched']} unmatched={len(info['unmatched_names'])}"
        )
        if info["unmatched_names"]:
            print(f"    unmatched sample: {info['unmatched_names'][:5]}")

    kind_codes = set().union(*(info["codes"] for info in kind_result.values()))
    print(f"[step3] KIND UNION codes: {len(kind_codes)}")

    # Audit log 적재
    _ensure_v3_exclusion_table(conn)
    _record_kind_exclusions(conn, TARGET_DATE, kind_result)
    print("[step3] audit log heroshik_v3_excluded: inserted")

    # Step 4: ETF (휴리스틱) EXCLUDE
    etf_codes = _get_etf_codes(conn)
    print(f"[step4] ETF codes (market+prefix heuristic): {len(etf_codes)}")

    excluded_union = kind_codes | etf_codes
    print(f"[step4] EXCLUDE UNION (KIND 4종 + ETF): {len(excluded_union)}")

    # Step 5: strict SQL with v3 filter
    print("[step5] applying strict SQL filter (v3)...")
    rows = apply_strict_filter_v3(conn, excluded_union)
    print(f"[step5] v3 strict satisfier count={len(rows)}")
    print("[step5] results:")
    for i, r in enumerate(rows, 1):
        code, name, market, trade_amount, high, prev_close, chg_high_pct, hmax15 = r
        amt_eok = (trade_amount or 0) // 100_000_000
        print(
            f"  [{i:2d}] {code} {name:20s} {market:6s} 거래대금={amt_eok:>7,}억 "
            f"high={high:>8} prev_close={prev_close:>8} chg_high={chg_high_pct:>6.2f}%"
        )

    # Step 6: diff vs existing sources
    cur = conn.cursor()
    v2_codes = {
        r[0]
        for r in cur.execute(
            "SELECT stock_code FROM daily_picks WHERE date=? AND source=?",
            (TARGET_DATE, "condition_500eok_OR_limitup_5_6_v2"),
        ).fetchall()
    }
    h_strict_codes = {
        r[0]
        for r in cur.execute(
            "SELECT stock_code FROM daily_picks WHERE date=? AND source=?",
            (TARGET_DATE, "heroshik_strict_5_6"),
        ).fetchall()
    }
    v3_codes = {r[0] for r in rows}
    print(f"[step6] v2(condition_500eok_OR_limitup_5_6_v2) count={len(v2_codes)}")
    print(f"[step6] heroshik_strict_5_6 count={len(h_strict_codes)}")
    print(f"[step6] v3 count={len(v3_codes)}")
    print(f"[step6] v3 ∩ v2 = {len(v3_codes & v2_codes)}")
    print(f"[step6] v3 ∩ heroshik_strict = {len(v3_codes & h_strict_codes)}")
    print(f"[step6] heroshik_strict \\ v3 = {len(h_strict_codes - v3_codes)}")
    if h_strict_codes - v3_codes:
        # Categorize newly-removed ones
        print("[step6] newly removed by v3 (in heroshik_strict but NOT in v3):")
        for code in sorted(h_strict_codes - v3_codes):
            row = cur.execute(
                "SELECT name FROM stocks WHERE code=?", (code,)
            ).fetchone()
            name = row[0] if row else ""
            cats = []
            for tag, info in kind_result.items():
                if code in info["codes"]:
                    cats.append(tag)
            if code in etf_codes:
                cats.append("ETF")
            cat_str = ",".join(cats) if cats else "(unknown)"
            print(f"    - {code} {name} -> {cat_str}")

    # Step 7: UPSERT
    print("[step7] upsert daily_picks (v3)...")
    inserted = upsert_daily_picks_v3(conn, rows)
    print(f"[step7] upserted {inserted} rows with source={SOURCE_TAG}")

    # Step 8: confirm preserved
    cur.execute(
        "SELECT source, COUNT(*) FROM daily_picks WHERE date=? GROUP BY source",
        (TARGET_DATE,),
    )
    print("[step8] daily_picks by source on 5/6 (post-v3):")
    for src, cnt in cur.fetchall():
        print(f"  {src}: {cnt}")

    conn.close()
    print("[heroshik_strict_5_6_v3] DONE")


if __name__ == "__main__":
    main()
