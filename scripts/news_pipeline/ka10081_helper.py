"""ka10081 응답 파싱 공통 helper — 분산 path verbatim 복제 봉쇄.

Q-20260519-CYCLE19-001 (2026-05-19 후행) — 사이클12 commit 83e967d로 collect_dailybars의
`_normalize_row` fix(trde_qty/trde_prica 7-tuple)가 backfill_dailybars_bulk와 verbatim 복제로
동기화되었으나, 두 path가 별도 함수로 분산 → FLR-20260406-TEC-001 동형 재발 risk (한쪽 fix
다른 쪽 누락). 본 module은 ka10081 응답 파싱 단일 출처(SSOT).

사용처:
  - `collect_dailybars._normalize_row` → `parse_ka10081_row()`
  - `backfill_dailybars_bulk._normalize_row` → `parse_ka10081_row()`
  - `collect_kiwoom_limit_up.fetch_dailybars_trade_amount` → `parse_ka10081_rows` + today row 추출

ka10081 응답 schema (KRX 일봉차트):
  - body.stk_dt_pole_chart_qry: List[{dt, open_pric, high_pric, low_pric, cur_prc, trde_qty, trde_prica, ...}]
  - dt: YYYYMMDD (8자리 문자열)
  - 가격 4종: 부호·콤마 가능 (음수=하락 시그널이나 abs() 통일)
  - trde_qty: 거래량 (주 단위)
  - trde_prica: 거래대금 **백만원 단위** → DB는 원 단위 (× 1_000_000 의무)

근거 evidence (verbatim grep):
  - collect_dailybars.py:165-190 (cycle12 Fix-3 commit 83e967d)
  - backfill_dailybars_bulk.py:122-140 (5/7 b3e5c96)
  - collect_kiwoom_limit_up.py:225-228 (cycle11 Q-001 LU helper)
  - DOC-20260507-DSN-001-arch-pipeline.md L109/123/166
"""

from __future__ import annotations

from typing import NamedTuple


class DailyBar(NamedTuple):
    """ka10081 응답 1행의 정규화 결과.

    필드:
      date: 'YYYY-MM-DD' (DB schema 정합, ISO 8601 date)
      open / high / low / close: 원 단위 정수 (abs 통일)
      volume: 주 단위 정수 (음수 없음)
      trade_amount_won: 원 단위 정수 (백만원 × 1,000,000, DB schema 정합)
    """

    date: str
    open: int
    high: int
    low: int
    close: int
    volume: int
    trade_amount_won: int


def parse_int_field(val) -> int:
    """키움 응답 숫자 파싱 (부호·콤마·패딩 제거, 음수 보존).

    verbatim:
      - collect_dailybars._pint (L69-81)
      - backfill_dailybars_bulk._pint (L66-77)
      - collect_kiwoom_limit_up._parse_int (동형)

    근거: 키움 응답이 "+1234,567" / "-0098" / "" 등 형식 다양 → 안전 파싱.
    """
    if val is None:
        return 0
    s = str(val).replace(",", "").replace("+", "").strip()
    if not s:
        return 0
    neg = s.startswith("-")
    s = s.lstrip("-").lstrip("0") or "0"
    try:
        return -int(s) if neg else int(s)
    except ValueError:
        return 0


def parse_ka10081_row(row: dict) -> DailyBar | None:
    """ka10081 1행 → DailyBar (정규화). 결측·유효성 위반 시 None.

    Args:
      row: ka10081 응답 stk_dt_pole_chart_qry 1개 원소.

    Returns:
      DailyBar(date, open, high, low, close, volume, trade_amount_won) 또는 None.

    유효성 규칙:
      - dt 필드 부재 → None
      - dt 길이 ≠ 8 → None (YYYYMMDD 위반)
      - close (cur_prc) ≤ 0 → None (거래 정지·신규상장 첫날 등)
      - 그 외 가격/거래량/거래대금 결측은 0으로 통일 (DB NULL 회피)

    대체 필드명 지원 (collect_dailybars 정합):
      - dt | date | stck_bsop_date
      - open_pric | open | stck_oprc
      - high_pric | high | stck_hgpr
      - low_pric | low | stck_lwpr
      - cur_prc | close | stck_clpr
    """
    date_raw = row.get("dt") or row.get("date") or row.get("stck_bsop_date")
    if not date_raw:
        return None
    d = str(date_raw).strip().replace("-", "")
    if len(d) != 8:
        return None
    date_str = f"{d[:4]}-{d[4:6]}-{d[6:8]}"

    open_v = abs(
        parse_int_field(row.get("open_pric") or row.get("open") or row.get("stck_oprc"))
    )
    high_v = abs(
        parse_int_field(row.get("high_pric") or row.get("high") or row.get("stck_hgpr"))
    )
    low_v = abs(
        parse_int_field(row.get("low_pric") or row.get("low") or row.get("stck_lwpr"))
    )
    close_v = abs(
        parse_int_field(row.get("cur_prc") or row.get("close") or row.get("stck_clpr"))
    )
    if close_v <= 0:
        return None

    volume = abs(parse_int_field(row.get("trde_qty")))
    trde_prica_m = abs(parse_int_field(row.get("trde_prica")))  # 백만원 단위
    trade_amount_won = trde_prica_m * 1_000_000  # 원 단위 변환

    return DailyBar(
        date=date_str,
        open=open_v,
        high=high_v,
        low=low_v,
        close=close_v,
        volume=volume,
        trade_amount_won=trade_amount_won,
    )


def parse_ka10081_response(rows: list[dict] | None) -> list[DailyBar]:
    """ka10081 응답 stk_dt_pole_chart_qry 전체 → List[DailyBar] (None 행 자동 제거).

    Args:
      rows: ka10081 응답 stk_dt_pole_chart_qry (list). None/빈 list 허용.

    Returns:
      유효 DailyBar list (정렬 보장 없음 — caller가 date 기준 정렬 의무).
    """
    if not rows:
        return []
    out: list[DailyBar] = []
    for r in rows:
        bar = parse_ka10081_row(r)
        if bar is not None:
            out.append(bar)
    return out


def find_today_trade_amount(rows: list[dict] | None, today_yyyymmdd: str) -> int | None:
    """ka10081 응답에서 오늘 row의 trade_amount_won 추출.

    collect_kiwoom_limit_up.fetch_dailybars_trade_amount용 — LU catch 후 종목별 ka10081
    추가 호출 시 오늘 거래대금만 필요한 path.

    Args:
      rows: ka10081 응답 stk_dt_pole_chart_qry.
      today_yyyymmdd: 'YYYYMMDD' 형식 오늘 날짜 (caller가 datetime.now().strftime("%Y%m%d") 전달).

    Returns:
      오늘 row의 trade_amount_won (int) 또는 None (오늘 row 부재 / 결측).

    근거: collect_kiwoom_limit_up.py:222-230 verbatim 정합 — 오늘 row 매치는 dt 문자열
    엄격 비교 (정렬 보장 없음). caller fallback (trde_qty × cur_prc 단순곱)으로 회귀.
    """
    if not rows:
        return None
    today_clean = today_yyyymmdd.replace("-", "").strip()
    for row in rows:
        dt_raw = str(row.get("dt") or "").strip()
        if dt_raw == today_clean:
            trde_prica_m = parse_int_field(row.get("trde_prica"))
            if trde_prica_m == 0:
                # 0 또는 결측 (parse_int_field는 None→0이므로 구분 불가하지만 LU 거래대금
                # 0원은 비현실적 — 호출자 fallback 회귀 유도)
                return None
            return abs(trde_prica_m) * 1_000_000
    return None
