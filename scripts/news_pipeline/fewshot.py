"""few-shot context 빌더 (REQ-20260415-REQ-008).

최근 N건 GOOD/BAD 케이스를 이시카와 프롬프트 앞에 동봉.

비판 반영 (2026-04-15 개발팀 분석):
- 토큰 비대화 방지: 최근 30일 + 제목 80자 trim + response 160자 trim
- self-eval 순환 방지: evaluator='human' 우선, 없으면 'togusa' 보조
- 실패시 빈 문자열 반환 (첫 운영일 정상)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from .db import connect

logger = logging.getLogger(__name__)

_WINDOW_DAYS = 30
_TITLE_MAX = 80
_RESP_MAX = 160


def _trim(s: str, n: int) -> str:
    s = (s or "").strip().replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def _fmt_resp(resp_json: str) -> str:
    """llm_response(JSON str)에서 summary만 뽑아 요약 표시. 실패 시 원문 일부."""
    try:
        d = json.loads(resp_json)
        s = (d.get("summary") or "").strip()
        return _trim(s, _RESP_MAX)
    except Exception:
        return _trim(resp_json, _RESP_MAX)


def build_fewshot_context(limit: int = 5, window_days: int = _WINDOW_DAYS) -> str:
    """최근 GOOD/BAD 케이스 limit건씩 프롬프트 컨텍스트로 반환.

    - 우선순위: evaluator='human' > 'togusa'
    - 오늘 기준 window_days 이내만
    - 비어있으면 빈 문자열 (첫 운영 시)
    """
    since = (datetime.now() - timedelta(days=window_days)).strftime("%Y-%m-%d")
    try:
        with connect() as c:
            good = c.execute(
                """SELECT raw_title, llm_response
                     FROM disclosure_review
                    WHERE verdict='good' AND date>=?
                    ORDER BY (evaluator='human') DESC, id DESC
                    LIMIT ?""",
                (since, limit),
            ).fetchall()
            bad = c.execute(
                """SELECT raw_title, llm_response, evaluation_note
                     FROM disclosure_review
                    WHERE verdict='bad' AND date>=?
                    ORDER BY (evaluator='human') DESC, id DESC
                    LIMIT ?""",
                (since, limit),
            ).fetchall()
    except Exception as e:
        logger.debug("build_fewshot_context FAIL: %s", e)
        return ""

    if not good and not bad:
        return ""

    parts: list[str] = []
    if good:
        parts.append("[잘 한 사례 — 참고]")
        for r in good:
            parts.append(
                f"- 제목: {_trim(r['raw_title'], _TITLE_MAX)}\n"
                f"  요약: {_fmt_resp(r['llm_response'])}"
            )
    if bad:
        parts.append("\n[피해야 할 사례 — 같은 실수 금지]")
        for r in bad:
            note = _trim(r["evaluation_note"] or "", 80)
            parts.append(
                f"- 제목: {_trim(r['raw_title'], _TITLE_MAX)}\n"
                f"  잘못된 요약: {_fmt_resp(r['llm_response'])}\n"
                f"  사유: {note}"
            )
    return "\n".join(parts) + "\n"


def _fmt_news_resp(resp_json: str) -> str:
    """news_review llm_response(JSON str)에서 causal_chain만 뽑아 요약."""
    try:
        d = json.loads(resp_json)
        s = (d.get("causal_chain") or "").strip()
        return _trim(s, _RESP_MAX)
    except Exception:
        return _trim(resp_json, _RESP_MAX)


def build_news_fewshot_context(limit: int = 5, window_days: int = _WINDOW_DAYS) -> str:
    """news_review 기반 GOOD/BAD few-shot 컨텍스트.

    구조는 build_fewshot_context(disclosure)와 동일.
    """
    since = (datetime.now() - timedelta(days=window_days)).strftime("%Y-%m-%d")
    try:
        with connect() as c:
            good = c.execute(
                """SELECT news_titles, llm_response
                     FROM news_review
                    WHERE verdict='good' AND date>=?
                    ORDER BY (evaluator='human') DESC, id DESC
                    LIMIT ?""",
                (since, limit),
            ).fetchall()
            bad = c.execute(
                """SELECT news_titles, llm_response, evaluation_note
                     FROM news_review
                    WHERE verdict='bad' AND date>=?
                    ORDER BY (evaluator='human') DESC, id DESC
                    LIMIT ?""",
                (since, limit),
            ).fetchall()
    except Exception as e:
        logger.debug("build_news_fewshot_context FAIL: %s", e)
        return ""

    if not good and not bad:
        return ""

    parts: list[str] = []
    if good:
        parts.append("[잘 한 뉴스 분석 사례 — 참고]")
        for r in good:
            titles = _trim(r["news_titles"], _TITLE_MAX)
            parts.append(
                f"- 뉴스: {titles}\n  분석: {_fmt_news_resp(r['llm_response'])}"
            )
    if bad:
        parts.append("\n[피해야 할 뉴스 분석 사례 — 같은 실수 금지]")
        for r in bad:
            titles = _trim(r["news_titles"], _TITLE_MAX)
            note = _trim(r["evaluation_note"] or "", 80)
            parts.append(
                f"- 뉴스: {titles}\n"
                f"  잘못된 분석: {_fmt_news_resp(r['llm_response'])}\n"
                f"  사유: {note}"
            )
    return "\n".join(parts) + "\n"


def build_tachikoma_fewshot_context(
    limit: int = 5, window_days: int = _WINDOW_DAYS
) -> str:
    """tachikoma_decisions에서 최근 판단 사례 few-shot 생성.

    성공/실패 패턴을 다음 판단에 동봉.
    """
    since = (datetime.now() - timedelta(days=window_days)).strftime("%Y-%m-%d")
    try:
        with connect() as c:
            # 실패 사례 (대표 수정, 에이전트 미커밋 등)
            fails = c.execute(
                """SELECT task_description, grade, delegation, result,
                          human_correction, lessons_learned
                   FROM tachikoma_decisions
                   WHERE result IN ('fail', 'partial') AND date >= ?
                   ORDER BY id DESC LIMIT ?""",
                (since, limit),
            ).fetchall()
            # 성공 사례
            goods = c.execute(
                """SELECT task_description, grade, delegation, agent_spawn_mode
                   FROM tachikoma_decisions
                   WHERE result = 'success' AND date >= ?
                   ORDER BY id DESC LIMIT ?""",
                (since, limit),
            ).fetchall()
    except Exception as e:
        logger.debug("build_tachikoma_fewshot_context FAIL: %s", e)
        return ""

    if not fails and not goods:
        return ""

    parts: list[str] = []
    if fails:
        parts.append("[타치코마 판단 — 피해야 할 패턴]")
        for r in fails:
            desc = _trim(r["task_description"], 80)
            correction = _trim(r["human_correction"] or "", 80)
            lesson = _trim(r["lessons_learned"] or "", 80)
            parts.append(
                f"- 작업: {desc} | 등급: {r['grade']} | 위임: {r['delegation']} | 결과: {r['result']}\n"
                f"  교정: {correction}\n"
                f"  교훈: {lesson}"
            )
    if goods:
        parts.append("\n[타치코마 판단 — 성공 패턴]")
        for r in goods:
            desc = _trim(r["task_description"], 80)
            parts.append(
                f"- 작업: {desc} | 등급: {r['grade']} | 위임: {r['delegation']} | 방식: {r['agent_spawn_mode']}"
            )
    return "\n".join(parts) + "\n"


if __name__ == "__main__":
    print(build_fewshot_context(limit=5) or "(no samples yet)")
