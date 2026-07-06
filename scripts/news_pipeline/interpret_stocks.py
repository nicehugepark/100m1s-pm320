"""
종목 해석 — Claude CLI (haiku) 사용.
Gemini 보류에 따라 claude -p 서브프로세스로 해석.
원문 본문은 DB 저장 금지 (법무팀 CAUTION). 해석 시점에만 메모리에서 사용.

REQ-076 Phase 2-B (2026-04-28): PROMPT_TEMPLATE_V3 신설.
- 다중 부모(N:M) 매크로 인지: canonical entry의 parents 메타를 LLM 컨텍스트에 노출.
- 본문 우선 + 다건 종합: 헤드라인 위주 매크로 오부착 차단 (GS건설 케이스).
- 로봇기사 후순위: VI/신고가/등락률 robot 패턴 _is_robot_title 휴리스틱.
- 출력 스키마 확장: themes[].parents 배열 + macro_events 배열.
- env INTERPRET_PROMPT_VERSION=V1로 fallback 가능 (기본 V3).
"""

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from .db import connect
from .fewshot import build_news_fewshot_context  # noqa: F401
from .llm_client import (  # noqa: F401
    ISHIKAWA_MODEL,
    TOGUSA_MODEL,
    call_model,
    call_model_cached,
    extract_json,
    hash_input,
    to_sqlite_text,
)
from .preferred_stock import (  # noqa: F401
    base_stock_code,
    build_preferred_context,
    get_base_code,
)
from .theme_normalizer import (
    THEME_META,
    apply_owner_overrides,
    link_stock_theme,
    normalize_list,
)

logger = logging.getLogger(__name__)

# 정규화 테마 사전 로드
_THEME_DICT_PATH = Path(__file__).parent / "theme_dictionary.json"
_CANONICAL_THEMES: list[str] = []
_INDUSTRY_SEEDS: dict[str, list[str]] = {}


def _load_theme_dictionary() -> list[str]:
    """theme_dictionary.json에서 canonical 테마명 목록을 로드."""
    global _CANONICAL_THEMES, _INDUSTRY_SEEDS
    if _CANONICAL_THEMES:
        return _CANONICAL_THEMES
    try:
        data = json.loads(_THEME_DICT_PATH.read_text(encoding="utf-8"))
        _CANONICAL_THEMES = [t["name"] for t in data.get("canonical_themes", [])]
        # REQ-028 W2 — industry_seeds 캐시 (164 industry → canonical 매핑)
        seeds = data.get("industry_seeds", {})
        _INDUSTRY_SEEDS = {k: v for k, v in seeds.items() if not k.startswith("_")}
    except Exception:
        _CANONICAL_THEMES = []
        _INDUSTRY_SEEDS = {}
    return _CANONICAL_THEMES


def _load_industry_seeds() -> dict[str, list[str]]:
    """REQ-028 W2 — industry_seeds 매핑 로드. theme_dictionary 동시 캐시."""
    if not _INDUSTRY_SEEDS:
        _load_theme_dictionary()
    return _INDUSTRY_SEEDS


def _apply_industry_seeds(code: str, industry: str, snap_date: str) -> list[str]:
    """REQ-028 W2 — industry_seeds 직매핑 적용 (LLM 미경유, SPEC-001 §VII.4).

    stocks.industry → canonical 1~3개 직접 부착. industry_seeds 빈 배열 [] 또는
    industry 미매핑 시 빈 리스트 반환 (LLM 위임). 외삽·환각 금지.
    Returns: 부착된 canonical 테마명 리스트.
    """
    if not industry or industry == "미분류":
        return []
    seeds = _load_industry_seeds()
    seed_themes = seeds.get(industry, [])
    if not seed_themes:
        return []
    canonical_set = set(_load_theme_dictionary())
    attached: list[str] = []
    for theme in seed_themes:
        if theme not in canonical_set:
            continue  # 사전 일관성 가드
        try:
            link_stock_theme(
                to_sqlite_text(code), to_sqlite_text(theme), snap_date, "industry_seed"
            )
            attached.append(theme)
        except ValueError as e:
            # [Q-20260511-FIX-B-1] 비활성 canonical (themes.is_active=0) skip 명시 전달.
            # silent return 시절: attached.append 무조건 실행 → DB 0행이지만 로그는 부착
            # 표시 (거짓 충실성, FLR-AGT-002 동형). raise + WARN으로 차단.
            logger.warning(
                f"[{code}] industry_seed SKIP (inactive canonical) "
                f"theme={theme} industry={industry} reason={e}"
            )
        except Exception as e:
            logger.warning(f"[{code}] industry_seed link FAIL theme={theme} err={e}")
    return attached


# Q-20260512-FRESH-LISTING-BADGE — 신규상장 자동 부착 룰
# 휴리스틱: dailybars 첫 row date >= today - 180일 (6개월) + backfill 일자 제외.
# 부착 대상은 theme '신규상장' (canonical, theme_dictionary v2.4.5 등록).
# 6개월 경과 종목은 자동 미부착 (date_last 갱신 정지 → 자연 만료).
# 주의: dailybars 첫 row가 backfill 시작일(2026-04-08, 2026-03-25)인 종목은 제외.
#       대규모 backfill 일자에 종목 2230개+8개가 동시 시작 — 상장일 아님.
_FRESH_LISTING_BACKFILL_DATES = frozenset(("2026-04-08", "2026-03-25"))
_FRESH_LISTING_WINDOW_DAYS = 180


def _apply_fresh_listing(code: str, snap_date: str) -> list[str]:
    """신규상장 자동 부착. dailybars 첫 row date 기준 휴리스틱.

    Returns: ['신규상장'] (부착 시) 또는 [] (미해당/만료/backfill 일자).
    대표 catch (2026-05-12 15:08 KST): "6개월 이내 신규상장 주식은 신규상장 배지".
    """
    from datetime import datetime as _dt

    with connect() as conn:
        row = conn.execute(
            "SELECT MIN(date) AS first_date FROM dailybars WHERE code=?", (code,)
        ).fetchone()
    if not row or not row["first_date"]:
        return []
    first_date = row["first_date"]
    # backfill 일자 제외 — 실제 상장일 아님
    if first_date in _FRESH_LISTING_BACKFILL_DATES:
        return []
    # 6개월 윈도우 판정 (snap_date 기준)
    try:
        snap_dt = _dt.strptime(snap_date, "%Y-%m-%d")
        first_dt = _dt.strptime(first_date, "%Y-%m-%d")
    except (ValueError, TypeError):
        return []
    if (snap_dt - first_dt).days > _FRESH_LISTING_WINDOW_DAYS:
        return []
    try:
        link_stock_theme(
            to_sqlite_text(code), to_sqlite_text("신규상장"), snap_date, "fresh_listing"
        )
        return ["신규상장"]
    except ValueError as e:
        logger.warning(f"[{code}] fresh_listing SKIP (inactive canonical) reason={e}")
        return []
    except Exception as e:
        logger.warning(f"[{code}] fresh_listing link FAIL err={e}")
        return []


# ── 가격 컨텍스트 (대표 제안 2026-05-31: 이시카와 해석에 가격/등락률 사실 주입) ──
# 목적: 뉴스만으로 "왜 올랐나"를 추상 추론하던 V3 프롬프트에 당일 시세(종가·등락률·
#   상한가 여부·52주 위치·거래대금)를 *사실값*으로 제공 → causal_chain/reasoning이
#   "오늘 +29.9% 상한가·52주 신고가 갱신" 같은 구체적 사실에 인과를 연결하게 한다.
# 거짓 날조 금지(FLR-AGT-002): 모든 값은 dailybars 실데이터에서만 계산. 추정·외삽 0.
# change_pct는 build_daily 와 동일 SSOT 정책(close 기준, 장중 max 미사용)으로 계산하여
#   카드/미니캔들과 mismatch 방지(FLR-20260406-TEC-001 한쪽 누락 교훈, build_daily.py
#   L2157~2172 close-기준 정책 정합). 52주 high/low 동률 = 신고가/신저가 판정도
#   build_daily _calc_range_240d_at(L365~) 와 동일 dailybars-240 윈도우 사용.
_LIMIT_UP_PCT = (
    29.0  # KRX 상하한가 ±30% — 호가단위 보정 후 29.0~30.0 (build_daily L2270 정합)
)


def _build_price_context(code: str, target_date: "str | None") -> str:
    """해당 종목·날짜의 가격 컨텍스트 블록 생성 (PROMPT_V3 {price_context} 주입용).

    Returns: 사람이 읽는 1~4줄 사실 요약 문자열. 데이터 없으면 "(시세 정보 없음)".
    모든 값은 dailybars 실데이터 — 날조 0 (FLR-AGT-002).
    """
    code = to_sqlite_text(code)
    today = (
        to_sqlite_text(target_date)
        if target_date
        else datetime.now().strftime("%Y-%m-%d")
    )
    with connect() as conn:
        # today 포함 이전 240행 (역순) — change_pct(직전 종가) + 52주 윈도우 동시 산출
        rows = conn.execute(
            """SELECT date, open, high, low, close, trade_amount FROM dailybars
               WHERE code=? AND date<=? ORDER BY date DESC LIMIT 240""",
            (code, today),
        ).fetchall()
    if not rows:
        return "(시세 정보 없음)"

    cur = rows[0]
    close = cur["close"]
    if not close or close <= 0:
        return "(시세 정보 없음)"
    parts: list[str] = []
    # 1) 종가 + 등락률 (직전 거래일 종가 대비, close 기준 SSOT)
    prev_close = None
    for r in rows[1:]:
        if r["close"] and r["close"] > 0:
            prev_close = r["close"]
            break
    chg_txt = ""
    is_limit_up = False
    if prev_close:
        change_pct = (close - prev_close) / prev_close * 100.0
        is_limit_up = change_pct >= _LIMIT_UP_PCT
        is_limit_down = change_pct <= -_LIMIT_UP_PCT
        sign = "+" if change_pct >= 0 else ""
        limit_tag = ""
        if is_limit_up:
            limit_tag = " 상한가"
        elif is_limit_down:
            limit_tag = " 하한가"
        chg_txt = f" (전일比 {sign}{change_pct:.2f}%{limit_tag})"
    # target_date에 봉 미적재 시 cur["date"]는 가장 최근 거래일 — 실제 봉 날짜로 표기
    # (날조 회피, FLR-AGT-002).
    parts.append(f"- 당일({cur['date']}) 종가: {close:,}원{chg_txt}")

    # 2) 장중 고가/저가 (점상 상한가면 OHLC 동일)
    o, h, lo = cur["open"], cur["high"], cur["low"]
    if h and lo:
        if is_limit_up and o == h == lo == close:
            parts.append(f"- 장중: 시초가부터 점상한가 (시=고=저=종 {close:,}원)")
        else:
            parts.append(f"- 장중 시가 {o:,} / 고가 {h:,} / 저가 {lo:,}원")

    # 3) 52주(240영업일) 위치 — 신고가/신저가 판정
    highs = [r["high"] for r in rows if r["high"] and r["high"] > 0]
    lows = [r["low"] for r in rows if r["low"] and r["low"] > 0]
    if highs and lows:
        hi240 = max(highs + [close])
        lo240 = min(lows + [close])
        if close >= hi240:
            parts.append(f"- 52주 신고가 갱신 (직전 240영업일 최고 {hi240:,}원 돌파)")
        elif hi240 > 0:
            pct_from_high = (close - hi240) / hi240 * 100.0
            parts.append(
                f"- 52주 고가 {hi240:,}원 대비 {pct_from_high:.1f}% (저가 {lo240:,}원)"
            )

    # 4) 거래대금 (억 단위)
    ta = cur["trade_amount"]
    if ta and ta > 0:
        parts.append(f"- 거래대금: {ta / 1e8:,.0f}억원")

    return "\n".join(parts)


# NOTE: `_build_trend_context` (20영업일 일봉 추세 요약 주입) 폐기 (2026-06-01 대표 catch).
# 사유: PROMPT 본문에 일봉 추세를 주입하면 "해석 = 시세 차트 해설"로 LLM bias 가
# 발생해 인과 본질(뉴스→가격)이 후순위로 밀린다. 대표 직접 발화 verbatim
# "뉴스 해석할때 스파크라인의 정보는 전달해도 일봉캔들 정보는 전달하지말자.
# 너무 과한것같다" (2026-06-01 11:5x KST). 함수 자체는 5/31 `49ac68c` 회귀 commit
# 도입 후 24h 이내 폐기 — 잔존 호출/import 0건 (grep PASS 후 정의 자체 제거).


_FETCH_TIMEOUT = 10
_FETCH_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; 100m1s-news-reader/1.0)"}
_MAX_BODY_CHARS = 3000


# patch-C: _to_text는 llm_client.to_sqlite_text의 alias. 기존 호출 부위 호환.
_to_text = to_sqlite_text


# URL → source 매핑 (config.RSS_FEEDS 도메인 패턴 기반).
# news_fetch_log.source 컬럼에 정규화 매체명 기록용 (NULL fallback OK).
# 매체별 분포 7d (2026-05-31 기준): 조선비즈 56.6% / 이데일리 9.7% / 매경 7.5% /
# 이투데이 6.3% / 아시아경제 5.2% / CNBC_World 5.2% / 파이낸셜뉴스 3.5% 외.
# 정확한 분포는 sqlite3 SELECT 결과 참조.
_SOURCE_DOMAIN_PATTERNS = (
    ("hankyung.com", "한경"),
    ("mk.co.kr", "매경"),
    ("edaily.co.kr", "이데일리"),
    ("biz.chosun.com", "조선비즈"),
    ("etoday.co.kr", "이투데이"),
    ("fnnews.com", "파이낸셜뉴스"),
    ("sedaily.com", "서울경제"),
    ("heraldcorp.com", "헤럴드경제"),
    ("asiae.co.kr", "아시아경제"),
    ("newsis.com", "뉴시스"),
    ("etnews.com", "전자신문"),
    ("cnbc.com", "CNBC_World"),
    ("reuters.com", "Reuters"),
)


def _infer_source_from_url(url: str) -> str:
    """URL host에서 매체명 추론. 매핑 부재 시 빈 문자열 (DB NULL OK)."""
    if not url:
        return ""
    url_lower = url.lower()
    for pattern, name in _SOURCE_DOMAIN_PATTERNS:
        if pattern in url_lower:
            return name
    return ""


def _log_fetch_attempt(
    url: str,
    body_len: int,
    success: bool,
    selector_hit: str = "",
    error_msg: str = "",
) -> None:
    """news_fetch_log 테이블에 fetch 시도 1행 insert.

    예외 시 silent skip — fetch 본 작업에 영향 0건 (메트릭 layer가 본문 fetch 실패
    유발 회피, FLR-AGT-002 거짓 충실성 hub 동형 회피).

    DB write 1행 부하 — interpret_stocks 호출 분량 기준 일 ~수백건 추정 안전.
    """
    try:
        from .db import connect

        source = _infer_source_from_url(url)
        fetched_at = datetime.now().isoformat()
        with connect() as conn:
            conn.execute(
                """INSERT INTO news_fetch_log
                   (fetched_at, url, source, selector_hit, body_len, success, error_msg)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    fetched_at,
                    url[:500],  # URL 최대 500자 trim (DB 부하 회피)
                    source or None,
                    selector_hit or None,
                    body_len,
                    1 if success else 0,
                    (error_msg[:200] if error_msg else None),
                ),
            )
            conn.commit()
    except Exception as e:
        # 메트릭 layer가 본 작업 영향 0건 의무 — silent skip
        logger.debug("news_fetch_log insert skip: %s", e)


def fetch_article_body(url: str, max_chars: int = _MAX_BODY_CHARS) -> str:
    """기사 URL에서 본문 텍스트를 추출. DB 저장 없이 해석용으로만 사용.

    실패 시 빈 문자열 반환 — 제목만으로 해석 (graceful fallback).

    [FLR 참조: 한미글로벌 오매칭] 관련기사/추천기사 섹션 제거 — 사이드바 종목명이
    본문 매칭에서 오탐을 유발하는 문제 방지.

    [DOC-20260531 메트릭 layer] news_fetch_log 1행 insert — selector_hit /
    body_len / success / error_msg 기록. 매체별 실 실패율 가시화 + 후속 매체별
    셀렉터 dict 신축 결정 evidence. cumulative lead 환각 19회차 직후 cycle session
    evidence-first 원칙 정합.
    """
    selector_hit = ""
    body_len = 0
    error_msg = ""
    try:
        resp = requests.get(url, timeout=_FETCH_TIMEOUT, headers=_FETCH_HEADERS)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # 관련기사/추천기사/사이드바 등 노이즈 섹션 제거 (본문 매칭 오탐 방지)
        _NOISE_SELECTORS = [
            # class/id 패턴: 관련기사, 추천기사, 인기기사, 사이드바
            {
                "class_": re.compile(
                    r"related|recommend|popular|sidebar|aside|ranking|more[-_]?news|other[-_]?news|hot[-_]?issue",
                    re.I,
                )
            },
            {
                "id": re.compile(
                    r"related|recommend|popular|sidebar|aside|ranking", re.I
                )
            },
            # 시맨틱 태그
        ]
        for sel in _NOISE_SELECTORS:
            for tag in soup.find_all(**sel):
                tag.decompose()
        # <aside> 태그 제거
        for aside in soup.find_all("aside"):
            aside.decompose()
        # <nav> 태그 제거 (네비게이션 내 기사 링크)
        for nav in soup.find_all("nav"):
            nav.decompose()

        # 일반적 기사 본문 셀렉터 시도
        article = soup.find("article")
        if article:
            selector_hit = "article_tag"
            text = article.get_text(separator=" ", strip=True)
        else:
            article = soup.find(
                class_=re.compile(
                    r"article[_-]?body|article[_-]?content|news[_-]?body|view[_-]?cont"
                )
            )
            if article:
                selector_hit = "article_body_regex"
                text = article.get_text(separator=" ", strip=True)
            else:
                # fallback: <p> 태그 텍스트 결합
                paragraphs = soup.find_all("p")
                text = " ".join(p.get_text(strip=True) for p in paragraphs)
                selector_hit = "p_fallback" if paragraphs else "none"

        result = text[:max_chars] if text else ""
        body_len = len(result)
        # success = body_len 100자+ 기준 (interpret_stocks 사용 기준선 정합 L658)
        _log_fetch_attempt(
            url,
            body_len,
            success=body_len >= 100,
            selector_hit=selector_hit,
        )
        return result
    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)[:150]}"
        _log_fetch_attempt(
            url,
            body_len=0,
            success=False,
            selector_hit=selector_hit or "error",
            error_msg=error_msg,
        )
        return ""


PROMPT_TEMPLATE = """종목 뉴스 분석. JSON만 반환.{body_notice}

종목: {name}({code}) / 업종: {industry} / 누적테마: {themes}

뉴스:
{news_block}
{body_block}
출력:
{{"causal_chain":"매크로→경로→종목영향 1~3문장","macro_event":"핵심 매크로 1줄","themes":["0~5개"],"evidence_span":"근거 title 1~2개 인용"}}

테마 규칙:
- 핵심사업이 테마와 직접 관련일 때만 채택. 간접 수혜(수주·납품·파트너십)는 ❌.
- 예시: 건설사의 데이터센터/원전/반도체팹 건축=건설만 O, 해당테마 X. 증권사 거래대금↑=증권만 O, 전쟁 X. 물류사 방산 장비 운송=물류만 O, 방산 X. 소재사 2차전지 소재 직접 생산=2차전지 O(예외).
- IR·홍보·ESG·사회공헌·KRX 업종분류명은 테마 아님.
- 정규화 목록에서만 선택. 목록 외 생성 금지: {theme_list}
- 해당 없으면 themes=[]. 누적 테마와 동일 의미면 누적 표현 그대로.

트럼프/정책 감지 시: causal_chain에 "트럼프 발언→경로" 포함, macro_event 최우선, 영향 트리(관세/무역전쟁/미중/금리/에너지/지정학) 명시.

영문 뉴스: 한국어로 번역·요약. 원문 노출 금지.

제약: 제공된 제목·본문에 없는 사실 추가 금지. 숫자·날짜만 사실로."""


# REQ-076 Phase 2-B — PROMPT_TEMPLATE_V3 (다중 부모 매크로 인지 + 본문 우선 + 출력 스키마 확장)
#
# V1 대비 차이:
# 1. 종목 프로필 5줄 — name/code/industry/sector/누적_stock_themes (도메인 간접수혜 추론 보조)
# 2. canonical 목록을 "name(parents=[...])" 형태로 노출 — 산업 대분류는 어떤 매크로 부모를 갖는지 인지
# 3. 본문 발췌 우선, 헤드라인은 보조 — 헤드라인 1회 등장으로 매크로 오부착 차단 (GS건설 "중동전쟁" 케이스)
# 4. 로봇기사 분리 — _is_robot_title 휴리스틱 후순위 마킹 (VI/신고가/등락률 자동기사)
# 5. 출력 스키마: themes[].parents 배열 + macro_events 배열 + reasoning 필드
# 6. 간접수혜 인지: 누적 테마와 도메인 평판 활용 권고. canonical 외 dangling 금지 재강조.
PROMPT_TEMPLATE_V3 = """종목 뉴스 분석 (PROMPT_V3 — 다중 부모 매크로 인지). JSON만 반환.

# 종목 프로필
- 이름: {name}
- 코드: {code}
- 업종(industry): {industry}
- 섹터(sector): {sector}
- 누적 테마(stock_themes): {themes}

# 당일 시세 (사실값 — dailybars 실데이터, 날조 금지)
{price_context}

# 뉴스 (본문 우선 — 헤드라인은 보조 단서)
{primary_block}
{secondary_block}
{robot_block}
# canonical 테마 목록 (반드시 이 안에서만 선택)
형식: 테마명(parents=[부모1, 부모2, ...]). parents=[]는 root(매크로) 테마. 산업 대분류 테마(건설/반도체/조선/방산/케이블 등)는 여러 매크로 부모를 가질 수 있다. 본 종목 해석 시 명시된 parents도 함께 부착 가능.

{theme_catalog}

# 작업
1) 본문(없으면 헤드라인 다건)을 종합해 본질 매크로 사슬을 식별한다. 헤드라인 1회 단어 등장에 의한 매크로 오부착 금지.
2) 산업 대분류 테마는 본 종목 핵심사업과 정합하면 채택. 해당 테마의 parents 중 본 사슬과 정합하는 것만 별도로 채택.
3) 간접 수혜(단순 수주·납품·파트너십·MOU)는 ❌. 단, 종목 도메인(industry/sector + 누적 테마)이 직접 매크로의 핵심 산업이면 직접 수혜로 본다.
4) IR·홍보·ESG·사회공헌·KRX 업종분류명은 테마 아님.
5) canonical 외 생성 금지. parents도 canonical 목록 안에서만 선택. dangling 금지.
6) 영문 뉴스는 한국어로 번역·요약. 원문 노출 금지.
7) 제공된 제목·본문에 없는 사실 추가 금지. 숫자·날짜만 사실로.
8) **당일 시세 반영 (생생한 해석 — 대표 제안 2026-05-31)**: 위 '당일 시세' 블록의 사실(등락률·상한가·52주 신고가·거래대금)을 causal_chain·reasoning에 *구체적으로* 연결하라. 예: "정책 수혜 기대 → 매수세 집중 → 당일 상한가(+29.9%)·52주 신고가 갱신". 시세 사실과 뉴스 인과를 결합해 "왜 이 가격이 나왔는지"를 생생히 서술한다.
   단, 가격/등락률/상한가 수치는 **제공된 사실값만** 인용하고 임의 수치 날조 절대 금지(FLR-AGT-002). '시세 정보 없음'이면 해당 언급하지 말 것.
   ※ 일봉 추세 블록은 폐기됨 (2026-06-01 대표 catch — "일봉 캔들 정보는 전달하지 말자, 너무 과한 것 같다"). 추세 추정·언급 금지.
9) 증자 유형 + 경영권 분쟁 자동 catch + 호재성 해석 (대표 매매 도메인 룰 영구화 2026-05-11):
   [테마 부여]
   - 공시/뉴스 본문 또는 제목에 '3자배정' 또는 '제3자배정' 등장 → themes에 '3자배정증자' 자동 부여 (산업 테마와 병행).
   - 공시/뉴스 본문 또는 제목에 '무상증자' 등장 → themes에 '무상증자' 자동 부여.
   - 본문/제목에 '경영권 분쟁' / '경영권분쟁' / '적대적 M&A' / '적대적 인수합병' / '임시주총' / '임시주주총회' / '이사 해임' / '지분 매집' / '경영권 방어' / '우호 지분 확보' / '의결권 행사' / '주주제안' 명시 → themes에 '경영권 분쟁' 자동 부여 (산업 테마와 병행).
   - 단순 '증자' 또는 '유상증자'(주주배정·일반공모)는 일반 자본조달로 본질 테마 미부여 — 모멘텀 약함.
   - 본문에 '제3자배정 유상증자'가 있으면 제목이 '유상증자'만 표기되어도 반드시 '3자배정증자' 부여.
   [호재성 해석 — causal_chain/reasoning 본질 반영]
   - '3자배정증자' = 외부 자본 유입 + 신규 투자자(전략적/재무적) 진입 + 보호예수 락업 + 후속 시세 상승 패턴 → 일반적으로 **호재**로 해석 (causal_chain에 "자본 유입·신규 투자자 진입·보호예수·후속 모멘텀" 명시).
   - '무상증자' = 주주가치 분할 + 권리락 후 단기 모멘텀 패턴 → 대체로 **호재**로 해석 (단 항상은 아님). 본문/제목에 무상증자 비율 명시(예: 1:1·1:2·1:3·주당 N주 등) 시 비율이 높을수록 모멘텀 강화 — causal_chain에 "고비율 무상증자(비율 명시)→권리락 단기 모멘텀 강화" 반영.
   - '경영권 분쟁' = 지분 매집·임시주총·이사 해임·우호 지분 확보 등 모멘텀 패턴 → 일반적으로 **호재**로 해석 (causal_chain에 "지분 매집·경영권 다툼·우호 지분 확보·후속 모멘텀" 명시). 단 분쟁 종료·합의·소송 패소 등 명시 시 모멘텀 소멸로 해석.
   - 단 본문에 부정적 맥락 (감자 동반 / 자본잠식 회피용 / 거래소 강제 / 철회·취소 / 기존주주 희석 우려 강조) 명시 시 호재 해석 회피하고 부정 맥락 반영.
   - 단순 '증자' (대규모 일반공모·주주배정) = 희석 부담 우세, 중립~약세로 해석.

# [중요 — REQ-079] themes 배열 구성 의무
themes 배열은 다음을 모두 포함해야 한다 (해당 시):
  (a) 종목 산업 분류 (industry_seed 또는 핵심사업) — 1건 (필수, 정합 시)
  (b) **causal_chain·macro_events에 명시된 매크로 이벤트** — canonical 매핑 (1~3건)
      예: causal_chain에 "휴전·지정학·구리"가 있으면 themes에 "전쟁완화"·"중동 지정학"·"비철금속" 모두 추가.
      causal_chain의 매크로를 themes에 누락하면 PROMPT_V3 의도 위반.
  (c) 종목 도메인 평판 + 본문 키워드 매크로 — canonical 매핑 (0~3건)

매핑 우선순위: canonical 정확 매칭 → aliases 매칭. 매칭 실패 시 부착하지 말 것 (외삽 금지).

# [중요 — P1a] articles 배열: 제공된 뉴스 각각의 유형 의미분류 (측정용)
위에 제공된 **모든 뉴스 기사**(본문 발췌·추가 헤드라인·자동 시세요약 기사 전부)에 대해 각 기사의 url과 유형(type)을 판정한다.
type 5종 (제목 형식이 아니라 **본문/제목의 실제 의미**로 판정):
  - "호재": 실적 개선·수주·신제품·정책 수혜·신규 투자자 유입 등 종목에 긍정적 펀더멘털/모멘텀 사건. **[특징주] 머리표라도 실제 호재 내용이면 호재**(예: "AI·로봇 사업 67% 폭등"은 단순 등락 요약이 아니라 사업 성장 호재).
  - "악재": 실적 악화·소송 패소·규제·리콜·횡령 등 부정적 사건.
  - "사건사고": 사고·범죄·재해 등 펀더멘털과 무관하거나 단발성인 사건(예: 사업장 흉기 난동·화재).
  - "시세정형": 단순 등락률·VI·신고가/신저가·시황 요약 등 **내용 없는 자동 시세 요약**(예: "전일比 +5% 상승", "장중 신고가"). [특징주] 머리표여도 등락률만 나열하면 시세정형.
  - "공시": 증자·배당·합병·자기주식 등 공시성 기사.
tradability(매매 직결도) 0~5 정수: 0=무관, 5=즉시 매매 직결 강한 모멘텀.
**호재 vs 시세정형 경계가 핵심**: 등락의 *이유*(사업/실적/정책)가 있으면 호재, *등락 사실만* 나열하면 시세정형.

# [중요 — Q-167 ②] 뉴스 중요도 점수: magnitude / virality (0.0~1.0 실수)
이 종목의 *오늘 뉴스 전체*를 종합해 "투자자에게 위로 올려 보여줄 만한 뉴스인가"를 두 축으로 평가한다.
판별 결과로 카드에서 큰 뉴스를 상단에 노출한다.
- magnitude(사건의 크기·영향력) 0.0~1.0: 종목 펀더멘털·주가에 미치는 충격의 크기.
  1.0=대형 인수합병·상한가급 호재/악재·실적 서프라이즈, 0.5=의미 있는 수주·신제품,
  0.1=단순 시황 언급, 0.0=무관.
- virality(전파력·화제성) 0.0~1.0: 시장의 관심·확산 정도(다수 매체 보도·테마 주도·이슈성).
  1.0=시장 전체 화제(정책 테마 주도·대형 이벤트), 0.5=업종 내 주목, 0.0=무관.
**🔴 판별 불가 시 (본문 부재·근거 없음·시세정형뿐) magnitude/virality를 추측해 채우지 말고 null로 둔다**
(거짓 점수 금지 — 점수 없는 뉴스는 정렬에서 자연 후순위가 정상).
점수는 *제공된 사실*에 근거할 때만 부여하고, 임의 과장 금지.

# 출력 스키마 (JSON only — themes는 반드시 dict 객체 배열, string 배열 절대 금지)
{{
  "themes": [
    {{"name": "<canonical>", "parents": ["<canonical_parent>", ...]}},
    ...
  ],
  "macro_events": ["<핵심 매크로 1줄>", ...],
  "causal_chain": "매크로→경로→종목영향 1~3문장",
  "reasoning": "본문 근거를 인용한 매핑 설명 1~3문장",
  "evidence_span": "근거 title 1~2개 인용",
  "magnitude": 0.0,
  "virality": 0.0,
  "articles": [
    {{"url": "<위 뉴스의 출처 url 그대로>", "type": "호재|악재|사건사고|시세정형|공시", "tradability": 0}},
    ...
  ]
}}
# magnitude/virality 판별 불가 시 해당 키를 null 로 두거나 생략. 추측 점수 금지.

# themes 형식 예시 (반드시 dict 객체 — string 단독 절대 금지)

[좋은 예 1 — 케이블 업종, 휴전·구리 매크로]
causal_chain: "미국-이란 휴전 → 구리 원가 부담 완화 → 케이블 수익성 개선"
"themes": [
  {{"name":"케이블","parents":[]}},
  {{"name":"전쟁완화","parents":["전쟁"]}},
  {{"name":"중동 지정학","parents":["전쟁"]}},
  {{"name":"비철금속","parents":[]}}
]

[좋은 예 2 — 건설 업종, 부동산·인프라 매크로]
causal_chain: "재개발 규제 완화 + 인프라 투자 확대 → 건설사 수주 증가"
"themes": [
  {{"name":"건설","parents":[]}},
  {{"name":"부동산 재개발","parents":["건설"]}},
  {{"name":"인프라 수혜","parents":["건설"]}}
]

[나쁜 예 1 — string 배열 (V1 형식, 절대 금지)]
"themes": ["케이블"]
"themes": ["부동산 재개발","인프라 수혜"]

[나쁜 예 2 — 매크로 누락 (causal_chain에 매크로 있는데 산업 1건만)]
causal_chain: "휴전·지정학·구리 영향" 인데
"themes": [{{"name":"케이블","parents":[]}}]   ← 매크로 누락. 위 [좋은 예 1]처럼 모두 부착해야 함.

[나쁜 예 3 — parents 키 누락]
"themes": [{{"name":"건설"}}]   ← parents 필드 필수. root이면 parents=[].

해당 없으면 themes=[], macro_events=[]. 누적 테마와 동일 의미면 누적 표현 그대로."""

# P1a — 출력 스키마 버전 태그. 캐시 키(input_hash)에 포함되어, 스키마 변경 시 이 태그만
# bump 하면 기존 캐시가 자동 무효화된다 (FLR-20260511-DAT-002 schema validation 정합).
# "V3a-articles" = V3 + articles[] (article_type 의미분류 shadow) 추가.
PROMPT_SCHEMA_TAG = "V3f-magvir"
# 2026-06-01 대표 catch 2건 묶음 (DOC-20260601):
# (a) 매 cron 주기 재해석 봉쇄 — price_context / trend_context 가 input_hash 에 포함되어
#     장중 가격·거래대금 변동 시 매 fire MISS → 전체 재해석 cascade 발생 (5/30 hit_pct=0%).
#     → price_context 는 input_hash 에서 제외 (PROMPT 본문 주입은 유지),
#        trend_context 자체 폐기 (prompt+hash 양쪽 모두).
#     → tag bump V3c-trendctx → V3d-noctxhash (기존 V3c 캐시 무효화 PASS,
#        새 입력 동일 뉴스셋만 HIT — FLR-20260511-DAT-002 정합).
# (b) 일봉 캔들 추세 주입 폐기 — 대표 verbatim "일봉캔들 정보는 전달하지말자, 과한 것 같다".
#
# 2026-06-03 대표 catch (DOC-20260603, ROOT 2 URL slide):
# - 잔존 ~25% miss = `news` (causal_chain IS NULL) URL 변동 본질.
# - input_hash payload 의 URL 집합을 `_fetch_today_news_urls()` 결과 = "오늘 발행 비로봇 뉴스
#   URL 전수" (causal_chain 무관, processed 여부 무관) 로 교체. LLM 호출 입력 분량 불변.
# - 005935 18회 재해석 (b20b3c3 동형 패턴) 결정적 봉쇄.
# - tag bump V3d-noctxhash → V3e-todayurls (V3d 캐시 자연 무효화 — FLR-20260511-DAT-002 정합).

CLAUDE_CMD = "/Users/seongjinpark/.local/bin/claude"
DAILY_CALL_LIMIT = 200


# REQ-076 Phase 2-B — 로봇 기사 휴리스틱
# DB의 is_robot 플래그 외 추가 가드. _fetch_stock_news는 이미 is_robot=0만 가져오지만,
# is_robot 미라벨 잔존 케이스(파이프라인 시점 전 적재) 보강.
_ROBOT_TITLE_PATTERNS = [
    re.compile(r"VI\s*발동"),
    re.compile(r"신고가"),
    re.compile(r"신저가"),
    re.compile(r"\d+거래일\s*연속"),
    re.compile(r"등락률\s*[+\-±]?\d"),
    re.compile(r"^\[(?:특징주|장중특징주|시황|마감)\]"),
    re.compile(r"전일比"),
    re.compile(r"^\[표\]"),
]


def _is_robot_title(title: str) -> bool:
    """제목 휴리스틱으로 로봇/공급기 기사 식별 (REQ-076 §1.1).

    - 길이가 너무 짧고(<=20자) 패턴 매칭되면 robot 확률 ↑
    - 등락률/VI/신고가 등 정형 자동기사 패턴
    """
    if not title:
        return False
    t = title.strip()
    for pat in _ROBOT_TITLE_PATTERNS:
        if pat.search(t):
            return True
    return False


def _build_news_block_with_body(news_rows: list, max_primary: int = 5) -> tuple:
    """REQ-076 §1.1 — 본문 우선 + 로봇기사 후순위 분류.

    Args:
        news_rows: SELECT title, url, source FROM news 결과 (최대 limit건)
        max_primary: 본문 fetch 시도 헤드라인 수

    Returns:
        (primary_block, secondary_block, robot_block) — PROMPT_V3에 주입할 텍스트.
        - primary_block: 본문 발췌가 있는 본질 뉴스 (긴 제목, 비-로봇)
        - secondary_block: 본문 fetch 실패 또는 짧은 헤드라인 (비-로봇)
        - robot_block: 로봇 패턴 매칭 — LLM에 후순위로 노출 (참고용)
    """
    primary_lines: list[str] = []
    secondary_lines: list[str] = []
    robot_lines: list[str] = []

    # 1차 분류 — 로봇 vs 비로봇
    # P1a (article_type shadow): robot_block 라인에도 url 노출 — [특징주] 호재가
    # type 판정(articles[])을 받게 한다. robot_block은 매크로 부착 후순위 정책은 유지하되,
    # article_type 의미분류는 전체 뉴스(robot 포함)를 대상으로 한다. (정렬 미변경 shadow)
    #
    # FLR-20260529-TEC-001 (LG전자 호재 식별불가 복붙): 제목 휴리스틱(_is_robot_title)이
    # "신고가"·"[특징주]" 패턴만으로 호재 분석 기사("피지컬 AI·로봇 신고가 돌파",
    # "[특징주] 젠슨 황 회동 급등")까지 robot으로 오분류 → 본문 fetch 자체가 안 됨 →
    # primary에 시황/사건사고만 남아 LLM이 "급등 사유 본문 없음 → 식별 불가" 판정.
    # 해결: robot 후보도 본문 fetch를 시도하여, 실질 본문(>=300자)이 잡히면 primary로
    # 승격(실제 분석 기사). 본문이 짧거나(시세요약 1줄) 안 잡히면 robot 유지.
    # 거짓 날조 0 (FLR-AGT-002): 실제 기사 본문이 존재할 때만 승격, 없으면 그대로 후순위.
    non_robot: list[dict] = []
    robot_candidates: list[dict] = []
    for n in news_rows:
        title = n["title"] if n["title"] else ""
        if _is_robot_title(title):
            robot_candidates.append(n)
        else:
            non_robot.append(n)

    # 본문 fetch 시도 — 비로봇 상위 max_primary건. 본문 100자 이상이면 primary, 아니면 secondary.
    fetched_count = 0
    for n in non_robot:
        title = n["title"] if n["title"] else ""
        url = n["url"] if n["url"] else ""
        source = n["source"] if n["source"] else ""
        if fetched_count < max_primary and url:
            body = fetch_article_body(url, max_chars=2000)
            if body and len(body) > 100:
                primary_lines.append(
                    f"## [{source}] {title}\n{body[:1500]}\n출처: {url}"
                )
                fetched_count += 1
                continue
        # 본문 fetch 실패 또는 max_primary 초과
        secondary_lines.append(f"- {title} · {source} · {url}")

    # robot 후보 본문 승격 — 실질 본문(>=300자)이면 primary, 아니면 robot_block 유지.
    # 임계 300자: 시세요약 1줄(수십~백자)은 거르고 실제 분석 기사만 승격.
    # 호재 키워드("급등"·"폭등"·"회동"·"AI"·"로봇"·"수주"·"신사업" 등) 보유 robot 후보를
    # 먼저 fetch — 비-robot 시황기사가 slot을 굶기지 않게 한다. 승격은 비-robot primary와
    # 별개 추가 예산(_ROBOT_PROMOTE_SLOTS)으로 보장.
    _ROBOT_PROMOTE_MIN = 300
    _ROBOT_PROMOTE_SLOTS = 3
    _BULLISH_KW = re.compile(
        r"급등|폭등|회동|로봇|AI|인공지능|수주|신사업|신제품|모멘텀|수혜|어닝|실적|목표주가|돌파|반등",
        re.I,
    )
    robot_candidates.sort(
        key=lambda n: 0 if _BULLISH_KW.search(n["title"] or "") else 1
    )
    promoted = 0
    for n in robot_candidates:
        title = n["title"] if n["title"] else ""
        url = n["url"] if n["url"] else ""
        source = n["source"] if n["source"] else ""
        if promoted < _ROBOT_PROMOTE_SLOTS and url:
            body = fetch_article_body(url, max_chars=2000)
            if body and len(body) >= _ROBOT_PROMOTE_MIN:
                primary_lines.append(
                    f"## [{source}] {title}\n{body[:1500]}\n출처: {url}"
                )
                promoted += 1
                continue
        robot_lines.append(f"- (자동요약) {title} · 출처: {url}")

    primary_block = (
        ("\n## 본문 발췌 (본질 뉴스 우선)\n" + "\n\n---\n\n".join(primary_lines))
        if primary_lines
        else ""
    )
    secondary_block = (
        ("\n## 추가 헤드라인 (보조)\n" + "\n".join(secondary_lines))
        if secondary_lines
        else ""
    )
    robot_block = (
        (
            "\n## 자동 시세요약 기사 (자동 시황·등락률 정형, 후순위 — 매크로 부착 근거로 쓰지 말 것)\n"
            + "\n".join(robot_lines)
        )
        if robot_lines
        else ""
    )
    return primary_block, secondary_block, robot_block


def _retire_stale_stock_themes(
    conn,
    stock_code: str,
    current_theme_ids: list[int],
    new_source: str = "ishikawa_v3",
    retire_source: str = "retired_v3",
):
    """REQ-076 Phase 2-B 재작업 (FLR-TEC-002 §3) — V3 재해석 시 stale stock_themes retire.

    옵션 B 채택 — DELETE 대신 source='retired_v3'로 UPDATE (history 보존, 라이브 chip은
    build_daily에서 source 필터로 제외하면 사라진다. build_daily 변경은 Phase 4 영역).

    동작:
    - 본 종목의 기존 stock_themes 행 중 V3 응답에 포함되지 않은(theme_id NOT IN current) 행을
      source='retired_v3'로 UPDATE. date_last 갱신.
    - 단, 보호 source ('industry_seed', 'industry_seeds', 'pipeline', 'manual', 'kiwoom_ranking')는
      retire 대상에서 제외 — LLM 외 경로(시드 직매핑·수동 부착·랭킹 수집)는 V3 재해석 영향 받지 않는다.
    - 이미 'retired_v3' 행은 노터치 (재배치 시 idempotent).

    Args:
        conn: 활성 sqlite connection (호출자가 transaction 보유)
        stock_code: 종목코드
        current_theme_ids: V3 응답이 부착한 theme_id 리스트 (보존)
        new_source: 신규 부착 source (현재 미사용 — 향후 source 비교용 reserved)
        retire_source: retire 후 마킹할 source 값

    Returns:
        retired_count: retire 처리된 행 수
    """
    if not current_theme_ids:
        # 안전 가드: V3가 빈 themes 반환 시 전부 retire 금지 (의도치 않은 wipeout 차단)
        # 빈 응답은 LLM 실패 가능성 — 본 분기는 retire skip
        return 0
    placeholders = ",".join("?" for _ in current_theme_ids)
    # 보호 source: LLM 외 경로
    # - industry_seed/industry_seeds: _apply_industry_seeds 시드 직매핑
    # - pipeline: schema enum (4종 후보 중 하나)
    # - owner: schema enum, 향후 대표 수동 부착 UI 노출 시 사용 (실데이터 0건이나 잠재 결함 봉쇄, Q-20260511-TOGUSA-V3-FIX-1)
    # - kiwoom_ranking: 거래대금 순위 자동 수집
    # - manual%: 'manual' + 'manual_seed_reqXXX' 등 수동·시드 부착 전체 (LIKE 정규화)
    # - retire_source: 이미 retire 처리된 행 idempotent
    protected_sources = (
        "industry_seed",
        "industry_seeds",
        "pipeline",
        "owner",
        "kiwoom_ranking",
        retire_source,
    )
    protected_placeholders = ",".join("?" for _ in protected_sources)
    cur = conn.execute(
        f"""UPDATE stock_themes
            SET source = ?, date_last = ?
            WHERE stock_code = ?
              AND theme_id NOT IN ({placeholders})
              AND source NOT IN ({protected_placeholders})
              AND source NOT LIKE 'manual%'""",
        (
            retire_source,
            datetime.now().strftime("%Y-%m-%d"),
            stock_code,
            *current_theme_ids,
            *protected_sources,
        ),
    )
    return cur.rowcount


def _build_theme_catalog() -> str:
    """REQ-076 §1.1 — canonical 테마를 'name(parents=[...])' 형태로 직렬화.

    PROMPT_V3에 주입. parents 메타가 있는 테마만 부모 표시; 빈 배열은 (root) 표시.
    THEME_META는 theme_normalizer 모듈 로드 시 이미 캐시됨.
    """
    canonical = _load_theme_dictionary()
    if not canonical:
        return "(사전 로드 실패)"
    lines: list[str] = []
    for name in canonical:
        meta = THEME_META.get(name) or {}
        parents = meta.get("parents") or []
        if parents:
            lines.append(f"{name}(parents={parents})")
        else:
            lines.append(f"{name}(root)")
    return ", ".join(lines)


# REQ-076 Phase 2-B 재작업 (FLR-20260428-TEC-002 §1) — togusa schema 동기화.
# 기존 결함: togusa가 fix.themes를 V1 string list로 재작성 → V3 themes[].parents 정보(dict) 손실.
# 해결: togusa는 verdict + reason만 출력. themes 재작성 권한 박탈. 이시카와 V3 응답을 신뢰.
# verdict='bad' 시 caller에서 pending 저장 또는 V3 재호출 (본 Phase는 pending 저장 — 라이브 부착 차단).
# fix 필드는 backward 호환을 위해 무시 처리 (extract만 하고 사용 안 함).
TOGUSA_NEWS_EVAL = """이시카와 응답 검증. 환각·간접수혜 적발 (themes 재작성 권한 없음).

종목: {name}({code})
제목: {titles}
응답: {response}

기준:
- causal_chain이 제목 근거인가
- themes가 직접관련만인가(간접 수혜=bad)
- 뉴스 외 사실 금지
- macro_event(s)가 실제 언급인가

themes 재작성 금지. 잘못된 부착은 verdict=bad + reason으로만 보고 (이시카와 V3 재해석 영역).

출력 JSON만:
{{"verdict":"good|bad","reason":"1~2문장 (bad일 때 어떤 테마/주장이 잘못됐는지 명시)"}}
"""


def _togusa_review_news(name, code, titles, ish_response, ignore_cache: bool = False):
    """토구사 검증 (REQ-076 §1 — themes 재작성 박탈, verdict + reason만).

    같은 (이시카와 응답, 모델)이면 같은 verdict 반환 (REQ-003 캐시).
    input_hash = sha256(code + 정규화된 이시카와 응답 JSON)
    """
    prompt = TOGUSA_NEWS_EVAL.format(
        name=name,
        code=code,
        titles=json.dumps(titles, ensure_ascii=False),
        response=json.dumps(ish_response, ensure_ascii=False),
    )
    # 이시카와 응답을 정규화하여 해시 — titles는 ish_response에 이미 반영됨
    h = hash_input([code, ish_response])
    raw = call_model_cached(
        prompt,
        TOGUSA_MODEL,
        domain="togusa_news",
        target_id=code,
        input_hash=h,
        agent="togusa",
        timeout=60,
        max_retries=1,
        ignore_cache=ignore_cache,
    )
    if not raw:
        return None
    return extract_json(raw)


def _save_news_review(
    conn, date, stock_code, agent, titles, resp, verdict, evaluator, note
):
    # FLR-005 보강: 모든 LLM-derived 필드 _to_text 래핑 (note가 dict/list 가능)
    conn.execute(
        """INSERT INTO news_review(date, stock_code, agent, news_titles, llm_response,
             verdict, evaluator, evaluation_note, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            date,
            stock_code,
            agent,
            json.dumps(titles, ensure_ascii=False, default=str),
            json.dumps(resp, ensure_ascii=False, default=str),
            _to_text(verdict),
            _to_text(evaluator),
            _to_text(note),
            datetime.now().isoformat(),
        ),
    )
    # FLR-009 hot-fix: news_review → news.causal_chain 동시 UPDATE (UI 조회 경로 일원화)
    # verdict='good' + causal_chain 존재 시, 매칭 news 레코드의 causal_chain/macro_event UPSERT.
    try:
        if isinstance(resp, dict) and _to_text(verdict) == "good":
            causal = resp.get("causal_chain") or ""
            macro = resp.get("macro_event") or ""
            if causal and isinstance(titles, (list, tuple)) and titles:
                placeholders = ",".join("?" * len(titles))
                params = [
                    _to_text(causal),
                    _to_text(macro),
                    stock_code,
                    date,
                ] + [str(t) for t in titles]
                conn.execute(
                    f"""UPDATE news
                        SET causal_chain = COALESCE(NULLIF(causal_chain, ''), ?),
                            macro_event = COALESCE(NULLIF(macro_event, ''), ?)
                        WHERE stock_code = ?
                          AND date(published_at) = ?
                          AND title IN ({placeholders})""",
                    params,
                )
    except Exception as e:
        print(f"[_save_news_review] news.causal_chain sync 실패 (FLR-009 hot-fix): {e}")


def _today_usage():
    today = datetime.now().strftime("%Y-%m-%d")
    with connect() as conn:
        row = conn.execute(
            "SELECT call_count FROM gemini_usage WHERE date=?", (today,)
        ).fetchone()
        return row["call_count"] if row else 0


def _increment_usage(in_tok: int = 0, out_tok: int = 0):
    today = datetime.now().strftime("%Y-%m-%d")
    cost = in_tok / 1_000_000 * 0.25 + out_tok / 1_000_000 * 1.25  # haiku pricing
    with connect() as conn:
        conn.execute(
            """INSERT INTO gemini_usage(date, call_count, input_tokens, output_tokens, est_cost_usd)
               VALUES(?, 1, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
                 call_count = call_count + 1,
                 input_tokens = input_tokens + ?,
                 output_tokens = output_tokens + ?,
                 est_cost_usd = est_cost_usd + ?""",
            (today, in_tok, out_tok, cost, in_tok, out_tok, cost),
        )
        conn.commit()


_MIN_NEWS_FOR_ANALYSIS = 3  # 이 미만이면 날짜 범위 확장
_LOOKBACK_DAYS = 14  # 최대 14일 전까지 (주말 포함 10영업일)


def idempotency_key_pref(pref_code: str, url: str) -> str:
    """우선주 보강 news 행의 idempotency key. 본주 뉴스를 우선주 코드로 적재할 때 사용."""
    import hashlib

    h = hashlib.sha1(url.encode()).hexdigest()[:12]  # noqa: S324 (idempotency only, match_stocks 동일 패턴)
    return f"{pref_code}:pref:{h}"


def _ensure_preferred_columns(conn):
    """news 테이블에 is_robot, match_source, article_type 컬럼 보장."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(news)").fetchall()}
    if "is_robot" not in cols:
        conn.execute("ALTER TABLE news ADD COLUMN is_robot INTEGER DEFAULT 0")
    if "match_source" not in cols:
        conn.execute("ALTER TABLE news ADD COLUMN match_source TEXT DEFAULT 'title'")
    # P1a — 뉴스 유형 의미분류 (shadow). NULL = 미판정/url 미매핑 (카드 정렬 미반영).
    if "article_type" not in cols:
        conn.execute("ALTER TABLE news ADD COLUMN article_type TEXT")


_VALID_ARTICLE_TYPES = {"호재", "악재", "사건사고", "시세정형", "공시"}


def _parse_article_types(parsed: dict) -> dict[str, str]:
    """LLM 응답 articles[] → {url: type} 맵. 유효 type만, url 없으면 제외.

    부분 실패(articles 없음/비정상/url 누락)는 빈 맵 또는 누락으로 graceful —
    호출부가 article_type=NULL fallback 처리한다 (카드 누락 악화 금지).
    """
    out: dict[str, str] = {}
    arts = parsed.get("articles")
    if not isinstance(arts, list):
        return out
    for a in arts:
        if not isinstance(a, dict):
            continue
        url = a.get("url")
        atype = a.get("type")
        if (
            isinstance(url, str)
            and url.strip()
            and isinstance(atype, str)
            and atype.strip() in _VALID_ARTICLE_TYPES
        ):
            out[url.strip()] = atype.strip()
    return out


# Q-167 ② — newzy_score 가중 (위로 올릴 뉴스 = 크기·전파력 핵심).
_NEWZY_W_MAGNITUDE = 0.6
_NEWZY_W_VIRALITY = 0.4


def _coerce_unit_score(v) -> "float | None":
    """LLM 점수값 → 0.0~1.0 float. 판별 불가/비정상은 None (거짓 충실성 가드, FLR-AGT-002).

    null/문자열/범위초과는 모두 None 으로 보수 처리하여, 추측·날조 점수가 정렬에
    끼어들지 않게 한다. None 인 축은 호출부에서 score=NULL 로 직결(자연 후순위).
    """
    if isinstance(v, bool):  # bool 은 int 서브타입 — 점수로 오용 차단
        return None
    if not isinstance(v, (int, float)):
        return None
    f = float(v)
    if f != f or f in (float("inf"), float("-inf")):  # NaN/inf
        return None
    if f < 0.0 or f > 1.0:
        return None
    return f


def _parse_newzy_score(
    parsed: dict,
) -> "tuple[float | None, float | None, float | None]":
    """LLM 응답 → (magnitude, virality, newzy_score). 산출 불가 축은 None.

    score = magnitude*0.6 + virality*0.4 — 단, 두 축 *모두* 유효할 때만 산출한다.
    한쪽이라도 None 이면 score=None (NULL 저장 → build_daily 정렬에서 자연 후순위).
    거짓 0.5 채움 금지 (FLR-AGT-002 거짓 충실성). 점수 없는 뉴스 = 후순위가 정상.
    """
    if not isinstance(parsed, dict):
        return (None, None, None)
    mag = _coerce_unit_score(parsed.get("magnitude"))
    vir = _coerce_unit_score(parsed.get("virality"))
    if mag is None or vir is None:
        # 한 축이라도 없으면 score 미산출 (부분 점수로 정렬 왜곡 금지). 축 값은 보존.
        return (mag, vir, None)
    score = round(mag * _NEWZY_W_MAGNITUDE + vir * _NEWZY_W_VIRALITY, 4)
    return (mag, vir, score)


def _fetch_news_rows(conn, code: str, limit: int, target_date: str = None):
    """단일 종목코드의 비로봇·중복제거 뉴스 행을 반환 (재해석/우선주 본주 후보 공용)."""
    if target_date:
        rows = conn.execute(
            """SELECT title, url, source, published_at FROM (
                 SELECT *, ROW_NUMBER() OVER (PARTITION BY title ORDER BY published_at DESC) AS rn
                 FROM news WHERE stock_code=? AND date(published_at)=?
                 AND COALESCE(is_robot, 0) = 0
               ) WHERE rn = 1
               ORDER BY published_at DESC LIMIT ?""",
            (code, target_date, limit),
        ).fetchall()
        if len(rows) < _MIN_NEWS_FOR_ANALYSIS:
            from datetime import datetime as _dt  # noqa: F401
            from datetime import timedelta

            since = (
                _dt.strptime(target_date, "%Y-%m-%d") - timedelta(days=_LOOKBACK_DAYS)
            ).strftime("%Y-%m-%d")
            rows = conn.execute(
                """SELECT title, url, source, published_at FROM (
                     SELECT *, ROW_NUMBER() OVER (PARTITION BY title ORDER BY published_at DESC) AS rn
                     FROM news WHERE stock_code=? AND date(published_at) BETWEEN ? AND ?
                     AND COALESCE(is_robot, 0) = 0
                   ) WHERE rn = 1
                   ORDER BY published_at DESC LIMIT ?""",
                (code, since, target_date, limit),
            ).fetchall()
    else:
        rows = conn.execute(
            """SELECT title, url, source, published_at FROM (
                 SELECT *, ROW_NUMBER() OVER (PARTITION BY title ORDER BY published_at DESC) AS rn
                 FROM news WHERE stock_code=? AND causal_chain IS NULL
                 AND COALESCE(is_robot, 0) = 0
               ) WHERE rn = 1
               ORDER BY published_at DESC LIMIT ?""",
            (code, limit),
        ).fetchall()
    return rows


def _fetch_today_news_urls(conn, code: str, target_date: str = None) -> list[str]:
    """input_hash 전용 — 종목코드의 '오늘 발행' 비로봇 뉴스 URL 전수 (causal_chain 무관).

    2026-06-03 대표 catch (DOC-20260603, ROOT 2 URL slide):
    - 기존 input_hash payload 의 `news` 리스트는 `_fetch_news_rows()` 결과 = `causal_chain IS NULL` 한정.
    - 동일 뉴스셋이라도 직전 fire 에서 일부 처리(causal_chain 적재) → 다음 fire 시 미처리 잔여만 가져옴
      → URL list 변동 → input_hash MISS → 재해석 cascade.
    - 005935 18회 재해석 본질 (b20b3c3 동형 패턴: price_context 변동성 → URL set 변동성).
    Fix: input_hash 용 URL 집합은 처리 완료 여부와 무관하게 "오늘 발행 뉴스 전수" 로 산출
         → 동일 뉴스셋이면 URL 집합 안정 → HIT 정상.
    LLM 호출 입력(`_fetch_news_rows`) 은 그대로 = 비용/분량 불변.
    """
    code = to_sqlite_text(code)
    today = (
        to_sqlite_text(target_date)
        if target_date
        else datetime.now().strftime("%Y-%m-%d")
    )
    rows = conn.execute(
        """SELECT DISTINCT url FROM news
           WHERE stock_code=? AND date(published_at)=?
             AND COALESCE(is_robot, 0) = 0
             AND url IS NOT NULL AND url != ''""",
        (code, today),
    ).fetchall()
    return sorted([r["url"] for r in rows if r["url"]])


def _fetch_robot_news_rows(conn, code: str, limit: int, target_date: str = None):
    """P1a 측정용 — DB is_robot=1 행을 메모리 합류용으로 fetch (DB 미변경).

    _fetch_news_rows는 is_robot=0만 가져오므로 DB is_robot=1로 사전 마킹된
    [특징주] 호재 등은 type 판정 자체를 못 받는다. 측정 완전성을 위해 robot 행도
    가져와 LLM 입력(robot_block)에 넣고 article_type write 대상이 되게 한다.
    build_daily는 is_robot=0만 읽으므로(L1955/1981) 라이브 정렬 영향 0.
    """
    if target_date:
        return conn.execute(
            """SELECT title, url, source, published_at FROM (
                 SELECT *, ROW_NUMBER() OVER (PARTITION BY title ORDER BY published_at DESC) AS rn
                 FROM news WHERE stock_code=? AND date(published_at)=?
                 AND COALESCE(is_robot, 0) = 1
               ) WHERE rn = 1
               ORDER BY published_at DESC LIMIT ?""",
            (code, target_date, limit),
        ).fetchall()
    return conn.execute(
        """SELECT title, url, source, published_at FROM (
             SELECT *, ROW_NUMBER() OVER (PARTITION BY title ORDER BY published_at DESC) AS rn
             FROM news WHERE stock_code=? AND article_type IS NULL
             AND COALESCE(is_robot, 0) = 1
           ) WHERE rn = 1
           ORDER BY published_at DESC LIMIT ?""",
        (code, limit),
    ).fetchall()


def _fetch_stock_news(code: str, limit: int = 10, target_date: str = None):
    # FLR-009 후속: limit 5→10 — 미처리 뉴스가 처리 완료 뉴스에 밀려 LIMIT 초과로 누락되는 이슈 완화.
    with connect() as conn:
        stock = conn.execute("SELECT * FROM stocks WHERE code=?", (code,)).fetchone()
        news = _fetch_news_rows(conn, code, limit, target_date)

        # P1a 측정 모드 (ARTICLE_TYPE_MEASURE=1) — DB is_robot=1 행도 메모리 합류해
        # type 판정 대상에 포함. 라이브 cron 기본 동작은 불변(게이트 off 시 0건).
        if os.environ.get("ARTICLE_TYPE_MEASURE") == "1":
            seen = {n["url"] for n in news if n["url"]}
            for rb in _fetch_robot_news_rows(conn, code, limit, target_date):
                if rb["url"] and rb["url"] not in seen:
                    news.append(rb)
                    seen.add(rb["url"])

        # DOC-20260530 우선주 보강 — 우선주이고 자체 비로봇 뉴스가 부족하면
        # 본주 뉴스를 후보로 메모리에서 합친다 (DB 복사 INSERT 없음, 원문 오염 방지).
        # 본주 뉴스는 interpret 프롬프트의 우선주 컨텍스트와 함께 우선주 관점으로 재해석된다.
        if stock and len(news) < _MIN_NEWS_FOR_ANALYSIS:
            base_code = get_base_code(code, stock["name"])
            if base_code:
                base_news = _fetch_news_rows(conn, base_code, limit, target_date)
                if base_news:
                    # 이미 우선주 자체 뉴스가 있으면 보존 + 본주 뉴스 append (URL 중복 제거)
                    seen_urls = {n["url"] for n in news if n["url"]}
                    merged = list(news)
                    for bn in base_news:
                        if bn["url"] and bn["url"] in seen_urls:
                            continue
                        merged.append(bn)
                        if len(merged) >= limit:
                            break
                    news = merged
    return stock, news


def interpret(code: str, target_date: str = None, ignore_cache: bool = False):
    # FLR-005 보강 v3: 모든 sqlite binding 파라미터 진입부 정규화
    # (line 452 InterfaceError 12회 재발 → code 자체가 비정상 타입 가능성)
    code = to_sqlite_text(code)
    if target_date is not None:
        target_date = to_sqlite_text(target_date)
    if _today_usage() >= DAILY_CALL_LIMIT:
        print(f"[{code}] daily cap reached, skip")
        return None
    stock, news = _fetch_stock_news(code, target_date=target_date)
    if not stock:
        return None
    # REQ-028 W2 — industry_seeds 직매핑 (LLM 미경유, SPEC-001 §VII.4).
    # 뉴스 유무 무관 항시 적용 (본질: 뉴스 없는 종목도 산업 테마 부착).
    snap_date_seed = to_sqlite_text(target_date or datetime.now().strftime("%Y-%m-%d"))
    industry_str = stock["industry"] or "미분류"
    seed_attached = _apply_industry_seeds(code, industry_str, snap_date_seed)
    if seed_attached:
        print(f"[{code}] industry_seeds: {seed_attached} (industry='{industry_str}')")
    # Q-20260512-FRESH-LISTING-BADGE — 신규상장 자동 부착 (6개월 휴리스틱)
    # dailybars 첫 row date 기준, backfill 일자 제외. 뉴스 유무 무관 항시 평가.
    fresh_attached = _apply_fresh_listing(code, snap_date_seed)
    if fresh_attached:
        print(f"[{code}] fresh_listing: {fresh_attached}")
    # DOC-20260530-REQ-002 — 대표 권한 force override 적용 (deny는 link_stock_theme
    # 가드가 처리). 자동 부착(seeds/fresh) 이후 마지막에 적용. 뉴스 유무 무관 항시
    # 적용해야 하므로 'if not news' 조기 반환 이전에 호출.
    owner_forced = apply_owner_overrides(code, snap_date_seed)
    if owner_forced:
        print(f"[{code}] owner_overrides force: {owner_forced}")
    # 뉴스 부재 시: industry_seeds + fresh_listing + owner force만 부착하고 LLM 스킵
    if not news:
        return None
    # 누적 테마 조회 (stock_themes 단일 소스, industry_seeds 결과 포함)
    # REQ-076 Phase 4-mini — retired_v3 행은 LLM 프롬프트 입력 제외
    # (이미 V3가 부적합으로 판정한 테마를 다시 컨텍스트에 노출하면 재부착 유도 가능)
    with connect() as conn:
        st_rows = conn.execute(
            """SELECT t.name FROM stock_themes st
               JOIN themes t ON st.theme_id = t.id
               WHERE st.stock_code = ?
                 AND COALESCE(st.source, '') != 'retired_v3'""",
            (code,),
        ).fetchall()
    all_themes = [r["name"] for r in st_rows]

    # REQ-076 Phase 2-B — PROMPT 버전 분기 (default V3)
    prompt_version = os.environ.get("INTERPRET_PROMPT_VERSION", "V3").upper()
    canonical = _load_theme_dictionary()

    # 가격 컨텍스트 (대표 제안 2026-05-31) — PROMPT 본문에만 주입, input_hash 제외.
    # 2026-06-01 대표 catch (DOC-20260601): 가격을 input_hash 에 포함했더니 cron 10분
    # 주기마다 장중 가격 변동 → MISS → 재해석 cascade → 본문 themes·causal_chain 변동
    # → 사이트 신뢰도 저하. 본 fix 후 = 신규 의미 뉴스(URL 셋 변동) 추가될 때만 MISS.
    price_context = _build_price_context(code, target_date)
    # trend_context 폐기 (2026-06-01) — 함수 정의 자체 제거, 호출/주입 0건.

    if prompt_version == "V3":
        # 본문 우선 + 로봇 후순위 분류
        primary_block, secondary_block, robot_block = _build_news_block_with_body(
            news, max_primary=5
        )
        # canonical 카탈로그 (parents 메타 포함)
        theme_catalog = _build_theme_catalog()
        sector_str = (
            stock["sector"] if "sector" in stock.keys() and stock["sector"] else ""
        )
        prompt = PROMPT_TEMPLATE_V3.format(
            name=stock["name"],
            code=code,
            industry=stock["industry"] or "미분류",
            sector=sector_str or "(미상)",
            themes=json.dumps(all_themes, ensure_ascii=False) if all_themes else "[]",
            price_context=price_context,
            primary_block=primary_block,
            secondary_block=secondary_block,
            robot_block=robot_block,
            theme_catalog=theme_catalog,
        )
    else:
        # V1 fallback (env INTERPRET_PROMPT_VERSION=V1)
        news_block = "\n".join(
            f"- {n['title']} · {n['source']} · {n['url']}" for n in news
        )
        bodies = []
        for n in news[:3]:
            url = n["url"] if n["url"] else ""
            if url:
                body = fetch_article_body(url)
                if body and len(body) > 100:
                    bodies.append(f"[{n['title'][:40]}] {body}")
        if bodies:
            body_notice = (
                "\n(일부 기사 본문 발췌가 포함되어 있다. 제목과 본문을 모두 참고하라.)"
            )
            body_block = "\n본문 발췌:\n" + "\n---\n".join(bodies) + "\n"
        else:
            body_notice = ""
            body_block = ""
        theme_list_str = (
            ", ".join(canonical)
            if canonical
            else "(사전 로드 실패 — 기존 누적 테마 참조)"
        )
        prompt = PROMPT_TEMPLATE.format(
            name=stock["name"],
            code=code,
            industry=stock["industry"] or "미분류",
            themes=json.dumps(all_themes, ensure_ascii=False) if all_themes else "[]",
            news_block=news_block,
            body_notice=body_notice,
            body_block=body_block,
            theme_list=theme_list_str,
        )

    # DOC-20260530 우선주 컨텍스트 주입 (V1/V3 공통) — 본주 뉴스를 우선주 관점으로
    # 재해석하도록 강제. 본주 해석 단순 복사 차단 (FLR-AGT-002 거짓 충실성 방지).
    base_code_for_ctx = get_base_code(code, stock["name"])
    if base_code_for_ctx:
        with connect() as _conn:
            _base_row = _conn.execute(
                "SELECT name FROM stocks WHERE code=?", (base_code_for_ctx,)
            ).fetchone()
        base_name_for_ctx = _base_row["name"] if _base_row else base_code_for_ctx
        pref_ctx = build_preferred_context(
            code, stock["name"], base_code_for_ctx, base_name_for_ctx
        )
        prompt = pref_ctx + prompt

    # few-shot 컨텍스트 동봉 (V1/V3 공통)
    fewshot = build_news_fewshot_context(limit=5)
    if fewshot:
        prompt = fewshot + "\n---\n" + prompt

    # === 1차: 이시카와 (Sonnet) ===
    # input_hash = code + 오늘 발행 뉴스 URL 전수 + 누적 테마 + SCHEMA_TAG (재현성 보장)
    # disclosure rcept_no는 fetch_stock_news가 가져오지 않으므로 url 집합으로 대체.
    # P1a (FLR-20260511-DAT-002 schema validation 정합): 출력 스키마가 articles[]로
    # 확장되었으므로 PROMPT_SCHEMA_TAG를 hash payload에 포함 → 기존(구 스키마) 캐시 자동
    # 무효화(input_hash 변동 → MISS). 스키마 변경 시 태그만 bump 하면 캐시 자연 invalidate.
    # input_hash 정책 (2026-06-01 대표 catch DOC-20260601 — 신규 의미 뉴스만 재해석):
    # 포함 = code + 정렬 뉴스 URL + 정렬 누적 테마 + SCHEMA_TAG
    # 제외 = price_context (PROMPT 본문 주입만, 가격 변동 무관 캐시 HIT)
    # 제외 = trend_context (폐기됨, 함수 정의 0건)
    # → 동일 뉴스셋 + 동일 누적테마면 가격·거래대금이 어떻게 변하든 HIT (재해석 0건).
    # → 신규 뉴스(URL 추가) 또는 누적테마 갱신 시만 MISS = 재해석.
    #
    # 2026-06-03 대표 catch (DOC-20260603, ROOT 2 URL slide) — V3d → V3e bump:
    # 기존 payload 의 `news` 는 `_fetch_news_rows()` 결과 = `causal_chain IS NULL` 한정.
    # 동일 뉴스셋이라도 직전 fire 가 일부를 처리하면 (causal_chain 적재) 다음 fire 시
    # 미처리 잔여만 가져와 URL list 변동 → input_hash MISS → 재해석 cascade.
    # 005935 18회 재해석 본질 = URL slide (b20b3c3 동형 패턴, price_context 변동성 → URL 변동성).
    # Fix: input_hash 용 URL 집합은 `_fetch_today_news_urls()` 로 "오늘 발행 비로봇 뉴스 URL 전수"
    #      를 별도 SELECT (processed 여부 무관, LLM 호출 분량 불변).
    #      → 동일 뉴스셋이면 URL 집합 안정 → 처리 완료 후 다음 fire 자연 HIT.
    with connect() as _hash_conn:
        today_urls = _fetch_today_news_urls(_hash_conn, code, target_date)
    ish_hash = hash_input(
        [
            code,
            today_urls,
            sorted(all_themes),
            PROMPT_SCHEMA_TAG,
        ]
    )
    # P1a 측정 모드는 robot 합류로 입력이 커지고 opus가 느릴 수 있어 timeout 상향.
    _ish_timeout = 150 if os.environ.get("ARTICLE_TYPE_MEASURE") == "1" else 60
    text = call_model_cached(
        prompt,
        ISHIKAWA_MODEL,
        domain="ishikawa_news",
        target_id=code,
        input_hash=ish_hash,
        agent="ishikawa",
        timeout=_ish_timeout,
        ignore_cache=ignore_cache,
    )
    if not text:
        print(f"[{code}] ishikawa FAIL")
        return None

    parsed = extract_json(text)
    if not parsed:
        print(f"[{code}] JSON parse FAIL")
        return None

    # REQ-076 Phase 2-B — V3 응답 정규화 + parents 추출
    # V3 출력: themes=[{"name":"...","parents":["..."]}, ...] + macro_events=[...]
    # V1 호환을 위해 themes는 string list로 평탄화하고, parents 매핑은 별도 dict로 보존.
    #
    # REQ-078 (FLR-TEC-002 §1) — V3 신뢰 정책 도입.
    #
    # REQ-079 (옵션 C) — V3 schema 가드 완화 + audit 마커 도입.
    # 변경 (FLR-TEC-002 §1 본질 fix 2단계):
    # - 전체 string fallback일 때 schema_fatal로 차단 → schema_legacy_audit으로 부착 진행.
    # - 차단 사유: 라이브 chip 0건 부작용 (대원전선 4/29 케이스). string list여도 V1 동등 부착이
    #   schema_fatal 차단보다 가치 ↑ (기존 V1 정책 = 부착 진행).
    # - 단, 혼합/비정상 형식(dict와 string 혼합)이 아닌 한 부착 진행. 비정상은 schema_fatal 잔존.
    parsed_theme_parents: dict[str, list[str]] = {}
    v3_schema_skip = False  # 라이브 부착 차단 플래그 (혼합 비정상 시만 True)
    v3_legacy_audit = False  # REQ-079 — string list fallback 시 audit 마커
    if prompt_version == "V3" and isinstance(parsed.get("themes"), list):
        flattened: list[str] = []
        schema_violations: list[str] = []
        string_fallback_count = 0
        total_items = 0
        non_string_items = 0  # dict 또는 valid 항목 수 (혼합 감지용)
        for item in parsed["themes"]:
            total_items += 1
            if isinstance(item, dict) and item.get("name"):
                tname = item["name"]
                pars = item.get("parents")
                if isinstance(pars, list):
                    parsed_theme_parents[tname] = [
                        p for p in pars if isinstance(p, str) and p.strip()
                    ]
                else:
                    # parents 키 누락/비-list — schema 위반. parents=[] 처리하고 로그.
                    parsed_theme_parents[tname] = []
                    schema_violations.append(f"{tname}(parents missing)")
                flattened.append(tname)
                non_string_items += 1
            elif isinstance(item, str) and item.strip():
                # string list fallback — V3 schema 위반. parents 정보 손실.
                # REQ-079 — 부착은 진행 (V1 동등). audit 마커로 추적.
                parsed_theme_parents[item] = []
                schema_violations.append(f"{item}(string fallback)")
                string_fallback_count += 1
                flattened.append(item)
            else:
                # None / 빈 string / 비정상 타입 — 무시 + 로그
                schema_violations.append(f"<malformed item: {type(item).__name__}>")
        parsed["themes"] = flattened
        if schema_violations:
            logger.warning(
                f"[{code}] V3 schema 위반 {len(schema_violations)}건 — "
                f"parents 정보 손실. 위반: {schema_violations[:5]}"
            )
        # REQ-079 — 임계 판정 재정의:
        # (1) total_items=0: 빈 themes (LLM 실제로 부착 없음) → 정상 처리, 부착 0건
        # (2) total_items>0 AND non_string_items=0 AND string_fallback_count=total_items
        #     → 전체 string list (V1 fallback). audit 마커 + 부착 진행. (기존 fatal → legacy_audit)
        # (3) total_items>0 AND non_string_items>0 AND string_fallback_count=0
        #     → 정합 V3 dict (정상 경로)
        # (4) total_items>0 AND non_string_items>0 AND string_fallback_count>0
        #     → 혼합 형식 (비정상). schema_fatal 차단 — LLM이 일관성 없는 응답.
        # (5) total_items>0 AND non_string_items=0 AND string_fallback_count=0
        #     → 모두 malformed (None/빈 string). schema_fatal 차단.
        if total_items > 0 and non_string_items > 0 and string_fallback_count > 0:
            # 혼합 — 비정상. 차단 (LLM 출력 일관성 없음 → 신뢰 불가)
            v3_schema_skip = True
            logger.error(
                f"[{code}] V3 schema FATAL — 혼합 형식 ({non_string_items} dict + "
                f"{string_fallback_count} string). LLM 출력 불일치 → 라이브 부착 skip."
            )
        elif total_items > 0 and non_string_items == 0 and string_fallback_count == 0:
            # 모두 malformed
            v3_schema_skip = True
            logger.error(
                f"[{code}] V3 schema FATAL — 전체 {total_items}건 malformed. 부착 skip."
            )
        elif (
            total_items > 0
            and non_string_items == 0
            and string_fallback_count == total_items
        ):
            # 전체 string list — V1 동등 fallback 부착 (REQ-079 옵션 C).
            v3_legacy_audit = True
            logger.warning(
                f"[{code}] V3 schema legacy — 전체 {total_items}건 string list. "
                f"V1 동등 부착 진행 (audit='schema_legacy_audit'). themes={flattened[:5]}"
            )
        # macro_events 배열을 V1 호환 macro_event 단일 필드로 추출 (첫 항목)
        macro_events = parsed.get("macro_events") or []
        if (
            isinstance(macro_events, list)
            and macro_events
            and not parsed.get("macro_event")
        ):
            parsed["macro_event"] = macro_events[0]

    # === 2차: 토구사 audit (V3 신뢰 정책 — REQ-078 / FLR-TEC-002 / REQ-079) ===
    # 정책 변경 (옵션 A): verdict='bad'여도 V3 themes 부착 진행. 토구사는 audit log만 기록.
    # 근거: V3 PROMPT 자체가 "본문 외 추가 금지" 조항 포함 + canonical 매처가 외삽 차단 + reasoning 통과.
    # 토구사의 보수적 재해석은 V3 효과를 0으로 만드는 본질 결함(FLR-TEC-002)이었음.
    # 단, V3 schema fatal (혼합/malformed)일 때는 skip — 라이브 부착 마지막 가드.
    # REQ-079 — schema_legacy_audit 마커: string list fallback 시 verdict 마킹 (부착은 진행).
    titles = [n["title"] for n in news]
    today = target_date or datetime.now().strftime("%Y-%m-%d")

    # V3 schema fatal — 부착 skip (옵션 A의 마지막 가드, 혼합/malformed 한정)
    if v3_schema_skip:
        with connect() as rc:
            _save_news_review(
                rc,
                today,
                code,
                "ishikawa",
                titles,
                parsed,
                "schema_fatal",
                "v3_schema_guard",
                "themes 비정상 형식 (혼합 또는 malformed) — V3 schema 미준수",
            )
            rc.commit()
        print(f"[{code}] V3 schema fatal — stock_themes 부착 skip")
        return parsed

    tog_eval = _togusa_review_news(
        stock["name"], code, titles, parsed, ignore_cache=ignore_cache
    )

    # REQ-078 (FLR-TEC-002 §1) — V3 신뢰 정책: verdict 무관 audit log만 기록.
    # REQ-079 — v3_legacy_audit가 True면 verdict를 'schema_legacy_audit'로 마킹.
    # 부착 skip 분기 제거. 후속 stock_themes UPSERT/retire 사이클은 verdict 무관 진행.
    tog_verdict = (tog_eval or {}).get("verdict")
    tog_reason = (tog_eval or {}).get("reason") or (tog_eval or {}).get("note")
    # REQ-079 — schema_legacy_audit 우선 (V3 schema fallback 추적)
    # legacy_audit 시에는 togusa verdict 무관 'schema_legacy_audit' 마커 + reason 보존.
    if v3_legacy_audit:
        with connect() as rc:
            legacy_reason = (
                f"V3 schema legacy fallback (string list). togusa_verdict={tog_verdict}; "
                f"togusa_reason={(tog_reason or '')[:150]}"
            )
            _save_news_review(
                rc,
                today,
                code,
                "ishikawa",
                titles,
                parsed,
                "schema_legacy_audit",
                "v3_schema_guard",
                legacy_reason,
            )
            rc.commit()
    elif tog_verdict == "bad":
        # AUDIT ONLY — V3 신뢰 정책. 부착은 진행하되 reason 기록.
        logger.warning(
            f"[togusa audit] verdict=bad but proceed (V3 trust policy). "
            f"code={code} reason={(tog_reason or '')[:200]}"
        )
        with connect() as rc:
            _save_news_review(
                rc,
                today,
                code,
                "ishikawa",
                titles,
                parsed,
                "bad_audit",  # 기존 'bad'와 구분 — 부착 진행됨을 명시
                "togusa",
                tog_reason,
            )
            rc.commit()
    elif tog_verdict == "good":
        with connect() as rc:
            _save_news_review(
                rc, today, code, "ishikawa", titles, parsed, "good", "togusa", None
            )
            rc.commit()
    else:
        with connect() as rc:
            _save_news_review(
                rc, today, code, "ishikawa", titles, parsed, "pending", None, tog_reason
            )
            rc.commit()

    # 모든 뉴스 행에 causal_chain 주입 — 모듈 레벨 _to_text() 사용 (FLR-005 보강)
    # DOC-20260530 우선주 보강: 우선주이면 본주에서 끌어온 후보 뉴스의 url이 우선주
    # 코드 행으로 존재하지 않을 수 있다. 그 경우 우선주 코드로 news 행을 INSERT한다
    # (제목+url+source만 — 원문 본문 저장 금지, 기존 정책 유지). causal_chain은
    # 우선주 관점으로 재해석된 parsed 결과. match_source='preferred_base'로 출처 명시.
    _is_pref = get_base_code(code, stock["name"]) is not None
    # P1a — articles[] url→type 맵. url 미매핑/판정 누락 시 NULL fallback (정렬 미반영 shadow).
    _article_types = _parse_article_types(parsed)
    # Q-167 ② — magnitude/virality/score (종목 단위, 오늘 뉴스 종합). 산출 불가 축은
    # None → NULL 저장 → build_daily 'newzy_score DESC' 정렬에서 자연 후순위 (거짓 0.5
    # 채움 금지, FLR-AGT-002). 종목 단위 점수이므로 본 종목의 news 전 행에 동일 부여.
    _mag, _vir, _score = _parse_newzy_score(parsed)
    with connect() as conn:
        _ensure_preferred_columns(conn)
        for n in news:
            _atype = _article_types.get(n["url"]) if n["url"] else None
            cur = conn.execute(
                """UPDATE news SET causal_chain=?, macro_event=?, evidence_span=?,
                       article_type=?, newzy_magnitude=?, newzy_virality=?,
                       newzy_score=?
                   WHERE stock_code=? AND url=?""",
                (
                    _to_text(parsed.get("causal_chain")),
                    _to_text(parsed.get("macro_event")),
                    _to_text(parsed.get("evidence_span")),
                    _atype,
                    _mag,
                    _vir,
                    _score,
                    code,
                    n["url"],
                ),
            )
            # 우선주 + 해당 url이 우선주 코드 행에 없음 = 본주 후보 뉴스. 우선주 행 신규 적재.
            if _is_pref and cur.rowcount == 0 and n["url"]:
                key = idempotency_key_pref(code, n["url"])
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO news(stock_code, title, url, published_at,
                               source, causal_chain, macro_event, evidence_span,
                               collected_at, idempotency_key, is_robot, match_source,
                               article_type, newzy_magnitude, newzy_virality, newzy_score)
                           VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'preferred_base',
                               ?, ?, ?, ?)""",
                        (
                            code,
                            n["title"],
                            n["url"],
                            n["published_at"],  # 본주 원본 발행일 보존
                            n["source"],
                            _to_text(parsed.get("causal_chain")),
                            _to_text(parsed.get("macro_event")),
                            _to_text(parsed.get("evidence_span")),
                            datetime.now().isoformat(),
                            key,
                            _atype,
                            _mag,
                            _vir,
                            _score,
                        ),
                    )
                except Exception as e:
                    logger.warning(
                        f"[{code}] preferred news insert FAIL url={n['url']} err={e}"
                    )
        # 마스터 테마 업데이트 — stock_themes 누적 기반
        # canonical_set: canonical 빈 경우 None (사전 로드 실패 fallback) — 후속 분기에서 None-safe
        canonical_set: set | None = set(canonical) if canonical else None
        if parsed.get("themes"):
            # LLM 응답 테마를 정규화 (양자기술→양자 등 alias 통합)
            parsed["themes"] = normalize_list(parsed["themes"])
            # 사전에 없는 테마 필터링 (LLM이 프롬프트 무시하고 자유 생성한 것 거부)
            if canonical_set is not None:
                rejected = [t for t in parsed["themes"] if t not in canonical_set]
                if rejected:
                    print(f"[{code}] 사전 외 테마 거부: {rejected}")
                parsed["themes"] = [t for t in parsed["themes"] if t in canonical_set]
            # stock_themes 단일 소스 — themes_json UPDATE 제거
            # FLR-005 보강 v3: 모든 binding 파라미터 to_sqlite_text (방어적)
            conn.execute(
                "UPDATE stocks SET last_updated=? WHERE code=?",
                (datetime.now().isoformat(), to_sqlite_text(code)),
            )
            # stock_theme_daily 일별 스냅샷 기록
            snap_date = to_sqlite_text(
                target_date or datetime.now().strftime("%Y-%m-%d")
            )
            # REQ-076 Phase 2-B 재작업 (FLR-TEC-002 §3) — V3 부착 theme_id 수집 (retire용)
            attached_theme_ids: list[int] = []
            attach_source = "ishikawa_v3" if prompt_version == "V3" else "ishikawa"
            for theme_name in parsed["themes"]:
                theme_name_safe = to_sqlite_text(theme_name)
                if theme_name_safe is None:
                    continue
                theme_row = conn.execute(
                    "SELECT id FROM themes WHERE name=?", (theme_name_safe,)
                ).fetchone()
                # [Q-20260511-FIX-B-1-CALLCHAIN-FIX] C7 — link_stock_theme이
                # raise ValueError (비활성 canonical) 또는 일반 Exception을 던지면
                # interpret() 전체 abort 발생 (BG 22:10 사고). try/except wrap +
                # link 성공 시에만 stock_theme_daily INSERT (audit 권고).
                if theme_row:
                    # 사전 commit — link_stock_theme은 새 connect를 열기 때문
                    # (REQ-076 Phase 2-B writer lock release).
                    conn.commit()
                    try:
                        link_stock_theme(
                            to_sqlite_text(code),
                            theme_name_safe,
                            snap_date,
                            attach_source,
                        )
                        # link 성공 시에만 stock_theme_daily INSERT (정합성)
                        conn.execute(
                            """INSERT OR IGNORE INTO stock_theme_daily
                               (date, stock_code, theme_id, source, created_at)
                               VALUES (?, ?, ?, 'ishikawa', ?)""",
                            (
                                snap_date,
                                to_sqlite_text(code),
                                theme_row["id"],
                                datetime.now().isoformat(),
                            ),
                        )
                    except ValueError as link_err:
                        # 비활성 canonical — skip 명시 + WARN 로그 (silent X)
                        logger.warning(
                            f"[{code}] inactive canonical skip "
                            f"theme={theme_name_safe} reason={link_err}"
                        )
                        continue
                    except Exception as link_err:
                        logger.warning(
                            f"[{code}] link FAIL theme={theme_name_safe} err={link_err}"
                        )
                        continue
                else:
                    # theme_row 없음 — ensure_theme 경유 신규 부착 시도
                    conn.commit()
                    try:
                        link_stock_theme(
                            to_sqlite_text(code),
                            theme_name_safe,
                            snap_date,
                            attach_source,
                        )
                    except ValueError as link_err:
                        logger.warning(
                            f"[{code}] inactive canonical skip "
                            f"theme={theme_name_safe} reason={link_err}"
                        )
                        continue
                    except Exception as link_err:
                        logger.warning(
                            f"[{code}] link FAIL theme={theme_name_safe} err={link_err}"
                        )
                        continue
                # 부착 후 theme_id 재조회 (link_stock_theme에서 ensure_theme이 신규 INSERT 가능)
                # 새로 conn 열어 LOCKED 회피
                attached_row = conn.execute(
                    "SELECT id FROM themes WHERE name=?", (theme_name_safe,)
                ).fetchone()
                if attached_row:
                    attached_theme_ids.append(attached_row["id"])
                # REQ-076 Phase 2-B — V3가 식별한 parents가 dictionary 메타와 다르면
                # ensure_theme(parents=...)로 theme_parents 누적 부착. canonical 외 parent는 거부.
                if prompt_version == "V3" and theme_name in parsed_theme_parents:
                    extra_parents = [
                        p
                        for p in parsed_theme_parents[theme_name]
                        if isinstance(p, str)
                        and p.strip()
                        and (canonical_set is None or p in canonical_set)
                    ]
                    if extra_parents:
                        try:
                            from .theme_normalizer import ensure_theme as _ensure_theme

                            _ensure_theme(theme_name, parents=extra_parents)
                        except Exception as e:
                            logger.warning(
                                f"[{code}] V3 parents 부착 FAIL theme={theme_name} "
                                f"parents={extra_parents} err={e}"
                            )
            # REQ-076 Phase 2-B 재작업 (FLR-TEC-002 §3) — V3 재해석 시 stale stock_themes retire
            # V3 응답에 포함되지 않은 기존 LLM 부착 행을 source='retired_v3'로 마킹.
            # 라이브 chip은 build_daily JSON 출력 시 source 필터로 제외 (Phase 4 영역).
            if prompt_version == "V3" and attached_theme_ids:
                retired_n = _retire_stale_stock_themes(
                    conn, code, attached_theme_ids, retire_source="retired_v3"
                )
                if retired_n:
                    print(
                        f"[{code}] V3 재해석 stale retire: {retired_n}건 "
                        f"(source=retired_v3, attached={len(attached_theme_ids)}건)"
                    )
        conn.commit()

    # REQ-028 W3+W5 — 1-패스 빈 themes + industry_seeds 부착 0건 시 폴백.
    # 가드 (개발팀 비판 보강): seed_attached 또는 1-패스 themes가 있으면 폴백 스킵 (LLM 콜 절감).
    one_pass_themes = parsed.get("themes") or []
    if not one_pass_themes and not seed_attached:
        sector_str = stock["sector"] if "sector" in stock.keys() else ""
        _interpret_structural(
            code, industry_str, sector_str, snap_date_seed, ignore_cache=ignore_cache
        )
    return parsed


def _interpret_structural(
    code: str,
    industry: str,
    sector: str,
    snap_date: str,
    ignore_cache: bool = False,
) -> list[str]:
    """REQ-028 W3 — 1-패스 빈 themes 종목 LLM 폴백 (SPEC-001 §VII.4).

    뉴스 무관, 항시 활성 구조 테마 1~3개를 canonical 116개에서 선택.
    외삽·환각 금지. 산업분류와 직접 일치만. canonical 외 거부.
    Returns: 부착된 canonical 테마명 리스트.
    """
    canonical = _load_theme_dictionary()
    if not canonical or not industry or industry == "미분류":
        return []
    canonical_set = set(canonical)
    theme_list_str = ", ".join(canonical)
    name_row = None
    with connect() as conn:
        name_row = conn.execute(
            "SELECT name FROM stocks WHERE code=?", (code,)
        ).fetchone()
    name = name_row["name"] if name_row else code

    prompt = (
        f"종목 {name}({code})의 핵심사업: '{industry}/{sector or ''}'.\n"
        f"뉴스 무관, 항시 활성 구조 테마 1~3개를 아래 canonical 목록에서 선택.\n"
        f"외삽·환각 금지. 산업분류와 직접 일치/포함하는 테마만.\n\n"
        f"canonical 테마: {theme_list_str}\n\n"
        f'JSON 형식으로 응답: {{"themes": ["...", "..."]}}\n'
        f"매핑 가능한 테마 없으면 빈 배열 [] 반환."
    )
    struct_hash = hash_input([code, industry, sector or ""])
    text = call_model_cached(
        prompt,
        ISHIKAWA_MODEL,
        domain="ishikawa_structural",
        target_id=code,
        input_hash=struct_hash,
        agent="ishikawa",
        timeout=60,
        ignore_cache=ignore_cache,
    )
    if not text:
        return []
    parsed = extract_json(text)
    if not parsed or not isinstance(parsed.get("themes"), list):
        return []
    raw_themes = normalize_list(parsed["themes"])
    valid = [t for t in raw_themes if t in canonical_set]
    rejected = [t for t in raw_themes if t not in canonical_set]
    if rejected:
        print(f"[{code}] W3 사전 외 테마 거부: {rejected}")
    attached: list[str] = []
    for theme in valid:
        try:
            link_stock_theme(
                to_sqlite_text(code),
                to_sqlite_text(theme),
                snap_date,
                "ishikawa_structural",
            )
            attached.append(theme)
        except Exception as e:
            logger.warning(f"[{code}] W3 link FAIL theme={theme} err={e}")
    if attached:
        print(f"[{code}] structural(W3): {attached}")
    return attached


def togusa_check_missed(
    code: str,
    industry: str,
    sector: str,
    snap_date: str,
    ignore_cache: bool = False,
) -> list[str]:
    """REQ-028 W5 — 토구사 사후 누락 검사 (SPEC-001 §VII.4).

    이시카와 1-패스 + W2 industry_seeds + W3 structural 모두 후에도 빈 themes 종목에
    대해 토구사가 industry/sector + 누적 테마 검토하여 누락 의심 시 추가 부착.
    canonical 외 거부. 빈 결과 시 빈 리스트.
    """
    canonical = _load_theme_dictionary()
    if not canonical:
        return []
    canonical_set = set(canonical)
    with connect() as conn:
        # REQ-076 Phase 4-mini — 토구사 사후 검증도 retire 제외
        cur_rows = conn.execute(
            """SELECT t.name FROM stock_themes st
               JOIN themes t ON st.theme_id = t.id
               WHERE st.stock_code = ?
                 AND COALESCE(st.source, '') != 'retired_v3'""",
            (code,),
        ).fetchall()
        name_row = conn.execute(
            "SELECT name FROM stocks WHERE code=?", (code,)
        ).fetchone()
    cumulative = [r["name"] for r in cur_rows]
    name = name_row["name"] if name_row else code
    theme_list_str = ", ".join(canonical)
    prompt = (
        f"[토구사 사후 검증] 종목 {name}({code}) industry='{industry}' sector='{sector or ''}'.\n"
        f"누적 테마: {cumulative}\n"
        f"누락된 구조 테마가 있는가? 아래 canonical에서 1~2개 선택. 없으면 빈 배열.\n"
        f"외삽·환각 금지. 산업분류와 직접 일치/포함만.\n\n"
        f"canonical 테마: {theme_list_str}\n\n"
        f'JSON 형식: {{"themes": ["..."]}} 또는 {{"themes": []}}'
    )
    miss_hash = hash_input([code, industry, sector or "", sorted(cumulative)])
    text = call_model_cached(
        prompt,
        TOGUSA_MODEL,
        domain="togusa_missed",
        target_id=code,
        input_hash=miss_hash,
        agent="togusa",
        timeout=60,
        ignore_cache=ignore_cache,
    )
    if not text:
        return []
    parsed = extract_json(text)
    if not parsed or not isinstance(parsed.get("themes"), list):
        return []
    raw_themes = normalize_list(parsed["themes"])
    valid = [t for t in raw_themes if t in canonical_set]
    attached: list[str] = []
    for theme in valid:
        if theme in cumulative:
            continue  # 이미 부착된 테마 중복 방지
        try:
            link_stock_theme(
                to_sqlite_text(code),
                to_sqlite_text(theme),
                snap_date,
                "togusa_recovery",
            )
            attached.append(theme)
        except Exception as e:
            logger.warning(f"[{code}] W5 link FAIL theme={theme} err={e}")
    if attached:
        print(f"[{code}] togusa_missed(W5): {attached}")
    return attached


def interpret_top_volume(codes: list):
    """거래대금 리스트 전량 순회."""
    results = {}
    for code in codes:
        r = interpret(code)
        if r:
            results[code] = r
    return results


if __name__ == "__main__":
    import os
    import sys

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    ignore_cache = "--ignore-cache" in flags
    only_all = "--all" in flags or not args

    if args and not only_all:
        interpret(args[0], ignore_cache=ignore_cache)
    else:
        # PIPELINE_DATE 환경변수 또는 오늘 날짜
        from datetime import date as _date

        target = os.environ.get("PIPELINE_DATE") or _date.today().isoformat()

        with connect() as conn:
            codes = [
                r["stock_code"]
                for r in conn.execute(
                    "SELECT DISTINCT stock_code FROM daily_picks WHERE date=? AND source='kiwoom'",
                    (target,),
                ).fetchall()
            ]
        if not codes:
            print(f"daily_picks에 {target} kiwoom 데이터 없음")
        else:
            print(f"[interpret] {target} {len(codes)}종목 해석 시작")
            for code in codes:
                try:
                    r = interpret(code, target_date=target, ignore_cache=ignore_cache)
                    status = "OK" if r else "SKIP"
                    print(f"[{code}] {status}")
                except Exception as e:
                    print(f"[{code}] ERROR: {e}")
        # 캐시 stats 출력
        from .llm_client import cache_stats

        print(f"[cache] {cache_stats()}")
