"""뉴지 — debug/*.html 기반 재파싱 (fetch 없음, LLM 뉴스 분석 skip).

목적:
  - data/cafe/debug/{pid}.html + {pid}.meta.json 을 소스로
  - 새 parse_post (형식 분류 + rank_table/essay) 적용
  - data/cafe/posts/{pid}.json 덮어쓰기
  - data/cafe/index.json 을 재생성 (posts 디렉토리 기준)

LLM 호출 안 함 — 기존 news_cards는 url/source/theme_hint 만 보존.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from main import ARTICLE_URL_TEMPLATE, parse_post, _extract_post_date  # noqa: E402

KST = timezone(timedelta(hours=9))
REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data" / "cafe"
DEBUG_DIR = DATA_DIR / "debug"
POSTS_DIR = DATA_DIR / "posts"
INDEX_PATH = DATA_DIR / "index.json"


def reparse_one(pid: str) -> dict | None:
    html_path = DEBUG_DIR / f"{pid}.html"
    meta_path = DEBUG_DIR / f"{pid}.meta.json"
    if not html_path.exists():
        print(f"[skip] {pid}: html 없음", file=sys.stderr)
        return None
    html = html_path.read_text(encoding="utf-8")
    title = None
    meta_fetched = None
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            title = meta.get("title") or None
            meta_fetched = meta.get("fetched_at")
        except json.JSONDecodeError:
            pass

    parsed = parse_post(html, title=title)

    stock_count = sum(len(s["stocks"]) for s in parsed.get("sections", []))
    news_count = sum(
        len(stock.get("news_cards", []))
        for s in parsed.get("sections", [])
        for stock in s["stocks"]
    )

    record = {
        "post_id": pid,
        "post_url": ARTICLE_URL_TEMPLATE.format(article_id=pid),
        "title": title,
        "post_date": parsed.get("post_date"),
        "fetched_at": meta_fetched or datetime.now(KST).isoformat(timespec="seconds"),
        "reparsed_at": datetime.now(KST).isoformat(timespec="seconds"),
        "parse_format": parsed.get("parse_format"),
        "parse_status": parsed.get("parse_status"),
        "stock_count": stock_count,
        "news_count": news_count,
        "sections": parsed.get("sections", []),
    }
    if "essay" in parsed:
        record["essay"] = parsed["essay"]

    POSTS_DIR.mkdir(parents=True, exist_ok=True)
    (POSTS_DIR / f"{pid}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return record


def main() -> int:
    debug_ids = sorted(
        (p.stem for p in DEBUG_DIR.glob("*.html")),
        key=lambda s: int(s) if s.isdigit() else 0,
        reverse=True,
    )
    print(f"debug 대상 {len(debug_ids)}개")

    fmt_counts = {"rank_table": 0, "essay": 0, "unknown": 0}

    for pid in debug_ids:
        rec = reparse_one(pid)
        if not rec:
            continue
        fmt = rec.get("parse_format") or "unknown"
        fmt_counts[fmt] = fmt_counts.get(fmt, 0) + 1

    # debug 없는 posts — body_snippet·title 로 post_date 재추출 시도
    debug_set = set(debug_ids)
    nondebug_reextract = 0
    for pj in sorted(POSTS_DIR.glob("*.json")):
        pid = pj.stem
        if pid in debug_set:
            continue
        try:
            rec = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if rec.get("post_date"):
            continue
        body = (rec.get("essay") or {}).get("body_snippet") or ""
        new_pd = _extract_post_date(body, rec.get("title"), None)
        if new_pd:
            rec["post_date"] = new_pd
            pj.write_text(
                json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            nondebug_reextract += 1
    print(f"debug 없는 post 재추출: {nondebug_reextract}건")

    # index.json: 전체 posts/*.json 스캔 (debug 없는 과거 post도 포함)
    index_posts = []
    date_filled = 0
    title_filled = 0
    for pj in sorted(POSTS_DIR.glob("*.json")):
        try:
            rec = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        pid = rec.get("post_id") or pj.stem
        fmt = rec.get("parse_format") or "unknown"
        if rec.get("post_date"):
            date_filled += 1
        if rec.get("title"):
            title_filled += 1
        index_posts.append(
            {
                "post_id": pid,
                "title": rec.get("title"),
                "post_date": rec.get("post_date"),
                "fetched_at": rec.get("fetched_at"),
                "parse_format": fmt,
                "stock_count": rec.get("stock_count", 0),
                "news_count": rec.get("news_count", 0),
            }
        )

    # index.json 덮어쓰기
    index_posts.sort(key=lambda p: p.get("post_id", ""), reverse=True)
    INDEX_PATH.write_text(
        json.dumps(
            {
                "posts": index_posts,
                "updated_at": datetime.now(KST).isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    total = len(index_posts)
    print(f"index 전체: {total}건")
    print(f"  debug 재파싱 형식별: rank_table={fmt_counts['rank_table']} essay={fmt_counts['essay']} unknown={fmt_counts['unknown']}")
    print(f"  post_date 채움: {date_filled}/{total}")
    print(f"  title 채움: {title_filled}/{total}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
