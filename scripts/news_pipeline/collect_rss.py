"""
언론사 RSS 국내 + 글로벌 병렬 수집.
저장: title, url, published_at, source 만. 본문 금지.
글로벌 RSS는 매크로 이벤트 전용 (종목 매칭 안 함).
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from .config import RSS_FEEDS, RSS_FEEDS_GLOBAL


def _parse(source: str, url: str):
    import feedparser

    feed = feedparser.parse(url)
    items = []
    for e in feed.entries:
        title = getattr(e, "title", "").strip()
        link = getattr(e, "link", "").strip()
        if not title or not link:
            continue
        try:
            pub_struct = getattr(e, "published_parsed", None) or getattr(
                e, "updated_parsed", None
            )
            pub_iso = (
                datetime(*pub_struct[:6]).isoformat()
                if pub_struct
                else datetime.now().isoformat()
            )
        except Exception:
            pub_iso = datetime.now().isoformat()
        items.append(
            {
                "title": title,
                "url": link,
                "published_at": pub_iso,
                "source": source,
            }
        )
    return items


def collect_all():
    """국내 RSS 수집 (종목 매칭 대상)."""
    all_items = []
    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = {pool.submit(_parse, src, url): src for src, url in RSS_FEEDS.items()}
        for fut in as_completed(futs):
            src = futs[fut]
            try:
                items = fut.result(timeout=30)
                print(f"[{src}] {len(items)} items")
                all_items.extend(items)
            except Exception as exc:
                print(f"[{src}] FAIL: {exc}")
    return all_items


def collect_global():
    """글로벌 RSS 수집 (매크로 이벤트 전용, 종목 매칭 안 함)."""
    all_items = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        futs = {
            pool.submit(_parse, src, url): src for src, url in RSS_FEEDS_GLOBAL.items()
        }
        for fut in as_completed(futs):
            src = futs[fut]
            try:
                items = fut.result(timeout=30)
                print(f"[글로벌:{src}] {len(items)} items")
                all_items.extend(items)
            except Exception as exc:
                print(f"[글로벌:{src}] FAIL: {exc}")
    return all_items


if __name__ == "__main__":
    items = collect_all()
    global_items = collect_global()
    print(f"국내: {len(items)}, 글로벌: {len(global_items)}")
