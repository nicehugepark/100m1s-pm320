"""
DART 공시 본문 LLM 요약 — 이시카와 스타일 (REQ-20260415-REQ-002).

본문 원문은 메모리에서만 사용, DB 저장 금지 (법무 원칙).
실패 시 기존 _make_summary 결과 유지, llm_summarized=0 그대로.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time  # noqa: F401 — legacy llm_summarize path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from .config import pipeline_date
from .db import connect
from .fewshot import build_fewshot_context
from .interpret_stocks import CLAUDE_CMD
from .llm_client import (
    ISHIKAWA_MODEL,
    TOGUSA_MODEL,
    call_model,
    daily_cost_summary,
    extract_json,
    to_sqlite_text,  # noqa: F401 — LLM 응답 DB 쓰기 sanitize (autoflake 재제거 방지, FLR-20260421-FLR-004 동종)
)

logger = logging.getLogger(__name__)

DART_MAIN_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp_no}"
DART_VIEWER_URL = (
    "https://dart.fss.or.kr/report/viewer.do"
    "?rcpNo={rcp_no}&dcmNo={dcm_no}&eleId=0&offset=0&length=0&dtd=HTML"
)
KIND_VIEWER_URL = (
    "https://kind.krx.co.kr/common/disclsviewer.do?method=search&acptno={rcp_no}"
)

_FETCH_TIMEOUT = 10
_FETCH_HEADERS = {"User-Agent": "100m1s-pipeline/1.0"}
_MAX_BODY_CHARS = 3000

# 프롬프트 버전 — 변경 시 값 갱신 → 기존 llm_summarized=1 레코드 재해석 판단 키.
# krx-stage-conditions.json 기반 fact만 반영 (창작 금지, FLR-20260423-002).
PROMPT_VERSION = "2026-04-23-v2"

PROMPT = """DART/KIND 공시 투자자 요약. JSON만 출력. 한국어.

body_status: {body_status} (HAS_BODY|NO_BODY)
제목: {title}
카테고리: {category}
본문(최대 3000자, 없으면 NO_BODY 토큰):
{body}

### 엄격 규칙(위반=무효)
1. 본문에 없는 사실 추가 금지. 금칙어: "정정요구/잠정 문서/잠정공시/미확정/정정공시 확인 필수/추정"
   (단, 제목에 "예고"가 있으면 "예고"만 허용)
2. NO_BODY → summary="본문 미확보, 제목 기반: <제목 1줄 요약>". 기간·조건·숫자 모두 null
3. 기간·조건·금액은 본문 명시 수치만. 없으면 null
4. "호재/악재" 단정 금지 (방향만 암시)

### KRX 시장경보/단기과열 규정 (본문 수치 없을 때만 제한 적용, 적용 시 summary 끝에 "(KRX 규정 기준)" 표기)
공시 제목·카테고리가 아래 4개 범주에 속할 때만 해당 조문 참조:

- **투자주의** (level 1): 지정 익일 자동 해제 재심사. **자동 거래 규제 없음**. 투자자 주의 환기 목적
- **투자경고** (level 2): 자동효과=신용거래 금지 + 위탁증거금 100% 현금 + 대용증권 불인정. 최소 10거래일 후 재심사(자동 해제 아님). **단일가매매 아님**
- **투자위험** (level 3): 지정과 동시에 1거래일 매매거래정지 + 투자경고 효과(신용/증거금/대용) 유지. **단일가매매 아님**
- **단기과열** (parallel 별개 제도): D+1 예고 → D+2 1거래일 거래정지 → D+3~D+5 30분 단위 단일가매매. **단일가매매 유일 출처**

⚠️ 주의:
- 단일가매매는 **단기과열 전용**. 투자경고/위험에 단일가 언급 금지
- VI(변동성완화장치) "2분 단일가"는 별개 제도. 시장경보와 혼동 금지
- 투자경고 진입 수치 요건(3일/5일/15일 급등률 등)은 KRX 공식 비공개 영역. 본문 수치 없으면 **null**로 두고 서술 금지

### 분류 기준 (stage_classification)
공시 제목 기반 5개 중 하나:
- attention  (투자주의)
- warning    (투자경고)
- risk       (투자위험)
- overheat   (단기과열)
- other      (위 4종 외 공시 — IR·실적·지분변동 등)

### tempo (지정예고/지정/해제 분류)
- upcoming  (제목에 "예고", "지정예정", "D-1" 등)
- active    (제목에 "지정", "변경지정", "재지정")
- released  (제목에 "해제", "종료")
- unknown   (추정 불가)

### 스타일
1~2문장 + 핵심 숫자(금액·비율·기간·주식수). 발행기간 있으면 포함(예: 2026-05-15~2029-05-15). 기간성 공시(투자주의/경고/위험/단기과열/매매거래정지/관리종목/상장폐지)는 period_start·period_end 추출. 종가 조건(예 "종가 XXX원 이상 3거래일")은 condition 1줄 + regulation_period에 적용기간. 날짜 YYYY-MM-DD 형식. 추출 불가 시 null.

### 출력 스키마 (JSON만, 불필요 텍스트 금지)
{{
  "summary": "1~2문장 요약(필수)",
  "key_numbers": ["원문 인용 숫자/기간 배열"],
  "period_start": "YYYY-MM-DD|null",
  "period_end": "YYYY-MM-DD|null",
  "condition": "종가·거래량 조건 1줄|null",
  "regulation_period": "조건 적용 기간 1줄|null",
  "stage_classification": "attention|warning|risk|overheat|other",
  "tempo": "upcoming|active|released|unknown",
  "body_source": "HAS_BODY|NO_BODY|TITLE_ONLY"
}}
"""

# NO_BODY일 때 summary에 절대 나타나면 안 되는 금칙어 (환각 감지)
FORBIDDEN_WHEN_NO_BODY = (
    "정정요구",
    "잠정 문서",
    "잠정공시",
    "미확정",
    "정정공시 확인 필수",
)


# 제목 fallback 분류 — LLM이 stage_classification/tempo를 누락/오응답할 때 최소 안전망.
# 규칙 정합(FLR-20260423-002): 본 함수는 "제목 어휘만" 기준, 규정 fact는 건드리지 않음.
_STAGE_KEYWORDS = (
    ("overheat", ("단기과열",)),
    ("risk", ("투자위험",)),
    ("warning", ("투자경고",)),
    ("attention", ("투자주의",)),
)


def _classify_stage_from_title(title: str) -> str:
    t = title or ""
    for tag, kws in _STAGE_KEYWORDS:
        if any(k in t for k in kws):
            return tag
    return "other"


def _classify_tempo_from_title(title: str) -> str:
    t = title or ""
    if "해제" in t or "종료" in t:
        return "released"
    if "예고" in t or "지정예정" in t:
        return "upcoming"
    if "지정" in t:  # "변경지정", "재지정" 포함
        return "active"
    return "unknown"


# 본문 유무 무관하게 summary에 있으면 환각 의심 → body 재확인
ALWAYS_SUSPICIOUS = ("정정요구",)


# KIND 뷰어 래퍼에 항상 등장하는 메타 문구 — 공시 본문이 아님.
# FLR-AGT-001: 이 메타가 LLM에 전달되면 "정정요구" 환각이 발생.
_KIND_META_PATTERNS = (
    re.compile(
        r"본\s*공지사항은\s*공시내용\s*기재\s*불충분\s*등의\s*사유로"
        r"\s*한국거래소\s*정정요구를\s*받은\s*사항입니다[^.]*\."
    ),
    re.compile(r"동\s*공시에\s*대한\s*정정공시가\s*이루어지는\s*경우[^.]*\."),
    re.compile(r"본\s*문서는\s*최종문서가\s*아니므로[^.]*확인하시기\s*바랍니다\.?"),
    re.compile(r"문서\s*목차\s*문서\s*목차"),
    re.compile(r"첨부서류\s*첨부문서선택"),
    re.compile(r"기공시\s*기공시선택"),
    re.compile(r"조회\s*본\s*문\s*본문선택"),
)


def _strip_kind_meta(text: str) -> str:
    for pat in _KIND_META_PATTERNS:
        text = pat.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_kind_body(rcp_no: str, max_chars: int = _MAX_BODY_CHARS) -> str:
    """KIND 공시 뷰어에서 본문 텍스트 추출. 실패 시 빈 문자열.

    KIND 뷰어는 iframe + AJAX 구조라 메인 HTML에는 공시 본문이 없고
    래퍼 메타 알림만 존재. 메타를 제거한 뒤 남은 텍스트만 반환.
    실제 공시 본문은 별도 엔드포인트 필요 — 현재는 제목/메타만 신뢰.
    """
    try:
        url = KIND_VIEWER_URL.format(rcp_no=rcp_no)
        r = requests.get(url, timeout=_FETCH_TIMEOUT, headers=_FETCH_HEADERS)
        if r.status_code != 200:
            return ""
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(separator=" ", strip=True)
        text = re.sub(r"\s+", " ", text).strip()
        text = _strip_kind_meta(text)
        # KIND 래퍼만 긁혀왔다면(~100자 미만 본문) NO_BODY 취급
        if len(text) < 100:
            return ""
        return text[:max_chars]
    except Exception as e:
        logger.debug("fetch_kind_body FAIL rcp=%s: %s", rcp_no, e)
        return ""


def fetch_disclosure_body(
    rcp_no: str,
    source: str = "DART",
    source_url: str | None = None,
    max_chars: int = _MAX_BODY_CHARS,
) -> str:
    """공시 상세 페이지에서 본문만 추출 (DART/KIND 분기). 메모리 사용만.

    분기 기준: source 인자, 또는 source_url에 'kind.krx.co.kr' 포함 여부.
    실패 시 빈 문자열 (title 단독 LLM 추론 허용).
    """
    is_kind = source == "KIND" or (source_url and "kind.krx.co.kr" in source_url)
    if is_kind:
        return fetch_kind_body(rcp_no, max_chars)
    try:
        url = DART_MAIN_URL.format(rcp_no=rcp_no)
        resp = requests.get(url, timeout=_FETCH_TIMEOUT, headers=_FETCH_HEADERS)
        resp.raise_for_status()
        # DART 페이지는 쌍따옴표 형태로 viewDoc("rcpNo","dcmNo",...) 포함.
        # (홑따옴표 케이스도 대비하여 둘 다 시도)
        m = re.search(r'viewDoc\(["\'](\d+)["\'],\s*["\'](\d+)["\']', resp.text)
        if not m:
            return ""
        dcm_no = m.group(2)
        doc_url = DART_VIEWER_URL.format(rcp_no=rcp_no, dcm_no=dcm_no)
        r = requests.get(doc_url, timeout=_FETCH_TIMEOUT, headers=_FETCH_HEADERS)
        if r.status_code != 200:
            return ""
        soup = BeautifulSoup(r.text, "html.parser")
        text = soup.get_text(separator=" ", strip=True)
        # 과도한 공백 축소
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]
    except Exception as e:
        logger.debug("fetch_disclosure_body FAIL rcp=%s: %s", rcp_no, e)
        return ""


def llm_summarize(title: str, category: str | None, body: str) -> dict:
    """Claude CLI Haiku로 요약 + 기간/조건 추출. 실패 시 빈 dict.

    body_status=NO_BODY(본문 100자 미만)일 때는 환각 방지 후처리 적용.
    """
    body_status = "HAS_BODY" if body and len(body) >= 100 else "NO_BODY"
    body_for_prompt = body if body_status == "HAS_BODY" else "(본문 미확보 — NO_BODY)"
    prompt = PROMPT.format(
        title=title,
        category=category or "기타",
        body=body_for_prompt,
        body_status=body_status,
    )
    try:
        result = subprocess.run(
            [CLAUDE_CMD, "-p", prompt, "--model", "haiku"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.warning(
                "Claude FAIL rc=%s stderr=%s", result.returncode, result.stderr[:200]
            )
            return {}
        out = result.stdout.strip()
        start = out.find("{")
        end = out.rfind("}")
        if start < 0 or end < 0:
            return {}
        data = json.loads(out[start : end + 1])
        summary = (data.get("summary") or "").strip()
        if not summary:
            return {}

        def _norm(v):
            if v is None:
                return None
            s = str(v).strip()
            if not s or s.lower() == "null":
                return None
            return s

        # 환각 감지 후처리:
        # 1) NO_BODY인데 summary에 금칙어가 있으면 원문 제목 기반으로 강제 교체
        # 2) HAS_BODY라도 summary에 '정정요구'가 있으면 body 원문 재확인 후 없으면 제거
        body_lower_text = body or ""
        if body_status == "NO_BODY":
            for w in FORBIDDEN_WHEN_NO_BODY:
                if w in summary:
                    logger.warning("환각 감지 NO_BODY: w=%s title=%s", w, title[:40])
                    summary = f"본문 미확보, 제목 기반: {title}"
                    return {
                        "summary": summary,
                        "period_start": None,
                        "period_end": None,
                        "condition": None,
                        "regulation_period": None,
                        "stage_classification": _classify_stage_from_title(title),
                        "tempo": _classify_tempo_from_title(title),
                        "body_source": "NO_BODY",
                        "prompt_version": PROMPT_VERSION,
                    }
        else:
            for w in ALWAYS_SUSPICIOUS:
                if w in summary and w not in body_lower_text and w not in title:
                    logger.warning("환각 감지 HAS_BODY: w=%s title=%s", w, title[:40])
                    # 문장 분할 후 해당 어휘 포함 문장만 제거
                    parts = re.split(r"(?<=[.!?])\s+", summary)
                    parts = [p for p in parts if w not in p]
                    summary = " ".join(parts).strip() or (
                        f"본문 미확보, 제목 기반: {title}"
                    )

        return {
            "summary": summary,
            "period_start": _norm(data.get("period_start")),
            "period_end": _norm(data.get("period_end")),
            "condition": _norm(data.get("condition")),
            "regulation_period": _norm(data.get("regulation_period")),
            "stage_classification": _norm(data.get("stage_classification"))
            or _classify_stage_from_title(title),
            "tempo": _norm(data.get("tempo")) or _classify_tempo_from_title(title),
            "body_source": _norm(data.get("body_source")) or body_status,
            "prompt_version": PROMPT_VERSION,
        }
    except subprocess.TimeoutExpired:
        logger.warning("Claude TIMEOUT title=%s", title[:30])
        return {}
    except Exception as e:
        logger.warning("llm_summarize FAIL: %s", e)
        return {}


# ── 2단 LLM 루프 (REQ-20260415-REQ-008) ──────────────────────────────

TOGUSA_EVAL_PROMPT = """토구사 검증 — 본문과 대조해 이시카와 환각·이탈 평가. JSON만.

제목: {title} / 카테고리: {category}
본문 (없으면 NO_BODY):
{body}

이시카와 응답:
{response}

기준: 본문 외 사실 추가(정정요구/잠정/미정 메타 환각 포함) / 숫자·날짜·기간 일치 / KRX 규정 추정시 "(KRX 규정 기준)" 표기 / NO_BODY는 "본문 미확보, 제목 기반:" 포맷 / "호재/악재" 단정 금지

출력:
{{"verdict":"good|bad","note":"1줄(bad 필수)","fix":{{"summary":"...","period_start":null,"period_end":null,"condition":null,"regulation_period":null}}}}

good=fix null 허용. bad=이시카와와 동일 구조의 수정안.
"""


def _save_review(
    conn,
    date: str,
    rcept_no: str,
    agent: str,
    raw_title: str,
    resp: dict,
    verdict: str,
    evaluator: str | None,
    note: str | None,
) -> None:
    # patch-C: LLM-derived 필드(verdict/evaluator/note) to_sqlite_text 래핑
    conn.execute(
        """INSERT INTO disclosure_review(
             date, rcept_no, agent, raw_title, llm_response,
             verdict, evaluator, evaluation_note, created_at
           ) VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            date,
            rcept_no,
            agent,
            raw_title,
            json.dumps(resp, ensure_ascii=False, default=str),
            to_sqlite_text(verdict),
            to_sqlite_text(evaluator),
            to_sqlite_text(note),
            datetime.now().isoformat(),
        ),
    )


def _togusa_evaluate(
    title: str, category: str | None, body: str, ish_resp: dict
) -> dict | None:
    """토구사 LLM으로 이시카와 응답 검증. None이면 검증 실패."""
    body_for_prompt = body if body and len(body) >= 100 else "(본문 미확보 — NO_BODY)"
    prompt = TOGUSA_EVAL_PROMPT.format(
        title=title,
        category=category or "기타",
        body=body_for_prompt[:3000],
        response=json.dumps(ish_resp, ensure_ascii=False),
    )
    raw = call_model(prompt, TOGUSA_MODEL, agent="togusa", timeout=60, max_retries=1)
    if not raw:
        return None
    return extract_json(raw)


def _process_one(
    row, kind_bodies: dict[str, str] | None = None, fewshot: str = ""
) -> dict:
    """단일 공시 2단 루프 처리. DB 저장은 호출자가 수행.

    kind_bodies: KIND 건 사전 batch fetch 결과 {rcept_no: body}. None이면 직접 fetch.
    fewshot: 사전 생성된 few-shot 컨텍스트 (루프마다 재생성 회피).

    Returns:
        {
          "row_id", "rcept_no", "title", "body_ok",
          "final_resp" (dict | None),
          "ish_resp" (dict | None),
          "tog_eval" (dict | None),
          "verdict_tag" ('good'|'bad'|'pending'|'ish_fail')
        }
    """
    src = row["source"] if "source" in row.keys() else "DART"
    title = row["title"]
    rcept_no = row["rcept_no"]
    category = row["disclosure_cat"]

    if src == "KIND" and kind_bodies is not None:
        body = kind_bodies.get(rcept_no, "")
    else:
        body = fetch_disclosure_body(
            rcept_no, source=src or "DART", source_url=row["source_url"]
        )
    body_ok = bool(body)

    # === 1차: 이시카와 (기존 llm_summarize 재사용 — 환각 후처리 포함) ===
    if not fewshot:
        fewshot = build_fewshot_context(limit=5)
    ish_resp = _ishikawa_with_fewshot(title, category, body, fewshot)
    if not ish_resp or not ish_resp.get("summary"):
        return {
            "row_id": row["id"],
            "rcept_no": rcept_no,
            "title": title,
            "body_ok": body_ok,
            "final_resp": None,
            "ish_resp": None,
            "tog_eval": None,
            "verdict_tag": "ish_fail",
        }

    # === 2차: 토구사 검증 ===
    tog_eval = _togusa_evaluate(title, category, body, ish_resp)

    if tog_eval and tog_eval.get("verdict") == "good":
        return {
            "row_id": row["id"],
            "rcept_no": rcept_no,
            "title": title,
            "body_ok": body_ok,
            "final_resp": ish_resp,
            "ish_resp": ish_resp,
            "tog_eval": tog_eval,
            "verdict_tag": "good",
        }

    if tog_eval and tog_eval.get("verdict") == "bad":
        fix = tog_eval.get("fix")
        if isinstance(fix, str):
            fix = extract_json(fix)
        if isinstance(fix, dict) and fix.get("summary"):
            return {
                "row_id": row["id"],
                "rcept_no": rcept_no,
                "title": title,
                "body_ok": body_ok,
                "final_resp": fix,
                "ish_resp": ish_resp,
                "tog_eval": tog_eval,
                "verdict_tag": "bad",
            }
        # fix 파싱 실패 → 이시카와 응답 채택 (pending)
        return {
            "row_id": row["id"],
            "rcept_no": rcept_no,
            "title": title,
            "body_ok": body_ok,
            "final_resp": ish_resp,
            "ish_resp": ish_resp,
            "tog_eval": tog_eval,
            "verdict_tag": "pending",
        }

    # 토구사 호출 자체 실패 → pending
    return {
        "row_id": row["id"],
        "rcept_no": rcept_no,
        "title": title,
        "body_ok": body_ok,
        "final_resp": ish_resp,
        "ish_resp": ish_resp,
        "tog_eval": None,
        "verdict_tag": "pending",
    }


def _ishikawa_with_fewshot(
    title: str, category: str | None, body: str, fewshot: str
) -> dict | None:
    """이시카와 1차 요약. fewshot 컨텍스트를 프롬프트에 삽입.

    기존 llm_summarize의 환각 후처리(FORBIDDEN_WHEN_NO_BODY 등)를 그대로 상속.
    """
    body_status = "HAS_BODY" if body and len(body) >= 100 else "NO_BODY"
    body_for_prompt = body if body_status == "HAS_BODY" else "(본문 미확보 — NO_BODY)"
    core = PROMPT.format(
        title=title,
        category=category or "기타",
        body=body_for_prompt,
        body_status=body_status,
    )
    prompt = (fewshot + "\n" + core) if fewshot else core
    raw = call_model(
        prompt, ISHIKAWA_MODEL, agent="ishikawa", timeout=30, max_retries=1
    )
    if not raw:
        return None
    data = extract_json(raw)
    if not data or not (data.get("summary") or "").strip():
        return None

    def _norm(v):
        if v is None:
            return None
        s = str(v).strip()
        if not s or s.lower() == "null":
            return None
        return s

    summary = data.get("summary", "").strip()
    body_text = body or ""
    # 환각 후처리 (기존 llm_summarize 동일)
    if body_status == "NO_BODY":
        for w in FORBIDDEN_WHEN_NO_BODY:
            if w in summary:
                logger.warning("환각 감지 NO_BODY: w=%s title=%s", w, title[:40])
                summary = f"본문 미확보, 제목 기반: {title}"
                return {
                    "summary": summary,
                    "period_start": None,
                    "period_end": None,
                    "condition": None,
                    "regulation_period": None,
                    "stage_classification": _classify_stage_from_title(title),
                    "tempo": _classify_tempo_from_title(title),
                    "body_source": "NO_BODY",
                    "prompt_version": PROMPT_VERSION,
                }
    else:
        for w in ALWAYS_SUSPICIOUS:
            if w in summary and w not in body_text and w not in title:
                logger.warning("환각 감지 HAS_BODY: w=%s title=%s", w, title[:40])
                parts = re.split(r"(?<=[.!?])\s+", summary)
                parts = [p for p in parts if w not in p]
                summary = " ".join(parts).strip() or f"본문 미확보, 제목 기반: {title}"

    return {
        "summary": summary,
        "period_start": _norm(data.get("period_start")),
        "period_end": _norm(data.get("period_end")),
        "condition": _norm(data.get("condition")),
        "regulation_period": _norm(data.get("regulation_period")),
        "stage_classification": _norm(data.get("stage_classification"))
        or _classify_stage_from_title(title),
        "tempo": _norm(data.get("tempo")) or _classify_tempo_from_title(title),
        "body_source": _norm(data.get("body_source")) or body_status,
        "prompt_version": PROMPT_VERSION,
    }


# FLR-20260422-FLR-009 근본 대응:
# `only_today`만 지원하던 기존 구조에서는 예고 공시(지정일 미래)가 당일 요약 실패 시
# 다음 날 `pipeline_date` 기준 쿼리에서 빠져 영원히 llm_summarized=0로 정착.
# `mode="forward"`는 "예고·지정·해제 계열 + 최근 N일 미요약"만 선별적으로 재시도.
_FORWARD_PENDING_WINDOW_DAYS = 14
_FORWARD_TITLE_KEYWORDS = (
    "예고",
    "지정예정",
    "투자주의",
    "투자경고",
    "투자위험",
    "단기과열",
    "매매거래정지",
    "관리종목",
    "상장폐지",
    "해제",
)


def interpret(
    date_str: str | None = None,
    only_today: bool = True,
    max_workers: int = 2,
    mode: str = "default",
) -> int:
    """해당 날짜 disclosures 2단 LLM 루프 (이시카와→토구사) 처리.

    mode:
      "default"  — only_today/all-unsummarized 기존 동작 유지.
      "forward"  — 최근 14일 미요약 중 예고·경보·정지·해제 계열만 재시도
                   (FLR-20260422-FLR-009 근본 후속).

    only_today=True (기본): 지정 날짜 건만 처리 (mode="default"에서만 유효).
    max_workers: ThreadPoolExecutor 동시성 (claude CLI subprocess 병렬).
    """
    target = date_str or pipeline_date()
    with connect() as conn:
        if mode == "forward":
            # 제목에 경보/예고/해제 계열 키워드 포함 + 최근 N일 + llm_summarized=0
            like_clauses = " OR ".join(["title LIKE ?"] * len(_FORWARD_TITLE_KEYWORDS))
            params = [f"%{k}%" for k in _FORWARD_TITLE_KEYWORDS]
            since = f"date('now','-{_FORWARD_PENDING_WINDOW_DAYS} day')"
            rows = conn.execute(
                f"""SELECT id, rcept_no, title, disclosure_cat, summary, source, source_url
                   FROM disclosures
                   WHERE COALESCE(llm_summarized, 0) = 0
                   AND date >= {since}
                   AND ({like_clauses})
                   ORDER BY date DESC""",
                params,
            ).fetchall()
        elif only_today:
            rows = conn.execute(
                """SELECT id, rcept_no, title, disclosure_cat, summary, source, source_url
                   FROM disclosures
                   WHERE date=? AND COALESCE(llm_summarized, 0) = 0""",
                (target,),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT id, rcept_no, title, disclosure_cat, summary, source, source_url
                   FROM disclosures
                   WHERE COALESCE(llm_summarized, 0) = 0"""
            ).fetchall()

        if not rows:
            print(f"interpret_disclosures: no rows to process (date={target})")
            return 0

        print(
            f"interpret_disclosures: {len(rows)}건 2단 루프 시작 "
            f"(date={target}, ishikawa={ISHIKAWA_MODEL}, togusa={TOGUSA_MODEL}, workers={max_workers})"
        )

        # KIND 건 batch fetch (Playwright 단일 브라우저 재사용, 직렬 throttle)
        kind_rcps = [r["rcept_no"] for r in rows if (r["source"] or "DART") == "KIND"]
        kind_bodies: dict[str, str] = {}
        if kind_rcps:
            try:
                from .kind_fetcher import fetch_kind_batch

                t0 = time.time()
                kind_bodies = fetch_kind_batch(kind_rcps)
                elapsed = time.time() - t0
                ok = sum(1 for v in kind_bodies.values() if v)
                print(
                    f"  kind_fetcher: {ok}/{len(kind_rcps)} body OK, "
                    f"elapsed={elapsed:.1f}s"
                )
            except Exception as e:
                logger.warning("kind_fetcher batch FAIL: %s", e)
                kind_bodies = {}

        # fewshot 1회 생성 (루프 간 재사용)
        fewshot = build_fewshot_context(limit=5)

        results: list[dict] = []
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = {ex.submit(_process_one, r, kind_bodies, fewshot): r for r in rows}
            for fut in as_completed(futs):
                try:
                    results.append(fut.result())
                except Exception as e:
                    r = futs[fut]
                    logger.warning("_process_one FAIL rcept=%s: %s", r["rcept_no"], e)

        # DB 업데이트 + 리뷰 적재 (메인 스레드에서 일괄)
        updated = 0
        body_ok = 0
        period_ok = 0
        verdict_counts = {"good": 0, "bad": 0, "pending": 0, "ish_fail": 0}
        for res in results:
            if res["body_ok"]:
                body_ok += 1
            verdict_counts[res["verdict_tag"]] = (
                verdict_counts.get(res["verdict_tag"], 0) + 1
            )

            if res["verdict_tag"] == "ish_fail":
                print(f"  [{res['rcept_no']}] SKIP (이시카와 실패)")
                continue

            data = res["final_resp"]
            ish = res["ish_resp"]
            tog = res["tog_eval"]
            if not data or not data.get("summary"):
                continue

            if data.get("period_start") or data.get("period_end"):
                period_ok += 1

            # patch-C: LLM 응답 6개 필드 모두 to_sqlite_text 래핑
            conn.execute(
                """UPDATE disclosures
                   SET summary=?, llm_summarized=1,
                       period_start=?, period_end=?,
                       condition_text=?, regulation_period=?
                   WHERE id=?""",
                (
                    to_sqlite_text(data["summary"]),
                    to_sqlite_text(data.get("period_start")),
                    to_sqlite_text(data.get("period_end")),
                    to_sqlite_text(data.get("condition")),
                    to_sqlite_text(data.get("regulation_period")),
                    res["row_id"],
                ),
            )

            # disclosure_review 기록
            today = datetime.now().strftime("%Y-%m-%d")
            tag = res["verdict_tag"]
            note = tog.get("note") if tog and isinstance(tog, dict) else None

            if tag == "good":
                _save_review(
                    conn,
                    today,
                    res["rcept_no"],
                    "ishikawa",
                    res["title"],
                    ish,
                    "good",
                    "togusa",
                    None,
                )
            elif tag == "bad":
                _save_review(
                    conn,
                    today,
                    res["rcept_no"],
                    "ishikawa",
                    res["title"],
                    ish,
                    "bad",
                    "togusa",
                    note,
                )
                _save_review(
                    conn,
                    today,
                    res["rcept_no"],
                    "togusa",
                    res["title"],
                    data,
                    "good",
                    "auto",
                    None,
                )
            else:  # pending
                pending_note = note or "토구사 판정 누락/파싱 실패"
                _save_review(
                    conn,
                    today,
                    res["rcept_no"],
                    "ishikawa",
                    res["title"],
                    ish,
                    "pending",
                    None,
                    pending_note,
                )

            conn.commit()
            updated += 1
            p = (
                f" [{data.get('period_start')}~{data.get('period_end')}]"
                if data.get("period_start") or data.get("period_end")
                else ""
            )
            print(f"  [{res['rcept_no']}/{tag}]{p}: {data['summary'][:80]}")

        cost = daily_cost_summary(target)
        print(
            f"interpret_disclosures: {updated}/{len(rows)} updated "
            f"(body {body_ok}/{len(rows)}, period {period_ok}) "
            f"verdict={verdict_counts} "
            f"llm_cost_est=${cost['total_usd']:.4f} ({cost['total_calls']} calls)"
        )
        return updated


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    args = sys.argv[1:]
    # 플래그 분리 (위치 인자는 첫 non-flag)
    flags = {a for a in args if a.startswith("--")}
    positional = [a for a in args if not a.startswith("--")]
    date_arg = positional[0] if positional else None

    if "--forward-pending" in flags:
        # FLR-20260422-FLR-009 대응: 경보·예고 계열 미요약 14일치 재시도.
        interpret(date_arg, mode="forward")
    else:
        only_today = "--all-unsummarized" not in flags
        interpret(date_arg, only_today=only_today)
