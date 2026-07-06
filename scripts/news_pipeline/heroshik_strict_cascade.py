"""영웅식 strict cascade — 5/6 단일 일자 백필 전용 (5/7, 5/8 미적용 폐기).

🔴 본 모듈은 5/6 single 일자 백필 전용. 5/7/5/8 등 cron 정상 작동 일자에는 절대 호출 금지.

배경 (2026-05-08 20:18 대표 직접 catch P0 — verbatim):
  "왜 다른 날짜까지 자체 조건검색을 수동으로 돌리게 된 건지 이해할 수 없다."

근거 시퀀스:
  - 5/6 사고 = cron 36시간 정지 (FLR-20260507-FLR-001) → ka10017 (kiwoom source) 5/6 부재
  - 5/6만 자체 SQL 백필 의무 (heroshik_strict_5_6 source, 16:17 결정)
  - 5/7/5/8 = ka10017 (kiwoom source) 정상 작동 — 자체 SQL 재적용 = over-engineering + 데이터 오염
  - 5/8 17:11 lead 결함: cascade 강제 적용 = 모든 후속 결함의 root

본래 시스템 동작 회복 (2026-05-08 20:18 verbatim):
  - SC = (ka10017 satisfier; daily_picks source='kiwoom') ∪ (LU 상한가 union)
  - heroshik_strict_<m_d> source는 5/6만 union 진입
  - cascade 모듈은 5/6 외 일자 호출 시 즉시 abort

영웅식 spec (5/6 백필 적용 시점 정합):
  조건식: !A and B and C and D
    A: KOSPI100 (KODEX 153270 ETF 추종, KRX 공식 인덱스)
       !A = KOSPI100 종목 제외
    B: [일] 1봉전 종가대비 0봉전 고가등락률 5% 이상 (high 기준)
    C: [일] 거래대금 [백만원] 50,001 이상 9,999,999,990 이하 (500억원~1경원)
    D: [일] 0봉전 고가가 16봉중 신고가 (strict greater)

멱등성 의무: 같은 일자 N번 실행 시 결과 동일 (DELETE+INSERT 패턴).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime

import requests

# DB_PATH SoT — config.py 의 DB_PATH 정합 (env M1S_HOMEPAGE override 가능).
# DSN-arch-pipeline §2.4 갱신 (cycle25 cron-isolation A1).
# cron worktree 격리 (lead-meta §11.32) 정합 — 2026-05-28 환경변수 일괄.
from .config import DB_PATH

HISTORY_LOOKBACK_DAYS = 16  # 16일 신고가 (D 조건)
MIN_TRADE_AMOUNT_KRW = (
    50_000_000_000  # 500억원 (C 하한, 영웅식 정확 spec: [백만] 50,001+ → 500.01억+)
)
MAX_TRADE_AMOUNT_KRW = 9_999_999_990_000_000  # 1경원 cap
MIN_HIGH_CHANGE_PCT = 5.0  # B 조건 (high 기준 등락률 5%+)


def date_to_source_tag(date_iso: str) -> str:
    """'2026-05-07' → 'heroshik_strict_5_7' (5/6 모듈 동형 컨벤션)."""
    parts = date_iso.split("-")
    m_d = "_".join(str(int(p)) for p in parts[1:])
    return f"heroshik_strict_{m_d}"


KOSPI100_SOURCE_TAG = (
    "kiwoom_etf_153270"  # 정확 KOSPI100 = KODEX 코스피100 ETF (KRX 공식 인덱스 추종)
)
KIWOOMETF_PDF_URL = "https://www.kiwoometf.com/service/etf/KO02010200MAjax4"
KIWOOMETF_REFERER = "https://www.kiwoometf.com/service/etf/KO02010200M?gcode=153270"


def fetch_kospi100_constituents() -> list[tuple[str, str]]:
    """KODEX 코스피100 ETF (153270) PDF에서 KOSPI100 구성종목 fetch.

    정확 KOSPI100 인덱스 = KRX 공식 KOSPI100 추종 ETF. Naver 시총 top 100과 다름.
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
                verify=False,  # kiwoometf 인증서 체인 일부 환경 verify fail
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
            continue  # CASH/원화/현금성 등 비주식 제외
        code = gcode[3:9]
        if not code.isdigit() or len(code) != 6:
            continue
        parsed.append((code, name))

    # 키움 ETF는 일자에 따라 ±1 종목 (전환·변경 기간) → 95~105 정상 범위
    return parsed


def ensure_kospi100_table(conn: sqlite3.Connection) -> None:
    """kospi100_constituents 테이블 생성 (없으면).

    DEFAULT source: kiwoom_etf_153270 (KODEX 코스피100 ETF, KRX 공식 KOSPI100 추종).
    Naver 시총 top 100은 인덱스 정의 위반 → 영구 폐기 (P0 2026-05-08 18:27 catch).
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


def kospi100_count(conn: sqlite3.Connection) -> int:
    cur = conn.execute("SELECT COUNT(*) FROM kospi100_constituents")
    return cur.fetchone()[0]


def seed_kospi100(conn: sqlite3.Connection, constituents: list[tuple[str, str]]) -> int:
    snapshot_at = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00")
    cur = conn.cursor()
    cur.execute("DELETE FROM kospi100_constituents")
    cur.executemany(
        "INSERT INTO kospi100_constituents (code, name, source, snapshot_at) VALUES (?, ?, ?, ?)",
        [(c, n, KOSPI100_SOURCE_TAG, snapshot_at) for c, n in constituents],
    )
    conn.commit()
    return cur.rowcount


def apply_strict_filter(conn: sqlite3.Connection, target_date: str) -> list[tuple]:
    """target_date 영웅식 strict satisfier 종목 list 산출.

    Returns: [(code, name, market, trade_amount, high, prev_close, chg_high_pct, hist_max_high_15), ...]
    """
    # 거래정지 종목 명시적 제외 룰 폐기 (2026-05-08 20:10 대표 catch P0 verbatim):
    #   "거래정지의 경우 우리가 직접 필터링 하는게 아니라 영웅문의 내 조건식에서 자체 필터링이
    #   될것이다. 굳이 판단할 필요가 없다."
    # → 영웅식 spec 자체 (B 5%+ 등락률 + D 16일 신고가 strict)로 거래정지 종목은 자동 미진입.
    #   거래정지 종목은 high=prev_close (B 0% 미만족) AND high <= hist_max (D 미만족)이므로
    #   영웅식 spec만 정확 적용하면 자연스럽게 catch X. 별도 룰 추가 = over-engineering.
    # Fix B Phase 3 (DOC-20260514-REQ-002 §3.3 + DOC-20260515-DSN-001 §1.4):
    #   prev_close subquery에 권리락 ratio 보정 적용 (동형 cascade).
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
      s.code NOT IN (SELECT code FROM kospi100_constituents)
      AND s.market IN ('KOSPI', 'KOSDAQ')
      AND b.prev_close IS NOT NULL AND b.prev_close > 0
      AND ((b.high - b.prev_close) * 100.0 / b.prev_close) >= ?
      AND COALESCE(a.trade_amount, da.trade_amount) >= ?
      AND COALESCE(a.trade_amount, da.trade_amount) <= ?
      AND b.hist_max_high_15 IS NOT NULL
      AND b.high > b.hist_max_high_15
    ORDER BY trade_amount DESC
    """
    rows = conn.execute(
        sql,
        (
            target_date,
            target_date,
            target_date,
            target_date,
            target_date,  # base_adj adj.date <= ?
            target_date,
            target_date,
            MIN_HIGH_CHANGE_PCT,
            MIN_TRADE_AMOUNT_KRW,
            MAX_TRADE_AMOUNT_KRW,
        ),
    ).fetchall()
    return rows


def upsert_daily_picks(
    conn: sqlite3.Connection, target_date: str, source_tag: str, rows: list[tuple]
) -> int:
    """source_tag 별로 daily_picks UPSERT (멱등: DELETE+INSERT).

    기존 'kiwoom' / 'condition_*' / 다른 source는 절대 touch 금지 (date+code+source UNIQUE).
    """
    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%S+09:00")
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM daily_picks WHERE date=? AND source=?",
        (target_date, source_tag),
    )

    inserted = 0
    for rank, row in enumerate(rows, start=1):
        code, _name, _market, trade_amount, high, _prev_close, chg_high_pct, _ = row
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
                target_date,
                code,
                rank,
                trade_amount,
                chg_high_pct,
                high,
                high,
                source_tag,
                now_iso,
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def run_for_date(target_date: str, refresh_kospi100: bool = False) -> dict:
    """단일 일자 영웅식 strict 적용. 멱등성 보장 (재실행 시 동일 결과)."""
    source_tag = date_to_source_tag(target_date)
    print(f"[heroshik_cascade] start target={target_date} source={source_tag}")
    print(f"[heroshik_cascade] DB={DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    ensure_kospi100_table(conn)

    existing = kospi100_count(conn)
    # source mismatch 강제 refresh — naver_marketcap_top100 등 stale source 차단 (P0 2026-05-08 18:27 catch)
    cur_src = conn.execute(
        "SELECT source, COUNT(*) FROM kospi100_constituents GROUP BY source"
    ).fetchall()
    stale_source = any(src != KOSPI100_SOURCE_TAG for src, _ in cur_src)
    if refresh_kospi100 or existing < 80 or stale_source:
        reason = (
            "stale_source"
            if stale_source
            else ("low_count" if existing < 80 else "explicit_refresh")
        )
        print(
            f"[step1] kospi100_constituents existing={existing} src_dist={cur_src} → re-fetch ({reason})"
        )
        constituents = fetch_kospi100_constituents()
        if len(constituents) < 80:
            print(
                f"[step1] FATAL: only {len(constituents)} constituents (expected ~100). Abort."
            )
            conn.close()
            sys.exit(1)
        seeded = seed_kospi100(conn, constituents)
        print(
            f"[step1] seeded kospi100_constituents: {seeded} rows source={KOSPI100_SOURCE_TAG}"
        )
    else:
        print(
            f"[step1] reuse existing kospi100_constituents: {existing} rows source={KOSPI100_SOURCE_TAG}"
        )

    print(f"[step2] applying strict SQL filter for {target_date}...")
    rows = apply_strict_filter(conn, target_date)
    print(f"[step2] strict satisfier count={len(rows)}")
    for i, r in enumerate(rows, 1):
        code, name, market, trade_amount, high, prev_close, chg_high_pct, _ = r
        amt_eok = (trade_amount or 0) // 100_000_000
        print(
            f"  [{i:2d}] {code} {name:20s} {market:6s} 거래대금={amt_eok:>7,}억 "
            f"high={high:>8} prev_close={prev_close:>8} chg_high={chg_high_pct:>6.2f}%"
        )

    excluded_check = ["005930", "000660", "402340", "005380", "373220"]
    cur = conn.cursor()
    for code in excluded_check:
        cur.execute("SELECT 1 FROM kospi100_constituents WHERE code=?", (code,))
        in_list = cur.fetchone() is not None
        present = any(r[0] == code for r in rows)
        print(f"[verify] {code}: in_kospi100={in_list}, in_strict_result={present}")

    print(f"[step3] upsert daily_picks source={source_tag}...")
    inserted = upsert_daily_picks(conn, target_date, source_tag, rows)
    print(f"[step3] upserted {inserted} rows")

    cur.execute(
        "SELECT source, COUNT(*) FROM daily_picks WHERE date=? GROUP BY source",
        (target_date,),
    )
    print(f"[step4] daily_picks by source on {target_date}:")
    for src, cnt in cur.fetchall():
        print(f"  {src}: {cnt}")

    conn.close()
    print(f"[heroshik_cascade] DONE {target_date} satisfier={len(rows)}")
    return {"date": target_date, "source": source_tag, "satisfier_count": len(rows)}


ALLOWED_DATES = {
    "2026-05-06"
}  # 5/6 single 일자 백필 전용 (대표 결정 2026-05-08 20:18 P0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--dates",
        required=True,
        help="comma-separated target dates (5/6 백필 전용 — 다른 일자 호출 시 abort)",
    )
    ap.add_argument(
        "--refresh-kospi100",
        action="store_true",
        help="KODEX ETF re-fetch (default reuse existing)",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="🔴 ALLOWED_DATES 가드 우회 (대표 명시 결정 시에만, FLR 의무)",
    )
    args = ap.parse_args()
    dates = [d.strip() for d in args.dates.split(",") if d.strip()]

    # 🔴 ALLOWED_DATES 가드 — 5/7/5/8 등 cron 정상 작동 일자 차단 (대표 P0 2026-05-08 20:18)
    illegal = [d for d in dates if d not in ALLOWED_DATES]
    if illegal and not args.force:
        print(
            "🔴 ABORT: heroshik_strict_cascade는 5/6 single 일자 백필 전용입니다.",
            file=sys.stderr,
        )
        print(f"   illegal dates: {illegal}", file=sys.stderr)
        print(f"   ALLOWED_DATES: {sorted(ALLOWED_DATES)}", file=sys.stderr)
        print(
            "   근거: 2026-05-08 20:18 대표 직접 catch P0 — '왜 다른 날짜까지 자체 조건검색을 "
            "수동으로 돌리게 된 건지 이해할 수 없다.'",
            file=sys.stderr,
        )
        print(
            "   본래 동작: SC = ka10017 (kiwoom source) ∪ LU. 5/7/5/8은 cron 정상 작동 = "
            "ka10017 자동 적재.",
            file=sys.stderr,
        )
        print(
            "   가드 우회 필요 시 --force 명시 (대표 결정 + FLR 의무).",
            file=sys.stderr,
        )
        sys.exit(2)

    refresh = args.refresh_kospi100
    results = []
    for i, d in enumerate(dates):
        r = run_for_date(d, refresh_kospi100=(refresh and i == 0))
        results.append(r)
    print("\n=== SUMMARY ===")
    for r in results:
        print(f"  {r['date']}  source={r['source']}  satisfier={r['satisfier_count']}")


if __name__ == "__main__":
    main()
