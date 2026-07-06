"""themes_pipeline 공용 설정. 메인 news_pipeline.config와 동일한 HOMEPAGE 경로."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# HOMEPAGE 경로: M1S_HOMEPAGE 환경변수 필수 (audit a106c8feefbc705f4 — 옛 DB write
# 봉쇄, FLR-AGT-002 거짓 충실성 cache layer 변종 hub). 폴백 폐기: news_pipeline.config:11
# 정합. cron + lead/sub-agent는 env 설정 필수.
_HOMEPAGE_ENV = os.environ.get("M1S_HOMEPAGE")
if not _HOMEPAGE_ENV:
    raise RuntimeError(
        "M1S_HOMEPAGE 환경변수 필수 — fallback 폐기, 옛 DB write 봉쇄 "
        "(audit a106c8feefbc705f4, FLR-AGT-002). "
        "pm320 레포 자립 실행: M1S_HOMEPAGE=<pm320 레포 루트> (코드+데이터 동일 레포), "
        "launchd 초안(launchd/drafts/)은 이 env 를 pm320 레포로 자동 설정."
    )
HOMEPAGE = Path(_HOMEPAGE_ENV)
DATA_DIR = HOMEPAGE / "data"
THEMES_DB_PATH = DATA_DIR / "themes.db"
LOG_DIR = ROOT / "scripts" / "themes_pipeline" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def pipeline_date() -> str:
    from datetime import datetime

    return os.environ.get("PIPELINE_DATE") or datetime.now().strftime("%Y-%m-%d")


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def parse_llm_json_array(response):
    """LLM 응답에서 JSON 배열을 견고하게 추출.

    우선순위:
      1) ```json ... ``` 또는 ``` ... ``` 코드 펜스 내용
      2) `[` ~ 매칭되는 `]` 균형 매칭 (페르소나 prefix·끝의 부연설명 제거)
      3) 단순 [ ... ] 첫/마지막 인덱스 폴백

    실패 시 None.
    """
    if not response:
        return None

    candidates = []
    for m in _JSON_FENCE_RE.finditer(response):
        candidates.append(m.group(1).strip())

    depth = 0
    start_idx = -1
    in_str = False
    esc = False
    for i, ch in enumerate(response):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "[":
            if depth == 0:
                start_idx = i
            depth += 1
        elif ch == "]":
            if depth > 0:
                depth -= 1
                if depth == 0 and start_idx >= 0:
                    candidates.append(response[start_idx : i + 1])
                    start_idx = -1

    s = response.find("[")
    e = response.rfind("]")
    if s >= 0 and e > s:
        candidates.append(response[s : e + 1])

    for c in candidates:
        try:
            parsed = json.loads(c)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, ValueError):
            continue
    return None
