"""per-stock dailybars 240영업일 JSON emit — cycle22 Phase 3 (Q-CYCLE22-001).

본 모듈 본질:
  - 카드 일봉캔들 클릭 확대 차트 (REQ-001/SPEC-001) 데이터 공급.
  - 모바일 페이로드 폭증 회피 (일자 stock-{date}.json inline 추가 시 +180~300 KB/일자).
  - per-stock lazy fetch 구조: `data/dailybars/{code}.json` → 카드 expand 시점 fetch.

POC P0-2 결정 (REQ-001 §7 P0-2): 옵션 C (혼합) 채택.
  - 본 모듈 = raw OHLCV 240행 expose (옵션 C 서버 측 raw 공급)
  - 서버 5종 사전 계산 (매물대 10등분 / Volume Profile / 일목균형표 / 분홍 강세 / 배당락)
    = 별건 후행 sub-agent 5건 분할 (본 단발 X)
  - 클라이언트 8종 (MA / MACD / RSI / Stochastic / OBV / 거래량 / 거래대금 / 피보나치)
    = SPEC-001 §2.2 indicator 모듈에서 raw rows 기반 계산

데이터 source:
  - dailybars 운영 SoT: ~/company/100m1s-homepage/data/stocks.db
    (DSN-arch-pipeline §1 + config.py:11)
  - schema: 8 columns (code/date/open/high/low/close/volume/trade_amount)
    (DOC-20260514-REQ-002 §2.2 verbatim grep PASS)
  - WINDOW = 240영업일 (collect_dailybars.py:167 WINDOW_DAYS=240 정합)

권리락 보정 (DSN §9.1-quater 옵션 C 정합):
  - dailybars_adjustments table JOIN (manual seed + 후행 ETL)
  - row.date 이후 발생한 ratio 누적곱 cumulative_ratio 적용
  - _calc_daily_20과 동형 패턴 (build_daily.py L499~510 verbatim 참조)

IPO edge case (DSN §7.5.1 정합):
  - dailybars 0행 시 emit skip (JSON 미생성). frontend는 fetch 404 graceful.
  - 신규 상장 일자만 있는 케이스도 emit (1행 JSON, frontend가 single-point 렌더).

cron 자동 add 화이트리스트:
  - pipeline.sh + kiwoom_cron.sh 양쪽 `data/dailybars/*.json` 화이트리스트 신설
    (SSOT 비대칭 회피 의무, FLR-20260406-TEC-001 recurring 95+회차 동형)

FLR cross-link:
  - FLR-20260428-TEC-001 (한쪽 코드·양 끝 누락) — 본 모듈 신설 시 cron 양쪽 동시 패치
  - FLR-AGT-002 (mock production 노출 금지) — raw dailybars만 expose, mock 0건
  - FLR-20260406-TEC-001 (SSOT 비대칭) — collect_dailybars/backfill_dailybars_bulk 동형
"""

from __future__ import annotations

import json
import sqlite3

from .config import HOMEPAGE
from .db import connect

# 출력 디렉토리 (homepage 별도 레포 — cron 자동 add 화이트리스트 정합)
DAILYBARS_DIR = HOMEPAGE / "data" / "dailybars"

# 240영업일 윈도우 (collect_dailybars.py:167 WINDOW_DAYS=240 정합).
# 본 윈도우는 REQ-001 §3 Phase 3 + AC-6 (240영업일 fetch) 정합.
WINDOW_DAYS = 240


def _fetch_dailybars_window(
    conn: sqlite3.Connection, code: str, today: str
) -> list[dict]:
    """code의 today 이전 최대 240영업일 일봉 OHLCV 시계열을 ASC 정렬로 반환.

    OHLC 일부 NULL 행은 close fallback 적용 (DSN §7.4 build_daily OHLC NULL fallback
    rule 정합, rules/data-continuity.md § OHLC NULL fallback). volume/trade_amount는
    적재 시점에 NULL 가능 — 본 emit 단계는 그대로 통과 (frontend가 null safe).

    수정주가 보정 (dailybars_adjustments 누적곱) — _calc_daily_20과 동형 (build_daily.py
    L499~530 verbatim). row.date 이후 발생한 모든 ratio 누적곱을 OHLC 4값에 적용.
    volume/trade_amount는 보정 적용 X (절대값 의미 유지 — 거래대금 추이 시계열 보존).
    """
    rows = conn.execute(
        """SELECT date, open, high, low, close, volume, trade_amount
           FROM dailybars
           WHERE code=? AND date<=?
           ORDER BY date DESC LIMIT ?""",
        (code, today, WINDOW_DAYS),
    ).fetchall()
    if not rows:
        return []

    # ratio 보정 (build_daily.py L500~510 동형 cumulative product)
    adj_rows = conn.execute(
        """SELECT date, ratio FROM dailybars_adjustments
           WHERE code=? AND date <= ? AND ratio > 0
           ORDER BY date ASC""",
        (code, today),
    ).fetchall()
    adj_pairs = [(a["date"], float(a["ratio"])) for a in adj_rows if a["ratio"]]

    out: list[dict] = []
    # ASC 정렬로 reverse (DESC SELECT → reversed iter)
    for r in reversed(rows):
        o = r["open"] or 0
        h = r["high"] or 0
        lo = r["low"] or 0
        c = r["close"] or 0
        v = r["volume"] or 0
        ta = r["trade_amount"] or 0

        # OHLC NULL fallback (DSN §7.4 정합, 전 종목 룰 — 종목 hardcode 금지).
        # close 우선 채움 → 그래도 NULL인 행은 skip (data layer 본질 결손).
        if c <= 0:
            continue
        if o <= 0:
            o = c
        if h <= 0:
            h = c
        if lo <= 0:
            lo = c

        # row.date 이후 ratio 누적곱 (수정주가 보정)
        cum = 1.0
        for adj_date, ratio in adj_pairs:
            if adj_date > r["date"]:
                cum *= ratio
        if cum != 1.0:
            o = int(round(o * cum))
            h = int(round(h * cum))
            lo = int(round(lo * cum))
            c = int(round(c * cum))
        # volume/trade_amount는 보정 적용 X — 거래대금 추이 시계열 보존 의도

        out.append(
            {
                "d": r["date"],
                "o": o,
                "h": h,
                "l": lo,
                "c": c,
                "v": v,
                "ta": ta,
            }
        )

    return out


def emit_for_stock(conn: sqlite3.Connection, code: str, name: str, today: str) -> bool:
    """단일 종목 dailybars 240행 JSON emit.

    Returns:
        True: 정상 emit (1행 이상 적재 + JSON write)
        False: skip (dailybars 0행 — IPO 첫날 미적재 등)
    """
    rows = _fetch_dailybars_window(conn, code, today)
    if not rows:
        return False

    payload = {
        "code": code,
        "name": name,
        "window_days": WINDOW_DAYS,
        "actual_days": len(rows),
        "generated_at_date": today,
        "rows": rows,
    }
    DAILYBARS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DAILYBARS_DIR / f"{code}.json"
    # idempotent write — INSERT OR REPLACE 패턴 동형. ratio 보정·신규 적재 시 자동 정합.
    out_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return True


def emit_for_stocks(codes_with_names: list[tuple[str, str]], today: str) -> dict:
    """N개 종목 일괄 emit.

    Args:
        codes_with_names: [(code, name)] 리스트. build_daily entry 작성 후 호출.
        today: 기준 일자 YYYY-MM-DD.

    Returns:
        {"emitted": int, "skipped": int, "total": int}
    """
    emitted = 0
    skipped = 0
    with connect() as conn:
        for code, name in codes_with_names:
            try:
                ok = emit_for_stock(conn, code, name or "", today)
                if ok:
                    emitted += 1
                else:
                    skipped += 1
            except Exception as e:
                # 한 종목 실패가 전체 build를 깨지 않도록 graceful — DSN §7 데이터 정합
                # 자동 검증 7.1 정합 (결함 발견 시 cron 후속 stage 정정 또는 SKIP).
                print(f"[emit_dailybars_per_stock] {code} skip — {e}")
                skipped += 1
    return {"emitted": emitted, "skipped": skipped, "total": len(codes_with_names)}


if __name__ == "__main__":
    # 단독 실행 — 오늘 일자 카드 종목 전수 emit (수동 backfill 용도).
    from .config import pipeline_date

    today = pipeline_date()
    with connect() as conn:
        # stock_master 활성 전 종목 — REQ-001 §5.2 AC-7 정합 (전 종목 일반화 룰).
        rows = conn.execute(
            "SELECT DISTINCT code, name FROM stocks "
            "WHERE code IS NOT NULL AND length(code)=6"
        ).fetchall()
        codes = [
            (r["code"], r["name"]) for r in rows if r["code"] and r["code"].isdigit()
        ]

    print(f"[emit_dailybars_per_stock] target={len(codes)} stocks, today={today}")
    result = emit_for_stocks(codes, today)
    print(
        f"[emit_dailybars_per_stock] emitted={result['emitted']} "
        f"skipped={result['skipped']} total={result['total']}"
    )
