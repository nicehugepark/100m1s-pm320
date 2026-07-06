"""news_review.llm_response.themes → stock_themes + stock_theme_daily 백필.

배경 (이시카와 작업):
  4/24 라이브 JSON에서 고영(098460) 외 다수 종목 카드에 뉴스 요약·인사이트는
  있으나 테마칩(themes)이 비어 있는 현상 발견. stock_theme_daily 4/23·4/24
  전체 0건. 원인 복합:

  1. interpret 실행 중 `database is locked` 에러로 link_stock_theme 실패
     (pipeline-2026-04-24.log: "sqlite3.OperationalError: database is locked").
  2. togusa fix가 사전 외 테마(반도체검사장비/AI반도체 등)만 반환 →
     interpret_stocks.py:476-479의 사전 거부로 빈 배열.

  본 스크립트는 news_review에 이미 저장된 LLM 응답을 재활용하여
  themes 필드를 추출·정규화·사전 매칭·DB 머지 (LLM 재호출 없음).

전략:
  - 대상 날짜의 news_review 모든 행 순회 (agent in (ishikawa,togusa), verdict in (good,bad))
  - llm_response JSON에서 themes 배열 추출
  - 같은 (date, stock_code)의 모든 응답 themes를 union (가장 풍부한 정보 채택)
  - normalize_list로 alias 통합 (반도체검사장비 → 반도체)
  - canonical 사전 외 테마는 거부 (interpret_stocks와 동일 정책)
  - link_stock_theme + stock_theme_daily INSERT OR IGNORE 동시 기록

사용:
  PIPELINE_DATE=2026-04-24 python -m scripts.news_pipeline.backfill_themes_from_review
  또는 인자: python -m scripts.news_pipeline.backfill_themes_from_review 2026-04-23 2026-04-24
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime

from .db import connect
from .theme_normalizer import link_stock_theme, normalize_list


def _load_canonical() -> set:
    """themes 테이블의 활성 테마 set 반환."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT name FROM themes WHERE is_active=1 OR is_active IS NULL"
        ).fetchall()
    return {r["name"] for r in rows}


def _extract_themes(llm_response: str) -> list:
    """llm_response JSON에서 themes 배열 추출."""
    if not llm_response:
        return []
    try:
        obj = json.loads(llm_response)
    except (json.JSONDecodeError, TypeError):
        return []
    raw = obj.get("themes") or []
    if not isinstance(raw, list):
        return []
    return [t for t in raw if isinstance(t, str) and t.strip()]


def backfill_date(target_date: str) -> dict:
    """단일 날짜 백필. 통계 dict 반환."""
    canonical = _load_canonical()
    stats = {
        "date": target_date,
        "stocks_processed": 0,
        "stocks_with_themes": 0,
        "themes_linked_total": 0,
        "rejected_themes": {},
    }

    with connect() as conn:
        rows = conn.execute(
            """SELECT stock_code, agent, verdict, llm_response
               FROM news_review
               WHERE date=?
               ORDER BY created_at""",
            (target_date,),
        ).fetchall()

    if not rows:
        print(f"[{target_date}] news_review 없음 — skip")
        return stats

    # (stock_code) → set(theme_names)
    by_stock: dict[str, set] = {}
    for r in rows:
        themes_raw = _extract_themes(r["llm_response"])
        if not themes_raw:
            continue
        by_stock.setdefault(r["stock_code"], set()).update(themes_raw)

    stats["stocks_processed"] = len({r["stock_code"] for r in rows})

    for code, theme_set in by_stock.items():
        # 정규화 (alias 통합)
        normalized = normalize_list(list(theme_set))
        # canonical 사전 매칭
        accepted = []
        for t in normalized:
            if t in canonical:
                accepted.append(t)
            else:
                stats["rejected_themes"].setdefault(t, []).append(code)

        if not accepted:
            continue

        stats["stocks_with_themes"] += 1
        for tname in accepted:
            # [Q-20260511-FIX-B-1-CALLCHAIN-FIX] C4 — raise ValueError catch +
            # stock_theme_daily 직접 INSERT는 link 성공 시에만 실행 (audit 권고).
            try:
                link_stock_theme(code, tname, target_date, "backfill_review")
            except ValueError as e:
                print(f"  [{code}] inactive canonical skip theme={tname} reason={e}")
                continue
            except Exception as e:
                print(f"  [{code}] link FAIL theme={tname} err={e}")
                continue
            # stock_theme_daily 직접 INSERT (link_stock_theme은 daily 업데이트 안 함)
            with connect() as conn:
                trow = conn.execute(
                    "SELECT id FROM themes WHERE name=?", (tname,)
                ).fetchone()
                if trow:
                    conn.execute(
                        """INSERT OR IGNORE INTO stock_theme_daily
                           (date, stock_code, theme_id, source, created_at)
                           VALUES(?,?,?,?,?)""",
                        (
                            target_date,
                            code,
                            trow["id"],
                            "backfill_review",
                            datetime.now().isoformat(),
                        ),
                    )
                    conn.commit()
                    stats["themes_linked_total"] += 1
        print(f"  [{code}] +{len(accepted)} themes: {accepted}")

    return stats


def main(dates: list[str]):
    print(f"=== backfill_themes_from_review: {dates} ===")
    overall = []
    for d in dates:
        s = backfill_date(d)
        overall.append(s)
        print(
            f"\n[{d}] stocks_processed={s['stocks_processed']} "
            f"stocks_with_themes={s['stocks_with_themes']} "
            f"themes_linked={s['themes_linked_total']}"
        )
        if s["rejected_themes"]:
            print(f"  rejected themes (사전 외, {len(s['rejected_themes'])}종):")
            for t, codes in sorted(
                s["rejected_themes"].items(), key=lambda x: -len(x[1])
            )[:10]:
                print(f"    - {t}: {len(codes)} stocks (e.g. {codes[:3]})")
    print("\n=== 완료 ===")
    return overall


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        targets = args
    else:
        env_date = os.environ.get("PIPELINE_DATE")
        if env_date:
            targets = [env_date]
        else:
            targets = [datetime.now().strftime("%Y-%m-%d")]
    main(targets)
