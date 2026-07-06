"""news_review → news.causal_chain 백필 (DOC-20260422-REQ-002 후속).

배경:
  이시카와·토구사 판정이 `news_review`에는 정상 저장되지만,
  `interpret_stocks.py`의 `news` 테이블 UPDATE 경로가 동기화되지 않아
  (FLR-008 동종 버그) `news.causal_chain`, `news.macro_event`,
  `news.evidence_span`이 NULL로 남아 UI 카드의 인과 사슬이 빈 값이다.

동작:
  - `news_review`에서 (stock_code, date, title) 단위로 가장 신뢰도 높은
    판정을 선택:
      1순위: agent='togusa' AND verdict='good' AND evaluator='auto' (토구사 fix)
      2순위: agent='ishikawa' AND verdict='good' (이시카와 원본 정상)
      3순위: agent='ishikawa' AND verdict='pending' (토구사 검증 실패 fallback)
    같은 그룹 내에서는 created_at 최신 1건.
  - `llm_response` JSON을 파싱하여 causal_chain / macro_event / evidence_span 추출.
  - `news_titles`(JSON array)의 각 title에 대해
    `UPDATE news SET ... WHERE stock_code=? AND title=? AND date(published_at)=?`.
  - causal_chain 컬럼이 NULL이거나 빈 문자열인 행만 UPDATE (기존 값 보존).

idempotent:
  - 재실행해도 이미 값이 있는 행은 건드리지 않음 (WHERE causal_chain IS NULL OR ='').
  - 동일 news_review 데이터 → 동일 결과.

사용:
  # dry-run (쓰기 없이 영향 건수만 출력)
  python3 -m scripts.news_pipeline.backfill_news_causal --dry-run

  # 실제 실행
  python3 -m scripts.news_pipeline.backfill_news_causal

  # 특정 날짜 범위
  python3 -m scripts.news_pipeline.backfill_news_causal --since 2026-04-10 --until 2026-04-22

제약:
  - 단발성. cron 금지. 근본 원인 fix 후에는 불필요.
  - news_review에 판정이 없는 뉴스는 복구 불가 (당연).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable

from .db import connect


def _safe_json(raw: str):
    """llm_response가 ```json ...``` 코드펜스 또는 순수 JSON 모두 대응."""
    if not raw:
        return None
    s = raw.strip()
    if s.startswith("```"):
        # 코드펜스 제거
        lines = s.splitlines()
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        s = "\n".join(lines)
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return None


def _extract_payload(llm_response: str) -> dict | None:
    """news_review.llm_response에서 causal_chain / macro_event / evidence_span 추출.

    토구사 fix 형태: {"verdict":"bad","note":"...","fix":{...}} → fix 내부 사용.
    토구사 good on auto 저장분: 직접 {"causal_chain":..., ...} 저장된 경우도 있음
    (interpret_stocks.py line 397-399에서 fix 그대로 저장).
    이시카와 원본: {"causal_chain":..., "macro_event":..., ...}
    """
    parsed = _safe_json(llm_response)
    if not isinstance(parsed, dict):
        return None
    # fix 필드가 있으면 그쪽이 정답 (토구사 원본 응답)
    if isinstance(parsed.get("fix"), dict):
        payload = parsed["fix"]
    else:
        payload = parsed
    causal = _coerce_text(payload.get("causal_chain"))
    if not causal:
        return None
    return {
        "causal_chain": causal,
        "macro_event": _coerce_text(payload.get("macro_event")),
        "evidence_span": _coerce_text(payload.get("evidence_span")),
    }


def _coerce_text(v) -> str | None:
    """LLM 응답 필드가 str / list / dict 혼재 → 안전하게 TEXT로 변환.

    - str: strip 후 빈 문자열이면 None
    - list: ", ".join (문자열 원소만)
    - dict/기타: json.dumps 폴백
    """
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s or None
    if isinstance(v, list):
        parts = [x.strip() for x in v if isinstance(x, str) and x.strip()]
        if parts:
            return ", ".join(parts)
        try:
            return json.dumps(v, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(v)
    try:
        return json.dumps(v, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(v) or None


def _select_reviews(conn, since: str | None, until: str | None) -> list[dict]:
    """(stock_code, date, title) 단위로 신뢰도 최상위 1건 선택.

    priority:
      togusa/good/auto → 3
      ishikawa/good    → 2
      ishikawa/pending → 1
    같은 priority 내 created_at 최신.
    """
    where = []
    params: list = []
    if since:
        where.append("date >= ?")
        params.append(since)
    if until:
        where.append("date <= ?")
        params.append(until)
    where.append(
        "((agent='togusa' AND verdict='good' AND evaluator='auto')"
        " OR (agent='ishikawa' AND verdict='good')"
        " OR (agent='ishikawa' AND verdict='pending'))"
    )
    where_sql = " AND ".join(where)
    sql = f"""
        SELECT id, date, stock_code, agent, verdict, evaluator,
               news_titles, llm_response, created_at
        FROM news_review
        WHERE {where_sql}
        ORDER BY date, stock_code, created_at DESC
    """
    rows = conn.execute(sql, params).fetchall()
    # dedup by (stock_code, date, title) — priority high first
    priority = {
        ("togusa", "good", "auto"): 3,
        ("ishikawa", "good", "togusa"): 2,
        ("ishikawa", "good", None): 2,
        ("ishikawa", "good", ""): 2,
        ("ishikawa", "pending", None): 1,
        ("ishikawa", "pending", ""): 1,
        ("ishikawa", "pending", "togusa"): 1,
    }
    # key: (stock_code, date, title) → best review dict
    best: dict[tuple, dict] = {}
    for r in rows:
        titles_raw = r["news_titles"]
        try:
            titles = json.loads(titles_raw) if titles_raw else []
        except (ValueError, TypeError):
            titles = []
        if not isinstance(titles, list):
            continue
        pr = priority.get((r["agent"], r["verdict"], r["evaluator"]), 0)
        if pr == 0:
            continue
        payload = _extract_payload(r["llm_response"])
        if not payload:
            continue
        for title in titles:
            if not isinstance(title, str) or not title.strip():
                continue
            key = (r["stock_code"], r["date"], title.strip())
            existing = best.get(key)
            if existing is None or pr > existing["_priority"]:
                best[key] = {
                    "stock_code": r["stock_code"],
                    "date": r["date"],
                    "title": title.strip(),
                    "payload": payload,
                    "_priority": pr,
                }
    return list(best.values())


def _count_targets(conn) -> tuple[int, int]:
    """현재 causal_chain 비어있는 뉴스 수 / 영향 종목 수."""
    total = conn.execute(
        "SELECT COUNT(*) FROM news"
        " WHERE (causal_chain IS NULL OR causal_chain='')"
        " AND COALESCE(is_robot,0)=0"
    ).fetchone()[0]
    impacted = conn.execute(
        "SELECT COUNT(DISTINCT stock_code) FROM news"
        " WHERE (causal_chain IS NULL OR causal_chain='')"
        " AND COALESCE(is_robot,0)=0"
    ).fetchone()[0]
    return int(total), int(impacted)


def run(
    dry_run: bool = False,
    since: str | None = None,
    until: str | None = None,
) -> dict:
    with connect() as conn:
        reviews = _select_reviews(conn, since, until)
        missing_before, impacted = _count_targets(conn)

    print(f"[backfill_news_causal] news_review 매칭 후보: {len(reviews)}")
    print(f"[backfill_news_causal] news.causal_chain 빈 행: {missing_before}")
    print(f"[backfill_news_causal] 영향 종목 수           : {impacted}")

    if dry_run:
        print("[backfill_news_causal] --dry-run: DB 쓰기 생략")
        return {
            "reviews": len(reviews),
            "missing_before": missing_before,
            "impacted_stocks": impacted,
            "updated": 0,
            "dry_run": True,
        }

    updated = 0
    failed = 0
    with connect() as conn:
        for rv in reviews:
            try:
                cur = conn.execute(
                    """
                    UPDATE news
                       SET causal_chain=?, macro_event=?, evidence_span=?
                     WHERE stock_code=?
                       AND title=?
                       AND date(published_at)=?
                       AND (causal_chain IS NULL OR causal_chain='')
                    """,
                    (
                        rv["payload"]["causal_chain"],
                        rv["payload"]["macro_event"],
                        rv["payload"]["evidence_span"],
                        rv["stock_code"],
                        rv["title"],
                        rv["date"],
                    ),
                )
                updated += cur.rowcount if cur.rowcount > 0 else 0
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(
                    f"  [FAIL] {rv['stock_code']} / {rv['date']} / "
                    f"{rv['title'][:30]}: {exc}",
                    file=sys.stderr,
                )
        conn.commit()

    with connect() as conn:
        missing_after, _ = _count_targets(conn)

    print(
        f"[backfill_news_causal] UPDATE 성공 행: {updated} / 실패: {failed}\n"
        f"[backfill_news_causal] 빈 행 변화: {missing_before} → {missing_after}"
    )
    return {
        "reviews": len(reviews),
        "missing_before": missing_before,
        "missing_after": missing_after,
        "impacted_stocks": impacted,
        "updated": updated,
        "failed": failed,
        "dry_run": False,
    }


def _parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="backfill_news_causal",
        description=(
            "news_review 판정을 news 테이블 causal_chain/macro_event/"
            "evidence_span 컬럼으로 일괄 백필. DOC-20260422-REQ-002 후속."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="DB 쓰기 없이 영향 건수만 출력"
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="시작 날짜 YYYY-MM-DD (news_review.date)",
    )
    parser.add_argument(
        "--until",
        type=str,
        default=None,
        help="종료 날짜 YYYY-MM-DD (news_review.date)",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


if __name__ == "__main__":
    args = _parse_args()
    result = run(dry_run=args.dry_run, since=args.since, until=args.until)
    sys.exit(1 if result.get("failed", 0) else 0)
