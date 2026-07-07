"""파서(main.py) 산출 → cafe.db 어댑터 입력 변환 glue.

단일 정합 지점(SoT) — main.py 실시간 배선과 cafe_backfill.py 재파싱이 이 모듈을
공유한다. 파서 반환 형태가 바뀌면 여기 한 곳만 고치면 양쪽에 반영된다
(FLR-20260406-TEC-001 recurring: fix 누락 방지 = 공통 모듈화).

파서 실측 반환(main.py parse_post, 2026-07-06):
  kind="theme_map" →
    { parse_format, parse_status, is_fallback, post_date,
      mappings: [{ theme: str|None, stocks: [종목명(str), ...], reason: str|None }],
      title_chain: [테마(str), ...], sections: [] }
  kind="limit_hl" →
    { parse_format, parse_status, is_fallback, post_date,
      sections: [{ type: "상승"|"하락", stocks: [{ name, ticker, theme_label,
                   news_cards: [{url, ...}, ...] }, ...] }, ...] }

cafe_db 어댑터 기대 입력:
  persist_theme_map(post_id, mappings=[{theme_name, parent_theme, seq,
                    stocks:[{stock_name, ticker, reason}]}])
  persist_market_summary(post_id, items=[{summary_date, section, stock_name,
                    ticker, reason}], news_links=[{url, host, anchor_text, seq}])

extract_stock_news_blocks(html) 실측 반환:
  [{stock_names:[str], url, source(=host), theme_label(=anchor)}]
"""

from __future__ import annotations

from urllib.parse import urlparse

# 파서 버전 SoT — main.py 실시간 배선 + cafe_backfill.py 재파싱 공용.
# 파서 로직 변경 시 여기만 올리면 양쪽 반영(백필 탈출 기준).
PARSER_VERSION = "2.0.0"  # 994 종목명 마스터 대조 정밀화(문장 오추출 제거). 2026-07-06


def theme_map_to_mappings(parsed: dict) -> list[dict]:
    """theme_map 파서 산출(mappings) → cafe_theme_mapping 입력.

    파서의 mapping 단위 = (테마, 종목명 리스트, 사유). DB 는 mapping→stock 계층이므로
    mapping 레벨 reason 을 각 종목에 부여(종목별 개별 사유는 파서 단계에 없음).
    parent_theme = title_chain 의 직전 원소(있으면). 없으면 None.
    """
    mappings = parsed.get("mappings")
    if not mappings:
        # 스텁/자동판별 경로: theme_map 데이터가 sections 로 온 경우 흡수.
        # sections[{type, parent, stocks:[{name, ticker, theme_label}]}] → mappings 형태로 정규화.
        norm: list[dict] = []
        for sec in parsed.get("sections") or []:
            names = [
                (s.get("name") or "").strip()
                for s in sec.get("stocks") or []
                if (s.get("name") or "").strip()
            ]
            reason = None
            for s in sec.get("stocks") or []:
                if s.get("theme_label") or s.get("reason"):
                    reason = s.get("theme_label") or s.get("reason")
                    break
            norm.append(
                {
                    "theme": sec.get("type"),
                    "parent": sec.get("parent"),
                    "stocks": names,
                    "reason": reason,
                }
            )
        mappings = norm
    title_chain = parsed.get("title_chain") or []
    # 제목 체인의 최상위(부모 후보) — 단일 값 사용(체인 있으면 첫 원소).
    parent_hint = title_chain[0] if title_chain else None

    out: list[dict] = []
    for i, m in enumerate(mappings):
        theme_name = (m.get("theme") or "").strip()
        if not theme_name:
            # 테마 헤더가 없던 종목(파서가 theme=None 로 flush) → 제목 체인/기본값 보정
            theme_name = (parent_hint or "미분류").strip()
        reason = m.get("reason")
        stocks = [
            {"stock_name": name, "ticker": None, "reason": reason}
            for name in (m.get("stocks") or [])
            if isinstance(name, str) and name.strip()
        ]
        # parent_theme: mapping 자체 parent 우선, 없으면 제목 체인 부모(자기참조 방지)
        parent = m.get("parent") or parent_hint
        if parent == theme_name:
            parent = None
        out.append(
            {
                "theme_name": theme_name,
                "parent_theme": parent,
                "seq": i,
                "stocks": stocks,
            }
        )
    return out


def limit_hl_to_market_items(parsed: dict) -> list[dict]:
    """limit_hl 파서 산출(sections) → cafe_market_item 입력."""
    out: list[dict] = []
    sdate = parsed.get("post_date")
    for sec in parsed.get("sections") or []:
        section = sec.get("type") or ""
        for s in sec.get("stocks") or []:
            name = (s.get("name") or "").strip()
            if not name:
                continue
            out.append(
                {
                    "summary_date": sdate,
                    "section": section,
                    "stock_name": name,
                    "ticker": s.get("ticker"),
                    "reason": s.get("theme_label"),
                }
            )
    return out


def news_blocks_to_links(blocks: list[dict] | None) -> list[dict]:
    """extract_stock_news_blocks 산출 → cafe_news_link 입력.

    블록 필드명 실측: url / source(host) / theme_label(anchor). backfill 스텁이
    host/text 키를 쓸 수도 있어 양쪽 키를 COALESCE 로 흡수.
    """
    out: list[dict] = []
    seen: set[str] = set()
    for i, b in enumerate(blocks or []):
        url = (b.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(
            {
                "url": url,
                "host": b.get("source") or b.get("host") or urlparse(url).netloc,
                "anchor_text": b.get("theme_label") or b.get("text"),
                "seq": i,
            }
        )
    return out
