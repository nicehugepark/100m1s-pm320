"""
키움 거래대금상위(ka10032) 수집 스크립트

기존 조건검색(main.py)과 독립적으로 동작.
장 마감 후 1회 실행하여 거래대금 상위 50종목(ETF 제외)을 DB + JSON에 저장.

저장 대상:
  - stocks.db: stocks (upsert) + daily_picks (source='kiwoom_ranking')
  - data/kiwoom/ranking-{date}.json

사용법:
  # pipeline.sh에서 호출
  python -m scripts.kiwoom_scraper.collect_ranking

  # 직접 실행 (kiwoom-scraper/ 디렉토리에서)
  cd scripts/kiwoom-scraper && python collect_ranking.py
"""

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parents[1]  # 100m1s-homepage
DATA_DIR = REPO_ROOT / "data" / "kiwoom"
DB_PATH = REPO_ROOT / "data" / "stocks.db"

# ETF 브랜드명 필터 (종목명 prefix 기준)
ETF_BRANDS = (
    "KODEX",
    "TIGER",
    "KBSTAR",
    "SOL",
    "ACE",
    "ARIRANG",
    "HANARO",
    "PLUS",
    "KOSEF",
    "TREX",
    "BNK",
    "TIMEFOLIO",
    "WOORI",
    "KB STAR",
    "FOCUS",
    "MASTER",
    "WON",
    "파워",
)

SOURCE = "kiwoom_ranking"
TOP_N = 50


def log(msg: str) -> None:
    print(f"[{datetime.now(KST).isoformat(timespec='seconds')}] {msg}", flush=True)


def is_etf(name: str) -> bool:
    """종목명으로 ETF 판별."""
    upper = name.strip().upper()
    for brand in ETF_BRANDS:
        if upper.startswith(brand.upper()):
            return True
    # ETN도 제외
    if "ETN" in upper:
        return True
    return False


def parse_int(val) -> int:
    if val is None or val == "":
        return 0
    s = str(val).replace("+", "").replace(",", "").strip()
    if s.startswith("-"):
        try:
            return int(s)
        except ValueError:
            return 0
    s = s.lstrip("0") or "0"
    try:
        return int(s)
    except ValueError:
        return 0


def parse_float(val) -> float:
    if val is None or val == "":
        return 0.0
    s = str(val).replace("+", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def parse_ranking_stock(item: dict) -> dict:
    """ka10032 응답 1건 -> 표준 dict."""
    code = str(item.get("stk_cd", "")).strip()
    name = str(item.get("stk_nm", "")).strip()
    return {
        "code": code,
        "name": name,
        "price": parse_int(item.get("cur_prc")),
        "change_pct": parse_float(item.get("flu_rt")),
        "trade_amount": parse_int(item.get("trde_prica")) * 1_000_000,  # 백만원 -> 원
        "volume": parse_int(item.get("acml_vol", item.get("vol", ""))),
        # ka10032에 OHLC가 있으면 사용, 없으면 0
        "open": parse_int(item.get("open_prc", item.get("strt_prc", ""))),
        "high": parse_int(item.get("high_prc", "")),
        "low": parse_int(item.get("low_prc", "")),
    }


def save_to_db(
    conn: sqlite3.Connection, date_str: str, stocks: list, now_iso: str
) -> int:
    """stocks + daily_picks에 저장. 삽입 건수 반환."""
    cur = conn.cursor()
    inserted = 0

    for i, s in enumerate(stocks):
        # stocks 테이블 upsert
        cur.execute(
            """INSERT INTO stocks (code, name, last_updated)
               VALUES (?, ?, ?)
               ON CONFLICT(code) DO UPDATE SET
                 name = excluded.name,
                 last_updated = excluded.last_updated""",
            (s["code"], s["name"], now_iso),
        )

        # daily_picks — source='kiwoom_ranking'으로 구분
        cur.execute(
            """INSERT INTO daily_picks
                 (date, stock_code, rank, trade_amount, change_pct, price,
                  open_price, high_price, low_price, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(date, stock_code, source) DO UPDATE SET
                 rank = excluded.rank,
                 trade_amount = excluded.trade_amount,
                 change_pct = excluded.change_pct,
                 price = excluded.price,
                 open_price = CASE WHEN excluded.open_price > 0
                              THEN excluded.open_price ELSE daily_picks.open_price END,
                 high_price = CASE WHEN excluded.high_price > 0
                              THEN excluded.high_price ELSE daily_picks.high_price END,
                 low_price = CASE WHEN excluded.low_price > 0
                             THEN excluded.low_price ELSE daily_picks.low_price END""",
            (
                date_str,
                s["code"],
                i + 1,
                s["trade_amount"],
                s["change_pct"],
                s["price"],
                s["open"] or None,
                s["high"] or None,
                s["low"] or None,
                SOURCE,
                now_iso,
            ),
        )
        inserted += 1

    conn.commit()
    return inserted


def migrate_unique_constraint(conn: sqlite3.Connection) -> None:
    """UNIQUE(date, stock_code) -> UNIQUE(date, stock_code, source) 마이그레이션.

    SQLite는 테이블 제약을 ALTER할 수 없으므로 테이블 재생성 필요.
    이미 마이그레이션 완료 상태면 스킵.
    """
    cur = conn.cursor()
    # 현재 테이블 DDL 확인
    row = cur.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='daily_picks'"
    ).fetchone()
    if not row:
        return  # 테이블 자체가 없음

    ddl = row[0]
    # 이미 source 포함 UNIQUE면 스킵
    if "stock_code, source" in ddl or "stock_code,source" in ddl:
        return

    log(
        "daily_picks UNIQUE 제약 마이그레이션: (date,stock_code) -> (date,stock_code,source)"
    )
    cur.executescript("""
        ALTER TABLE daily_picks RENAME TO daily_picks_old;

        CREATE TABLE daily_picks (
          id            INTEGER PRIMARY KEY AUTOINCREMENT,
          date          TEXT NOT NULL,
          stock_code    TEXT NOT NULL,
          rank          INTEGER,
          trade_amount  INTEGER,
          change_pct    REAL,
          price         INTEGER,
          open_price    REAL,
          high_price    REAL,
          low_price     REAL,
          source        TEXT DEFAULT 'kiwoom',
          created_at    TEXT NOT NULL,
          UNIQUE(date, stock_code, source)
        );

        INSERT INTO daily_picks
          SELECT * FROM daily_picks_old;

        DROP TABLE daily_picks_old;

        CREATE INDEX IF NOT EXISTS idx_picks_date ON daily_picks(date DESC);
        CREATE INDEX IF NOT EXISTS idx_picks_code ON daily_picks(stock_code);
    """)
    log("마이그레이션 완료")


def save_json(date_str: str, stocks: list, now_iso: str) -> Path:
    """ranking-{date}.json 저장."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = {
        "date": date_str,
        "fetched_at": now_iso,
        "source": SOURCE,
        "count": len(stocks),
        "stocks": stocks,
    }
    path = DATA_DIR / f"ranking-{date_str}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def run() -> int:
    sys.path.insert(0, str(SCRIPT_DIR))

    appkey = os.environ.get("KIWOOM_APPKEY")
    secret = os.environ.get("KIWOOM_SECRETKEY")
    if not appkey or not secret:
        log("KIWOOM_APPKEY/KIWOOM_SECRETKEY 환경변수 없음")
        return 2

    try:
        from kiwoom_client import KiwoomClient
    except ImportError as e:
        log(f"kiwoom_client import 실패: {e}")
        return 3

    client = KiwoomClient()
    log("토큰 발급...")
    client.get_token()
    if not client.token:
        log("토큰 발급 실패")
        return 4

    now = datetime.now(KST)
    date_str = os.environ.get("PIPELINE_DATE", now.strftime("%Y-%m-%d"))
    now_iso = now.isoformat(timespec="seconds")

    try:
        # 1. ka10032 호출 (2페이지 = 최대 200건)
        log("ka10032 거래대금상위 조회 (2페이지)...")
        raw = client.get_trade_amount_ranking(mrkt_tp="000", pages=2)
        log(f"수신: {len(raw)}건")

        if not raw:
            log("결과 0건 (장 시간외 또는 API 오류)")
            return 0

        # 2. 파싱 + ETF 제외
        parsed = [parse_ranking_stock(item) for item in raw]
        parsed = [s for s in parsed if s["code"] and not is_etf(s["name"])]
        log(f"ETF 제외 후: {len(parsed)}건")

        # 3. 거래대금 내림차순 정렬 + 상위 50
        parsed.sort(key=lambda x: x["trade_amount"], reverse=True)
        top = parsed[:TOP_N]

        # 4. OHLC 보강 — ka10032 응답에 시가/고가/저가가 없는 종목만 일봉 조회
        missing_ohlc = [s for s in top if not s["open"] and not s["high"]]
        if missing_ohlc:
            log(f"OHLC 미제공 {len(missing_ohlc)}종목 — 일봉 조회...")
            for s in missing_ohlc:
                try:
                    chart = client.get_daily_chart(s["code"], count=1)
                    if chart:
                        c = chart[0]
                        s["open"] = parse_int(c.get("open") or c.get("strt_prc", ""))
                        s["high"] = parse_int(c.get("high") or c.get("high_prc", ""))
                        s["low"] = parse_int(c.get("low") or c.get("low_prc", ""))
                except Exception as e:
                    log(f"  {s['code']} 일봉 실패: {e}")
                time.sleep(0.3)  # rate limit

        # 5. DB 저장
        if not DB_PATH.exists():
            log(f"DB 미존재: {DB_PATH}")
            return 5

        conn = sqlite3.connect(str(DB_PATH))
        try:
            migrate_unique_constraint(conn)
            cnt = save_to_db(conn, date_str, top, now_iso)
            log(f"DB 저장: {cnt}건 (daily_picks source={SOURCE})")
        finally:
            conn.close()

        # 6. JSON 저장
        json_path = save_json(date_str, top, now_iso)
        log(f"JSON 저장: {json_path}")

        log(f"완료 - {date_str} 거래대금 상위 {len(top)}종목")

    finally:
        try:
            client.revoke_token()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(run())
