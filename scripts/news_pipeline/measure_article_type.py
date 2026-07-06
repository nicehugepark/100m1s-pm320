#!/usr/bin/env python3
"""P1a 측정 스크립트 — news.article_type 의미분류 오분류율 측정용.

article_type이 채워진 후, type 분포 + "호재인데 시세정형/사건사고로 분류된" false-negative
의심건을 사람 라벨 대조용 CSV로 출력한다. (라이브 정렬 미반영 shadow 데이터.)

사용:
    python -m scripts.news_pipeline.measure_article_type --date 2026-05-28 --min 50
    python -m scripts.news_pipeline.measure_article_type --code 066570
출력:
    - stdout: type 분포 + [특징주] 머리표 분류 교차표 + false-negative 의심 리스트
    - CSV (--csv 지정 시): url,stock_code,title,is_robot,article_type 전수 (사람 라벨 대조용)
"""

from __future__ import annotations

import argparse
import csv
import sys

from .db import connect

# [특징주] 등 머리표인데 시세정형이 아닌(=실호재 가능성) 의심 판정 키워드.
# 측정 목적: 머리표 기사가 호재/시세정형 중 어디로 갔는지 교차 확인.
_HEADLINE_TAGS = ("[특징주]", "[장중특징주]", "[시황]", "[마감]")


def _fetch(args) -> list[dict]:
    where = ["article_type IS NOT NULL"]
    params: list = []
    if args.code:
        where.append("stock_code = ?")
        params.append(args.code)
    if args.date:
        where.append("date(published_at) = ?")
        params.append(args.date)
    sql = (
        "SELECT stock_code, title, url, COALESCE(is_robot,0) AS is_robot, article_type "
        "FROM news WHERE " + " AND ".join(where) + " ORDER BY published_at DESC"
    )
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="published_at 날짜 필터 YYYY-MM-DD")
    ap.add_argument("--code", help="종목코드 필터")
    ap.add_argument("--min", type=int, default=50, help="최소 샘플 수 경고 임계")
    ap.add_argument("--csv", help="사람 라벨 대조용 CSV 출력 경로")
    args = ap.parse_args(argv)

    rows = _fetch(args)
    n = len(rows)
    print(f"=== article_type 측정 (N={n}) ===")
    if n < args.min:
        print(
            f"[경고] 샘플 {n} < 최소 {args.min}. 더 많은 종목 interpret 후 재측정 권고."
        )

    # 1) type 분포
    dist: dict[str, int] = {}
    for r in rows:
        dist[r["article_type"]] = dist.get(r["article_type"], 0) + 1
    print("\n[type 분포]")
    for t, c in sorted(dist.items(), key=lambda x: -x[1]):
        print(f"  {t}: {c} ({c / n * 100:.1f}%)" if n else f"  {t}: {c}")

    # 2) 머리표([특징주] 등) 교차표 — robot_block 패턴이 호재로 갔는지 시세정형으로 갔는지
    print("\n[머리표(특징주/시황/마감) 기사 → 분류 교차]")
    tag_cross: dict[str, int] = {}
    tag_total = 0
    for r in rows:
        title = r["title"] or ""
        if any(title.lstrip().startswith(tag) for tag in _HEADLINE_TAGS):
            tag_total += 1
            tag_cross[r["article_type"]] = tag_cross.get(r["article_type"], 0) + 1
    if tag_total:
        for t, c in sorted(tag_cross.items(), key=lambda x: -x[1]):
            print(f"  머리표 → {t}: {c} ({c / tag_total * 100:.1f}%)")
    else:
        print("  (머리표 기사 없음)")

    # 3) false-negative 의심 — 머리표인데 호재로 분류된 건(올바른 catch 후보) +
    #    호재 단어가 제목에 있는데 시세정형/사건사고로 분류된 건(오분류 의심)
    print("\n[검토 필요 — 머리표 기사가 호재로 분류된 건 (올바른 catch 후보)]")
    catch = [
        r
        for r in rows
        if any((r["title"] or "").lstrip().startswith(t) for t in _HEADLINE_TAGS)
        and r["article_type"] == "호재"
    ]
    for r in catch[:30]:
        print(f"  [{r['stock_code']}] {r['article_type']} | {(r['title'] or '')[:60]}")
    print(f"  ... 총 {len(catch)}건")

    # 4) CSV 전수 출력 (사람 라벨 대조용)
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(
                [
                    "stock_code",
                    "url",
                    "is_robot",
                    "article_type",
                    "human_label",
                    "title",
                ]
            )
            for r in rows:
                w.writerow(
                    [
                        r["stock_code"],
                        r["url"],
                        r["is_robot"],
                        r["article_type"],
                        "",  # human_label 빈칸 — 사람이 채워 대조
                        r["title"],
                    ]
                )
        print(f"\n[CSV] {args.csv} ({n} rows, human_label 컬럼 빈칸)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
