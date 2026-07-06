"""키움 ka10027 전일대비등락률상위 → 당일 상한가(>=29.79%) 종목코드 집합.

ROOT (2026-06-18, 상한가 과소수집 근본책 — 대표 직접 지시):
    cron 서빙 DB(M1S_HOMEPAGE/data/stocks.db)의 dailybars 는 당일 union universe
    (latest_stocks ∪ carded, ~40종)만 적재한다. 그런데 상한가 종목은 v1 조건검색
    (거래대금 500억 ∪ 상한가)이 거래대금/조건식 미달로 누락하는 경우가 있어
    (예: 6/11 실제 상한가 8종 중 서빙 dailybars 1종, 297090 씨에스베어링 +29.79%)
    그 종목의 dailybars 가 처음부터 cron DB 에 적재되지 않는다 → limit-up-trend.json
    chain 이 종목 자체를 못 잡아 과소수집.

    종전 즉효책(build_theme_stats 백필 DB read-only ATTACH, commit 2a7f8a01)은 빌더
    측 사후 보강이라 백필 DB 가 healthy 해야만 동작한다. 본 모듈은 **수집 단계**에서
    당일 상한가 종목을 universe 에 직접 편입 → cron 서빙 DB dailybars 에 매 cycle
    상한가 종목이 적재되어 chain 이 자체적으로 잡는다(백필 의존 제거).

설계 (대표 결정 — 키움 API 호출 비용 고민 불요, 데이터 완전성 최우선):
    "전종목 종가 스캔" = ka10027 전일대비등락률상위(/api/dostk/rkinfo)를 등락률
    desc 정렬로 호출. 상한가(flu_rt >= 29.79%)는 항상 결과 최상위에 위치하므로,
    상위에서 flu_rt < 29.79% 가 나오면 즉시 중단(나머지는 더 낮음 — 페이지 낭비 0).
    전종목 일봉(ka10081 ~4288콜 ≈ 21분)을 도는 over-collection 이 아니라, 등락률
    상위만 보면 상한가가 빠짐없이 잡히는 효율 구조 (FLR-20260527-TEC-001 전종목
    over-collection cold-start 1h stale 재현 회피).

키움 API spec (4종 cross-check, lead-meta §11.15 — WebSearch+공식+repo+라이브실측):
    (a) WebSearch 2회 corroborating: ka10027 = "전일대비등락률상위요청",
        경로 POST /api/dostk/rkinfo, body params trde_qty_cnd/stk_cnd/sort_tp/
        updown_incls/mrkt_tp/crd_cnd/pric_cnd/trde_prica_cnd/stex_tp.
    (b) 키움 공식 가이드(openapi.kiwoom.com) + scribd 공식 문서 + 오픈소스 래퍼
        (younghwan91/kiwoom-rest-api, jackerlab OPT10027) 명칭·경로 일치.
    (c) repo 기존 키움 호출 verbatim: ka10081/ka10099/ka10172 동일 호출 패턴
        (api-id 헤더 + return_code==0 + 429/rc=5 backoff). flu_rt 필드는
        collect_ranking.py(ka10032)·collect_kiwoom_limit_up.py(ka10017)에서 사용 중.
    (d) 🔴 라이브 실측(2026-06-18 10:35 KST, 개발팀 직접 curl — 거짓 충실성 회피):
        - 필수 파라미터 2종(trde_prica_cnd, stex_tp) 누락 시 return_code=2
          입력값오류[1511] → 호출 자체 실패. (초기 버전 누락분 실측으로 보강)
        - 정상 호출 시 응답 list key = "pred_pre_flu_rt_upper" (len=200,
          등락률 desc — 삼화전기 009470 +30.00 최상위, [8]행부터 컷 미달).
        - row 필드: stk_cls/stk_cd/stk_nm/cur_prc/pred_pre_sig/pred_pre/flu_rt/
          sel_req/buy_req/now_trde_qty/cntr_str/cnt.

ka10027 요청 body (전 종목 커버 — mrkt_tp '000' 전체 시장):
    sort_tp '1' = 급등(상승률 desc). trde_qty_cnd '0000' = 전체(거래량 조건 없음
    — 거래량 적은 상한가 포함). trde_prica_cnd '0' = 거래대금 조건 없음(전체 —
    거래대금 미달 상한가 포함, 본 모듈 핵심 목적). stk_cnd '0' = 전체.
    crd_cnd '0' = 전체. updown_incls '1' = 상하한 포함. pric_cnd '0' = 전체.
    stex_tp '1' = KRX (필수). 모두 라이브 실측 통과.

안전 (FLR-AGT-002 거짓 충실성 회피):
    토큰 발급 실패 / HTTP 비정상 / 응답 schema 이탈 → 빈 set 반환 + print
    (silent 흡수 안 함). 빈 set 이어도 collect_dailybars 는 carded ∪ search_pass
    union 으로 graceful degrade (본 모듈은 누락 보강용 union 한 축).
"""

from __future__ import annotations

import os
import time
from datetime import datetime

import requests

from .kiwoom_client import get_token

# collect_dailybars 와 동일 env 우선순위 (실전 우선, 모의 fallback).
KIWOOM_BASE = os.environ.get("KIWOOM_LIVE_BASE_URL") or os.environ.get(
    "KIWOOM_BASE_URL", "https://api.kiwoom.com"
)
KIWOOM_APPKEY = os.environ.get("KIWOOM_LIVE_APPKEY") or os.environ.get("KIWOOM_APPKEY")
KIWOOM_SECRETKEY = os.environ.get("KIWOOM_LIVE_SECRETKEY") or os.environ.get(
    "KIWOOM_SECRETKEY"
)

# 상한가 임계 = 등락률 >= 29.79% (D축, 대표 3회 확정 2026-06-16).
# build_daily.LIMIT_UP_THRESHOLD / build_theme_stats.LIMIT_UP_THRESHOLD 와 동일 값.
LIMIT_UP_THRESHOLD = 29.79

# ka10027 페이징 안전 상한. 등락률 desc 이므로 1페이지(~100건)에서 상한가는 모두
# 나오는 게 정상. 폭등장 대비 여유 cap (cont-yn 이 Y 여도 컷 미달 시 break).
_MAX_PAGES = 5

# (mrkt_tp, 라벨) — ka10027 전체 시장. '000' = 전체(코스피+코스닥+ETF 등).
# build_daily/추이는 6자리 종목코드만 채택하므로 ETF/ELW 등은 후단에서 자연 제외.
_MARKET = "000"


def _parse_flu_rt(raw: object) -> float | None:
    """ka10027 flu_rt 문자열 → float 등락률(%).

    키움 등락률 문자열은 부호 포함('+29.85', '-3.2') 또는 빈 문자열.
    collect_ranking.parse_float 동형 (콤마/부호/빈값 방어). 파싱 불가 → None.
    """
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "").replace("+", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _normalize_code(raw: object) -> str | None:
    """ka10027 stk_cd → 6자리 종목코드. 'A' 접두/공백 제거. 비정상 → None."""
    if raw is None:
        return None
    code = str(raw).strip().lstrip("A")
    if code.isdigit():
        code = code.zfill(6)
    if len(code) == 6 and code.isdigit():
        return code
    return None


def _fetch_page(
    token: str, mrkt_tp: str, cont_yn: str, next_key: str
) -> tuple[list[dict], bool, str]:
    """ka10027 1페이지 호출 → (rows, has_next, next_key).

    repo 기존 키움 호출 패턴(collect_dailybars.fetch_daily_chart) verbatim 정합:
      api-id 헤더 / return_code==0 성공 / 429·rc=5 backoff retry / 비정상 시 중단.
    실패 시 ([], False, "") 반환 (호출측 graceful — 부분 적재 방지).
    """
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
        "api-id": "ka10027",
    }
    if cont_yn == "Y":
        headers["cont-yn"] = "Y"
        headers["next-key"] = next_key
    body = {
        "mrkt_tp": mrkt_tp,
        "sort_tp": "1",  # 상승률(등락률 desc) — 상한가가 최상위
        "trde_qty_cnd": "0000",  # 전체 (거래량 조건 없음 — 거래량 적은 상한가 포함)
        "stk_cnd": "0",  # 전체 종목
        "crd_cnd": "0",  # 신용 전체
        "updown_incls": "1",  # 상하한 포함
        "pric_cnd": "0",  # 가격 전체
        # 라이브 실측(2026-06-18 10:35 KST, 개발팀 직접 curl)로 확정한 ka10027
        # 필수 파라미터 2종. 누락 시 return_code=2 입력값오류[1511] 로 호출 자체 실패.
        "trde_prica_cnd": "0",  # 거래대금 조건 없음(전체) — 거래대금 미달 상한가 포함
        "stex_tp": "1",  # 거래소 KRX (collect_kiwoom_limit_up.fetch_limit_up 동형)
    }
    for attempt in range(4):
        try:
            r = requests.post(
                f"{KIWOOM_BASE}/api/dostk/rkinfo",
                json=body,
                headers=headers,
                timeout=20,
            )
        except Exception as e:  # noqa: BLE001 - 네트워크 일시 오류
            print(f"[collect_limit_up_universe] {mrkt_tp} exception: {e}")
            return [], False, ""
        if r.status_code == 429:
            back = 2 ** (attempt + 1)
            print(f"[collect_limit_up_universe] {mrkt_tp} 429, backoff {back}s")
            time.sleep(back)
            continue
        if r.status_code != 200:
            print(
                f"[collect_limit_up_universe] {mrkt_tp} http {r.status_code}: "
                f"{r.text[:200]}"
            )
            return [], False, ""
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            return [], False, ""
        if data.get("return_code") == 5:
            back = 2 ** (attempt + 1)
            print(f"[collect_limit_up_universe] {mrkt_tp} rc=5, backoff {back}s")
            time.sleep(back)
            continue
        if data.get("return_code") != 0:
            print(
                f"[collect_limit_up_universe] {mrkt_tp} rc="
                f"{data.get('return_code')}: {data.get('return_msg')}"
            )
            return [], False, ""
        # 응답 list key = "pred_pre_flu_rt_upper" (라이브 실측 확정, 2026-06-18
        # 10:35 KST 개발팀 직접 curl: return_code=0, len=200, 등락률 desc 정렬
        # 확인 — 삼화전기 009470 +30.00 최상위). 버전 변동 대비 후보 fallback 유지.
        rows = (
            data.get("pred_pre_flu_rt_upper")
            or data.get("pred_pre_flu_rt_upr")
            or data.get("output")
            or data.get("list")
            or data.get("data")
            or []
        )
        if not isinstance(rows, list):
            return [], False, ""
        has_next = (r.headers.get("cont-yn") or "").upper() == "Y" or (
            str(data.get("cont_yn") or "").upper() == "Y"
        )
        nk = r.headers.get("next-key") or data.get("next_key") or ""
        return rows, bool(has_next and nk), nk
    return [], False, ""


def fetch_limit_up_codes(date_str: str | None = None) -> set[str]:
    """당일 상한가(등락률 >= 29.79%) 종목코드 집합 — ka10027 등락률 desc 스캔.

    거래일이 아니거나(휴장) 토큰/응답 실패 시 빈 set (graceful). 등락률 desc 이므로
    컷(29.79%) 미달 행이 나오면 그 페이지에서 즉시 중단(나머지는 더 낮음).
    """
    from .config import is_market_holiday, pipeline_date

    today = date_str or pipeline_date()
    if is_market_holiday(today):
        print(f"[collect_limit_up_universe] {today} 휴장일 — 상한가 스캔 skip")
        return set()

    if not (KIWOOM_APPKEY and KIWOOM_SECRETKEY):
        print("[collect_limit_up_universe] 키움 키 부재 — 상한가 스캔 skip")
        return set()

    try:
        token = get_token(KIWOOM_BASE, KIWOOM_APPKEY, KIWOOM_SECRETKEY)
    except Exception as e:  # noqa: BLE001
        print(f"[collect_limit_up_universe] token FAIL: {e}")
        return set()

    out: set[str] = set()
    cont_yn = "N"
    next_key = ""
    scanned = 0
    for page in range(_MAX_PAGES):
        rows, has_next, nk = _fetch_page(token, _MARKET, cont_yn, next_key)
        if not rows:
            break
        page_hit = 0
        cut_reached = False
        for row in rows:
            scanned += 1
            flu = _parse_flu_rt(row.get("flu_rt"))
            if flu is None:
                continue
            # 등락률 desc 정렬 가정 — 컷 미달이면 이 페이지부터 더 낮음 → 중단.
            if flu < LIMIT_UP_THRESHOLD:
                cut_reached = True
                break
            code = _normalize_code(row.get("stk_cd"))
            if code:
                out.add(code)
                page_hit += 1
        print(
            f"[collect_limit_up_universe] page {page + 1}: "
            f"+{page_hit} 상한가 (scanned~{scanned}, cut={cut_reached})"
        )
        if cut_reached or not has_next:
            break
        cont_yn = "Y"
        next_key = nk
        time.sleep(0.3)

    print(
        f"[collect_limit_up_universe] {today} 상한가(>={LIMIT_UP_THRESHOLD}%) "
        f"{len(out)}종목"
    )
    return out


if __name__ == "__main__":
    codes = fetch_limit_up_codes()
    ts = datetime.now().isoformat(timespec="seconds")
    print(f"[{ts}] limit_up_codes={sorted(codes)}")
