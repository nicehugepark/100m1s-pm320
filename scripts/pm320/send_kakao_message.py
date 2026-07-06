#!/usr/bin/env python3
"""
PM320 카톡 푸시 — 본일 picks JSON read → 대표 카톡 send.

flow:
  1. .env (KAKAO_REST_API_KEY / KAKAO_CLIENT_SECRET / KAKAO_REFRESH_TOKEN) read
  2. refresh_token → access_token 갱신 (POST kauth.kakao.com/oauth/token)
  3. 본일 picks JSON read (projects/pm320/data/daily/picks/{YYYY-MM-DD}.json)
  4. 메시지 본문 build (lead 라이브 테스트 verbatim 정합)
  5. POST kapi.kakao.com/v2/api/talk/memo/default/send (template_object feed)
  6. result_code=0 PASS 또는 stderr 로그

rules:
- .env 키 본문 stdout/stderr/log 0건 (rules/security.md §2)
- picks 미존재 / API 에러 / token 갱신 실패 시 graceful exit
- refresh_token 갱신 발급 시 .env atomic rewrite (race 봉쇄)
- 캘린더 기반 picks 날짜 (KST 본일)

usage:
  python3 scripts/pm320/send_kakao_message.py [--date YYYY-MM-DD] [--dry-run]
  --dry-run: 카카오 API 호출 skip, 메시지 본문만 stdout

doc_id: feat(pm320,P0,kakao-push,DSN-pm320,FLR-AGT-002)
generated: 2026-06-02 (대표 trigger Phase 2 단발 카톡 푸시)
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# --- paths ---
REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = REPO_ROOT / ".env"
PICKS_DIR = REPO_ROOT / "projects" / "pm320" / "data" / "daily" / "picks"
# dailybars DB (cron worktree, P0 close source — sqlite3 SELECT)
STOCKS_DB = Path.home() / "company" / "100m1s-homepage-cron" / "data" / "stocks.db"
# 한국 거래소 휴장일 SoT (D+3 거래일 계산용)
HOLIDAYS_JSON = (
    Path.home() / "company" / "100m1s-homepage-cron" / "data" / "holidays.json"
)

# --- kakao endpoints (lead-meta §11.15 verbatim from kakao developers docs) ---
# token URL 은 kakao_token.py (공용 모듈) 로 이동 — 2026-07-06 공용화
KAKAO_MEMO_SEND_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"

# --- shareUrl domain (CNAME verbatim) ---
HOMEPAGE_ORIGIN = "https://100m1s.com"

KST = timezone(timedelta(hours=9))

# --- 계산식 상수 (전략 프로파일 SSOT) ---
# 비가변 공통: 익절 +3.2% / 물타기 -6.4%.
WATERING_RATIO = 0.936  # P0 × 0.936 = -6.4%
TAKE_PROFIT_RATIO = 1.032  # P0 × 1.032 (또는 평단 × 1.032)


def _load_active_profile() -> Any:
    """전략 프로파일 active 로드 (profiles.json SSOT).

    함수-로컬 import 로 strategy_profiles 를 가져온다 — autoflake 가 모듈-레벨 import 를
    "미사용"으로 오제거하는 문제 회피 (직전 하드코딩 "2배" half-applied 버그 통일 목적).
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from strategy_profiles import load_active_profile

    return load_active_profile()


# 축③ 물타기 비중 — 직전 하드코딩 "2배"(build_card 1배와 불일치 버그) 제거, 프로파일 참조.
_PROFILE = _load_active_profile()
WATERING_WEIGHT = float(_PROFILE["watering_weight"])
WATERING_WEIGHT_LABEL = _PROFILE.get("watering_weight_label") or (
    f"첫 매수의 {WATERING_WEIGHT:g}배"
)
# 평단 = (1 + WATERING_WEIGHT) / (1/P0 + WATERING_WEIGHT/P_water). 물타기 체결가 = P0 × 0.936.
TOTAL_UNITS_AFTER_WATER = 1.0 + WATERING_WEIGHT
# 축④ 만기 — 전략 이원 구조(미물타기=D+base 만기 / 물타기=D+water 연장)를 카톡에 병기.
# 화면 카드·백테스트는 실제 exit_date라 이미 정확하나, 카톡은 발송 시점에 어느 경로(물타기
# 여부)인지 미확정이라 base/water 두 만기일을 함께 노출한다 (둘이 같으면 단일 표기).
# 두 값 모두 profiles.json 유래(하드코딩 금지) — base != water 인 active 프로파일(예
# water_d6: base=3, water=6)에서 D+3 + 물타기 시 D+6 병기.
FORWARD_BASE = int(_PROFILE["forward_d_base"])
FORWARD_WATER = int(_PROFILE["forward_d_water"])


def log(msg: str) -> None:
    """stderr 로그 (키 본문 0건 의무)."""
    print(f"[send_kakao_message] {msg}", file=sys.stderr, flush=True)


def _kakao_token_module() -> Any:
    """공용 토큰 모듈 로드 (kakao_token.py — 함수-로컬 import, autoflake 회피 동일 패턴).

    read_env/write_env_refresh_token/refresh_access_token 로직은 2026-07-06
    kakao_token.py 로 verbatim 공용화 (FLR-20260406-TEC-001 한쪽 fix·다른 쪽 누락 봉쇄).
    본 파일 wrapper 는 기존 log 메시지·exit code 동작 유지.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import kakao_token

    return kakao_token


def read_env() -> dict[str, str]:
    """.env 파싱. KAKAO_* 3종만 추출, 키 본문 stdout 0건."""
    kt = _kakao_token_module()
    try:
        return kt.read_kakao_env(ENV_PATH)
    except kt.KakaoTokenError as exc:
        log(f"FAIL: {exc}")
        sys.exit(2)


def write_env_refresh_token(new_refresh_token: str) -> None:
    """카카오가 새 refresh_token 발급 시 .env atomic rewrite (race 봉쇄)."""
    kt = _kakao_token_module()
    try:
        kt.write_env_refresh_token(ENV_PATH, new_refresh_token)
        log("OK: .env refresh_token rewritten (atomic)")
    except kt.KakaoTokenError as exc:
        # 기존 동작 유지: rewrite 실패는 non-fatal (WARN/FAIL 로그 후 발송 계속)
        log(f"FAIL: .env rewrite: {exc}")


def refresh_access_token(env: dict[str, str]) -> str:
    """refresh_token → access_token 갱신 (kakao_token.request_token_refresh 위임).

    kakao spec verbatim (developers.kakao.com/docs/latest/ko/kakaologin/rest-api#refresh-token)
    — 상세 주석·응답 schema 는 kakao_token.py 참조 (단일 SSOT).
    """
    kt = _kakao_token_module()
    try:
        body = kt.request_token_refresh(env)
    except kt.KakaoTokenError as exc:
        # KakaoAuthError(4xx) 포함 — 토큰 값 미노출 (error_code 만)
        log(f"FAIL: token refresh: {exc}")
        sys.exit(3)
    access_token = body["access_token"]
    new_rt = body.get("refresh_token")
    if new_rt:
        log("INFO: kakao issued new refresh_token, rewriting .env")
        write_env_refresh_token(new_rt)
    log("OK: access_token refreshed")
    return access_token


def load_picks(date_str: str) -> dict[str, Any] | None:
    """본일 picks JSON read. 미존재 시 None."""
    fp = PICKS_DIR / f"{date_str}.json"
    if not fp.exists():
        log(f"INFO: picks not found: {fp.name} (graceful exit)")
        return None
    try:
        return json.loads(fp.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"FAIL: picks parse: {type(exc).__name__}")
        return None


def load_close_price(code: str, date_str: str) -> int | None:
    """dailybars DB → 종목 close 조회 (P0 source, lead 정정 2026-06-02 verbatim).

    schema: dailybars(code TEXT, date TEXT, close INTEGER, ...)
    """
    if not STOCKS_DB.exists():
        log(f"WARN: stocks.db not found: {STOCKS_DB}")
        return None
    try:
        conn = sqlite3.connect(f"file:{STOCKS_DB}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT close FROM dailybars WHERE code=? AND date=?",
                (code, date_str),
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        log(f"FAIL: stocks.db query: {type(exc).__name__}")
        return None
    if row is None or row[0] is None:
        log(f"WARN: dailybars row missing: code={code} date={date_str}")
        return None
    return int(row[0])


def load_market_closed_set() -> set[str]:
    """한국 거래소 휴장일 set (holidays.json SoT).

    schema verbatim (확인 2026-06-02):
      { "year": 2026, "market_closed": { "YYYY-MM-DD": "사유", ... } }

    fallback: 파일 부재 시 빈 set (요일만으로 거래일 판단, 공휴일 누락 위험 → log WARN).
    """
    if not HOLIDAYS_JSON.exists():
        log(f"WARN: holidays.json not found: {HOLIDAYS_JSON} (weekday-only fallback)")
        return set()
    try:
        d = json.loads(HOLIDAYS_JSON.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"FAIL: holidays.json parse: {type(exc).__name__}")
        return set()
    mc = d.get("market_closed")
    if isinstance(mc, dict):
        return set(mc.keys())
    if isinstance(mc, list):
        return {str(x) for x in mc}
    return set()


def add_trading_days(start_date: str, n: int) -> str | None:
    """start_date에서 n번째 다음 거래일 (휴장일/주말 skip).

    예: 2026-06-02 (화) entry, n=3
      → 6/3 (지방선거 휴장) skip
      → 6/4 (목) = +1
      → 6/5 (금) = +2
      → 6/6 (현충일 토 휴장) skip / 6/7 (일) skip
      → 6/8 (월) = +3 ✓
    """
    closed = load_market_closed_set()
    try:
        dt = datetime.strptime(start_date, "%Y-%m-%d").date()
    except ValueError:
        log(f"FAIL: start_date parse: {start_date}")
        return None
    added = 0
    cur = dt
    # 최대 30일 탐색 (안전장치 — 연속 휴장 cascade 봉쇄)
    for _ in range(30):
        cur = cur + timedelta(days=1)
        iso = cur.strftime("%Y-%m-%d")
        # 토(5) / 일(6) skip + market_closed skip
        if cur.weekday() >= 5:
            continue
        if iso in closed:
            continue
        added += 1
        if added == n:
            return iso
    log(f"WARN: add_trading_days 30-day cap exceeded (start={start_date} n={n})")
    return None


def _short_md(iso_date: str) -> str:
    """YYYY-MM-DD → MM/DD 표시 단축 (만기 병기 라인 200자 가드 여유 확보).

    파싱 실패 시 원본 그대로 반환 (정보 손실 0 — graceful).
    """
    try:
        dt = datetime.strptime(iso_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return iso_date
    return dt.strftime("%m/%d")


def build_no_pick_message(date_str: str) -> tuple[str, str]:
    """추천 부재(보류일) 안내 메시지 build.

    통합 모델 보류일(select_daily_pick.py 분기 '보류' = 선제거 後 잔존 0개)에는
    picks JSON 의 picked 가 null 이다 → 종목 추천이 0건. 사용자가 "시스템 장애"로
    오인하지 않도록 안내 메시지를 발송한다 (대표 요청 2026-06-05 19:07 KST).

    문안 톤 = DSN-arch-frontend §3.6.8 홈페이지 안내 라인 정합:
      '오늘은 추천 종목이 없습니다' + 다음 거래일 안내 (중립 정보성 톤).

    link = PM320 페이지 (종목 카드 부재 → 카드 직접 링크 대신 pm320.html, Q-20260605-104 News→PM320 이전).

    return: (text, link_url)
    """
    # 다음 거래일 (실패 시 "다음 거래일" 문자열 fallback)
    next_day = add_trading_days(date_str, 1)
    next_label = next_day if next_day else "다음 거래일"

    cache_token = datetime.now(KST).strftime("%Y%m%d%H")
    link_url = f"{HOMEPAGE_ORIGIN}/pm320.html?v={cache_token}"

    text = (
        f"📊 PM320 ({date_str})\n"
        f"오늘은 추천 종목이 없습니다.\n"
        f"조건에 맞는 종목이 없어 추천을 보류합니다.\n"
        f"📅 다음 거래일: {next_label}\n"
        f"※ 투자 권유가 아닙니다."
    )
    return text, link_url


def build_message(picks: dict[str, Any], close_price: int | None) -> tuple[str, str]:
    """메시지 본문 build — 물타기 비중·만기는 전략 프로파일(profiles.json) 참조.

    계산식 (프로파일 active 정합, build_card_history.py 와 동일 SSOT):
      P0 = close_price (dailybars D 종가)
      물타기 가격 = P0 × 0.936 (= -6.4%)
      익절 (물타기 X) = P0 × 1.032
      평단 (물타기 O) = (1+w) / (1/P0 + w/P_water)  [w = WATERING_WEIGHT, 1배=1.0/2배=2.0]
      익절 (물타기 O) = 평단 × 1.032
      만기 = holidays.json 기반 거래일 시퀀스 +FORWARD_BASE (휴장일/주말 skip)

    직전 하드코딩 "2배" + 평단 (P0+2×P0×0.936)/3 = build_card 1배와 불일치 버그였음
    (7f3f82b half-applied). 본 fix 로 프로파일 단일 SSOT 통일.

    return: (text, link_url)
    """
    date_str = picks["date"]
    picked = picks["picked"]
    code = picked["code"]
    name = picked["name"]
    trade_amount = picked["trade_amount"]
    change_pct = picked["change_pct"]

    # 만기 거래일 계산 — 미물타기=D+base / 물타기=D+water (이원 구조 병기).
    # 계산 실패 시 각각 "D+N" 문자열 fallback (add_trading_days 가 30일 cap 초과 등으로 None).
    # 표시는 MM/DD 단축 (병기 시 연도 2회 중복 제거 → 200자 가드 여유 확보, 정직 푸터 공존).
    exit_base_date = add_trading_days(date_str, FORWARD_BASE)
    exit_base = _short_md(exit_base_date) if exit_base_date else f"D+{FORWARD_BASE}"
    exit_water_date = add_trading_days(date_str, FORWARD_WATER)
    exit_water = _short_md(exit_water_date) if exit_water_date else f"D+{FORWARD_WATER}"
    # base == water (예 live_current: 둘 다 3) 면 단일 표기, 다르면 물타기 연장 병기.
    if exit_base == exit_water:
        expiry_line = f"⏰ 만기청산: {exit_base} 종가"
    else:
        expiry_line = f"⏰ 만기청산: {exit_base} 종가 (물타기 시 {exit_water}까지)"
    # 물타기 비중 문구 (프로파일 라벨에서 "첫 매수의 1배/2배" → "첫 매수 N배" 단축)
    weight_label = WATERING_WEIGHT_LABEL.replace("의 ", " ").replace("와 동일 수량", "")

    # 카드 직접 링크 (renderer.js _computeShareUrl OG 경로 정합, KST 시간 단위 cache token).
    #   Q-20260606-119 — 상세 URL 에서 stock 세그먼트 제거 (/pm320/stock/{date}/{code} → /pm320/{date}/{code}).
    cache_token = datetime.now(KST).strftime("%Y%m%d%H")
    link_url = f"{HOMEPAGE_ORIGIN}/pm320/{date_str}/{code}.html?v={cache_token}"

    # text template 200자 제한 가드 (FLR 2026-06-04 ROOT — feed template 4줄 화면 표시
    # 제한 + 본문 trim 사고 회피). send_kakao_memo 200자 fail-loud 가드로 overflow catch.
    if close_price is None or close_price <= 0:
        # P0 조회 실패 시 가격 없이 fallback (graceful)
        text = (
            f"📊 PM320 종목 ({date_str})\n"
            f"🎯 {name}\n"
            f"거래대금 {trade_amount / 1e12:.2f}조 / 등락 {change_pct:+.2f}%\n"
            f"💰 매수: 종가\n"
            f"📉 물타기: D 종가 × 0.936\n"
            f"↳ 비중: {weight_label}\n"
            f"📈 익절: D 종가 × 1.032\n"
            f"↳ 물타기 시: 평단 × 1.032\n"
            f"{expiry_line}"
        )
        return text, link_url

    p0 = close_price
    pullback_price = p0 * WATERING_RATIO
    tp_no_pullback = p0 * TAKE_PROFIT_RATIO
    # 표시 평단 = (P0 + w×P0×0.936) / (1+w) — 가격 가중 산술평균 (같은 수량 매수 기준).
    #   build_card_history.py compute_pm320_pick (avg_after_watering) 와 동일 SSOT 공식.
    #   (simulator 내부는 harmonic 평단을 쓰나, 카드/카톡 표시값은 산술평균으로 통일 — 직전
    #    라이브 카톡 origin 공식과 동일 분모/분자, w 만 프로파일화. 2배 버그 = w 미반영이었음.)
    avg_pullback = (p0 + WATERING_WEIGHT * pullback_price) / TOTAL_UNITS_AFTER_WATER
    tp_with_pullback = avg_pullback * TAKE_PROFIT_RATIO

    text = (
        f"📊 PM320 종목 ({date_str})\n"
        f"🎯 {name}\n"
        f"거래대금 {trade_amount / 1e12:.2f}조 / 등락 {change_pct:+.2f}%\n"
        f"💰 매수: 종가\n"
        f"📉 물타기: {pullback_price:,.0f}원 부근\n"
        f"　↳ 비중: {weight_label}\n"
        f"📈 익절: {tp_no_pullback:,.0f}원 부근\n"
        f"　↳ 물타기 시: {tp_with_pullback:,.0f}원 부근\n"
        f"{expiry_line}\n"
        # 정직 표기 (대표 결정 2026-06-16 15:37 KST): 카톡은 15:20 발송 시점에 cron 으로
        # build_message 가 호출되어 P0(close_price)가 *장중 잠정종가*다 (정본 종가는 15:30
        # 마감 후 확정). 물타기/익절/평단 모두 이 P0 기반 → 잠정값이 카톡에 영구 고정되어
        # 마감 후 정본 종가와 괴리(예 6/16 발송 56,300 vs 마감 55,800, 0.9%)가 수신자에게
        # 보이지 않는 false-fidelity(FLR-AGT-002 동형). 발송 시각(15:20)·brand promise 유지하고
        # 가격이 15:20 기준 잠정값임을 본문에 정직 명시. 가드: 전 4가격 라인 공통이라 라인별
        # 중복 0인 푸터 1줄로 표기 (187자, 200자 가드 여유 13자).
        f"※ 가격은 15:20 기준 · 종가 마감 후 확정"
    )
    return text, link_url


def send_kakao_memo(access_token: str, text: str, link_url: str) -> int:
    """카카오 talk/memo/default/send 호출.

    kakao spec verbatim (developers.kakao.com/docs/latest/ko/message/message-template):
      POST https://kapi.kakao.com/v2/api/talk/memo/default/send
      Authorization: Bearer {access_token}
      Content-Type: application/x-www-form-urlencoded
      template_object={JSON} (text template — 6/4 빈 본문 사고 회피, FLR 2026-06-04)

    응답: { "result_code": 0 } = success

    text template 채택 사유 (2026-06-04 ROOT 분석):
      - feed template + image_url = 화면 표시 4줄 (title 2 + description 2) 제한, 12줄
        본문 자동 trim → 빈 본문 사고 (6/4 15:20 catch)
      - text template = "text" 필드 최대 200자, 멀티라인 \n 정상 표시 + 단일 button 지원
      - 200자 길이 가드는 build_message 단계에서 사전 assert
    """
    if len(text) > 200:
        log(f"FAIL: text exceeds 200 chars (got {len(text)}), aborting send")
        return -1
    template_object = {
        "object_type": "text",
        "text": text,
        "link": {
            "web_url": link_url,
            "mobile_web_url": link_url,
        },
        "button_title": "종목카드 보기",
    }
    data = urllib.parse.urlencode(
        {
            "template_object": json.dumps(template_object, ensure_ascii=False),
        }
    ).encode("utf-8")
    req = urllib.request.Request(KAKAO_MEMO_SEND_URL, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8")
        except Exception:
            err_body = "<unreadable>"
        log(f"FAIL: kakao send HTTP {exc.code}: {err_body[:200]}")
        return -1
    except Exception as exc:
        log(f"FAIL: kakao send: {type(exc).__name__}")
        return -1
    result_code = body.get("result_code", -1)
    log(f"OK: kakao result_code={result_code}")
    return result_code


def main() -> int:
    parser = argparse.ArgumentParser(description="PM320 카톡 푸시")
    parser.add_argument("--date", default=None, help="YYYY-MM-DD (default: 본일 KST)")
    parser.add_argument("--dry-run", action="store_true", help="카카오 API 호출 skip")
    args = parser.parse_args()

    date_str = args.date or datetime.now(KST).strftime("%Y-%m-%d")
    log(f"START: date={date_str} dry_run={args.dry_run}")

    # 본일 휴장 명시 catch (대표 결정 2026-06-02 18:48 KST):
    # holidays.json.market_closed (한국 거래소 SoT) 기반 휴장일 발송 0건 보장.
    # 토/일 주말 동시 catch (cron은 평일 trigger만이지만 manual run 안전장치).
    market_closed = load_market_closed_set()
    try:
        wd = datetime.strptime(date_str, "%Y-%m-%d").weekday()
    except ValueError:
        wd = -1
    if date_str in market_closed or wd >= 5:
        reason = (
            "weekend" if wd >= 5 and date_str not in market_closed else "market_closed"
        )
        log(f"HOLIDAY: skip (date={date_str} reason={reason})")
        return 0

    picks = load_picks(date_str)
    if picks is None:
        log("EXIT: no picks file (graceful)")
        return 0

    # 보류일 분기: 통합 모델 '보류' = 선제거 後 잔존 0개 → picked=null.
    # 종목 추천 0건이라도 안내 메시지 발송 (대표 요청 2026-06-05 19:07 KST).
    if picks.get("picked") is None:
        log(f"INFO: no-pick day (branch={picks.get('branch')}) → notice message")
        text, link_url = build_no_pick_message(date_str)
    else:
        # P0 = picks.picked.code 의 D 종가 (dailybars SoT)
        picked_code = picks["picked"]["code"]
        close_price = load_close_price(picked_code, date_str)
        log(f"INFO: P0={close_price} for code={picked_code}")
        text, link_url = build_message(picks, close_price)
    log(f"INFO: link={link_url}")

    if args.dry_run:
        print(text)
        print(f"\n[link] {link_url}")
        log("DONE: dry-run")
        return 0

    env = read_env()
    access_token = refresh_access_token(env)
    result_code = send_kakao_memo(access_token, text, link_url)
    if result_code == 0:
        log("DONE: sent")
        return 0
    log(f"FAIL: result_code={result_code}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
