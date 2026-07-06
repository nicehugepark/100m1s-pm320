"""
매크로 이벤트 추출 — LLM 프롬프팅 기반 (2026-04-10 전환).

1차: Claude CLI(haiku)로 뉴스 제목 목록에서 매크로 이벤트 추출.
폴백: LLM 실패 시 PRIORITY_KEYWORDS 키워드 빈도 카운팅으로 보완.

[FLR 참조: FLR-20260410-TEC-001] summary를 DB에 영속화.
[FLR 참조: 우크라 단일 키워드 맥락 부재] LLM이 맥락 판단하여 해결.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta

from .db import connect

# REQ-20260420-REQ-003: LLM 캐시 적용. autoflake가 import를 제거한 사고
# (라이브 cron NameError, 11:02·11:32) 재발 방지를 위해 noqa: F401 명시.
# 사용처는 _extract_via_llm 내부.
from .llm_client import (  # noqa: F401
    call_model_cached,
    hash_input,
    to_sqlite_text,  # noqa: F401
)

# 매크로 추출 모델 — 기존 동작 유지 (haiku, REQ-003 model_version 키)
MACRO_MODEL = "haiku"

# LLM에 전달할 최대 뉴스 제목 수
MAX_NEWS_FOR_LLM = 80

# 최우선 매크로 키워드 — LLM 실패 시 폴백 전용
PRIORITY_KEYWORDS = {
    "트럼프",
    "Trump",
    "관세",
    "tariff",
    "무역전쟁",
    "미중",
    "트루스소셜",
    "TruthSocial",
    "연준",
    "Fed",
    "FOMC",
    "이란",
    "우크라",
    "NATO",
    "나토",
}

# 로봇기사 제목 접두사 패턴
ROBOT_ARTICLE_PREFIXES = (
    "[강세 토픽]",
    "[급등]",
    "[특징주]",
    "[시황]",
    "[마감시황]",
    "[장중시황]",
    "[개장시황]",
)

STOPWORDS = {
    "그리고",
    "있다",
    "있는",
    "하지만",
    "대한",
    "에서",
    "보다",
    "했다",
    "한다",
    "이날",
    "오늘",
    "어제",
    "이번",
    "올해",
    "지난",
    "이후",
    "이전",
    "이상",
    "이하",
    "대비",
    "전년",
    "전월",
    "전일",
    "내년",
    "위해",
    "따라",
    "통해",
    "관련",
    "가운데",
    "기자",
    "뉴스",
    "속보",
    "단독",
    "특징주",
    "마감",
    "개장",
    "장중",
    "거래",
    "종목",
    "주가",
    "증시",
    "코스피",
    "코스닥",
    "시장",
    "투자",
    "주식",
    "매수",
    "매도",
    "상승",
    "하락",
    "급등",
    "급락",
    "강세",
    "약세",
    "거래대금",
    "거래량",
    "시가총액",
    "기술",
    "사업",
    "획득",
    "제동",
    "요구",
    "확대",
    "축소",
    "전망",
    "분석",
    "발표",
    "계획",
    "진행",
    "추진",
    "검토",
    "결정",
    "참여",
    "협력",
    "개발",
    "생산",
    "수출",
    "매출",
    "실적",
    "성장",
    "기대",
    "우려",
    "가능",
    "필요",
    "변경",
    "공시",
    "보고",
    "전환",
    "조정",
    "개선",
    "강화",
    "지원",
    "운영",
    "도입",
    "적용",
    "수주",
    "계약",
    "대상",
    "보유",
    "매각",
    "공개",
    "인수",
    "출시",
    "체결",
    "확보",
    "평가",
    "청약",
    "신규",
    "상장",
    "중단",
    "역량",
    "수혜",
    "세계",
    "글로벌",
    "재건",
    "목표가",
    "영업이익",
    "상한가",
    "상승세",
    "하락세",
    "반도체",
    "솔루션",
    "인프라",
    "기판",
    "판권",
    "견본주택",
    "유상증자",
    "레버리지",
    "스타트업",
    "금감원",
    "정정신고서",
    "ETN",
    "ETF",
    "스팩",
    "압구정",
    "강남",
    "서초",
    "송파",
}

MIN_TOKEN_LEN = 3

_JUNK_RE = re.compile(
    r"^\d+$"
    r"|^\d+만원$|^\d+억$|^\d+조$"
    r"|[에를의은는이가로]$"
    r"|^최[상하]위$|^차세대$"
)

LLM_PROMPT_TEMPLATE = """한국 주식 매크로 이벤트 추출(이시카와). JSON 배열만.

## 뉴스 제목
{news_block}

## 원칙
- 인과 사슬 필수: "사건→경로→수혜/피해 섹터". (X)"이란,이스라엘,건설" (O)"이스라엘-이란 전쟁→중동 재건→한국 건설사 수주 기대"
- 교차검증: 2+ 독립 언론사=confirmed, 단일=unconfirmed
- 가비지 필터: VI발동·신고가·등락률 로봇기사·[강세 토픽]·[특징주] 라벨 인용 금지
- 매크로=섹터·시장 수준. 개별 IR·홍보·공시 제외. "왜 이 섹터로 돈이 몰리나?"
- 트럼프/정책: 메타 이슈. 관세·무역전쟁·금리·에너지·지정학 중 영향 트리 명시

## 규칙
1. 최대 5개 / 2. summary는 "사건→영향→섹터" 1문장 / 3. keyword 2~4글자 / 4. 2+ 언론사=confirmed / 5. sources에 실제 매체

영문 뉴스: 한국어로 번역·요약하여 summary 반영. 원문 노출 금지. 출처는 약어(예: "[CNBC]")로 sources에 기재.

출력:
[
  {{"summary": "미-이란 휴전 협상 재개 → 중동 재건 기대감 → 건설·인프라 섹터 이틀째 강세", "keyword": "이란휴전", "verified": true, "sources": ["한경","매경","이투데이"], "source_count": 3}}
]"""


def _is_robot_article(title: str) -> bool:
    return any(title.strip().startswith(p) for p in ROBOT_ARTICLE_PREFIXES)


def _ensure_verified_column(conn):
    """verified 컬럼이 없으면 추가 (기존 DB 마이그레이션)."""
    cols = {
        r["name"] for r in conn.execute("PRAGMA table_info(macro_events)").fetchall()
    }
    if "verified" not in cols:
        conn.execute("ALTER TABLE macro_events ADD COLUMN verified INTEGER DEFAULT 1")
        conn.commit()


def _extract_via_llm(
    news_rows: list, date: str | None = None, ignore_cache: bool = False
) -> list[dict] | None:
    """call_model_cached로 매크로 이벤트 추출 (REQ-003 캐시 적용).

    input_hash = sha256(정렬된 뉴스 제목 첫 MAX_NEWS_FOR_LLM개) — 같은 뉴스 집합이면
    같은 매크로. 모델 버전 변경 시 자동 invalidate. ignore_cache=True면 강제 재호출.
    """
    if not news_rows:
        return None

    # 뉴스 제목 블록 구성 (로봇기사 제외, 최대 MAX_NEWS_FOR_LLM건)
    lines = []
    title_set = []
    for r in news_rows:
        if _is_robot_article(r["title"]):
            continue
        lines.append(f"- {r['title']} ({r['source']})")
        title_set.append(r["title"])
    if not lines:
        return None

    lines = lines[:MAX_NEWS_FOR_LLM]
    title_set = title_set[:MAX_NEWS_FOR_LLM]
    news_block = "\n".join(lines)
    prompt = LLM_PROMPT_TEMPLATE.format(news_block=news_block)

    target_id = date or datetime.now().strftime("%Y-%m-%d")
    h = hash_input(title_set)
    text = call_model_cached(
        prompt,
        MACRO_MODEL,
        domain="extract_macros",
        target_id=target_id,
        input_hash=h,
        agent="ishikawa",
        timeout=120,
        ignore_cache=ignore_cache,
    )
    if not text:
        print("[extract_macros] LLM FAIL or empty")
        return None

    # gemini_usage 카운터 호환 유지 (interpret_stocks._today_usage 일일 cap용)
    _increment_usage(len(prompt), len(text))

    # JSON 파싱
    try:
        start = text.find("[")
        end = text.rfind("]")
        if start < 0 or end < 0:
            print("[extract_macros] no JSON array in LLM response")
            return None
        parsed = json.loads(text[start : end + 1])
        if not isinstance(parsed, list):
            print("[extract_macros] LLM response is not a list")
            return None
        return parsed
    except json.JSONDecodeError as exc:
        print(f"[extract_macros] JSON parse FAIL: {exc}")
        return None


def _increment_usage(prompt_len: int, response_len: int):
    """LLM 사용량 기록 — interpret_stocks.py와 동일 테이블."""
    today = datetime.now().strftime("%Y-%m-%d")
    in_tok = prompt_len // 4
    out_tok = response_len // 4
    cost = in_tok / 1_000_000 * 0.25 + out_tok / 1_000_000 * 1.25
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


def tokenize(title: str):
    tokens = re.findall(r"[가-힣A-Za-z0-9]{2,}", title)
    return [
        t
        for t in tokens
        if t not in STOPWORDS and len(t) >= MIN_TOKEN_LEN and not _JUNK_RE.search(t)
    ]


def _fallback_priority_keywords(news_rows: list, date: str):
    """LLM 실패 시 PRIORITY_KEYWORDS 전용 키워드 빈도 폴백.

    일반 키워드 빈도 카운팅은 폐기. PRIORITY_KEYWORDS만 1개 언론사로도 매크로 승격.
    """
    keyword_sources = defaultdict(set)
    keyword_titles = defaultdict(list)
    title_source_map = {}

    for r in news_rows:
        title_source_map[r["title"]] = r["source"]
        for tok in tokenize(r["title"]):
            if tok in PRIORITY_KEYWORDS:
                keyword_sources[tok].add(r["source"])
                keyword_titles[tok].append(r["title"])

    clusters = []
    now = datetime.now().isoformat()

    with connect() as conn:
        for kw, sources in keyword_sources.items():
            # 로봇기사 제외 소스 수
            non_robot = set()
            for t in keyword_titles[kw]:
                if kw in t and not _is_robot_article(t):
                    non_robot.add(title_source_map.get(t, ""))
            if not non_robot:
                continue

            # 관련 뉴스 제목들을 조합하여 최소한의 요약문 생성
            relevant = [
                t for t in keyword_titles[kw] if kw in t and not _is_robot_article(t)
            ]
            if not relevant:
                continue
            # 가장 토큰 다양성 높은 제목을 주축으로, 나머지에서 보충 맥락 추출
            relevant_sorted = sorted(
                relevant, key=lambda t: len(set(tokenize(t))), reverse=True
            )
            best = relevant_sorted[0]
            src_names = ", ".join(sorted(sources)[:3])
            if len(relevant_sorted) >= 2:
                # 2번째 제목에서 보충 맥락 추가
                supplement = relevant_sorted[1]
                summary = f"{best} / {supplement} ({src_names})"
            else:
                summary = f"{best} ({src_names})"

            conn.execute(
                """INSERT INTO macro_events(date, keyword, summary, source_count,
                                            stock_codes_json, created_at, source, verified)
                   VALUES(?, ?, ?, ?, '[]', ?, 'fallback', 0)""",
                (date, kw, summary, len(sources), now),
            )
            clusters.append((kw, len(sources)))
        conn.commit()

    return clusters


def extract(date: str = None, ignore_cache: bool = False):
    from .config import pipeline_date

    date = date or pipeline_date()
    since = (datetime.fromisoformat(date) - timedelta(days=1)).isoformat()

    with connect() as conn:
        _ensure_verified_column(conn)
        rows = conn.execute(
            """SELECT title, source, stock_code FROM news
               WHERE collected_at >= ?
               ORDER BY published_at DESC""",
            (since,),
        ).fetchall()

    if not rows:
        print("macro clusters: 0 (no news)")
        return []

    # 1차: LLM 추출 (캐시 적용 — REQ-003)
    llm_events = _extract_via_llm(rows, date=date, ignore_cache=ignore_cache)

    now = datetime.now().isoformat()
    clusters = []

    with connect() as conn:
        # 누적 구조: 기존 매크로는 보존, 새 매크로만 추가 (keyword 중복 시 summary 업데이트)
        # 하루 동안 뉴스가 누적되므로 매크로도 줄어들면 안 됨

        if llm_events:
            # 기존 keyword 목록 조회
            existing = {
                r[0]
                for r in conn.execute(
                    "SELECT keyword FROM macro_events WHERE date = ? AND source IN ('extract', 'llm', 'fallback')",
                    (date,),
                ).fetchall()
            }

            for evt in llm_events:
                # patch-C: LLM-derived 필드 to_sqlite_text 일괄 래핑
                summary = to_sqlite_text(evt.get("summary", ""))
                keyword = to_sqlite_text(evt.get("keyword", (summary or "")[:20]))
                verified = 1 if evt.get("verified", False) else 0
                source_count = evt.get("source_count", len(evt.get("sources", [])))
                if not isinstance(source_count, int):
                    try:
                        source_count = int(source_count)
                    except (TypeError, ValueError):
                        source_count = 0
                sources_json = json.dumps(
                    evt.get("sources", []), ensure_ascii=False, default=str
                )

                if keyword in existing:
                    # 기존 매크로: summary 등만 업데이트 — created_at(발생 origin 시각)은
                    # 보존(morning_pin). DOC-20260605-DSN(arch-pipeline §7.11):
                    # 매 cron rerun 시 created_at=now 로 덮어쓰면 오전 발생 매크로가
                    # 오후 시각으로 재기록 → build_daily morning_pin tie-break 무력화 →
                    # 오전 핵심 뉴스 밀어내기 root. UPDATE 에서 created_at 제거.
                    conn.execute(
                        """UPDATE macro_events SET summary=?, source_count=?, verified=?,
                                                   stock_codes_json=?
                           WHERE date=? AND keyword=? AND source IN ('extract', 'llm', 'fallback')""",
                        (
                            summary,
                            source_count,
                            verified,
                            sources_json,
                            date,
                            keyword,
                        ),
                    )
                else:
                    # 새 매크로: INSERT
                    conn.execute(
                        """INSERT INTO macro_events(date, keyword, summary, source_count,
                                                    stock_codes_json, created_at, source, verified)
                           VALUES(?, ?, ?, ?, ?, ?, 'llm', ?)""",
                        (
                            date,
                            keyword,
                            summary,
                            source_count,
                            sources_json,
                            now,
                            verified,
                        ),
                    )
                clusters.append((keyword, source_count))

            conn.commit()
            print(
                f"macro clusters: {len(clusters)} (LLM, {len(existing)} existing preserved)"
            )
            return clusters

        # LLM 실패 → PRIORITY_KEYWORDS 폴백
        conn.commit()

    fallback_clusters = _fallback_priority_keywords(rows, date)
    print(
        f"macro clusters: {len(fallback_clusters)} (fallback: PRIORITY_KEYWORDS only)"
    )
    return fallback_clusters


if __name__ == "__main__":
    import sys

    ignore_cache = "--ignore-cache" in sys.argv[1:]
    extract(ignore_cache=ignore_cache)
    from .llm_client import cache_stats

    print(f"[cache] {cache_stats()}")
