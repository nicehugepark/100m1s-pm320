"""5/6 영웅식 strict 적용 (단일 일자) — !A NOT KOSPI100 + 500억 cap + 16일 신고가 strict.

대표 결정 P0 (2026-05-08 16:17 KST):
  "5.6만 해. 다른 날은 이미 잘 동작했었는데 굳이..."

영웅식 spec (togusa-kospi1100-spec-verify 16:04 결론):
  조건식: !A and B and C and D
    A: KOSPI1100 (= KOSPI100 OCR 오인식, 시총 상위 100 KOSPI 보통주)
       !A = KOSPI100 종목 제외
    B: [일] 1봉전 종가대비 0봉전 고가등락률 5% 이상 (high 기준)
    C: [일] 거래대금 [백만원] 50,001 이상 9,999,999,990 이하 (= 500억원~1경원, 정정 P0 2026-05-08 18:23 — togusa 보고 500,000=5,000억 오해석 본 catch에서 정정)
    D: [일] 0봉전 고가가 16봉중 신고가 (strict greater)

대상: KOSPI + KOSDAQ 전체 (관리/환기/정리/불성실/ETF 제외 — 향후 보강)

KOSPI100 source: Naver finance 시가총액 상위 100 KOSPI 보통주 (우선주·ETF 제외)
  - 정확한 KOSPI100 인덱스 구성종목과 다소 차이 있음 (KOSPI200 부분집합 vs 단순 시총)
  - 메가캡 블루칩 제외 목적은 충족
  - 향후 KRX OpenAPI 또는 키움 API 도입 시 정밀화 (P1)

본 모듈 = 5/6 단일 일자 only. 다른 날 절대 X.
출력 source = 'heroshik_strict_5_6' (기존 'kiwoom' 88건 + 'condition_500eok_OR_limitup_5_6_v2' 87건 보존).
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime

import requests

# DB_PATH SoT — config.py 의 DB_PATH 정합 (env M1S_HOMEPAGE override 가능).
# DSN-arch-pipeline §2.4 동기 완료 (2026-05-09 02:13 KST, FLR-20260509-AGT-001 후속).
# cron worktree 격리 (lead-meta §11.32) 정합 — 2026-05-28 cycle25 cron-isolation A1 환경변수 일괄.
from .config import DB_PATH

TARGET_DATE = "2026-05-06"
SOURCE_TAG = "heroshik_strict_5_6"
HISTORY_LOOKBACK_DAYS = 16  # 16일 신고가 (D 조건)
MIN_TRADE_AMOUNT_KRW = (
    50_000_000_000  # 500억원 (C 하한, 영웅식 정확 spec: [백만] 50,001+ → 500.01억+)
)
MAX_TRADE_AMOUNT_KRW = 9_999_999_990_000_000  # 1경원 cap
MIN_HIGH_CHANGE_PCT = 5.0  # B 조건 (high 기준 등락률 5%+)


KOSPI100_SOURCE_TAG = "kiwoom_etf_153270"
KIWOOMETF_PDF_URL = "https://www.kiwoometf.com/service/etf/KO02010200MAjax4"
KIWOOMETF_REFERER = "https://www.kiwoometf.com/service/etf/KO02010200M?gcode=153270"


def fetch_kospi100_constituents() -> list[tuple[str, str]]:
    """KODEX 코스피100 ETF (153270) PDF에서 KOSPI100 구성종목 fetch.

    정확 KOSPI100 인덱스 = KRX 공식 KOSPI100 추종 ETF. Naver 시총 top 100과 정의 다름.
    대표 P0 catch (2026-05-08 18:27): "Naver 시총 top 100은 KOSPI100 인덱스가 아니다."

    Returns: [(code, name), ...] (~100~101개)
    """
    pdf_dt = datetime.now().strftime("%Y%m%d")
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": KIWOOMETF_REFERER,
    }
    data = {"schGubun1": "153270", "startDate": pdf_dt}

    for attempt in range(3):
        try:
            r = requests.post(
                KIWOOMETF_PDF_URL,
                headers=headers,
                data=data,
                timeout=20,
                verify=False,
            )
            r.raise_for_status()
            payload = r.json()
            break
        except Exception:
            if attempt == 2:
                raise
            continue

    items = payload.get("pdfList") or []
    parsed: list[tuple[str, str]] = []
    for it in items:
        gcode = (it.get("gcode") or "").strip()
        name = (it.get("itemTitle") or "").strip()
        if not gcode.startswith("KR") or len(gcode) != 12:
            continue
        code = gcode[3:9]
        if not code.isdigit() or len(code) != 6:
            continue
        parsed.append((code, name))

    return parsed


def ensure_kospi100_table(conn: sqlite3.Connection) -> None:
    """kospi100_constituents 테이블 생성 (없으면).

    DEFAULT source: kiwoom_etf_153270 (KODEX 코스피100 ETF 추종, P0 2026-05-08 18:27).
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kospi100_constituents (
          code        TEXT PRIMARY KEY,
          name        TEXT NOT NULL,
          source      TEXT NOT NULL DEFAULT 'kiwoom_etf_153270',
          snapshot_at TEXT NOT NULL
        )
    """)
    conn.commit()


def seed_kospi100(conn: sqlite3.Connection, constituents: list[tuple[str, str]]) -> int:
    """kospi100_constituents 갱신 (delete-and-insert로 snapshot 단일 보장)."""
    snapshot_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00")
    cur = conn.cursor()
    cur.execute("DELETE FROM kospi100_constituents")
    cur.executemany(
        "INSERT INTO kospi100_constituents (code, name, source, snapshot_at) VALUES (?, ?, ?, ?)",
        [(c, n, KOSPI100_SOURCE_TAG, snapshot_at) for c, n in constituents],
    )
    conn.commit()
    return cur.rowcount


def apply_strict_filter(conn: sqlite3.Connection) -> list[tuple]:
    """5/6 영웅식 strict satisfier 종목 list 산출.

    Returns: [(code, name, market, trade_amount, high, prev_close, chg_high_pct, hist_max_high_15), ...]
    """
    # Fix B Phase 3 (DOC-20260514-REQ-002 §3.3 + DOC-20260515-DSN-001 §1.4):
    #   prev_close subquery에 권리락 ratio 보정 적용.
    #   adj_ratio = prev_date(exclusive) ~ TARGET_DATE(inclusive) 사이 발생한 권리락 ratio.
    #   DEFAULT 1.0 (권리락 미발생 종목 효과 무변, 단일 권리락 가정 LIMIT 1).
    sql = """
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
      -- B: high 기준 등락률 5% 이상 (권리락 보정 적용)
      AND b.prev_close IS NOT NULL AND b.prev_close > 0
      AND ((b.high - b.prev_close) * 100.0 / b.prev_close) >= ?
      -- C: 거래대금 500억 이상 + 1경 cap (정정 P0 2026-05-08)
      AND COALESCE(a.trade_amount, da.trade_amount) >= ?
      AND COALESCE(a.trade_amount, da.trade_amount) <= ?
      -- D: 16일 신고가 strict
      AND b.hist_max_high_15 IS NOT NULL
      AND b.high > b.hist_max_high_15
    ORDER BY trade_amount DESC
    """
    rows = conn.execute(
        sql,
        (
            TARGET_DATE,
            TARGET_DATE,
            TARGET_DATE,
            TARGET_DATE,
            TARGET_DATE,  # base_adj adj.date <= ?
            TARGET_DATE,
            TARGET_DATE,
            MIN_HIGH_CHANGE_PCT,
            MIN_TRADE_AMOUNT_KRW,
            MAX_TRADE_AMOUNT_KRW,
        ),
    ).fetchall()
    return rows


def upsert_daily_picks(conn: sqlite3.Connection, rows: list[tuple]) -> int:
    """heroshik_strict_5_6 source 별로 daily_picks UPSERT.

    기존 'kiwoom' / 'condition_500eok_OR_limitup_5_6_v2' source는 절대 touch 금지.
    """
    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00")
    cur = conn.cursor()
    # First clear any prior heroshik_strict_5_6 entries for 5/6 (idempotent re-run)
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
    print(f"[heroshik_strict_5_6] start target={TARGET_DATE}")
    print(f"[heroshik_strict_5_6] DB={DB_PATH}")

    # Step 1: KOSPI100 fetch (KODEX ETF 153270, P0 2026-05-08 18:27)
    print("[step1] fetching KOSPI100 constituents from KODEX ETF 153270...")
    constituents = fetch_kospi100_constituents()
    print(f"[step1] KOSPI100 count={len(constituents)}")
    print(f"[step1] sample first 5={constituents[:5]}")
    print(f"[step1] sample last 5={constituents[-5:]}")

    if len(constituents) < 80:
        print(
            f"[step1] FATAL: only {len(constituents)} constituents fetched (expected ~100). Abort."
        )
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)

    # Step 2: seed table
    ensure_kospi100_table(conn)
    seeded = seed_kospi100(conn, constituents)
    print(f"[step2] seeded kospi100_constituents: {seeded} rows")

    # Step 3: apply strict SQL
    print("[step3] applying strict SQL filter...")
    rows = apply_strict_filter(conn)
    print(f"[step3] strict satisfier count={len(rows)}")
    print("[step3] results:")
    for i, r in enumerate(rows, 1):
        code, name, market, trade_amount, high, prev_close, chg_high_pct, hmax15 = r
        amt_eok = (trade_amount or 0) // 100_000_000
        print(
            f"  [{i:2d}] {code} {name:20s} {market:6s} 거래대금={amt_eok:>7,}억 "
            f"high={high:>8} prev_close={prev_close:>8} chg_high={chg_high_pct:>6.2f}%"
        )

    # Verify KOSPI100 exclusion
    excluded_check_codes = [
        "005930",
        "000660",
        "402340",
        "005380",
        "373220",
    ]  # 삼성전자/하이닉스/SK스퀘어/현대차/LG에너지솔루션
    cur = conn.cursor()
    for code in excluded_check_codes:
        cur.execute("SELECT 1 FROM kospi100_constituents WHERE code=?", (code,))
        in_list = cur.fetchone() is not None
        present_in_result = any(r[0] == code for r in rows)
        print(
            f"[verify] {code}: in_kospi100_list={in_list}, in_strict_result={present_in_result}"
        )

    # Step 4: UPSERT
    print("[step4] upsert daily_picks...")
    inserted = upsert_daily_picks(conn, rows)
    print(f"[step4] upserted {inserted} rows with source={SOURCE_TAG}")

    # Step 5: confirm preserved
    cur.execute(
        "SELECT source, COUNT(*) FROM daily_picks WHERE date=? GROUP BY source",
        (TARGET_DATE,),
    )
    print("[step5] daily_picks by source on 5/6:")
    for src, cnt in cur.fetchall():
        print(f"  {src}: {cnt}")

    conn.close()
    print("[heroshik_strict_5_6] DONE")


if __name__ == "__main__":
    main()
