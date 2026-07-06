"""미국 야간 미장요약(US overnight digest) JSON 빌더 — Q-20260605-103 Phase 3.

수집(collect_us_indices) → 미장 RSS 뉴스 → LLM 한국어 요약(news_chips) → JSON 빌드 →
homepage 산출물 `data/us-indices/{kstDate}.json` 으로 배포.

설계 계약 schema (프론트 병렬 구현 — 임의 변경 금지, 필드 추가만 허용):
  {trade_date_local, indices:[{name,point,change_pct,spark,candle:{o,h,l,c}}],
   news_chips:[{summary, source, url}]}

핵심 분리 원칙 (오염 금지, 최소 침습 — DSN-arch-pipeline §7.12 박제):
  - 미장 매크로는 기존 '오늘의 뉴스요약'(국내 macro_events / build_daily) 과 **완전 분리**.
    macro_events 테이블 touch 0건. 별도 산출물(us-indices JSON) 에만 기록.
  - 뉴스요약 로직은 기존 extract_macros LLM 프롬프팅 패턴 + _dedup_and_rank_macros
    재사용 (신규 엔진 금지, 대표 확정). 본 모듈은 US 전용 프롬프트로 호출만 분리.

법무 4조건 (조건부 진행 승인, 2026-06-05):
  ① news_chips.summary = LLM 자체 생성 한국어만 (RSS 원문 verbatim 노출 0건)
  ② source = 약어 + url = 원문 딥링크 필수
  ③ 아침 1회 빌드 (entry 스크립트 07:00 plist)
  ④ 매매 권유 표현 금지 (프롬프트 명시)

사용:
  python3 -m scripts.news_pipeline.build_us_digest          # 수집+빌드+배포
  python3 -m scripts.news_pipeline.build_us_digest --dry-run # JSON stdout 만 (배포 X)
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from .collect_us_futures import collect as collect_futures
from .collect_us_indices import (
    US_INDEX_TARGETS,
    fetch_intraday,
    is_us_regular_open,
    parse_intraday,
    parse_intraday_quote,
)
from .collect_us_indices import collect as collect_indices
from .config import (
    HK_GLOBAL_NAVER_QUERY,
    HOMEPAGE,
    RSS_FEEDS_HK_GLOBAL,
    RSS_FEEDS_US_DIGEST,
)
from .llm_client import call_model_cached, hash_input

OUT_DIR = HOMEPAGE / "data" / "us-indices"

# 미장 요약 모델 — 기존 매크로와 동일 haiku (extract_macros.MACRO_MODEL 정합)
US_DIGEST_MODEL = "haiku"

# LLM 입력 최대 기사 수 (description 포함 → title-only 대비 토큰 多, 보수적 상한)
MAX_NEWS_FOR_LLM = 60  # 2026-06-15: 40→60 (소스 균형 정렬 후 WSJ세계 등 탈락 방지)

# 최종 news_chips 개수 상한. 프론트는 첫 5개만 기본 노출하고 나머지는 더보기로 접는다.
MAX_NEWS_CHIPS = 10
NEWS_CHIP_SIGNATURE_VERSION = "news_chips_v3_ts_impact"

# 법무 ④ 매매 권유 금지 + ① 원문 verbatim 금지 명시 프롬프트.
US_DIGEST_PROMPT_TEMPLATE = """미국 야간 미장 마감 요약(이시카와). JSON 배열만.

## 미장 뉴스 (제목 + 요약, 출처)
{news_block}

## 원칙
- 한국 투자자 관점의 "미국 증시 마감 핵심"만. 지수·섹터·매크로(연준/관세/실적) 수준.
- 각 항목 summary = 너의 한국어 자체 서술 1문장. **영문 원문/제목/description 을 그대로
  옮기거나 번역 인용 금지** — 사실을 재구성한 한국어 요약만.
- 매매 권유·투자 조언 표현 절대 금지 (예 "매수 추천"/"지금 사야"/"비중 확대" 등 금지).
  사실 전달만 ("나스닥 0.3% 하락 마감", "엔비디아 실적 호조로 반도체 강세" 식).
- 교차검증: 단일 매체만 근거인 불확실 정보는 단정 금지.
- 개별 종목 홍보·루머 제외. 시장·섹터 영향이 있는 사건만.

## 규칙
1. 최대 10개 / 2. summary 1문장(한국어, 매매권유 0건) / 3. source 는 매체 약어(예 "CNBC","한경")
   / 4. url 은 입력에 주어진 원문 링크 그대로
5. **FOMC/연준 우선**: 입력에 FOMC 회의·금리 결정·의사록(minutes)·연준 의장/이사 발언
   관련 기사가 있으면 **반드시 1건 이상 포함**(최우선순위). 미장 핵심이므로 누락 금지.
6. **지정학 우선·인과 명시**: 무력분쟁·제재·공급망 차단이 원유·상품·매크로에 영향이
   있으면 **1건 이상 포함**하고 summary 에 인과 1단계를 명시한다 (예: 중동 분쟁→유가↑→
   정유 / 중국·대만 긴장→반도체 공급망 / 러시아·우크라이나→곡물·천연가스·방산). **단,
   원유·상품·매크로 영향이 없는 순수 정치·군사·외교 뉴스(인사·의전·성명·국내 정쟁 등)는
   제외**(시장 메커니즘 없는 사건은 미장 요약 아님). 규칙 5(연준)와 동일 우선순위.
7. **최신 기사 우선**: 입력 기사는 최신순(published 역순)으로 정렬되어 있다. 최근 3일 이내
   기사를 오래된 기사보다 우선 선택한다. 동일 중요도면 최신 기사를 택한다.

출력:
[
  {{"summary": "나스닥이 반도체 약세에 0.3% 하락 마감, 연준 금리 경계 지속", "source": "CNBC", "url": "https://www.cnbc.com/..."}}
]"""


_RSS_STALE_DAYS = 14  # 이 일수보다 오래된 기사는 LLM 입력에서 제외 (신선도 필터)
_RSS_PER_SOURCE_CAP = 15  # 소스별 최대 기사 수 (CNBC 30건 독점 방지, 소스 균형)


def _fetch_us_rss() -> list[dict]:
    """미장 RSS(CNBC) + 한경 국제 RSS 수집. description 포함.

    반환: [{title, description, source, url, published_ts}] (본문 저장 아님 — LLM 입력 휘발성).

    신선도 정책 (2026-06-15 신설 — 오래된 기사 노출 방지):
    - 소스별 상한 _RSS_PER_SOURCE_CAP 건 (CNBC 30건 독점 차단, 소스 균형 보장)
    - published 날짜 파싱 → _RSS_STALE_DAYS 일 초과 기사 제외
    - 최종 rows를 published_ts 역순(최신순) 정렬 → LLM[:MAX_NEWS_FOR_LLM] 에 최신 기사 우선 배치
    """
    import calendar
    import time as _t

    import feedparser

    # 한경 등 일부 매체는 빈 UA 에 403 → 명시 UA 의무 (2026-06-05 실측: 한경
    # international 빈 UA=403/0건, Mozilla UA=200/50건). collect_rss.py 는 종래
    # 차단 없었으나 본 US-digest 소스(한경 국제)는 UA 필수.
    _ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    rows: list[dict] = []
    feeds = {**RSS_FEEDS_US_DIGEST, **RSS_FEEDS_HK_GLOBAL}
    now_ts = _t.time()  # UTC epoch seconds
    stale_cutoff = now_ts - _RSS_STALE_DAYS * 86400

    for source, url in feeds.items():
        try:
            feed = feedparser.parse(url, agent=_ua)
        except Exception as exc:  # noqa: BLE001 — 단일 피드 실패가 전체 중단 막지 않음
            print(f"[us-digest] {source} parse FAIL: {exc}", file=sys.stderr)
            continue
        source_rows: list[dict] = []
        for e in feed.entries:
            title = getattr(e, "title", "").strip()
            link = getattr(e, "link", "").strip()
            desc = (getattr(e, "summary", "") or getattr(e, "description", "")).strip()
            if not title or not link:
                continue
            # published_parsed: UTC time.struct_time or None
            pub_struct = getattr(e, "published_parsed", None)
            if pub_struct:
                pub_ts = float(calendar.timegm(pub_struct))
                if pub_ts < stale_cutoff:
                    continue  # _RSS_STALE_DAYS 초과 → 제외
            else:
                pub_ts = 0.0  # 날짜 미상 기사는 낮은 우선순위로 보존
            source_rows.append(
                {
                    "title": title,
                    "description": desc,
                    "source": source,
                    "url": link,
                    "published_ts": pub_ts,
                }
            )
            if len(source_rows) >= _RSS_PER_SOURCE_CAP:
                break  # 소스별 상한
        rows.extend(source_rows)
        print(
            f"[us-digest] {source}: {len(feed.entries)} entries "
            f"→ {len(source_rows)} 신선({_RSS_STALE_DAYS}일 이내)",
            file=sys.stderr,
        )

    # 최신순 정렬 → LLM 입력 앞쪽에 최신 기사 배치
    rows.sort(key=lambda r: r["published_ts"], reverse=True)
    return rows


def _fetch_hk_global_naver() -> list[dict]:
    """한경 글로벌 보강 — Naver 공식 검색 API (직접 스크레이핑 아님).

    naver_news_search.search_news 재사용. 실패/키 부재 시 빈 리스트(보조 소스라
    전체 빌드 중단 안 함). description 은 Naver 응답 제공(본문 저장 아님, LLM 휘발).
    """
    try:
        from .naver_news_search import _strip_html, search_news

        resp = search_news(HK_GLOBAL_NAVER_QUERY, display=20, sort="date")
    except Exception as exc:  # noqa: BLE001 — 보조 소스, 실패 허용
        print(f"[us-digest] 한경 Naver 보강 skip: {exc}", file=sys.stderr)
        return []

    rows: list[dict] = []
    for it in resp.get("items", []):
        title = _strip_html(it.get("title", ""))
        link = it.get("originallink") or it.get("link") or ""
        desc = _strip_html(it.get("description", ""))
        # 한경 발행 기사만 (originallink 도메인 필터)
        if "hankyung.com" not in (it.get("originallink") or ""):
            continue
        if not title or not link:
            continue
        rows.append(
            {"title": title, "description": desc, "source": "한경", "url": link}
        )
    print(f"[us-digest] 한경 Naver 보강: {len(rows)} rows", file=sys.stderr)
    return rows


# FOMC/연준 관련 기사 식별 키워드 (대표 2026-06-06 "fomc 기사는 꼭 언급").
# title + description 합쳐 소문자 매칭. 영문(WSJ/CNBC) + 한글(한경) 양쪽 커버.
# 과매칭 방지: "federal"(연방정부 일반) 단독 제외, "fed"/"fomc"/"연준" 등 명확 토큰만.
_FOMC_KEYWORDS = (
    "fomc",
    "federal reserve",
    "fed chair",
    "fed official",
    "fed minutes",
    "rate decision",
    "interest rate",
    "rate cut",
    "rate hike",
    "powell",
    "warsh",  # 신임 연준 의장 (2026)
    "연준",
    "fomc",
    "기준금리",
    "금리 결정",
    "연방준비",
)


def _is_fomc_article(row: dict) -> bool:
    """기사(title+description)가 FOMC/연준 관련인지 판정."""
    text = f"{row.get('title', '')} {row.get('description', '')}".lower()
    return any(kw in text for kw in _FOMC_KEYWORDS)


# 고임팩트(시장 충격) 기사 식별 키워드 — FOMC/연준 외 정책·지정학·시장충격 축.
# news_chips 시간역순 정렬 시 24h 이내 고임팩트 칩을 상단에 핀(고정)하는 데 사용.
# 과매칭 방지: 시장 메커니즘이 명확한 토큰만(일반 정치/인사 뉴스 배제). FOMC 는
# _is_fomc_article 로 별도 커버하므로 여기서는 중복 키워드 최소화.
_HIGH_IMPACT_KEYWORDS = (
    # 통상·관세 정책
    "tariff",
    "sanction",
    "trade war",
    "export control",
    "관세",
    "제재",
    "수출통제",
    # 지정학 충격 (시장 영향 있는 분쟁/공급망)
    "war",
    "ceasefire",
    "conflict",
    "supply chain",
    "공급망",
    "전쟁",
    "분쟁",
    # 시장 충격 (급변동·고용·물가 서프라이즈)
    "plunge",
    "crash",
    "selloff",
    "sell-off",
    "surge",
    "cpi",
    "inflation",
    "jobs report",
    "payroll",
    "급락",
    "급등",
    "폭락",
    "물가",
    "고용지표",
)


def _is_high_impact_article(row: dict) -> bool:
    """기사가 고임팩트(시장 충격)인지 판정 — FOMC/연준 또는 정책·지정학·시장충격.

    news_chips 시간역순 정렬에서 최신성과 별개로 중요 사건을 상단 고정하는 신호.
    """
    if _is_fomc_article(row):
        return True
    text = f"{row.get('title', '')} {row.get('description', '')}".lower()
    return any(kw in text for kw in _HIGH_IMPACT_KEYWORDS)


_FALLBACK_NEWS_THEMES = (
    (
        "fomc",
        (
            "fomc",
            "federal reserve",
            "fed chair",
            "fed official",
            "powell",
            "warsh",
            "연준",
            "기준금리",
        ),
        "연준과 금리 경로 관련 발언이 미국 증시의 정책 민감도를 키우는 흐름",
    ),
    (
        "inflation",
        (
            "cpi",
            "ppi",
            "inflation",
            "producer price",
            "consumer price",
            "물가",
            "인플레이션",
        ),
        "미국 물가 지표가 금리 경계감과 섹터별 밸류에이션 부담을 동시에 자극하는 흐름",
    ),
    (
        "geopolitics",
        (
            "iran",
            "middle east",
            "oil",
            "war",
            "ceasefire",
            "geopolitical",
            "중동",
            "유가",
        ),
        "지정학 리스크와 에너지 가격 변동이 위험자산 심리에 부담으로 작용하는 흐름",
    ),
    (
        "tariff",
        ("tariff", "trade", "china", "forced labor", "관세", "통상", "중국"),
        "관세와 통상 정책 불확실성이 글로벌 공급망과 경기민감주 부담으로 이어지는 흐름",
    ),
    (
        "ai",
        (
            "ai",
            "artificial intelligence",
            "nvidia",
            "data center",
            "데이터센터",
            "인공지능",
        ),
        "AI 인프라 투자 이슈가 기술주와 전력 인프라 섹터의 차별화 요인으로 부각되는 흐름",
    ),
    (
        "semiconductor",
        ("chip", "semiconductor", "amd", "broadcom", "반도체"),
        "반도체 수요와 공급망 뉴스가 기술주 투자심리의 핵심 변수로 작용하는 흐름",
    ),
    (
        "market",
        ("dow", "nasdaq", "s&p", "stocks", "wall street", "market", "증시", "나스닥"),
        "미국 주요 지수 변동성이 확대되며 섹터별 차별화가 커지는 흐름",
    ),
    (
        "earnings",
        ("earnings", "revenue", "profit", "guidance", "실적"),
        "기업 실적과 가이던스 변화가 대형주 중심의 선별 흐름을 강화하는 흐름",
    ),
    (
        "labor",
        ("jobs", "payroll", "unemployment", "jobless", "labor", "고용", "실업"),
        "미국 고용 지표가 경기 둔화와 임금 압력 해석을 동시에 키우는 흐름",
    ),
    (
        "finance",
        ("bank", "banks", "jpmorgan", "goldman", "credit", "금융", "은행"),
        "금융권 규제와 신용 리스크 이슈가 대형 금융주 심리에 영향을 주는 흐름",
    ),
    (
        "consumer",
        ("consumer", "retail", "spending", "household", "소비", "소매"),
        "가계 재정과 소비 부담 지표가 경기 방어 심리에 영향을 주는 흐름",
    ),
    (
        "infrastructure",
        ("utility", "power", "electricity", "infrastructure", "전력", "인프라"),
        "전력·유틸리티 투자와 인프라 수요가 방어주와 산업재 관심을 확대하는 흐름",
    ),
)


def _fallback_us_news_chips(
    news_rows: list[dict], exclude_urls: set[str] | None = None
) -> list[dict]:
    """LLM 실패 시 쓰는 보수적 news_chips fallback.

    원문 제목을 그대로 노출하지 않고, 기사 메타데이터를 주제 버킷으로만 압축한다.
    신선한 기사 링크는 유지하되 같은 주제는 1회만 노출해 5개 고착을 막는다.
    """
    exclude_urls = exclude_urls or set()
    ranked: list[tuple[int, int, dict]] = []
    for idx, row in enumerate(news_rows):
        url = (row.get("url") or "").strip()
        source = (row.get("source") or "").strip()
        if (
            not url.startswith(("http://", "https://"))
            or not source
            or url in exclude_urls
        ):
            continue
        text = f"{row.get('title', '')} {row.get('description', '')}".lower()
        matched = None
        for priority, (theme, keywords, summary) in enumerate(_FALLBACK_NEWS_THEMES):
            if any(kw in text for kw in keywords):
                matched = (priority, theme, summary)
                break
        if matched is None:
            matched = (
                len(_FALLBACK_NEWS_THEMES),
                "general",
                "미국 증시 주요 뉴스가 매크로와 섹터별 투자심리 변화로 연결되는 흐름",
            )
        priority, theme, summary = matched
        ranked.append(
            (
                priority,
                idx,
                {"theme": theme, "summary": summary, "source": source, "url": url},
            )
        )

    chips: list[dict] = []
    seen_themes: set[str] = set()
    seen_urls: set[str] = set()
    for _, _, item in sorted(ranked, key=lambda x: (x[0], x[1])):
        if item["theme"] in seen_themes or item["url"] in seen_urls:
            continue
        seen_themes.add(item["theme"])
        seen_urls.add(item["url"])
        chips.append(
            {
                "summary": item["summary"],
                "source": item["source"],
                "url": item["url"],
            }
        )
        if len(chips) >= MAX_NEWS_CHIPS:
            break
    return chips


def _fill_news_chips_from_fallback(
    chips: list[dict], news_rows: list[dict]
) -> list[dict]:
    """LLM 결과가 부족할 때 fallback 주제로 MAX_NEWS_CHIPS까지 보강."""
    if len(chips) >= MAX_NEWS_CHIPS:
        return chips[:MAX_NEWS_CHIPS]
    existing_urls = {c.get("url") for c in chips if c.get("url")}
    for chip in _fallback_us_news_chips(news_rows, exclude_urls=existing_urls):
        if len(chips) >= MAX_NEWS_CHIPS:
            break
        chips.append(chip)
    return chips


def _fallback_sources_for(
    news_rows: list[dict], keywords: tuple[str, ...], limit: int = 3
) -> list[dict]:
    """주제별 fallback 근거 링크. 제목은 노출하지 않고 source/url만 사용."""
    ranked = []
    for idx, row in enumerate(news_rows):
        url = (row.get("url") or "").strip()
        source = (row.get("source") or "").strip()
        if not url.startswith(("http://", "https://")) or not source:
            continue
        text = f"{row.get('title', '')} {row.get('description', '')}".lower()
        matched = any(kw in text for kw in keywords)
        ranked.append((0 if matched else 1, idx, {"name": source, "url": url}))

    sources: list[dict] = []
    seen_urls: set[str] = set()
    for _, _, source in sorted(ranked, key=lambda x: (x[0], x[1])):
        if source["url"] in seen_urls:
            continue
        seen_urls.add(source["url"])
        sources.append(source)
        if len(sources) >= limit:
            break
    return sources


def _fallback_index_news(
    news_rows: list[dict], realtime: bool = False
) -> dict[str, dict]:
    """지수/선물 카드 뉴스 LLM 실패 시 stale 문구를 막는 fallback."""
    macro_sources = _fallback_sources_for(
        news_rows,
        (
            "fomc",
            "fed",
            "inflation",
            "cpi",
            "ppi",
            "tariff",
            "oil",
            "iran",
            "market",
            "연준",
            "물가",
        ),
    )
    if not macro_sources:
        return {}
    tech_sources = _fallback_sources_for(
        news_rows,
        (
            "ai",
            "nvidia",
            "semiconductor",
            "chip",
            "data center",
            "nasdaq",
            "반도체",
        ),
    )
    dow_sources = _fallback_sources_for(
        news_rows,
        (
            "dow",
            "bank",
            "industrial",
            "utility",
            "energy",
            "tariff",
            "consumer",
            "금융",
        ),
    )

    if realtime:
        return {
            "NASDAQ": {
                "summary": "AI·반도체와 금리 민감 뉴스가 나스닥100 선물의 변동성 요인으로 부각되는 흐름",
                "sources": tech_sources or macro_sources,
            },
            "S&P500": {
                "summary": "연준·물가·지정학 뉴스가 S&P500 선물의 방향성 판단 변수로 부각되는 흐름",
                "sources": macro_sources,
            },
            "DOW": {
                "summary": "금융·산업재·에너지 관련 매크로 뉴스가 다우 선물의 경기민감 흐름에 영향을 주는 흐름",
                "sources": dow_sources or macro_sources,
            },
        }
    return {
        "NASDAQ": {
            "summary": "AI·반도체와 금리 민감 뉴스가 나스닥의 섹터 차별화 요인으로 부각되는 흐름",
            "sources": tech_sources or macro_sources,
        },
        "S&P500": {
            "summary": "연준·물가·지정학 뉴스가 미국 증시 전반의 매크로 부담으로 작용하는 흐름",
            "sources": macro_sources,
        },
        "DOW": {
            "summary": "금융·산업재·에너지 관련 뉴스가 다우의 경기민감 흐름에 영향을 주는 흐름",
            "sources": dow_sources or macro_sources,
        },
    }


def _article_signature(news_rows: list[dict]) -> str:
    """기사 집합 + news_chips 정책 버전 시그니처."""
    return hash_input(
        {
            "version": NEWS_CHIP_SIGNATURE_VERSION,
            "urls": sorted({r["url"] for r in news_rows}),
        }
    )


# published_ts 핀 윈도우 — 이 시간 이내 고임팩트 칩만 상단 고정(오래된 FOMC 가
# 영구히 상단을 점유하는 것을 방지). 그 외에는 순수 최신순(published_ts 역순).
_IMPACT_PIN_WINDOW_SEC = 24 * 3600


def _sort_chips_time_desc_impact_pin(
    chips: list[dict], news_rows: list[dict]
) -> list[dict]:
    """news_chips 를 (고임팩트 24h 핀, published_ts 역순)으로 재정렬 + 필드 부착.

    chip 에는 url 만 있으므로 news_rows 의 url→row 역참조로 published_ts/impact_high 를
    매핑한다(chip 본문에는 title/description 이 없어 row 메타로 판정). LLM 이 url 을
    source-fallback 으로 치환한 경우(환각 방어)도 chip.url 기준이라 정합.

    - published_ts: row 에 있으면 그 값, 없으면 0.0(날짜 미상 → 최하위)
    - impact_high: row 메타로 _is_high_impact_article 판정(FOMC/정책/지정학/충격)
    - 정렬 키: (impact_high AND 24h 이내) 우선 → published_ts 내림차순
    """
    if not chips:
        return chips
    # url → row (동일 url 중복 시 최초 row 유지). published_ts 결측 row 도 매핑 보존.
    by_url: dict[str, dict] = {}
    for r in news_rows:
        u = (r.get("url") or "").strip()
        if u and u not in by_url:
            by_url[u] = r
    now_ts = time.time()

    enriched: list[dict] = []
    for chip in chips:
        url = (chip.get("url") or "").strip()
        row = by_url.get(url)
        pub_ts = float(row.get("published_ts", 0.0)) if row else 0.0
        impact = _is_high_impact_article(row) if row else False
        # 원본 chip dict 변형(상위 호출이 그대로 payload 에 싣는 구조) — 필드만 추가.
        chip["published_ts"] = pub_ts
        chip["impact_high"] = impact
        is_pinned = (
            impact and pub_ts > 0.0 and (now_ts - pub_ts) <= _IMPACT_PIN_WINDOW_SEC
        )
        # 정렬 보조키: 핀(1/0) 내림차순, published_ts 내림차순. 안정정렬로 동률 시 기존 순서 유지.
        enriched.append((1 if is_pinned else 0, pub_ts, chip))

    enriched.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [c for _, _, c in enriched]


def extract_us_news_chips(news_rows: list[dict]) -> list[dict]:
    """미장 뉴스 → LLM 한국어 요약 news_chips (extract_macros 패턴 재사용).

    description 을 LLM 입력에 포함 (현행 국내 macro 는 title-only — 본 모듈만 확장).
    macro_events 테이블 touch 0건 (오염 금지). 캐시 도메인 분리(us_digest).
    """
    if not news_rows:
        return []

    lines = []
    sig_set = []
    for r in news_rows[:MAX_NEWS_FOR_LLM]:
        desc = (r.get("description") or "")[:200]
        lines.append(f"- {r['title']} | {desc} ({r['source']}) [{r['url']}]")
        sig_set.append(r["title"])
    news_block = "\n".join(lines)
    prompt = US_DIGEST_PROMPT_TEMPLATE.format(news_block=news_block)

    target_id = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")
    text = call_model_cached(
        prompt,
        US_DIGEST_MODEL,
        domain="us_digest",  # 캐시 도메인 분리 — 국내 extract_macros 와 무충돌
        target_id=target_id,
        input_hash=hash_input(
            {"version": NEWS_CHIP_SIGNATURE_VERSION, "titles": sig_set}
        ),
        agent="ishikawa",
        timeout=120,
    )
    if not text:
        print("[us-digest] LLM FAIL or empty", file=sys.stderr)
        fallback = _fallback_us_news_chips(news_rows)
        if fallback:
            print(
                f"[us-digest] fallback news_chips 사용({len(fallback)}건)",
                file=sys.stderr,
            )
        return fallback

    try:
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end < 0:
            print("[us-digest] no JSON array in LLM response", file=sys.stderr)
            return _fallback_us_news_chips(news_rows)
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        print(f"[us-digest] JSON parse FAIL: {exc}", file=sys.stderr)
        return _fallback_us_news_chips(news_rows)
    if not isinstance(parsed, list):
        return _fallback_us_news_chips(news_rows)

    # 입력에 실제로 존재한 url 집합 (LLM 환각 url 차단 — 법무 ② 딥링크 정확성)
    valid_urls = {r["url"] for r in news_rows}
    chips: list[dict] = []
    for evt in parsed[:MAX_NEWS_CHIPS]:
        summary = (evt.get("summary") or "").strip()
        source = (evt.get("source") or "").strip()
        url = (evt.get("url") or "").strip()
        if not summary:
            continue
        # url 환각 방어: 입력에 없는 url 이면 source 매칭 첫 기사 url 로 대체, 없으면 drop
        if url not in valid_urls:
            fallback = next(
                (r["url"] for r in news_rows if r["source"] == source), None
            )
            if not fallback:
                continue
            url = fallback
        chips.append({"summary": summary, "source": source, "url": url})

    # FOMC 결정적 보장 (대표 2026-06-06): 기사 풀에 FOMC/연준 기사가 있는데 LLM 칩에
    # 한 건도 안 잡혔으면 강제 1건 삽입. 가짜 생성 금지 — 풀에 실제 FOMC 기사 있을 때만.
    fomc_rows = [r for r in news_rows if _is_fomc_article(r)]
    if fomc_rows:
        chip_urls = {c["url"] for c in chips}
        has_fomc_chip = any(
            _is_fomc_article(r) for r in news_rows if r["url"] in chip_urls
        )
        if not has_fomc_chip:
            forced = _summarize_single_fomc(fomc_rows[0])
            if not forced:
                forced = next(iter(_fallback_us_news_chips(fomc_rows)), None)
            if forced:
                # 상한 유지: 꽉 찼으면 마지막 칩을 FOMC 로 치환(FOMC 최우선).
                if len(chips) >= MAX_NEWS_CHIPS:
                    chips[-1] = forced
                else:
                    chips.append(forced)
                print("[us-digest] FOMC 칩 결정적 보장 삽입", file=sys.stderr)
    return _fill_news_chips_from_fallback(chips, news_rows)


# 단일 FOMC 기사 → 한국어 1문장 요약 (결정적 보장 fallback 전용).
_FOMC_ONE_PROMPT = """다음 미국 연준/FOMC 관련 기사 1건을 한국 투자자용 한국어 1문장으로
요약하라. 영문 원문/제목 verbatim 번역 금지(사실 재구성), 매매 권유 표현 금지, 사실 전달만.
JSON 객체 1개만 출력: {{"summary": "...", "source": "...", "url": "..."}}

기사: {title} | {desc} ({source}) [{url}]"""


def _summarize_single_fomc(row: dict) -> dict | None:
    """FOMC 기사 1건 → news_chip 1건 (LLM 자체 요약, verbatim 금지 — 법무 ①).

    summary 는 LLM 한국어 생성, url/source 는 입력 그대로(딥링크 정확성 — 법무 ②).
    실패 시 None(가짜 생성 금지 — 빈손이면 삽입 안 함).
    """
    prompt = _FOMC_ONE_PROMPT.format(
        title=row["title"],
        desc=(row.get("description") or "")[:200],
        source=row["source"],
        url=row["url"],
    )
    target_id = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")
    text = call_model_cached(
        prompt,
        US_DIGEST_MODEL,
        domain="us_digest_fomc",  # 캐시 도메인 분리
        target_id=target_id,
        input_hash=hash_input([row["url"]]),
        agent="ishikawa",
        timeout=120,
    )
    if not text:
        return None
    try:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < 0:
            return None
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    summary = (obj.get("summary") or "").strip()
    if not summary:
        return None
    # url/source 는 LLM 환각 방지 위해 입력 row 값으로 고정(법무 ② 딥링크 정확성).
    return {"summary": summary, "source": row["source"], "url": row["url"]}


# 지수별 연관 기사 분류 + 요약 프롬프트 (대표 2026-06-05 20:31 — 종목카드 뉴스 영역).
# 국내 종목카드 news 영역과 동형: index 당 {summary(한국어 1문단), sources:[{name,url}]}.
# 연관 기사 없는 지수는 결과에서 생략 (강제 채움 금지 — 없으면 없다).
INDEX_NEWS_PROMPT_TEMPLATE = """미국 지수별 연관 뉴스 분류·요약(이시카와). JSON 객체만.

## 미장 뉴스 (제목 | 요약 (출처) [링크])
{news_block}

## 지수별 분류 기준
- NASDAQ: 기술·반도체·성장주·빅테크 관련
- S&P500: 시장 전반·매크로(연준/금리/물가/관세)·광범위 영향
- DOW: 대형 전통주·산업재·금융·경기민감 — **블루칩·다우 구성종목(보잉/캐터필러/JP모건/골드만/3M 등)·
  경기순환주·고용·관세·인프라·산업 정책 기사 포함**.

## 원칙
- 각 지수에 **명확히 연관된** 기사를 분류. 단, **시장 전반(매크로) 기사(연준/금리/물가/고용/관세/
  지정학)는 3대 지수 모두에 공통 적용 가능** — 매크로는 다우·S&P·나스닥 모두를 움직이므로,
  해당 지수 관점에서 의미가 있으면 같은 기사를 복수 지수에 분류해도 된다(특히 DOW 가 비면 매크로·
  경기민감 기사를 DOW 관점으로 분류). 무관한 기사를 억지로 붙이는 것만 금지.
- summary = 그 지수 관점의 한국어 자체 서술 1문단(2~3문장). **영문 원문/제목 verbatim 인용 금지**.
- **말투 = 개조식·해석형 명사종결**. "...가속화 중", "...둔화되는 추세", "...약세 신호 심화" 처럼
  **명사·명사구로 끝맺는다**(서술형 완결문 -다 회피). 선물 카드 뉴스와 동일 톤(시제만 마감/실시간 차이).
- 매매 권유·투자 조언 표현 절대 금지(예 "매수"/"비중확대"/"지금 사야"). 사실 전달만.
- sources = 그 summary 의 근거 기사 1~3건. name=매체약어, url=입력에 주어진 링크 그대로.
- **진짜 무관한 지수만 키 생략**(빈 배열·무관 기사 억지 요약 금지). 매크로 연관 가능하면 포함.
- **FOMC/연준 우선**: 입력에 FOMC 회의·금리 결정·의사록·연준 의장/이사 발언 기사가 있으면
  S&P500(매크로) summary 에 **반드시 반영**하고 해당 기사를 sources 에 포함(누락 금지).

출력 (연관 있는 지수만 키 포함):
{{
  "NASDAQ": {{"summary": "반도체주 약세에 나스닥 하락 마감. AI 투자 열풍은 지속, 금리 경계감 부각.", "sources": [{{"name": "CNBC", "url": "https://www.cnbc.com/..."}}]}},
  "S&P500": {{"summary": "...", "sources": [{{"name": "CNBC", "url": "..."}}]}}
}}"""

# 지수명 → 프롬프트/응답 키 정합 (collect_us_indices 의 name 과 동일).
_INDEX_NEWS_KEYS = {"NASDAQ", "S&P500", "DOW"}
MAX_INDEX_NEWS_SOURCES = 3


def extract_index_news(news_rows: list[dict]) -> dict[str, dict]:
    """지수별 연관 뉴스 분류·요약 → {indexName: {summary, sources:[{name,url}]}}.

    연관 기사 없는 지수는 키 생략. LLM 환각 url 차단(입력 url 집합 대조).
    extract_macros LLM 패턴 재사용(신규 엔진 0). 캐시 도메인 분리(us_index_news).
    """
    if not news_rows:
        return {}

    lines = []
    sig_set = []
    for r in news_rows[:MAX_NEWS_FOR_LLM]:
        desc = (r.get("description") or "")[:160]
        lines.append(f"- {r['title']} | {desc} ({r['source']}) [{r['url']}]")
        sig_set.append(r["title"])
    prompt = INDEX_NEWS_PROMPT_TEMPLATE.format(news_block="\n".join(lines))

    target_id = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")
    text = call_model_cached(
        prompt,
        US_DIGEST_MODEL,
        domain="us_index_news",  # 캐시 도메인 분리 — news_chips/국내 macro 무충돌
        target_id=target_id,
        input_hash=hash_input(sig_set),
        agent="ishikawa",
        timeout=120,
    )
    if not text:
        print("[us-index-news] LLM FAIL or empty", file=sys.stderr)
        return _fallback_index_news(news_rows)

    try:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < 0:
            return _fallback_index_news(news_rows)
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        print(f"[us-index-news] JSON parse FAIL: {exc}", file=sys.stderr)
        return _fallback_index_news(news_rows)
    if not isinstance(parsed, dict):
        return _fallback_index_news(news_rows)

    valid_urls = {r["url"] for r in news_rows}
    out: dict[str, dict] = {}
    for key, val in parsed.items():
        if key not in _INDEX_NEWS_KEYS or not isinstance(val, dict):
            continue
        summary = (val.get("summary") or "").strip()
        raw_sources = val.get("sources") or []
        if not summary or not isinstance(raw_sources, list):
            continue
        sources = []
        seen = set()
        for s in raw_sources:
            if not isinstance(s, dict):
                continue
            url = (s.get("url") or "").strip()
            name = (s.get("name") or "").strip()
            # url 환각 방어: 입력에 없는 url 은 drop (억지 채움 금지)
            if url not in valid_urls or url in seen:
                continue
            seen.add(url)
            sources.append({"name": name, "url": url})
            if len(sources) >= MAX_INDEX_NEWS_SOURCES:
                break
        # 근거 링크 0건이면 지수 news 생략 (딥링크 없는 요약 금지 — 법무 ②)
        if not sources:
            continue
        out[key] = {"summary": summary, "sources": sources}

    # 매크로 공통 fallback (대표 2026-06-06 Q-20260606-114): 특정 지수(주로 DOW)가
    # LLM 분류에서 0건이어도, S&P500(매크로·시장 전반) 요약이 있으면 그 매크로 요약을
    # 공통 적용한다(연준/금리/물가/고용/관세는 3대 지수 모두를 움직임 — 같은 출처 링크
    # 동일 유지, 가짜 매핑 0건). DOW 와 무관한 개별 기사를 억지로 붙이는 게 아니라,
    # 시장 전반 매크로 요약을 공유하는 것이므로 거짓 충실성(FLR-AGT-002) 아님.
    macro = out.get("S&P500")
    if macro:
        for missing in _INDEX_NEWS_KEYS:
            if missing not in out:
                out[missing] = {
                    "summary": macro["summary"],
                    "sources": list(macro["sources"]),
                }
    return out


# 선물 지수명(collect_us_futures US_FUTURES_TARGETS 의 name) → 뉴스 분류 키.
# extract_futures_news 결과(NASDAQ/S&P500/DOW)를 futures[].name 에 부착하기 위한 매핑.
_FUT_NAME_TO_NEWS_KEY = {
    "나스닥100 선물": "NASDAQ",
    "S&P500 선물": "S&P500",
    "다우 선물": "DOW",
}

# 선물 장중 뉴스 분류·요약 프롬프트 (Q-20260608-143, 대표 verbatim "실시간 뉴스를 보여줘야지").
# 정규장 index_news(전일 미장 *마감* 요약)와 시제 분리 — 본 프롬프트는 *한국 장중(=미 야간
# 선물 거래시간대) 실시간* 관점. 선물 동향·아시아장·당일 발생 매크로 중심. FOMC 우선 규칙은
# 정규장과 동일 재사용(extract_index_news 동형). 영문 verbatim/매매권유 금지(법무 ①④).
FUTURES_NEWS_PROMPT_TEMPLATE = """미국 지수 *선물* 장중 실시간 뉴스 분류·요약(이시카와). JSON 객체만.

## 현재 시점
지금은 한국 장중(낮)이며, 이는 미국 야간 = **지수 선물(ES/NQ/YM)이 실시간 거래 중인 시간대**다.
아래 기사들은 그 시간대에 수집한 **실시간/최신** 기사다(전일 미국 정규장 *마감* 요약이 아님).

## 최신 뉴스 (제목 | 요약 (출처) [링크])
{news_block}

## 지수별(선물) 분류 기준
- NASDAQ(나스닥100 선물): 기술·반도체·성장주·빅테크 관련
- S&P500(S&P500 선물): 시장 전반·매크로(연준/금리/물가/관세)·광범위 영향
- DOW(다우 선물): 대형 전통주·산업재·금융·경기민감 — 블루칩·다우 구성종목(보잉/캐터필러/
  JP모건/골드만/3M 등)·경기순환주·고용·관세·인프라·산업 정책 기사 포함.

## 원칙
- **선물 동향·아시아 증시·당일 발생 매크로(연준/금리/물가/고용/관세/지정학)·기업 뉴스** 중심.
  매크로 기사는 3대 지수 선물 모두에 공통 적용 가능(특히 DOW 가 비면 매크로·경기민감을 DOW 관점으로).
- summary = 그 선물 관점의 한국어 자체 서술 1문단(2~3문장). **영문 원문/제목 verbatim 인용 금지**.
- **말투 = 개조식·해석형 명사종결**(정규장 뉴스 카드와 동일 톤). "...가속화 중", "...둔화되는 추세",
  "...약세 신호 심화", "...우위" 처럼 **명사·명사구로 끝맺는다**. "...있다 / ...이끌고 있다 / ...지속되고
  있다 / ...점하는 추세다" 같은 **서술형 완결문(종결어미 -다)은 쓰지 않는다**(정규장과 톤 불일치 차단).
- **시제는 현재/실시간**("선물 강세", "아시아장 약세에 동조" 등). "마감" 같은 과거 마감 표현 지양
  (마감은 정규장 카드 몫). 단 기사가 명백히 전일 마감 사실이면 그대로 사실 전달.
- 매매 권유·투자 조언 표현 절대 금지(예 "매수"/"비중확대"/"지금 사야"). 사실 전달만.
- sources = 그 summary 의 근거 기사 1~3건. name=매체약어, url=입력에 주어진 링크 그대로.
- **진짜 무관한 지수만 키 생략**(빈 배열·무관 기사 억지 요약 금지).
- **FOMC/연준 우선**: 입력에 FOMC 회의·금리 결정·의사록·연준 의장/이사 발언 기사가 있으면
  S&P500(매크로) summary 에 반드시 반영하고 해당 기사를 sources 에 포함(누락 금지).

출력 (연관 있는 지수만 키 포함):
{{
  "NASDAQ": {{"summary": "반도체 강세에 나스닥100 선물 동조. AI 펀딩 열풍 지속, 빅테크 매수세 유입.", "sources": [{{"name": "CNBC", "url": "https://www.cnbc.com/..."}}]}},
  "S&P500": {{"summary": "...", "sources": [{{"name": "CNBC", "url": "..."}}]}}
}}"""


def extract_futures_news(news_rows: list[dict]) -> dict[str, dict]:
    """선물 지수별 *장중 실시간* 뉴스 분류·요약 → {indexKey: {summary, sources:[{name,url}]}}.

    정규장 extract_index_news 와 동형 구조(같은 frontend 렌더 경로)이나 (a) 실시간 시제
    프롬프트 + (b) 캐시 도메인 분리(us_futures_news) 로 정규장 *마감* 뉴스와 시제 분리한다.
    연관 기사 없는 지수는 키 생략(강제 채움 금지). LLM 환각 url 차단(입력 url 집합 대조).
    뉴스 0건/LLM 실패 시 빈 dict → 호출측이 news 키 미부착(정규장 fallback 금지, FLR-AGT-002).
    """
    if not news_rows:
        return {}

    lines = []
    sig_set = []
    for r in news_rows[:MAX_NEWS_FOR_LLM]:
        desc = (r.get("description") or "")[:160]
        lines.append(f"- {r['title']} | {desc} ({r['source']}) [{r['url']}]")
        sig_set.append(r["title"])
    prompt = FUTURES_NEWS_PROMPT_TEMPLATE.format(news_block="\n".join(lines))

    target_id = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")
    text = call_model_cached(
        prompt,
        US_DIGEST_MODEL,
        domain="us_futures_news",  # 캐시 도메인 분리 — 정규장 us_index_news 와 무충돌(시제 분리)
        target_id=target_id,
        input_hash=hash_input(sig_set),
        agent="ishikawa",
        timeout=120,
    )
    if not text:
        print("[us-futures-news] LLM FAIL or empty", file=sys.stderr)
        return _fallback_index_news(news_rows, realtime=True)

    try:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end < 0:
            return _fallback_index_news(news_rows, realtime=True)
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        print(f"[us-futures-news] JSON parse FAIL: {exc}", file=sys.stderr)
        return _fallback_index_news(news_rows, realtime=True)
    if not isinstance(parsed, dict):
        return _fallback_index_news(news_rows, realtime=True)

    valid_urls = {r["url"] for r in news_rows}
    out: dict[str, dict] = {}
    for key, val in parsed.items():
        if key not in _INDEX_NEWS_KEYS or not isinstance(val, dict):
            continue
        summary = (val.get("summary") or "").strip()
        raw_sources = val.get("sources") or []
        if not summary or not isinstance(raw_sources, list):
            continue
        sources = []
        seen = set()
        for s in raw_sources:
            if not isinstance(s, dict):
                continue
            url = (s.get("url") or "").strip()
            name = (s.get("name") or "").strip()
            if url not in valid_urls or url in seen:
                continue
            seen.add(url)
            sources.append({"name": name, "url": url})
            if len(sources) >= MAX_INDEX_NEWS_SOURCES:
                break
        if not sources:
            continue
        out[key] = {"summary": summary, "sources": sources}

    # 매크로 공통 fallback (정규장 extract_index_news 동형): DOW 등 0건이어도 S&P500(매크로)
    # 요약이 있으면 공통 적용(연준/금리/물가는 3대 선물 모두 영향 — 같은 출처 링크, 가짜 매핑 0건).
    macro = out.get("S&P500")
    if macro:
        for missing in _INDEX_NEWS_KEYS:
            if missing not in out:
                out[missing] = {
                    "summary": macro["summary"],
                    "sources": list(macro["sources"]),
                }
    return out


def _attach_futures_news(futures: dict, fut_news: dict[str, dict]) -> None:
    """선물 페이로드의 각 futures[].name 에 대응 실시간 news 부착(in-place).

    fut_news 키(NASDAQ/S&P500/DOW) ↔ futures[].name(나스닥100 선물 등) 매핑.
    대응 뉴스 없으면 news 키 미부착(정규장 fallback 금지 — 시제 혼동 재발 차단, FLR-AGT-002).
    """
    if not fut_news or not isinstance(futures, dict):
        return
    for f in futures.get("futures") or []:
        if not isinstance(f, dict):
            continue
        key = _FUT_NAME_TO_NEWS_KEY.get(f.get("name"))
        nv = fut_news.get(key) if key else None
        if nv:
            f["news"] = nv
        else:
            f.pop("news", None)  # 직전 stale news 제거(없으면 없다)


def _kst_today() -> str:
    return datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")


def build(dry_run: bool = False) -> dict | None:
    """전체 빌드: 지수 수집 → 뉴스 요약 → JSON 조립 → 배포.

    지수 수집 실패 시 전일 JSON 유지 (부분 산출물 금지, FLR-20260605-TEC-001).
    """
    kst_date = _kst_today()
    out_path = OUT_DIR / f"{kst_date}.json"

    idx = collect_indices()
    if idx is None:
        print(
            "[us-digest] 지수 수집 실패 — 전일 JSON 유지, 신규 빌드 skip "
            "(부분 산출물 금지)",
            file=sys.stderr,
        )
        return None

    news_rows = _fetch_us_rss() + _fetch_hk_global_naver()
    # 시간역순 + 고임팩트 핀 정렬 + published_ts/impact_high 부착 (프론트 시간역순 발현).
    news_chips = _sort_chips_time_desc_impact_pin(
        extract_us_news_chips(news_rows), news_rows
    )

    # 지수별 연관 뉴스 (대표 2026-06-05 20:31) — name 매칭으로 indices[].news 부착.
    # 연관 기사 없는 지수는 news 키 자체 생략 (강제 채움 금지).
    index_news = extract_index_news(news_rows)
    for ix in idx["indices"]:
        nv = index_news.get(ix["name"])
        if nv:
            ix["news"] = nv

    # 선물 (Phase 4) — 아침 빌드에도 포함(schema 일관). 장중엔 refresh_intraday 가 갱신.
    # 선물 실패해도 아침 빌드 자체는 진행(선물은 장중 핵심, 아침엔 보조) — None 이면 생략.
    futures = collect_futures()

    payload = {
        "trade_date_local": idx["trade_date_local"],
        "indices": idx["indices"],
        "news_chips": news_chips,
        # 장중 refresh 가 기사 집합 변경 여부 판정에 쓰는 시그니처 (LLM 비용 절제).
        "_article_sig": _article_signature(news_rows),
        "built_at_kst": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
    }
    if futures:
        payload["futures"] = futures

    if dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return payload

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"[us-digest] wrote {out_path}", file=sys.stderr)
    return payload


def _latest_prior_us_indices(kst_date: str) -> dict | None:
    """오늘 이전 us-indices JSON 중 가장 최근 유효 파일 로드 (없으면 None).

    Q-20260608-133 (FLR-20260606-TEC-001) — 월요일 등 모닝 빌드 부재일에 직전
    거래일(금요장) 마감 indices 를 seed 로 재사용해 today 파일 schema 유효성 확보.
    indices(필수) 미충족 파일은 무시 — 부분/무효 산출물 seed 금지 (FLR-AGT-002).
    """
    if not OUT_DIR.exists():
        return None
    dates = sorted(
        p.stem
        for p in OUT_DIR.glob("*.json")
        if p.stem < kst_date and len(p.stem) == 10
    )
    for d in reversed(dates):
        try:
            data = json.loads((OUT_DIR / f"{d}.json").read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if (
            isinstance(data, dict)
            and isinstance(data.get("indices"), list)
            and data["indices"]
        ):
            return data
    return None


def _refresh_index_intraday(payload: dict) -> bool:
    """정규장 지수 장중 갱신 (Q-20260608 정규장 장중 실시간) — in-place.

    fetch_intraday(1d/5m) raw 를 지수당 1회만 받아(추가 API 콜 0) 둘을 산출한다:
      (1) spark  — 항상 갱신. 당일 장중 5분봉 종가열 + candle.o prepend (선물 동형).
                   아침 빌드의 옛(일봉) spark 를 당일 장중 path 로 정정.
      (2) quote  — **정규장 개장 시에만**(is_us_regular_open()). point/change_pct/
                   candle 을 당일 장중값으로 교체 + daily_expanded 마지막 항목을 당일
                   봉으로 갱신/append (date 동일이면 교체, 새 거래일이면 append).
                   range_240d/news 는 보존(아침 빌드값 — 장중 재계산 불요·비용 0).
    폐장 시(False): quote 미갱신 → point/change_pct/candle/daily_expanded 마감값 보존
      (회귀 0, FLR-AGT-002 거짓 충실성 차단 — 마감 데이터를 장중으로 위장 금지).
    부분 갱신 허용: 한 지수 intraday 결손이어도 그 지수만 기존값 유지, 나머지 갱신
      (3지수 all-present 불변식은 아침 빌드가 이미 보장 — FLR-20260605-TEC-001 §1).

    반환: regular_open (bool) — 이번 fire 의 정규장 개장 판정(payload 플래그·프론트용).
    """
    regular_open = is_us_regular_open()
    indices = payload.get("indices")
    if not isinstance(indices, list) or not indices:
        return regular_open
    by_name = {ix.get("name"): ix for ix in indices if isinstance(ix, dict)}
    for name, symbol in US_INDEX_TARGETS:
        ix = by_name.get(name)
        if ix is None:
            continue
        raw = fetch_intraday(symbol)
        # (1) spark — 항상 (raw 1회 재활용).
        intraday = parse_intraday(name, raw)
        if intraday:
            candle_o = (ix.get("candle") or {}).get("o")
            ix["spark"] = [candle_o, *intraday] if candle_o is not None else intraday
        else:
            print(
                f"[us-digest:intraday] {name} 장중 5분봉 수집 실패 — 기존 spark 유지",
                file=sys.stderr,
            )
        # (2) quote — 개장 시에만 point/change_pct/candle/daily_expanded 당일값 교체.
        if not regular_open:
            continue
        q = parse_intraday_quote(name, raw)
        if not q:
            print(
                f"[us-digest:intraday] {name} 장중 quote 산출 실패 — 마감값 보존",
                file=sys.stderr,
            )
            continue
        ix["point"] = q["point"]
        ix["change_pct"] = q["change_pct"]
        ix["candle"] = q["candle"]
        # P1 (Q-20260609) — 거래일 = 미 동부 현지 날짜(parse_intraday_quote 산출, 미 6/8).
        #   한국 날짜 파일(6/9)에 담겨도 거래일은 미 현지(6/8) — seed 의 마감일(6/5)/한국날짜 아님.
        #   indices[].trade_date_local 채움(기존 None) + 아래 top-level 동기화 소스.
        if q.get("trade_date_local"):
            ix["trade_date_local"] = q["trade_date_local"]
        # spark 첫 점(candle.o) 을 당일 시가로 재정합 (candle 교체 직후 prepend 갱신).
        if intraday and q["candle"].get("o") is not None:
            ix["spark"] = [q["candle"]["o"], *intraday]
        _upsert_daily_expanded(ix, q)
    return regular_open


def _us_trade_date_from_indices(payload: dict) -> str | None:
    """indices[].trade_date_local 중 첫 유효값 반환 (3지수 동일 미 거래일 가정).

    개장 중 _refresh_index_intraday 가 채운 미 현지 거래일. payload top-level
    trade_date_local 동기화용 (P1 Q-20260609 — 한국 날짜/seed 마감일 오매핑 교정).
    """
    for ix in payload.get("indices") or []:
        if isinstance(ix, dict) and ix.get("trade_date_local"):
            return ix["trade_date_local"]
    return None


def _upsert_daily_expanded(ix: dict, q: dict) -> None:
    """daily_expanded 마지막 항목을 당일 장중 봉으로 갱신/append (in-place).

    같은 거래일(date 일치)이면 마지막 항목 교체(장중 OHLC 갱신), 새 거래일이면 append.
    v(거래량)는 장중 미집계 → 키 생략(아침/마감 빌드가 채움). date 미상이면 no-op.
    """
    de = ix.get("daily_expanded")
    if not isinstance(de, list):
        return
    td = q.get("trade_date_local")
    if not td:
        return
    candle = q["candle"]
    bar = {
        "date": td,
        "o": candle["o"],
        "h": candle["h"],
        "l": candle["l"],
        "c": candle["c"],
    }
    if de and de[-1].get("date") == td:
        de[-1] = bar
    else:
        de.append(bar)


def refresh_intraday(dry_run: bool = False) -> dict | None:
    """장중 경량 갱신 (Phase 4, 15분 주기) — 선물 + (신규 기사 시) news_chips 만.

    아침 빌드 산출물(indices·range_240d·daily_expanded·index news·trade_date_local)은
    그대로 보존. LLM 비용 절제: news_chips 재생성은 *신규 기사(미수록 url)* 있을 때만.

    오늘 JSON 부재 시(예: 월요일 — 미 일요 휴장으로 모닝 빌드 plist 미발화):
      직전 거래일 마감 indices 를 seed 로 재사용해 today 파일 생성 후 선물만 오버레이.
      → 평일 장중 선물이 미장 모닝 빌드 유무와 무관하게 표시됨 (Q-20260608-133).
      seed 의 trade_date_local 은 실제 마감일(금요장) 그대로 보존 → stale 오인 없음.
      직전 유효 파일도 없으면 skip (seed 불가 — 빈/무효 산출물 금지, FLR-AGT-002).
    선물 fetch 실패 시 직전 JSON 유지(stale 명시 — as_of_kst 불변).
    """
    kst_date = _kst_today()
    out_path = OUT_DIR / f"{kst_date}.json"
    if out_path.exists():
        try:
            payload = json.loads(out_path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[us-digest:intraday] 기존 JSON read 실패: {exc}", file=sys.stderr)
            return None
    else:
        # 오늘 모닝 빌드 부재 → 직전 거래일 마감 데이터 seed (선물 단독 표시 목적).
        seed = _latest_prior_us_indices(kst_date)
        if seed is None:
            print(
                "[us-digest:intraday] 오늘 JSON 부재 + 직전 유효 파일 없음 — skip "
                "(seed 불가, 빈 산출물 금지)",
                file=sys.stderr,
            )
            return None
        # seed 복사 — trade_date_local/indices/news_chips 등 마감 데이터 보존.
        # 묵은 선물·갱신시각은 제거(아래에서 당일 선물·시각으로 재설정).
        payload = dict(seed)
        payload.pop("futures", None)
        payload.pop("intraday_refreshed_kst", None)
        print(
            f"[us-digest:intraday] 오늘 JSON 부재 — 직전 거래일 seed "
            f"(trade_date_local={payload.get('trade_date_local')}) + 선물 오버레이",
            file=sys.stderr,
        )

    # 1) 선물 갱신 (핵심). 실패 시 직전 futures 유지(as_of_kst 안 바뀜 → stale 표시).
    # collect_futures() 는 news 미포함 → 아래 3)에서 직전/신규 실시간 뉴스를 재부착.
    futures = collect_futures()
    if futures:
        payload["futures"] = futures
    else:
        print(
            "[us-digest:intraday] 선물 수집 실패 — 직전 futures 유지(stale)",
            file=sys.stderr,
        )

    # 1.5) 정규장 지수 장중 갱신 (Q-20260608 정규장 장중 실시간) — fetch_intraday raw
    # 1회 재활용(추가 API 콜 0). 항상 spark 갱신 + **개장 시** point/change_pct/candle/
    # daily_expanded 당일값 교체. 폐장 시 마감값 보존(회귀 0). regular_open 플래그를
    # payload + indices[].session_open 에 실어 프론트가 "장중"/"미장 마감" 분기.
    regular_open = _refresh_index_intraday(payload)
    payload["regular_open"] = regular_open
    for ix in payload.get("indices") or []:
        if isinstance(ix, dict):
            ix["session_open"] = regular_open
    # P1 (Q-20260609) — 개장 중이면 top-level trade_date_local 도 미 현지 거래일로 동기화.
    #   seed(금요장 6/5)/한국 날짜(6/9) 오매핑 교정. 폐장 시엔 seed 마감일 그대로(무회귀).
    if regular_open:
        us_td = _us_trade_date_from_indices(payload)
        if us_td:
            payload["trade_date_local"] = us_td

    # 2) news_chips: 수집 기사 집합이 직전과 달라졌을 때만 LLM 재호출 (비용 절제).
    # 전체 수집 url-set 시그니처를 `_article_sig` 로 박제 → 동일하면 LLM skip.
    # (news_chips[].url 5건만 비교하면 매 fire 재호출 — 전체 집합 대조가 정답.)
    news_rows = _fetch_us_rss() + _fetch_hk_global_naver()
    cur_sig = _article_signature(news_rows)
    prev_sig = payload.get("_article_sig")
    articles_changed = bool(news_rows) and cur_sig != prev_sig
    if articles_changed:
        refreshed = _sort_chips_time_desc_impact_pin(
            extract_us_news_chips(news_rows), news_rows
        )
        if refreshed:
            payload["news_chips"] = refreshed
            payload["_article_sig"] = cur_sig
            print(
                "[us-digest:intraday] news_chips 갱신(기사 집합 변경)", file=sys.stderr
            )
    else:
        print(
            "[us-digest:intraday] 기사 집합 동일 — news_chips LLM 호출 skip",
            file=sys.stderr,
        )

    # 3) 선물 전용 *실시간* 뉴스 (Q-20260608-143) — 정규장 indices[].news(전일 마감)와 시제 분리.
    # 비용 절제: news_chips 와 동일 기사-집합 시그니처(_article_sig) 기준으로 변경 시에만 LLM 재호출,
    #   미변경 시 직전 결과(_futures_news)를 그대로 재부착(매 10분 재요약 토큰 부담 회피).
    # collect_futures() 는 매 fire news 미포함 신규 dict → 직전/신규 뉴스를 항상 재부착해야 유지됨.
    fut_news = payload.get("_futures_news") or {}
    if articles_changed:
        regenerated = extract_futures_news(news_rows)
        if regenerated:
            fut_news = regenerated
            payload["_futures_news"] = regenerated
            print(
                "[us-digest:intraday] 선물 실시간 뉴스 갱신(기사 집합 변경)",
                file=sys.stderr,
            )
        # regenerated 빈 dict(LLM 실패/무관)면 직전 fut_news 유지(stale) — 정규장 fallback 0.
    else:
        print(
            "[us-digest:intraday] 기사 집합 동일 — 선물 뉴스 LLM 호출 skip",
            file=sys.stderr,
        )
    # 신규 futures 페이로드에 (신규 or 직전) 실시간 뉴스 재부착. 대응 키 없으면 미부착.
    if isinstance(payload.get("futures"), dict):
        _attach_futures_news(payload["futures"], fut_news)

    payload["intraday_refreshed_kst"] = datetime.now(ZoneInfo("Asia/Seoul")).isoformat()

    if dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return payload

    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"[us-digest:intraday] updated {out_path}", file=sys.stderr)
    return payload


def main():
    ap = argparse.ArgumentParser(description="미국 야간 미장요약 JSON 빌드")
    ap.add_argument(
        "--dry-run", action="store_true", help="JSON stdout 만 (배포 안 함)"
    )
    ap.add_argument(
        "--intraday",
        action="store_true",
        help="장중 경량 갱신(선물+신규뉴스만, 아침 산출물 보존)",
    )
    args = ap.parse_args()
    if args.intraday:
        result = refresh_intraday(dry_run=args.dry_run)
    else:
        result = build(dry_run=args.dry_run)
    if result is None:
        sys.exit(1)


if __name__ == "__main__":
    main()
