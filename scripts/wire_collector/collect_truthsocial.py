#!/usr/bin/env python3
"""트루스 소셜 트럼프 게시물 wire lane collector — 야간 미국 속보 커버리지 강화.

대표 GO 2026-06-14 23:13 (밤새 트럼프 야간 커버) / 법무 조건부 GO DOC-20260614-LEGAL-002.

배경: wire 야간 미국 속보 = 정부 RSS 3종(SEC/Fed/WH)뿐 — 트럼프 실시간 발언을 못 잡는다.
      트루스 소셜 트럼프 게시물 = 공개·실시간이나 공식 developer API 없음 → 3자 스크래퍼 경유.

소스 (§11.15 외부 spec 사전 검증 통과 — WebSearch 2회 + 공식 문서 cross-check + 본 주석 verbatim):
  - ScrapeCreators Truth Social API   https://api.scrapecreators.com/v1/truthsocial/user/posts
    * GET ?handle=realDonaldTrump&trim=true  /  헤더 x-api-key (env SCRAPE_CREATORS_API_KEY)
    * 응답 envelope: {"success": bool, "posts": [...], "next_max_id": str}
    * post 필드: id / text(plain) / content(HTML) / created_at(ISO) / url / uri / account
    * 무료 100 크레딧·카드 불요·만료 없음 (테스트/저빈도 야간 폴링에 충분).
      출처: scrapecreators.com/truthsocial-api + docs.scrapecreators.com (2026-06-14 실측)
    * 2025-08-27 이후 트루스 소셜은 트럼프·밴스 등 주요 인물 공개 프로필만 비인증 조회 허용
      → @realDonaldTrump 는 fetch 가능 (법무 §1 hiQ 공개·비인증 데이터 범위).

🔴 법무 5대 운영 게이트 (DOC-20260614-LEGAL-002 §III — 본 모듈이 직접 집행):
  1. 전문 복제·장기 저장 금지 → 본문은 EXCERPT_CHARS 발췌만, 산출 JSON 에 full text 비저장.
     (interpret_wire 가 한국어 요약 생성 후 영문 본문은 노출 운반체에서 소거 — wire_news.json 에는
      ko_title/causal_summary 만 잔존, 영문 게시물 원형 미보관.)
  2. 출처 링크 항상 병기 → 모든 항목 url = 트루스 소셜 원문 직링크 (없으면 항목 제외).
  3. 발화 주체 명확 귀속 → source="트럼프(트루스 소셜)" + interpret_wire 프롬프트가 "트럼프 발언" 귀속.
  4. 수집량·빈도 최소 → 트럼프 계정 단일, MAX_POSTS 상한, run 당 1 API 호출 (전수·고빈도 금지).
  5. 차단 시 graceful degradation → API 키 부재/HTTP 실패/JSON 손상 = 빈 리스트 반환 (예외 전파 0).
     collect_wire 가 다른 feed 와 동일하게 격리 — 트루스 끊겨도 wire 전체 무중단.

저장 필드: title(발췌)+source+url(원문)+published_at(KST) — collect_wire 항목 shape 동형.
산출: collect_wire 가 FEEDS 와 합산 → wire_news.json (interpret_wire 가 KO 해석).
"""

from __future__ import annotations

import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

KST = timezone(timedelta(hours=9))
UTC = timezone(timedelta(hours=0))
# collect_wire.UA 동일 값 (순환 import 회피 — 변경 시 양쪽 동기)
UA = "Mozilla/5.0 (compatible; 100m1s-wire/1.0; nicehugepark@gmail.com)"

API_BASE = "https://api.scrapecreators.com/v1/truthsocial/user/posts"
SOURCE_LABEL = "트럼프(트루스 소셜)"  # 법무 §3 발화 주체 명확 귀속
HANDLE = os.environ.get("TRUTHSOCIAL_HANDLE", "realDonaldTrump")

# 법무 §1 — 발췌 상한 (전문 비복제). interpret_wire LLM 입력용 짧은 발췌만.
EXCERPT_CHARS = 500
# 법무 §4 — run 당 수집 게시물 상한 (전수 금지). 야간 누적 + 48h 윈도우면 충분.
MAX_POSTS = 20
# 야간 트럼프 커버 윈도우 — collect_wire WINDOW_HOURS(48) 와 동일 컷오프를 collect_wire 가 재적용.
WINDOW_HOURS = 48
FETCH_TIMEOUT = 20


def _api_key() -> str | None:
    """SCRAPE_CREATORS_API_KEY 환경변수 또는 .env 파일에서 로드 (graceful — 부재 시 None)."""
    key = os.environ.get("SCRAPE_CREATORS_API_KEY")
    if key:
        return key.strip()
    # .env 폴백 (news_pipeline 관행 — 키는 .env, 코드/JSON 비노출)
    for env_path in (
        os.path.join(
            os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            ),
            ".env",
        ),
    ):
        try:
            with open(env_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("SCRAPE_CREATORS_API_KEY="):
                        return line.split("=", 1)[1].strip().strip("'\"")
        except OSError:
            continue
    return None


class _StripHTML(HTMLParser):
    """content(HTML) → 평문 (stdlib-only — bs4 신규 의존성 회피). <br>/<p> 경계는 공백."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._buf: list[str] = []

    def handle_data(self, data):
        self._buf.append(data)

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "p"):
            self._buf.append(" ")

    def text(self) -> str:
        return " ".join("".join(self._buf).split())


def _plain_text(post: dict) -> str:
    """post.text(평문) 우선, 없으면 content(HTML) strip. 법무 §1 — 발췌만 반환."""
    text = (post.get("text") or "").strip()
    if not text:
        raw = post.get("content") or ""
        parser = _StripHTML()
        try:
            parser.feed(html.unescape(raw))
            parser.close()
            text = parser.text()
        except Exception:  # noqa: BLE001 — 파싱 실패 시 빈 문자열 (항목 자연 제외)
            text = ""
    return text.strip()


def _parse_created(text: str):
    """created_at(ISO 또는 RFC822) → KST datetime | None (시각 파싱 실패 시 제외 — 조작 금지)."""
    text = (text or "").strip()
    if not text:
        return None
    # ISO 8601 (Mastodon/Truth Social 표준: "2026-06-14T13:05:22.000Z")
    iso = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        try:
            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(KST)


# 게시물 제목 = 발췌 1줄 (개행 정규화 후 EXCERPT_CHARS). 빈 발췌(미디어 only)는 호출부에서 제외.
_WS_RE = re.compile(r"\s+")


def _title_from_post(post: dict) -> str:
    text = _plain_text(post)
    text = _WS_RE.sub(" ", text).strip()
    return text[:EXCERPT_CHARS]


def fetch_posts(api_key: str, handle: str):
    """ScrapeCreators 1 호출 → ([{title, source, url, published_at}], status) (법무 §4 run당 1 호출)."""
    url = f"{API_BASE}?handle={urllib.parse.quote(handle)}&trim=true"
    # S310 suppress: url 은 고정 ScrapeCreators API + quote 처리 handle 만 — 사용자 자유입력 0.
    req = urllib.request.Request(  # noqa: S310
        url,
        headers={"x-api-key": api_key, "User-Agent": UA, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:  # noqa: S310
        status = resp.status
        raw = resp.read()
    data = json.loads(raw)
    posts = data.get("posts")
    if not isinstance(posts, list):
        print(
            f"[truthsocial] 응답에 posts 배열 없음 (success={data.get('success')})",
            file=sys.stderr,
        )
        return [], status
    out = []
    for post in posts[:MAX_POSTS]:
        if not isinstance(post, dict):
            continue
        link = (post.get("url") or post.get("uri") or "").strip()
        title = _title_from_post(post)
        dt = _parse_created(post.get("created_at"))
        # 법무 §2 — 원문 링크 없으면 제외 (출처 병기 불가 항목 노출 금지).
        # 발췌 빈 항목(이미지/영상 only)도 제외 — 해석할 텍스트 없음.
        if not link.startswith("http") or not title or dt is None:
            continue
        out.append(
            {
                "title": title,
                "source": SOURCE_LABEL,
                "url": link,
                "published_at": dt.isoformat(timespec="seconds"),
            }
        )
    return out, status


def collect():
    """트럼프 게시물 수집 → wire 항목 리스트 (graceful: 실패 시 빈 리스트).

    collect_wire.collect() 가 FEEDS 결과와 extend 합산하여 단일 wire_news.json 산출.
    예외·키부재·HTTP실패 = 빈 리스트 (법무 §5 graceful degradation — wire 전체 무중단).
    """
    api_key = _api_key()
    if not api_key:
        print(
            "[truthsocial] SCRAPE_CREATORS_API_KEY 미설정 — 트럼프 lane SKIP "
            "(.env 에 키 추가 시 활성. 무료 100크레딧: app.scrapecreators.com)",
            file=sys.stderr,
        )
        return []
    try:
        items, status = fetch_posts(api_key, HANDLE)
    except urllib.error.HTTPError as e:
        # 401/403 = 키 무효·차단 / 429 = rate / 5xx = 서버. 모두 graceful (빈 리스트).
        print(
            f"[truthsocial] HTTP {e.code} {e.reason} — 트럼프 lane SKIP ({HANDLE})",
            file=sys.stderr,
        )
        return []
    except Exception as e:  # noqa: BLE001 — 한 lane 실패가 wire 전체를 죽이면 안 됨 (법무 §5)
        print(
            f"[truthsocial] FAIL {type(e).__name__}: {e} — 트럼프 lane SKIP",
            file=sys.stderr,
        )
        return []
    latest = max((i["published_at"] for i in items), default="-")
    print(
        f"[truthsocial] HTTP={status} handle={HANDLE} items={len(items)} latest_kst={latest}"
    )
    return items


if __name__ == "__main__":
    # 단독 실행 = 수집 결과 표준출력 (collect_wire 통합 전 검증용).
    result = collect()
    print(
        json.dumps(
            {"source": SOURCE_LABEL, "count": len(result), "items": result},
            ensure_ascii=False,
            indent=2,
        )
    )
    sys.exit(0)
