"""
뉴스 아이템 → 종목 매칭.
종목명 regex 매칭 (제목 + 본문) + 로봇 기사 필터 적용.
idempotency key로 중복 방지.

[FLR 참조: FLR-20260409-TEC-001] 사명 부분문자열 오탐 방지 — 경계 regex + 블랙리스트
[REQ: DOC-20260414-REQ-002] 로봇기사 필터링 강화 + 본문 기반 매칭
[FIX: 본문 오매칭 방지] 등장 2회+ 필터 + 시황 기사 스킵 + AMBIGUOUS 확장
"""

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from .db import connect

# ── 로봇 기사 필터 ──────────────────────────────────────────

# 제목 패턴 기반 로봇 기사 (가비지 뉴스)
ROBOT_PATTERNS = [
    # 기존 패턴
    re.compile(
        r"주가[,、]\s*\d+월\s*\d+일.*[\d,]+원.*[\d.]+%.*(상승|하락|급등|급락).*(마감|거래)"
    ),
    re.compile(r"주가\s*📈|실시간\s*차트|Snapshot\s*\||Company\s*Guide"),
    re.compile(r"Stock\s*Price\s*Today|Live\s*Ticker|과거\s*가격\s*데이터"),
    # REQ-002 추가: [강세 토픽], [약세 토픽]
    re.compile(r"\[강세\s*토픽\]"),
    re.compile(r"\[약세\s*토픽\]"),
    # VI 발동/해제
    re.compile(r"VI\s*발동"),
    re.compile(r"VI\s*해제"),
    # 52주 신고가/신저가
    re.compile(r"52주\s*신고가"),
    re.compile(r"52주\s*신저가"),
    # 상한가/하한가
    re.compile(r"상한가"),
    re.compile(r"하한가"),
    # 상승폭/하락폭 확대
    re.compile(r"상승폭\s*확대"),
    re.compile(r"하락폭\s*확대"),
    # 거래량 급증/폭발
    re.compile(r"거래량\s*(급증|폭발|돌파)"),
    # 순수 등락률만 있는 기사: "종목명, +XX.XX%" 또는 "종목명, -XX.XX%"
    re.compile(r"^\s*\S+,?\s*[+-]?\d+[\d.]*%\s*(급등|급락|상승|하락)?\s*$"),
    # 기존 VI/신고가/등락률 (숫자 접두)
    re.compile(r"[+-]?\d+[\d.]*%\s*VI\s*발동"),
    re.compile(r"[+-]?\d+[\d.]*%\s*\d+주\s*신(고|저)가"),
    re.compile(r"[+-]?\d+[\d.]*%\s*(상한가|하한가)"),
    re.compile(r"거래량\s*(폭발|급증|돌파).*\d+만?\s*주"),
]
ROBOT_DOMAINS = {
    "comp.fnguide.com",
    "thinkpool.com",
    "valueline.co.kr",
    "paxnet.co.kr",
    "tossinvest.com",
    "finance.daum.net",
    "alphasquare.co.kr",
}


def is_robot(item):
    """로봇/가비지 기사 여부 판단."""
    title = item.get("title", "") if isinstance(item, dict) else item
    if any(p.search(title) for p in ROBOT_PATTERNS):
        return True
    url = item.get("url", "") if isinstance(item, dict) else ""
    return any(d in url for d in ROBOT_DOMAINS)


def is_robot_title(title: str) -> bool:
    """제목 문자열만으로 로봇 기사 판단 (DB 레코드 재검사용)."""
    return any(p.search(title) for p in ROBOT_PATTERNS)


# ── 종목명 매칭 ──────────────────────────────────────────


def idempotency_key(code, published_at, url):
    h = hashlib.sha1(url.encode()).hexdigest()[:12]
    return f"{code}:{published_at}:{h}"


# 사명 오탐 방지: 앞뒤 경계 문자로 한글/영숫자가 붙으면 부분 일치로 간주
# 예: "하이브" vs "하이브리드", "DSR" vs "DSR통신", "로젠" vs "에이프로젠"
_BOUNDARY = r"(?<![가-힣A-Za-z0-9])(?:{})(?![가-힣A-Za-z0-9])"

# 일반명사·복합어 오탐이 심한 2글자 사명 수동 블랙리스트 (신뢰도 0)
AMBIGUOUS_SHORT_NAMES = {
    "도부",
    "로젠",
    "하이브",
    "케이엠",
    "DSR",
    "SK",
    "LG",
    "GS",
    # REQ-002 본문 매칭에서 발견된 일반명사 오탐
    "남성",  # 남성(003920) — "남성" 일반명사 빈출
    "대상",  # 대상(001680) — "대상" 일반명사 빈출
    "신원",  # 신원(009270) — "신원" 일반명사 빈출
    "신흥",  # 신흥(004080) — "신흥" 일반명사 빈출
    "HDC",  # HDC(012630) — 자회사 뉴스에서 과잉 매칭
    "SBS",  # SBS(034120) — 연예 뉴스에서 과잉 매칭
    "SM",  # SM C&C — 연예 뉴스에서 과잉 매칭
}

# 시황/종합 기사 패턴: 이런 제목이면 본문 매칭 스킵 (개별 종목 기사가 아님)
MARKET_OVERVIEW_PATTERNS = [
    re.compile(r"코스피.*마감"),
    re.compile(r"코스닥.*마감"),
    re.compile(r"장.*마감|개장|시황"),
    re.compile(r"증시.*동향|증시.*전망"),
    re.compile(r"(코스피|코스닥)\s*\d+"),  # "코스피 5967" 등 지수 언급
    re.compile(r"외국인.*순매수|기관.*순매수|개인.*순매수"),
    re.compile(r"환율|달러.*원"),
]


def load_stock_names():
    with connect() as conn:
        rows = conn.execute("SELECT code, name FROM stocks").fetchall()
    out = []
    for r in rows:
        name = r["name"].strip()
        if len(name) < 2:
            continue
        # 경계 매칭 regex 사전 컴파일
        pattern = re.compile(_BOUNDARY.format(re.escape(name)))
        out.append((r["code"], name, pattern))
    return out


def _match_title(title: str, stocks: list) -> list:
    """제목에서 종목명 매칭. [(code, name)] 반환."""
    matched = []
    for code, name, pattern in stocks:
        if name in AMBIGUOUS_SHORT_NAMES:
            continue
        if pattern.search(title):
            matched.append((code, name))
    return matched


# ── 본문 기반 매칭 ──────────────────────────────────────────


def _fetch_body(url: str) -> str:
    """기사 URL에서 본문 텍스트를 추출. interpret_stocks.py의 fetch_article_body 재사용."""
    from .interpret_stocks import fetch_article_body

    return fetch_article_body(url, max_chars=3000)


def _match_body(
    body: str, stocks: list, exclude_codes: set = None, dp_codes: set = None
) -> list:
    """본문에서 종목명 매칭.

    Args:
        exclude_codes: 이미 제목 매칭된 종목 (중복 방지)
        dp_codes: daily_picks 종목 코드 set. 비어있지 않으면 해당 종목만 매칭 대상.
    Returns:
        [(code, name)]
    """
    if not body or len(body) < 50:
        return []
    exclude = exclude_codes or set()
    matched = []
    for code, name, pattern in stocks:
        if code in exclude:
            continue
        # daily_picks 필터: dp_codes가 주어지면 해당 종목만 매칭
        if dp_codes and code not in dp_codes:
            continue
        if name in AMBIGUOUS_SHORT_NAMES:
            continue
        # 등장 횟수 2회 이상만 매칭 — 1회 언급은 맥락상 관련 가능성 낮음
        count = len(pattern.findall(body))
        if count >= 2:
            matched.append((code, name))
    return matched


# ── 메인 매칭 + 저장 ──────────────────────────────────────────


def match_and_store(items, enable_body_match: bool = True):
    """RSS 아이템을 종목 매칭하여 news 테이블에 저장.

    Args:
        items: RSS 수집 결과 [{title, url, published_at, source}]
        enable_body_match: True면 제목 매칭 실패 시 본문 fetch하여 매칭 시도
    """
    stocks = load_stock_names()
    now = datetime.now().isoformat()
    matched = 0
    body_matched = 0
    robot_skipped = 0

    # daily_picks 종목 코드 로드 (본문 매칭 대상 제한 — 전체 종목은 너무 느림)
    dp_codes = set()
    if enable_body_match:
        with connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT stock_code FROM daily_picks ORDER BY date DESC LIMIT 100"
            ).fetchall()
            dp_codes = {r["stock_code"] for r in rows}

    with connect() as conn:
        # is_robot, match_source 컬럼 존재 보장 (마이그레이션)
        _ensure_columns(conn)

        for item in items:
            robot = is_robot(item)
            title = item["title"]

            # 제목 기반 매칭
            title_matches = _match_title(title, stocks)

            if robot:
                # 로봇 기사: 저장은 하되 is_robot=1 마킹
                for code, _name in title_matches:
                    key = idempotency_key(code, item["published_at"], item["url"])
                    try:
                        conn.execute(
                            """INSERT OR IGNORE INTO news(stock_code, title, url, published_at, source,
                                                causal_chain, macro_event, evidence_span,
                                                collected_at, idempotency_key, is_robot, match_source)
                               VALUES(?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, 1, 'title')""",
                            (
                                code,
                                title,
                                item["url"],
                                item["published_at"],
                                item["source"],
                                now,
                                key,
                            ),
                        )
                        robot_skipped += 1
                    except Exception:
                        pass  # UNIQUE violation = 중복
                continue

            # 비로봇 기사: 제목 매칭
            for code, _name in title_matches:
                key = idempotency_key(code, item["published_at"], item["url"])
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO news(stock_code, title, url, published_at, source,
                                            causal_chain, macro_event, evidence_span,
                                            collected_at, idempotency_key, is_robot, match_source)
                           VALUES(?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, 0, 'title')""",
                        (
                            code,
                            title,
                            item["url"],
                            item["published_at"],
                            item["source"],
                            now,
                            key,
                        ),
                    )
                    matched += 1
                except Exception:
                    pass

            # 본문 매칭: 제목 매칭이 없고, enable_body_match이면 본문 시도
            if enable_body_match and not title_matches:
                # 시황/종합 기사는 본문 매칭 스킵 (개별 종목에 대한 기사가 아님)
                if any(p.search(title) for p in MARKET_OVERVIEW_PATTERNS):
                    continue
                body = _fetch_body(item["url"])
                if body:
                    body_matches = _match_body(body, stocks, dp_codes=dp_codes)
                    for code, _name in body_matches:
                        key = idempotency_key(code, item["published_at"], item["url"])
                        try:
                            conn.execute(
                                """INSERT OR IGNORE INTO news(stock_code, title, url, published_at, source,
                                                    causal_chain, macro_event, evidence_span,
                                                    collected_at, idempotency_key, is_robot, match_source)
                                   VALUES(?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, 0, 'body')""",
                                (
                                    code,
                                    title,
                                    item["url"],
                                    item["published_at"],
                                    item["source"],
                                    now,
                                    key,
                                ),
                            )
                            body_matched += 1
                        except Exception:
                            pass

        conn.commit()

    print(
        f"title matched: {matched}, body matched: {body_matched}, robot skipped: {robot_skipped}"
    )
    return matched + body_matched


def _ensure_columns(conn):
    """news 테이블에 is_robot, match_source 컬럼이 없으면 추가 (마이그레이션)."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(news)").fetchall()}
    if "is_robot" not in cols:
        conn.execute("ALTER TABLE news ADD COLUMN is_robot INTEGER DEFAULT 0")
    if "match_source" not in cols:
        conn.execute("ALTER TABLE news ADD COLUMN match_source TEXT DEFAULT 'title'")


# ── 글로벌 뉴스 저장 ──────────────────────────────────────────


def store_global_news(items: list):
    """글로벌 뉴스를 stock_code='MACRO'로 저장 (매크로 이벤트 추출용)."""
    now = datetime.now().isoformat()
    stored = 0
    with connect() as conn:
        for item in items:
            key = idempotency_key("MACRO", item["published_at"], item["url"])
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO news(stock_code, title, url, published_at, source,
                                       collected_at, idempotency_key)
                       VALUES('MACRO', ?, ?, ?, ?, ?, ?)""",
                    (
                        item["title"],
                        item["url"],
                        item["published_at"],
                        item["source"],
                        now,
                        key,
                    ),
                )
                stored += 1
            except Exception:
                pass
        conn.commit()
    print(f"global news stored: {stored}")
    return stored


# ── 재매칭: 기존 로봇 기사 마킹 + 본문 기반 신규 매칭 ──────────


def rematch_existing(target_date: str = None):
    """기존 뉴스의 로봇 기사 마킹 + 미매칭 RSS에서 본문 기반 매칭.

    1단계: 기존 news 테이블에서 로봇 기사를 is_robot=1로 마킹
    2단계: RSS 재수집 → 제목 미매칭 기사를 본문 매칭 시도
    """
    stocks = load_stock_names()
    now = datetime.now().isoformat()

    # daily_picks 종목 코드 로드 (본문 매칭 대상 제한)
    with connect() as conn:
        dp_rows = conn.execute(
            "SELECT DISTINCT stock_code FROM daily_picks ORDER BY date DESC LIMIT 100"
        ).fetchall()
        dp_codes = {r["stock_code"] for r in dp_rows}

    # 1단계: 기존 로봇 기사 마킹
    with connect() as conn:
        _ensure_columns(conn)

        # 날짜 필터
        date_clause = ""
        params = []
        if target_date:
            date_clause = "AND date(published_at) = ?"
            params = [target_date]

        # stock_code != 'MACRO'인 뉴스 중 로봇 기사 마킹
        rows = conn.execute(
            f"""SELECT id, title FROM news
                WHERE stock_code != 'MACRO' {date_clause}""",
            params,
        ).fetchall()

        robot_marked = 0
        for row in rows:
            if is_robot_title(row["title"]):
                conn.execute("UPDATE news SET is_robot = 1 WHERE id = ?", (row["id"],))
                robot_marked += 1
        conn.commit()
        print(f"[rematch] robot articles marked: {robot_marked}")

    # 2단계: RSS 재수집 → 본문 매칭
    print("[rematch] collecting RSS for body matching...")
    from .collect_rss import collect_all

    rss_items = collect_all()

    # 날짜 필터링
    if target_date:
        rss_items = [i for i in rss_items if i["published_at"].startswith(target_date)]

    # 로봇 기사 제외 + 이미 매칭된 URL 제외
    with connect() as conn:
        existing_urls = set()
        rows = conn.execute(
            f"SELECT DISTINCT url FROM news WHERE stock_code != 'MACRO' {date_clause}",
            params,
        ).fetchall()
        existing_urls = {r["url"] for r in rows}

    # 미매칭 + 비로봇 + 비시황 기사만 추출
    candidates = []
    for item in rss_items:
        if is_robot(item):
            continue
        title_matches = _match_title(item["title"], stocks)
        if title_matches:
            # 이미 제목 매칭 가능 — 기존 match_and_store에서 처리됨
            continue
        # 시황/종합 기사는 본문 매칭 스킵
        if any(p.search(item["title"]) for p in MARKET_OVERVIEW_PATTERNS):
            continue
        if item["url"] in existing_urls:
            continue
        candidates.append(item)

    print(f"[rematch] body match candidates: {len(candidates)}")

    # 본문 fetch + 매칭 (병렬)
    body_matched = 0
    with connect() as conn:
        _ensure_columns(conn)

        def _fetch_and_match(item):
            body = _fetch_body(item["url"])
            if not body:
                return []
            matches = _match_body(body, stocks, dp_codes=dp_codes)
            return [(item, code, name) for code, name in matches]

        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = {pool.submit(_fetch_and_match, item): item for item in candidates}
            for fut in as_completed(futures):
                try:
                    results = fut.result(timeout=15)
                    for item, code, name in results:
                        key = idempotency_key(code, item["published_at"], item["url"])
                        try:
                            conn.execute(
                                """INSERT OR IGNORE INTO news(stock_code, title, url, published_at, source,
                                                    causal_chain, macro_event, evidence_span,
                                                    collected_at, idempotency_key, is_robot, match_source)
                                   VALUES(?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, 0, 'body')""",
                                (
                                    code,
                                    item["title"],
                                    item["url"],
                                    item["published_at"],
                                    item["source"],
                                    now,
                                    key,
                                ),
                            )
                            body_matched += 1
                            print(f"  [body] {name} ← {item['title'][:60]}")
                        except Exception:
                            pass
                except Exception as exc:
                    print(f"  [body] fetch error: {exc}")

        conn.commit()

    print(f"[rematch] body matched total: {body_matched}")
    return {"robot_marked": robot_marked, "body_matched": body_matched}


if __name__ == "__main__":
    # CLI: python -m scripts.news_pipeline.match_stocks [--rematch] [--date YYYY-MM-DD] [--body]
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--rematch", action="store_true", help="기존 데이터 재매칭")
    parser.add_argument("--date", type=str, default=None, help="대상 날짜 (YYYY-MM-DD)")
    parser.add_argument("--body", action="store_true", help="본문 매칭 활성화")
    args = parser.parse_args()

    if args.rematch:
        rematch_existing(target_date=args.date)
    else:
        from .collect_rss import collect_all

        match_and_store(collect_all(), enable_body_match=args.body)
