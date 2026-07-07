"""
daily_picks OHLC + 거래대금 백필 스크립트

데이터 소스 우선순위:
  1. 키움 ka10081 일봉 API (KIWOOM_APPKEY 설정 시 — GitHub Actions용)
  2. pykrx (로컬 fallback — 네이버 금융 경유, 거래대금은 volume*close 근사)

동작:
  1. DB의 daily_picks에서 고유 종목코드 추출
  2. 각 종목 240거래일치 OHLCV 조회
  3. daily_picks 기존 레코드 업데이트 (OHLC + trade_amount가 비어있는 경우)
  4. daily_picks에 없는 날짜도 INSERT (과거 데이터 축적)

사용법:
  # 로컬 (pykrx fallback)
  python backfill_ohlc.py [--dry-run] [--limit N] [--stock CODE] [--days 240]

  # GitHub Actions (키움 API)
  KIWOOM_APPKEY=... KIWOOM_SECRETKEY=... python backfill_ohlc.py
"""

import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parents[1]
DB_PATH = REPO_ROOT / "data" / "stocks.db"
KST = timezone(timedelta(hours=9))


def log(msg: str) -> None:
    ts = datetime.now(KST).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ─── 데이터 소스: 키움 API ────────────────────────────────


def fetch_ohlcv_kiwoom(client, code: str, days: int = 240) -> list:
    """키움 ka10081 일봉 → [{date, open, high, low, close, volume, trade_amount}, ...]"""
    chart = client.get_daily_chart(code, count=days)
    if not chart:
        return []

    results = []
    for row in chart:
        # 가능한 키 이름들 시도
        def get(keys):
            for k in keys:
                v = row.get(k)
                if v is not None and str(v).strip():
                    return v
            return None

        date_raw = get(["date", "stck_bsop_date", "0"])
        if not date_raw:
            continue

        d = str(date_raw).strip().replace("-", "")
        if len(d) != 8:
            continue
        date_str = f"{d[:4]}-{d[4:6]}-{d[6:8]}"

        def pint(keys):
            v = get(keys)
            if v is None:
                return 0
            s = str(v).replace("+", "").replace(",", "").strip()
            neg = s.startswith("-")
            s = s.lstrip("-").lstrip("0") or "0"
            try:
                return -int(s) if neg else int(s)
            except ValueError:
                return 0

        open_v = pint(["open", "stck_oprc", "1", "16"])
        high_v = pint(["high", "stck_hgpr", "2", "17"])
        low_v = pint(["low", "stck_lwpr", "3", "18"])
        close_v = pint(["close", "stck_clpr", "4", "10"])
        volume = pint(["volume", "acml_vol", "5", "13"])
        trade_amount = pint(["trade_amount", "acml_tr_pbmn", "6", "14"])

        if close_v <= 0:
            continue

        # 거래대금 없으면 근사
        if trade_amount == 0 and volume > 0:
            trade_amount = volume * close_v

        results.append(
            {
                "date": date_str,
                "open": open_v,
                "high": high_v,
                "low": low_v,
                "close": close_v,
                "volume": volume,
                "trade_amount": trade_amount,
            }
        )

    return results


# ─── 데이터 소스: pykrx ───────────────────────────────────


def fetch_ohlcv_pykrx(code: str, days: int = 240) -> list:
    """pykrx(네이버 금융 경유) → [{date, open, high, low, close, volume, trade_amount}, ...]

    거래대금 컬럼이 없으므로 volume * close로 근사.
    """
    from pykrx import stock

    end_dt = datetime.now(KST)
    # 영업일 기준 240일 ≈ 달력 350일
    start_dt = end_dt - timedelta(days=int(days * 1.5))

    start_s = start_dt.strftime("%Y%m%d")
    end_s = end_dt.strftime("%Y%m%d")

    try:
        df = stock.get_market_ohlcv_by_date(start_s, end_s, code)
    except Exception as e:
        log(f"  pykrx 오류: {e}")
        return []

    if df is None or df.empty:
        return []

    results = []
    for idx, row in df.iterrows():
        date_str = idx.strftime("%Y-%m-%d")
        open_v = int(row.get("시가", 0))
        high_v = int(row.get("고가", 0))
        low_v = int(row.get("저가", 0))
        close_v = int(row.get("종가", 0))
        volume = int(row.get("거래량", 0))

        if close_v <= 0:
            continue

        trade_amount = volume * close_v  # 근사값

        results.append(
            {
                "date": date_str,
                "open": open_v,
                "high": high_v,
                "low": low_v,
                "close": close_v,
                "volume": volume,
                "trade_amount": trade_amount,
            }
        )

    return results


# ─── DB 업데이트 ──────────────────────────────────────────


def upsert_daily_picks(conn: sqlite3.Connection, code: str, rows: list) -> dict:
    """daily_picks에 UPSERT. 기존 레코드는 빈 필드만 업데이트, 새 레코드는 INSERT.

    Returns: {"updated": N, "inserted": N}
    """
    updated = 0
    inserted = 0

    for r in rows:
        # 기존 레코드 확인
        existing = conn.execute(
            "SELECT id, open_price, high_price, low_price, price, trade_amount "
            "FROM daily_picks WHERE stock_code = ? AND date = ?",
            (code, r["date"]),
        ).fetchone()

        if existing:
            # 빈 필드만 업데이트
            needs_update = (
                existing[1] is None  # open_price
                or existing[2] is None  # high_price
                or existing[3] is None  # low_price
                or existing[4] is None  # price
                or existing[5] is None
                or existing[5] == 0  # trade_amount
            )
            if needs_update:
                conn.execute(
                    """UPDATE daily_picks
                       SET open_price = COALESCE(open_price, ?),
                           high_price = COALESCE(high_price, ?),
                           low_price  = COALESCE(low_price, ?),
                           price      = COALESCE(price, ?),
                           trade_amount = CASE
                             WHEN trade_amount IS NULL OR trade_amount = 0
                             THEN ? ELSE trade_amount END
                     WHERE stock_code = ? AND date = ?""",
                    (
                        r["open"] if r["open"] > 0 else None,
                        r["high"] if r["high"] > 0 else None,
                        r["low"] if r["low"] > 0 else None,
                        r["close"] if r["close"] > 0 else None,
                        r["trade_amount"] if r["trade_amount"] > 0 else None,
                        code,
                        r["date"],
                    ),
                )
                updated += 1
        else:
            # 새 레코드 INSERT
            now_iso = datetime.now(KST).isoformat(timespec="seconds")
            conn.execute(
                """INSERT INTO daily_picks
                   (date, stock_code, price, open_price, high_price, low_price,
                    trade_amount, source, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'backfill', ?)""",
                (
                    r["date"],
                    code,
                    r["close"],
                    r["open"] if r["open"] > 0 else None,
                    r["high"] if r["high"] > 0 else None,
                    r["low"] if r["low"] > 0 else None,
                    r["trade_amount"] if r["trade_amount"] > 0 else None,
                    now_iso,
                ),
            )
            inserted += 1

    return {"updated": updated, "inserted": inserted}


# ─── 메인 ────────────────────────────────────────────────


def main():
    import argparse

    parser = argparse.ArgumentParser(description="daily_picks OHLC backfill")
    parser.add_argument("--dry-run", action="store_true", help="조회만, DB 쓰기 없음")
    parser.add_argument("--limit", type=int, default=0, help="처리할 종목 수 제한")
    parser.add_argument("--stock", type=str, default="", help="특정 종목코드 1개만")
    parser.add_argument("--days", type=int, default=240, help="조회할 거래일 수")
    parser.add_argument(
        "--source",
        choices=["auto", "kiwoom", "pykrx"],
        default="auto",
        help="데이터 소스 (auto: 키움 키 있으면 키움, 없으면 pykrx)",
    )
    args = parser.parse_args()

    # --- 데이터 소스 결정 ---
    use_kiwoom = False
    kiwoom_client = None

    if args.source in ("auto", "kiwoom"):
        appkey = os.environ.get("KIWOOM_APPKEY")
        secret = os.environ.get("KIWOOM_SECRETKEY")
        if appkey and secret:
            sys.path.insert(0, str(SCRIPT_DIR))
            from kiwoom_client import KiwoomClient

            kiwoom_client = KiwoomClient()
            log("키움 토큰 발급...")
            kiwoom_client.get_token()
            if kiwoom_client.token:
                use_kiwoom = True
                log(f"키움 API 사용 ({kiwoom_client.base_url})")
            else:
                log("키움 토큰 발급 실패, pykrx fallback")
        elif args.source == "kiwoom":
            log("KIWOOM_APPKEY/KIWOOM_SECRETKEY 환경변수 없음")
            sys.exit(1)

    if not use_kiwoom:
        try:
            from pykrx import stock as _  # noqa: F401

            log("pykrx 사용 (네이버 금융 경유, 거래대금은 근사값)")
        except ImportError:
            log("pykrx 미설치. pip install pykrx")
            sys.exit(1)

    # --- DB 연결 + 종목코드 조회 ---
    log(f"DB: {DB_PATH}")
    conn = sqlite3.connect(str(DB_PATH))

    if args.stock:
        codes = [args.stock]
    else:
        rows = conn.execute(
            "SELECT DISTINCT stock_code FROM daily_picks ORDER BY stock_code"
        ).fetchall()
        codes = [r[0] for r in rows]

    if args.limit > 0:
        codes = codes[: args.limit]

    log(f"대상 종목: {len(codes)}개, 조회일수: {args.days}")

    if not codes:
        log("업데이트할 종목 없음")
        conn.close()
        return

    # --- 종목별 처리 ---
    total_updated = 0
    total_inserted = 0
    total_api = 0
    first_logged = False

    try:
        for i, code in enumerate(codes):
            log(f"[{i + 1}/{len(codes)}] {code}")

            try:
                if use_kiwoom:
                    ohlcv = fetch_ohlcv_kiwoom(kiwoom_client, code, args.days)
                else:
                    ohlcv = fetch_ohlcv_pykrx(code, args.days)
                total_api += 1
            except Exception as e:
                log(f"  조회 실패: {e}")
                # 키움 토큰 만료 시 재발급
                if use_kiwoom and ("401" in str(e) or "token" in str(e).lower()):
                    log("  토큰 재발급 시도...")
                    kiwoom_client.get_token()
                    if not kiwoom_client.token:
                        log("  토큰 재발급 실패, 중단")
                        break
                    try:
                        ohlcv = fetch_ohlcv_kiwoom(kiwoom_client, code, args.days)
                        total_api += 1
                    except Exception as e2:
                        log(f"  재시도 실패: {e2}")
                        continue
                else:
                    continue

            if not ohlcv:
                log("  데이터 없음")
                continue

            # 첫 종목 응답 로깅
            if not first_logged:
                s = ohlcv[0]
                log(
                    f"  샘플: {s['date']} O={s['open']} H={s['high']} "
                    f"L={s['low']} C={s['close']} V={s['volume']} TA={s['trade_amount']}"
                )
                first_logged = True

            log(f"  {len(ohlcv)}일치 ({ohlcv[-1]['date']} ~ {ohlcv[0]['date']})")

            if args.dry_run:
                for p in ohlcv[:3]:
                    log(
                        f"    {p['date']} O={p['open']} H={p['high']} "
                        f"L={p['low']} C={p['close']} TA={p['trade_amount']}"
                    )
                continue

            result = upsert_daily_picks(conn, code, ohlcv)
            conn.commit()

            if result["updated"] > 0 or result["inserted"] > 0:
                log(f"  updated={result['updated']}, inserted={result['inserted']}")
            total_updated += result["updated"]
            total_inserted += result["inserted"]

            # rate limit: pykrx는 1초, 키움은 0.5초
            if i < len(codes) - 1:
                time.sleep(1.0 if not use_kiwoom else 0.5)

    finally:
        if use_kiwoom and kiwoom_client:
            try:
                kiwoom_client.revoke_token()
                log("키움 토큰 폐기")
            except Exception:
                pass
        conn.close()

    log(
        f"완료: {total_api} API 호출, {total_updated} updated, {total_inserted} inserted"
    )


if __name__ == "__main__":
    main()
