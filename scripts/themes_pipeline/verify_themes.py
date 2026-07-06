"""토구사 검증 — 이시카와 themes_raw를 verdict로 판정.

dedupe: themes_raw 입력 해시 기준. 동일 입력 → LLM 0회.
"""

from __future__ import annotations

from datetime import datetime

from scripts.news_pipeline.llm_client import (  # noqa: F401
    ISHIKAWA_MODEL,
    TOGUSA_MODEL,
    hash_input,
)

from .config import parse_llm_json_array, pipeline_date  # noqa: F401
from .db import call_model_cached, connect, init_schema

PROMPT = """매매 관점 토구사 테마 검증. JSON 배열만.

## 후보 테마
{themes_block}

## 기준
- "매수 이유"가 되는가? (IR/홍보/단발 호재 → reject)
- 인과 사슬 자연스러운가? (이시카와 비약 → reject)
- 동일 사실 중복 테마인가? → weak

## 출력
[
  {{"theme_name": "...", "verdict": "pass|weak|reject", "reason": "..."}}
]"""


def _fetch_raw(conn, date: str):
    return [
        dict(r)
        for r in conn.execute(
            """SELECT id, theme_name, parent_theme, summary, source_count
           FROM themes_raw WHERE date=?
           ORDER BY source_count DESC, id ASC""",
            (date,),
        ).fetchall()
    ]


def verify(date: str | None = None, ignore_cache: bool = False):
    init_schema()
    date = date or pipeline_date()

    with connect() as conn:
        candidates = _fetch_raw(conn, date)
        if not candidates:
            print("[verify_themes] no raw themes to verify")
            return []

        payload = {"raw_ids": [c["id"] for c in candidates], "date": date}
        ihash = hash_input(payload)

        block = "\n".join(
            f"- [{c['theme_name']}] {c['summary']} (sources={c['source_count']})"
            for c in candidates
        )
        prompt = PROMPT.format(themes_block=block)

        # 1차: opus(TOGUSA_MODEL) — 엄격 검증
        used_model = TOGUSA_MODEL
        response = call_model_cached(
            prompt,
            TOGUSA_MODEL,
            domain="theme_verify",
            target_id=f"{date}:verify",
            input_hash=ihash,
            agent="themes:togusa",
            ignore_cache=ignore_cache,
        )

        # FLR 보강: opus 3회 timeout/실패 시 ISHIKAWA_MODEL(haiku)로 자동 fallback
        # themes_verified 신선도 회복 우선 (검증 0건보다 haiku 검증이 낫다)
        if response is None:
            print("[verify_themes] opus FAIL → haiku fallback")
            used_model = ISHIKAWA_MODEL
            response = call_model_cached(
                prompt,
                ISHIKAWA_MODEL,
                domain="theme_verify",
                target_id=f"{date}:verify:fallback",
                input_hash=ihash,
                agent="themes:togusa:fallback",
                ignore_cache=ignore_cache,
            )

        if response is None:
            print("[verify_themes] LLM FAIL (opus + haiku 모두 실패)")
            return []

        verdicts = parse_llm_json_array(response)
        if verdicts is None:
            print(f"[verify_themes] JSON parse FAIL — first 200ch: {response[:200]!r}")
            return []

        now = datetime.now().isoformat()
        raw_by_name = {c["theme_name"]: c["id"] for c in candidates}

        for v in verdicts:
            theme_name = v.get("theme_name", "").strip()
            if not theme_name:
                continue
            conn.execute(
                """INSERT OR IGNORE INTO themes_verified
                   (date, theme_name, verdict, reason, raw_id, input_hash, model_version, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    date,
                    theme_name,
                    v.get("verdict", "weak"),
                    v.get("reason", ""),
                    raw_by_name.get(theme_name),
                    ihash,
                    used_model,
                    now,
                ),
            )
        conn.commit()
        print(f"[verify_themes] {date} verdicts={len(verdicts)} model={used_model}")
        return verdicts


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser()
    p.add_argument("--date")
    p.add_argument("--ignore-cache", action="store_true")
    args = p.parse_args()
    verify(args.date, args.ignore_cache)
