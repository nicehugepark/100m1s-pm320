"""테마뉴스 전용 RSS 수집. 메인 collect_rss와 동일 소스를 재사용.

종목 매칭은 메인이 담당하므로, 여기서는 *수집된 뉴스 ID 집합*을 반환만 한다.
themes.db에는 실제 뉴스를 다시 적재하지 않는다 (단일 소스 원칙: stocks.db.news).
"""

from __future__ import annotations

from scripts.news_pipeline.collect_rss import collect_all, collect_global


def collect():
    """RSS 수집. 메인 collect_rss와 동일 호출. 호출 측이 stocks.db에 적재."""
    domestic = collect_all()
    global_items = collect_global()
    return {"domestic": domestic, "global": global_items}


if __name__ == "__main__":
    out = collect()
    print(f"국내 {len(out['domestic'])}건 / 글로벌 {len(out['global'])}건")
