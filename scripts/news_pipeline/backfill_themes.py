"""
4/8~4/9 테마 데이터 보강 — 재처리 스크립트.
Phase 1: 이미 처리된 뉴스의 themes_json → stock_themes 반영 (날짜별)
Phase 2: 미처리 뉴스 interpret 재실행 (LLM 호출)
Phase 3: build_daily 재실행 → JSON 갱신

사용법:
  # Phase 1만 (LLM 없이, stock_themes 백필)
  PIPELINE_DATE=2026-04-09 python -m scripts.news_pipeline.backfill_themes --phase1

  # Phase 2 (LLM 호출로 미처리 뉴스 해석)
  PIPELINE_DATE=2026-04-09 python -m scripts.news_pipeline.backfill_themes --phase2

  # Phase 3 (build_daily 재실행)
  PIPELINE_DATE=2026-04-09 python -m scripts.news_pipeline.backfill_themes --phase3

  # 전체
  PIPELINE_DATE=2026-04-09 python -m scripts.news_pipeline.backfill_themes --all
"""

import sys

from .config import pipeline_date
from .db import connect
from .theme_normalizer import link_stock_theme, normalize_list


def phase1_backfill_stock_themes(target_date: str):
    """이미 interpret된 뉴스의 themes를 stock_themes 테이블에 날짜별로 반영."""
    print(f"\n=== Phase 1: stock_themes 백필 ({target_date}) ===")

    with connect() as conn:
        # 해당 날짜에 뉴스가 있는 종목의 기존 stock_themes 조회
        rows = conn.execute(
            """SELECT DISTINCT n.stock_code
               FROM news n
               JOIN stocks s ON n.stock_code = s.code
               WHERE date(n.published_at) = ?
                 AND n.causal_chain IS NOT NULL""",
            (target_date,),
        ).fetchall()

    if not rows:
        print(f"  처리된 뉴스 없음 ({target_date})")
        return 0

    linked = 0
    with connect() as conn:
        for row in rows:
            code = row["stock_code"]
            theme_rows = conn.execute(
                """SELECT t.name FROM stock_themes st
                   JOIN themes t ON st.theme_id = t.id
                   WHERE st.stock_code = ?""",
                (code,),
            ).fetchall()
            themes = normalize_list([r["name"] for r in theme_rows])
            for t in themes:
                # [Q-20260511-FIX-B-1-CALLCHAIN-FIX] C2 — raise ValueError catch
                try:
                    link_stock_theme(code, t, target_date, "backfill")
                    linked += 1
                except ValueError as e:
                    print(f"  [{code}] inactive canonical skip theme={t} reason={e}")
                except Exception as e:
                    print(f"  [{code}] link FAIL theme={t} err={e}")

    print(f"  {len(rows)}개 종목, {linked}개 stock_theme 링크 생성/갱신")
    return linked


def phase2_interpret_unprocessed(target_date: str):
    """해당 날짜의 뉴스를 interpret_stocks으로 (재)처리. target_date 기반 뉴스만."""
    print(f"\n=== Phase 2: 뉴스 interpret ({target_date}) ===")

    from .interpret_stocks import interpret

    with connect() as conn:
        # 해당 날짜에 뉴스가 있는 종목 전체 (재해석 포함)
        codes = conn.execute(
            """SELECT DISTINCT stock_code
               FROM news
               WHERE date(published_at) = ?
                 AND stock_code != 'MACRO'""",
            (target_date,),
        ).fetchall()

    if not codes:
        print(f"  뉴스 없음 ({target_date})")
        return 0

    code_list = [r["stock_code"] for r in codes]
    print(f"  {len(code_list)}개 종목 → interpret 시작 (target_date={target_date})")

    success = 0
    for code in code_list:
        result = interpret(code, target_date=target_date)
        if result:
            success += 1
            themes = normalize_list(result.get("themes", []))
            for t in themes:
                # [Q-20260511-FIX-B-1-CALLCHAIN-FIX] C3 — raise ValueError catch
                try:
                    link_stock_theme(code, t, target_date, "interpret")
                except ValueError as e:
                    print(f"  [{code}] inactive canonical skip theme={t} reason={e}")
                except Exception as e:
                    print(f"  [{code}] link FAIL theme={t} err={e}")
            print(f"  [{code}] OK — themes: {themes}")
        else:
            print(f"  [{code}] SKIP (no news or LLM fail)")

    print(f"  {success}/{len(code_list)} 종목 해석 완료")
    return success


def phase3_rebuild_daily(target_date: str):
    """build_daily 재실행 → stock JSON + theme stats 갱신."""
    import os

    print(f"\n=== Phase 3: build_daily 재빌드 ({target_date}) ===")
    os.environ["PIPELINE_DATE"] = target_date

    from .build_daily import build

    result = build()
    if result:
        print(f"  JSON 생성: {result}")
    else:
        print("  build 실패 또는 스킵")
    return result


def phase4_rebuild_theme_stats():
    """theme-trend.json, theme-tree.json 재생성."""
    print("\n=== Phase 4: theme stats 재빌드 ===")
    from .build_theme_stats import build

    build()
    print("  완료")


if __name__ == "__main__":
    target = pipeline_date()
    args = set(sys.argv[1:])

    if not args:
        print(__doc__)
        sys.exit(0)

    run_all = "--all" in args

    if run_all or "--phase1" in args:
        phase1_backfill_stock_themes(target)

    if run_all or "--phase2" in args:
        phase2_interpret_unprocessed(target)

    if run_all or "--phase3" in args:
        phase3_rebuild_daily(target)

    if run_all or "--phase4" in args:
        phase4_rebuild_theme_stats()
