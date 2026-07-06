"""네이버 검색 API (공식) 기반 종목 뉴스 backfill 모듈.

Q-20260520-CYCLE19-012 P0 (옵션 B-1 채택, 2026-05-20 00:32 대표 결정).

목적
----
5/11+5/12 raw news 결손 (5/11=264, 5/12=243 vs 인접일 5/13=911, 5/14=895)
backfill. cron pipeline SSOT-guard 누적 SKIP 으로 RSS 본체 미진입한
이력 보완.

본 모듈은 RSS 수집 대체가 아니라 **history-only backfill 보조** 이다.
정상 운영은 collect_rss.py (RSS 11개 source + 글로벌 3개) 가 담당하며,
본 모듈은 과거 일자 결손 catch 시 종목명 검색으로 보강한다.

공식 spec (사전 검증 ≥3종, lead-meta §11.15)
-------------------------------------------
- Endpoint     : GET https://openapi.naver.com/v1/search/news.json
- Headers      : X-Naver-Client-Id, X-Naver-Client-Secret (.env)
- Params       :
    query   (str)  - 검색어, UTF-8 인코딩
    display (int)  - 1~100 (한 페이지 결과 수, max 100)
    start   (int)  - 1~1000 (페이지 시작 위치, max 1000 — 1000 초과 불가)
    sort    (str)  - 'sim' (정확도) | 'date' (날짜 내림차순, 본 모듈 사용)
- Response (items[]):
    title         - HTML <b> 태그 포함, 본 모듈 strip 처리
    originallink  - 원문 매체 URL
    link          - 네이버 뉴스 URL (없으면 originallink와 동일)
    description   - 본문 요약 (사용 안 함 — DB 본문 금지 정책 정합)
    pubDate       - RFC822 형식, 예: "Mon, 11 May 2026 19:14:00 +0900"
- 일일 한도    : 25,000 calls
- date range filter : 미지원 → client-side pubDate post-filter 의무

본 모듈 사전 검증 출처:
1. WebSearch: naver/naver-openapi-guide GitHub swagger (verbatim)
2. WebSearch: choonghyunryu.github.io Open API 가이드 (start max 1000)
3. WebFetch (developers.naver.com 차단 → 공식 swagger로 대체)

DB 적재 정합
-----------
- 테이블       : news (schema.sql §53~70 정합)
- idempotency_key : match_stocks.idempotency_key(code, published_at, url) 재사용
- INSERT OR IGNORE : UNIQUE 충돌 시 skip → 기존 264+243건 중복 자동 회피
- match_source : 'naver_api' (기존 'title'/'body'/'manual' 확장)
- is_robot     : match_stocks.is_robot() 재사용
- causal_chain/macro_event/evidence_span : NULL (LLM 후행)

cron pipeline SSOT 정합 (FLR-20260504-FLR-003 / lead-meta §11.27)
----------------------------------------------------------------
본 모듈은 단발 수동 실행 전용. cron 자동 호출 0건. stocks.db 만 write,
SSOT 4종 (MASTER-QUEUE / failures INDEX / records INDEX / sw.js) touch 0건.

호출 예시
--------
    cd /Users/seongjinpark/company/100m1s
    set -a; . ./.env; set +a
    python -m scripts.news_pipeline.naver_news_search \\
        --date-from 2026-05-11 --date-to 2026-05-12 \\
        --max-codes 2802

법무 정합
--------
- 본문 description 저장 0건 (제목 + URL + pubDate + source 만 적재).
- robots.txt 정합 책임은 Naver Open API (공식) 측 — 본 모듈은
  공식 API 호출만, 직접 스크랩 0건.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from datetime import (  # noqa: F401 — L124 timezone.utc 사용 (ruff UP017 ignore per pyproject.toml)
    datetime,
    timezone,
)

from .config import DB_PATH
from .db import connect
from .match_stocks import idempotency_key, is_robot

logger = logging.getLogger(__name__)

# ── 환경변수 (.env 사전 source 의무) ──────────────────────
NAVER_CLIENT_ID = os.environ.get("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.environ.get("NAVER_CLIENT_SECRET")

NAVER_ENDPOINT = "https://openapi.naver.com/v1/search/news.json"
NAVER_DISPLAY_MAX = 100
NAVER_START_MAX = 1000
NAVER_DAILY_CALL_LIMIT = 25_000  # 공식 문서 명시

# 호출 간 sleep (rate limit 안전 마진, 25,000/일 = 약 0.29회/초)
SLEEP_SEC = 0.15

# HTML 태그 strip (Naver 응답 title 에 <b> 강조 태그 포함)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """제목에서 <b>...</b> 등 HTML 태그 제거."""
    if not text:
        return ""
    return _TAG_RE.sub("", text).strip()


def _parse_pubdate(rfc822: str) -> str | None:
    """RFC822 → ISO8601 변환. 실패 시 None.

    예: 'Mon, 11 May 2026 19:14:00 +0900' → '2026-05-11T19:14:00+09:00'
    """
    if not rfc822:
        return None
    # email.utils.parsedate_to_datetime이 RFC822 표준 준수
    # NOTE: py3.9 호환 — datetime.UTC alias (3.11+) 미사용, timezone.utc 함수내 import
    from email.utils import parsedate_to_datetime

    try:
        dt = parsedate_to_datetime(rfc822)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except (TypeError, ValueError) as exc:
        logger.warning("pubDate parse 실패: %r (%s)", rfc822, exc)
        return None


def _date_in_range(iso_dt: str, date_from: str, date_to: str) -> bool:
    """ISO8601 published_at 이 [date_from, date_to] (YYYY-MM-DD, KST 기준) 범위 안인가.

    Naver pubDate 는 +0900 (KST) 로 도착하므로 date 부분 직접 비교.
    """
    if not iso_dt:
        return False
    return date_from <= iso_dt[:10] <= date_to


def _extract_source(originallink: str) -> str:
    """originallink 도메인에서 매체명 추출 (호스트명 → naver_api 접두).

    예: 'https://www.hankyung.com/article/...' → 'naver_api:hankyung.com'
    RSS source ('한경'/'매경' 등) 와 시각적 구분 + 11 source coverage
    회귀 test 시 분류 가능.
    """
    if not originallink:
        return "naver_api:unknown"
    try:
        parsed = urllib.parse.urlparse(originallink)
        host = parsed.hostname or "unknown"
        # www. 접두 제거 + 마지막 2~3 도메인만
        host = host.lstrip(".").removeprefix("www.")
        return f"naver_api:{host}"
    except Exception:
        return "naver_api:unknown"


def _http_get_json(url: str) -> dict:
    """Naver 검색 API GET 호출. JSON 파싱 후 반환.

    실패 시 dict (errorCode 포함) 또는 raise.
    """
    if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
        raise RuntimeError(
            "NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 미설정 — .env 사전 source 필요"
        )
    req = urllib.request.Request(url)
    req.add_header("X-Naver-Client-Id", NAVER_CLIENT_ID)
    req.add_header("X-Naver-Client-Secret", NAVER_CLIENT_SECRET)
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body)


def search_news(
    query: str, display: int = 100, start: int = 1, sort: str = "date"
) -> dict:
    """단일 페이지 검색. items[] 포함 dict 반환.

    파라미터 사전 검증 (공식 spec):
      display ∈ [1, 100], start ∈ [1, 1000], sort ∈ {'sim', 'date'}
    """
    display = max(1, min(display, NAVER_DISPLAY_MAX))
    start = max(1, min(start, NAVER_START_MAX))
    if sort not in ("sim", "date"):
        raise ValueError(f"sort 는 'sim' 또는 'date' 만 가능: {sort!r}")

    params = {
        "query": query,
        "display": display,
        "start": start,
        "sort": sort,
    }
    qs = urllib.parse.urlencode(params, encoding="utf-8")
    url = f"{NAVER_ENDPOINT}?{qs}"
    return _http_get_json(url)


def fetch_for_code(
    code: str,
    name: str,
    date_from: str,
    date_to: str,
    max_pages: int = 3,
) -> list[dict]:
    """단일 종목 backfill.

    page 1~max_pages 순회. sort=date (최신순) → date_to 보다 새로운 결과는 skip,
    date_from 보다 오래된 결과를 만나면 조기 종료 (페이지 절약).

    Returns:
        list of {code, title, url, published_at, source, is_robot}
    """
    rows: list[dict] = []
    early_stop = False

    for page in range(max_pages):
        start = 1 + page * NAVER_DISPLAY_MAX
        if start > NAVER_START_MAX:
            break

        try:
            resp = search_news(
                name, display=NAVER_DISPLAY_MAX, start=start, sort="date"
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 429:  # rate limit
                logger.warning("[%s/%s] 429 rate limit, 5초 대기", code, name)
                time.sleep(5)
                continue
            logger.error("[%s/%s] HTTP %d: %s", code, name, exc.code, exc.reason)
            break
        except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
            logger.error("[%s/%s] fetch 실패: %s", code, name, exc)
            break

        items = resp.get("items", [])
        if not items:
            break

        page_oldest = None
        for it in items:
            title = _strip_html(it.get("title", ""))
            link = it.get("link") or it.get("originallink") or ""
            originallink = it.get("originallink") or link
            pubdate_iso = _parse_pubdate(it.get("pubDate", ""))

            if not (title and link and pubdate_iso):
                continue

            page_oldest = pubdate_iso  # 마지막 item 이 가장 오래된 (sort=date)

            # date range post-filter
            if not _date_in_range(pubdate_iso, date_from, date_to):
                continue

            row = {
                "code": code,
                "title": title,
                "url": link,
                "published_at": pubdate_iso,
                "source": _extract_source(originallink),
            }
            row["is_robot"] = 1 if is_robot(row) else 0
            rows.append(row)

        # 조기 종료: 페이지 끝 item 이 date_from 보다 오래되었으면 다음 페이지 의미 없음
        if page_oldest and page_oldest[:10] < date_from:
            early_stop = True
            break

        time.sleep(SLEEP_SEC)

    logger.debug("[%s/%s] %d rows (early_stop=%s)", code, name, len(rows), early_stop)
    return rows


def insert_rows(rows: Iterable[dict]) -> tuple[int, int]:
    """INSERT OR IGNORE 으로 news 테이블 적재.

    Returns:
        (inserted, skipped) — skipped 는 idempotency_key UNIQUE 충돌 + 예외 합.
    """
    inserted = 0
    skipped = 0
    now = datetime.now().isoformat()

    with connect() as conn:
        for r in rows:
            key = idempotency_key(r["code"], r["published_at"], r["url"])
            try:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO news(
                          stock_code, title, url, published_at, source,
                          causal_chain, macro_event, evidence_span,
                          collected_at, idempotency_key, is_robot, match_source)
                       VALUES(?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?, 'naver_api')""",
                    (
                        r["code"],
                        r["title"],
                        r["url"],
                        r["published_at"],
                        r["source"],
                        now,
                        key,
                        r["is_robot"],
                    ),
                )
                if cur.rowcount > 0:
                    inserted += 1
                else:
                    skipped += 1
            except Exception as exc:
                logger.warning("INSERT 실패 (skip): %s", exc)
                skipped += 1
        conn.commit()

    return inserted, skipped


def load_stock_targets(limit: int | None = None) -> list[tuple[str, str]]:
    """stocks 테이블에서 (code, name) 로드.

    match_stocks.AMBIGUOUS_SHORT_NAMES 필터링은 본 모듈에서 적용하지 않음
    (Naver 검색은 정확 검색이므로 일반명사 오탐 가능성이 RSS 매칭보다 낮음).
    """
    with connect() as conn:
        cur = conn.execute("SELECT code, name FROM stocks ORDER BY code")
        rows = cur.fetchall()
    out = [
        (r["code"], r["name"].strip())
        for r in rows
        if r["name"] and len(r["name"]) >= 2
    ]
    if limit:
        out = out[:limit]
    return out


def run_backfill(
    date_from: str,
    date_to: str,
    max_codes: int | None = None,
    max_pages: int = 3,
) -> dict:
    """전체 backfill 실행. 결과 통계 dict 반환."""
    targets = load_stock_targets(limit=max_codes)
    print(
        f"[naver_backfill] targets={len(targets)} codes, range=[{date_from}, {date_to}]"
    )

    total_fetched = 0
    total_inserted = 0
    total_skipped = 0
    api_calls = 0
    sources_seen: dict[str, int] = {}

    for idx, (code, name) in enumerate(targets, 1):
        rows = fetch_for_code(code, name, date_from, date_to, max_pages=max_pages)
        api_calls += min(
            max_pages, max(1, (len(rows) + NAVER_DISPLAY_MAX - 1) // NAVER_DISPLAY_MAX)
        )
        total_fetched += len(rows)

        for r in rows:
            sources_seen[r["source"]] = sources_seen.get(r["source"], 0) + 1

        if rows:
            ins, skp = insert_rows(rows)
            total_inserted += ins
            total_skipped += skp

        if idx % 100 == 0:
            print(
                f"[naver_backfill] {idx}/{len(targets)} done — "
                f"fetched={total_fetched} inserted={total_inserted} "
                f"skipped={total_skipped} api_calls~{api_calls}"
            )

    summary = {
        "date_from": date_from,
        "date_to": date_to,
        "targets": len(targets),
        "fetched": total_fetched,
        "inserted": total_inserted,
        "skipped": total_skipped,
        "api_calls_approx": api_calls,
        "sources": sources_seen,
        "db_path": str(DB_PATH),
        "finished_at": datetime.now().isoformat(),
    }
    return summary


def main():
    parser = argparse.ArgumentParser(
        description="Naver Open API 기반 종목 뉴스 backfill (옵션 B-1)"
    )
    parser.add_argument("--date-from", required=True, help="YYYY-MM-DD (inclusive)")
    parser.add_argument("--date-to", required=True, help="YYYY-MM-DD (inclusive)")
    parser.add_argument(
        "--max-codes",
        type=int,
        default=None,
        help="최대 종목 수 (테스트용, default=전체)",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=3,
        help="종목당 최대 페이지 수 (default=3, 100*3=300건/종목)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    summary = run_backfill(args.date_from, args.date_to, args.max_codes, args.max_pages)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
