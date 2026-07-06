"""disclosure_review markdown 덤프 (REQ-20260415-REQ-008).

records/qa/disclosure_review_{date}.md 생성.
대표가 직접 verdict를 수정(human evaluator)하면 few-shot에 반영.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from .config import ROOT, pipeline_date
from .db import connect

# REQ-003: db_cache_summary는 dump_review_md 내부에서만 호출 → autoflake가
# 제거하지 못하도록 noqa: F401 명시.
from .llm_client import daily_cost_summary, db_cache_summary  # noqa: F401

REVIEW_DIR = ROOT / "records" / "qa"


def _fmt_resp(s: str) -> str:
    try:
        d = json.loads(s)
        summary = d.get("summary") or ""
        period = ""
        if d.get("period_start") or d.get("period_end"):
            period = f" [{d.get('period_start') or '?'}~{d.get('period_end') or '?'}]"
        return f"{summary}{period}"
    except Exception:
        return s[:160]


def dump_review_md(date_str: str | None = None) -> Path:
    target = date_str or pipeline_date()
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REVIEW_DIR / f"disclosure_review_{target}.md"

    with connect() as conn:
        rows = conn.execute(
            """SELECT id, rcept_no, agent, raw_title, llm_response,
                      verdict, evaluator, evaluation_note, created_at
                 FROM disclosure_review
                WHERE date=?
                ORDER BY rcept_no, agent""",
            (target,),
        ).fetchall()

    cost = daily_cost_summary(target)
    cache = db_cache_summary()

    lines: list[str] = []
    lines.append(f"# Disclosure Review — {target}")
    lines.append("")
    lines.append(f"- Generated: {datetime.now().isoformat()}")
    lines.append(f"- 총 레코드: {len(rows)}건")
    lines.append(
        f"- LLM 비용(추정): ${cost['total_usd']:.4f} / {cost['total_calls']}회 호출"
    )
    lines.append(
        f"- LLM 캐시(REQ-003): entries={cache['total_entries']} hits={cache['total_hits']} reuse_ratio={cache['reuse_ratio']}"
    )
    if cache.get("by_domain"):
        for d in cache["by_domain"]:
            lines.append(f"  - {d['domain']}: entries={d['entries']} hits={d['hits']}")
    lines.append("")
    lines.append("## 사용 가이드 (휴먼 피드백)")
    lines.append("")
    lines.append(
        "- 아래 PASS/FAIL이 잘못되었으면 DB `disclosure_review`에서 "
        "`verdict`를 수정하고 `evaluator='human'`, `evaluation_note`에 사유 기록."
    )
    lines.append("- 이후 fewshot 빌더가 human 검증본을 우선 반영.")
    lines.append("")

    # verdict 분포
    dist: dict[str, int] = {}
    for r in rows:
        dist[r["verdict"]] = dist.get(r["verdict"], 0) + 1
    if dist:
        lines.append("## Verdict 분포")
        lines.append("")
        for k, v in sorted(dist.items()):
            lines.append(f"- `{k}`: {v}건")
        lines.append("")

    # 레코드별 상세
    lines.append("## 상세")
    lines.append("")
    cur_rcept = None
    for r in rows:
        if r["rcept_no"] != cur_rcept:
            cur_rcept = r["rcept_no"]
            lines.append(f"### {cur_rcept}")
            lines.append("")
            lines.append(f"- 제목: {r['raw_title']}")
            lines.append("")
        emoji = {"good": "PASS", "bad": "FAIL", "pending": "PEND"}.get(
            r["verdict"], "?"
        )
        lines.append(
            f"- **[{emoji}] {r['agent']}** "
            f"(evaluator={r['evaluator'] or '-'}, "
            f"id={r['id']}, {r['created_at'][:19]})"
        )
        lines.append(f"  - 응답: {_fmt_resp(r['llm_response'])}")
        if r["evaluation_note"]:
            lines.append(f"  - 사유: {r['evaluation_note']}")
        lines.append("")

    if not rows:
        lines.append("_(레코드 없음)_")
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"dump_review: {out_path} ({len(rows)} records)")
    return out_path


if __name__ == "__main__":
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    dump_review_md(date_arg)
