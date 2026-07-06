"""Claude CLI 경유 LLM 호출 헬퍼 (REQ-20260415-REQ-008).

모델 라우팅:
- ISHIKAWA_MODEL: 1차 분석 (기본 haiku, 저비용 빠른 호출)
- TOGUSA_MODEL: 검증 (기본 CLAUDE_TOP_MODEL=claude-fable-5, 엄격)

환경변수:
  CLAUDE_TOP_MODEL=claude-fable-5
  CLAUDE_TOP_MODEL_FALLBACK=claude-sonnet-4-6
  ISHIKAWA_MODEL=haiku  (선택; 미설정 시 haiku)
  TOGUSA_MODEL=${CLAUDE_TOP_MODEL}  (선택; 미설정 시 CLAUDE_TOP_MODEL)

비용 추적: DB usage_llm 테이블에 호출 건수/추정 토큰/USD 누적.

캐시 (REQ-20260420-REQ-003):
- call_model_cached() — 입력 해시 기반 캐시. 같은 입력·같은 모델이면 같은 결과 보장.
- IGNORE_CACHE 환경변수 또는 ignore_cache=True로 강제 재호출.
- HIT/MISS 카운터는 프로세스 메모리 + DB hit_count 컬럼 양쪽에 누적.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime

logger = logging.getLogger(__name__)

CLAUDE_CMD = "/Users/seongjinpark/.local/bin/claude"

TOP_MODEL = os.environ.get("CLAUDE_TOP_MODEL", "claude-fable-5")
TOP_MODEL_FALLBACK = os.environ.get("CLAUDE_TOP_MODEL_FALLBACK", "claude-sonnet-4-6")
ISHIKAWA_MODEL = os.environ.get("ISHIKAWA_MODEL", "haiku")
TOGUSA_MODEL = os.environ.get("TOGUSA_MODEL", TOP_MODEL)


# ──────────────────────────────────────────────────────────────────────
# LLM 회복력 (2026-05-26, 대표 지시 "llm 죽을 수 있는 상황 방지·대비").
#
# 근본 사고 (5/26 pipeline log): ISHIKAWA_MODEL 이 .env 에서 opus-4-7 로 override
# 되어 1차 분석(cron 1cycle ~50콜)이 종목당 60s+ 누적 → interpret_loop budget(420s)
# + with_lock CHILD_TIMEOUT(540s) 초과 → 파이프라인 SIGTERM (call_model TIMEOUT
# opus ishikawa 48회, CHILD TIMEOUT 5회). throttle/429 는 0회 (llm_throttle_log
# 테이블 부재 = 단 1건도 기록 안 됨) → 순수 opus 응답 지연이 원인.
#
# 방지책:
#  (1) 모델별 적응형 timeout — opus 류는 60s 가 짧으므로 기본 늘림 (env tunable).
#      단 cron 1cycle 예산 안에서 (haiku 60s, opus/sonnet 90s). interpret_loop 가
#      budget(420s) 로 종목 수를 제한하므로 개별 timeout 상향이 cron 을 깨지 않음.
#  (2) ishikawa 에 opus-class 모델이 설정되면 module load 시 1회 loud WARN — 동일
#      regression(고빈도 stage 에 무거운 모델) 재발을 즉시 가시화.
#  (3) call_model 은 어떤 모델·timeout·실패에도 예외를 raise 하지 않고 None 반환
#      (기존 계약 유지) → interpret() 가 graceful skip → build_daily 선행 publish
#      (73405a8) 로 카드/집계는 항상 보호.
# ──────────────────────────────────────────────────────────────────────

# 모델 prefix 별 기본 per-call timeout (초). env CALL_TIMEOUT_<HAIKU|SONNET|OPUS>
# 로 개별 override 가능. 호출부가 timeout 인자를 명시하면 그 값이 우선.
_DEFAULT_TIMEOUTS = {
    "haiku": int(os.environ.get("CALL_TIMEOUT_HAIKU", "60")),
    "sonnet": int(os.environ.get("CALL_TIMEOUT_SONNET", "90")),
    # opus 90→150 (2026-05-27 대표 승인): real ishikawa 프롬프트(news body 포함
    # ~8k+ char)가 cold 시 90s 를 초과 → news-heavy 종목 3-attempt 모두 timeout FAIL
    # 재현 (FLR-20260527-TEC-001 2차 병목). "한 종목당 뉴스수집~해석요약 시간 확보"
    # 직결. 모델·단계는 그대로(opus 유지), timeout 값만 상향.
    "opus": int(os.environ.get("CALL_TIMEOUT_OPUS", "150")),
}

# call_model 1회(모든 attempt 포함) wall-clock 상한 (초). with_lock CHILD_TIMEOUT
# (540s) 훨씬 아래로 둬 단일 종목이 cron 전체를 잡지 못하게 한다. 개별 attempt
# timeout 의 합이 이 값을 넘기지 않도록 attempt 진입 전 잔여 예산을 점검.
#
# 200→300 (2026-05-27): opus per-call timeout 을 150s 로 올리면 wall=200s 는 단 1
# full attempt(150s) + 잔여 50s 만 허용 → 재시도/429 backoff 가 truncate 됨
# (attempt_timeout = min(timeout, remaining), call_model:286). 300s 로 상향 시 1 full
# 150s attempt + 429 backoff(≤30s) + 부분 retry 수용. 동시성 50 (전 종목 동시) 이라
# cron 전체 dur ≈ max(per-stock) ≈ 300s 로 with_lock 540s 안쪽 유지.
_CALL_WALL_BUDGET_SEC = int(os.environ.get("CALL_WALL_BUDGET_SEC", "300"))


def _effective_wall_budget() -> int:
    """Q-20260527-001 (옵션 B): per-call effective wall budget = min(static, 잔여 BUDGET).

    interpret_loop 가 export 한 INTERPRET_DEADLINE_MONOTONIC (time.monotonic() 기준
    deadline 절대값) 을 읽어 호출 시점 잔여초 계산. 잔여 BUDGET < static 일 때만 동적
    캡 적용. 환경변수 미설정 (interpret_loop 외 호출 경로 — backfill / recovery / CLI
    직접 호출 등) 시 static _CALL_WALL_BUDGET_SEC 그대로 반환 (하위호환).

    worst-case wall 봉쇄:
    - 정상 budget: deadline 멀면 static 300s 유지 → 기존 거동 동일
    - budget 임박: 잔여 60s 면 60s 캡 → opus 1 attempt (150s) 도 절단 (attempt_timeout)
    - budget 소진: 잔여 ≤0 → 0 반환 → call_model 진입 즉시 wall budget guard 가
      graceful None 반환 (llm_client.py:282-293 동일 경로)

    Returns:
        effective wall budget (초). 0 이면 budget 소진 신호.
    """
    deadline_raw = os.environ.get("INTERPRET_DEADLINE_MONOTONIC", "")
    if not deadline_raw:
        return _CALL_WALL_BUDGET_SEC
    try:
        deadline = float(deadline_raw)
    except ValueError:
        return _CALL_WALL_BUDGET_SEC
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return 0
    return min(_CALL_WALL_BUDGET_SEC, int(remaining))


def _is_heavy_model(model: str) -> bool:
    """opus/sonnet 등 응답 지연이 큰 무거운 모델 여부."""
    m = (model or "").lower()
    return "opus" in m or "sonnet" in m


def _default_timeout_for(model: str) -> int:
    """모델 prefix 매칭으로 기본 per-call timeout 반환 (미매칭이면 opus 보수값)."""
    m = (model or "").lower()
    if "haiku" in m:
        return _DEFAULT_TIMEOUTS["haiku"]
    if "sonnet" in m:
        return _DEFAULT_TIMEOUTS["sonnet"]
    if "opus" in m:
        return _DEFAULT_TIMEOUTS["opus"]
    return _DEFAULT_TIMEOUTS["opus"]


# (2) ishikawa(고빈도 1차 stage)에 무거운 모델이 걸리면 가시화.
# 옵션 B (2026-05-26, 대표 지시 "opus 유지 + 호출 최소화"): ishikawa 를 opus 로
# 의도적으로 운영하되 interpret_loop 동시 실행(INTERPRET_CONCURRENCY≥2)으로 wall-time
# 을 단축해 budget 안에 처리. 따라서 동시성이 켜져 있으면 본 조합은 정상 운영 구성 →
# WARN 대신 INFO 로 가시화 (regression 오경보 방지). 동시성 미설정(순차) + opus 면
# 5/26 사고 동형 위험이 그대로이므로 WARN 유지.
if _is_heavy_model(ISHIKAWA_MODEL):
    _concurrency = max(1, int(os.environ.get("INTERPRET_CONCURRENCY", "50")))
    if _concurrency >= 2:
        logger.info(
            "ISHIKAWA_MODEL=%s (opus/sonnet) — 옵션 B 의도적 운영 구성. "
            "interpret_loop 동시 실행(INTERPRET_CONCURRENCY=%d)으로 budget 내 처리 "
            "+ 카드 decouple(473ba38) 보호. budget 초과 종목은 graceful skip + 다음 "
            "cycle/캐시 HIT 보완.",
            ISHIKAWA_MODEL,
            _concurrency,
        )
    else:
        logger.warning(
            "ISHIKAWA_MODEL=%s 는 무거운(opus/sonnet) 모델인데 INTERPRET_CONCURRENCY=%d "
            "(순차) 입니다. ishikawa 1차 분석은 cron 1cycle 당 다수 호출되어 지연 누적 "
            "→ 파이프라인 timeout 위험 (2026-05-26 사고 동형). opus 유지 시 "
            "INTERPRET_CONCURRENCY≥2 권장, 또는 고빈도 stage 는 haiku.",
            ISHIKAWA_MODEL,
            _concurrency,
        )

# 환경변수 IGNORE_CACHE=1 → 모든 call_model_cached가 강제 MISS 처리
_IGNORE_CACHE_ENV = os.environ.get("IGNORE_CACHE", "").lower() in ("1", "true", "yes")

# 프로세스 단위 HIT/MISS 카운터 (cron 종료 시 로그 출력용)
_CACHE_STATS: dict[str, int] = {"hit": 0, "miss": 0, "store_fail": 0}

# 추정 단가 (USD per 1M tokens, 2026-04 기준 Anthropic 공개)
_PRICING = {
    "haiku": {"in": 0.80, "out": 4.00},
    "claude-haiku-4-5-20251001": {"in": 0.80, "out": 4.00},
    "claude-sonnet-4-6": {"in": 3.00, "out": 15.00},
    "claude-opus-4-6": {"in": 5.00, "out": 25.00},
    "claude-opus-4-8": {"in": 5.00, "out": 25.00},
    "claude-fable-5": {
        "in": 10.00,
        "out": 50.00,
    },  # 공식 단가 (2026-06-09 출시, opus-4-8 2배)
}


def _price(model: str) -> dict:
    """모델명 prefix 매칭으로 단가 반환. 미등록이면 opus 수준으로 가정 (보수적)."""
    for k, v in _PRICING.items():
        if model.startswith(k) or k in model:
            return v
    return _PRICING["claude-opus-4-8"]


def _estimate_tokens(text: str) -> int:
    """Claude tokens 대략치 — 한글은 char/2, 영문은 char/4 가중."""
    if not text:
        return 0
    kor = sum(1 for c in text if "\uac00" <= c <= "\ud7a3")
    other = len(text) - kor
    return int(kor / 1.5 + other / 4)


def _record_usage(
    model: str, agent: str, in_tokens: int, out_tokens: int, cost_usd: float
) -> None:
    """DB usage_llm 테이블에 누적. 스키마 없으면 자동 생성."""
    try:
        from .db import connect

        today = datetime.now().strftime("%Y-%m-%d")
        with connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS usage_llm (
                    date TEXT NOT NULL,
                    model TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    call_count INTEGER DEFAULT 0,
                    input_tokens INTEGER DEFAULT 0,
                    output_tokens INTEGER DEFAULT 0,
                    est_cost_usd REAL DEFAULT 0.0,
                    PRIMARY KEY(date, model, agent)
                )"""
            )
            conn.execute(
                """INSERT INTO usage_llm(date,model,agent,call_count,input_tokens,output_tokens,est_cost_usd)
                   VALUES(?,?,?,1,?,?,?)
                   ON CONFLICT(date,model,agent) DO UPDATE SET
                     call_count=call_count+1,
                     input_tokens=input_tokens+?,
                     output_tokens=output_tokens+?,
                     est_cost_usd=est_cost_usd+?""",
                (
                    today,
                    model,
                    agent,
                    in_tokens,
                    out_tokens,
                    cost_usd,
                    in_tokens,
                    out_tokens,
                    cost_usd,
                ),
            )
            conn.commit()
    except Exception as e:
        logger.debug("usage_llm record FAIL: %s", e)


def _record_throttle(model: str, agent: str, note: str) -> None:
    """throttle/overloaded/429 발생 기록."""
    try:
        from .db import connect

        today = datetime.now().strftime("%Y-%m-%d")
        with connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS llm_throttle_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    model TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    note TEXT,
                    occurred_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                "INSERT INTO llm_throttle_log(date,model,agent,note,occurred_at) VALUES(?,?,?,?,?)",
                (today, model, agent, note[:500], datetime.now().isoformat()),
            )
            conn.commit()
    except Exception as e:
        logger.debug("throttle record FAIL: %s", e)


def call_model(
    prompt: str,
    model: str,
    agent: str = "unknown",
    timeout: int | None = None,
    max_retries: int = 2,
) -> str | None:
    """claude CLI로 모델 호출. 실패 시 None.

    - timeout 초과 시 재시도 (최대 max_retries+1회)
    - 성공 시 usage_llm 누적
    - fallback 모델로 자동 강등하지 않음 (비용 예측 안정)

    회복력 (2026-05-26):
    - timeout=None 이면 모델 prefix 별 기본값 적용 (haiku 60s / opus·sonnet 90s).
      호출부가 명시한 timeout 은 그대로 존중 (하위호환).
    - 모든 attempt 의 wall-clock 합이 _CALL_WALL_BUDGET_SEC(기본 200s) 를 넘지
      않도록 attempt 진입 전 잔여 예산을 점검 → 단일 종목이 cron 전체(540s)를
      잡지 못하게 한다.
    - 어떤 실패(timeout/throttle/exception)에도 예외를 raise 하지 않고 None 반환.

    Q-20260527-001 (옵션 B): per-call wall budget 진입 시점 _effective_wall_budget()
    1회 snapshot → static _CALL_WALL_BUDGET_SEC vs 잔여 INTERPRET_BUDGET 중 작은 값
    사용. interpret_loop budget 임박 시 retry/backoff 자연 truncate → with_lock 540s
    안쪽 worst-case 강제 (budget + opus 1 attempt cap). budget 환경변수 미설정 (CLI
    직접 호출 / backfill 등) 시 static 값 그대로 사용 (하위호환).
    """
    # timeout 미지정 시 모델별 적응형 기본값. 지정 시 호출부 값 존중 (하위호환).
    if timeout is None:
        timeout = _default_timeout_for(model)
    call_start = time.monotonic()  # wall-budget guard 기준 시각 (loop 내 참조)
    # Q-20260527-001: per-call wall budget = min(static, 잔여 INTERPRET BUDGET).
    # 진입 시점 1회 snapshot (lock-in) — 매 attempt 마다 재조회 시 race window 발생
    # (interpret deadline 가까워질수록 줄어듦, 같은 종목 진행 도중 budget 변동 비일관성).
    # snapshot 0 이면 budget 이미 소진 — 첫 attempt 도 진입 거부 (graceful None).
    wall_budget = _effective_wall_budget()
    if wall_budget == 0:
        logger.warning(
            "call_model entry — INTERPRET BUDGET 이미 소진 (effective_wall=0) "
            "model=%s agent=%s → graceful None (interpret_loop 동적 캡 작동)",
            model,
            agent,
        )
        return None
    # Q-20260511-FIX-A-LLM-CACHE — `--setting-sources user` 의무.
    # 이유: claude CLI는 cwd 기준 project CLAUDE.md를 자동 inherit한다.
    # interpret_stocks.py가 cwd=/Users/seongjinpark/company/100m1s 에서 호출되면
    # 본 회사 CLAUDE.md ("타치코마 페르소나", "응답 시각:", "대표님" 등)이 system
    # prompt에 inject → LLM이 JSON 응답 대신 페르소나 자연어 응답 반환 → DB UPDATE 0건.
    # `--setting-sources user`는 project local 설정/메모리 무시, user-level만 로드.
    # 검증: 동일 prompt로 default vs `--setting-sources user` 비교 시 후자는 정상 JSON.
    cmd = [
        CLAUDE_CMD,
        "--setting-sources",
        "user",
        "-p",
        prompt,
        "--model",
        model,
    ]
    if TOP_MODEL_FALLBACK and TOP_MODEL_FALLBACK != model:
        cmd += ["--fallback-model", TOP_MODEL_FALLBACK]
    for attempt in range(max_retries + 1):
        # wall-clock budget guard — 직전 attempt 의 timeout/지연이 누적되어 단일
        # 종목이 cron 전체(with_lock 540s)를 잡는 것을 차단. 남은 예산이 0 이하이거나
        # 이번 attempt timeout 보다 작으면 더 시도하지 않고 None 반환 (graceful).
        # Q-20260527-001 (옵션 B): wall_budget = call 진입 snapshot (static vs 잔여
        # INTERPRET BUDGET 중 작은 값). interpret_loop budget 임박 시 동적 캡 작동.
        remaining = wall_budget - (time.monotonic() - call_start)
        if remaining <= 0:
            logger.warning(
                "call_model WALL BUDGET 소진 model=%s agent=%s budget=%ds "
                "(attempt %d/%d 중단, graceful None)",
                model,
                agent,
                wall_budget,
                attempt + 1,
                max_retries + 1,
            )
            break
        # 이번 attempt 의 effective timeout = min(설정 timeout, 남은 예산)
        attempt_timeout = max(1, int(min(timeout, remaining)))
        try:
            start = time.time()
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=attempt_timeout,
            )
            elapsed = time.time() - start
            if r.returncode == 0 and r.stdout.strip():
                out = r.stdout.strip()
                in_tok = _estimate_tokens(prompt)
                out_tok = _estimate_tokens(out)
                p = _price(model)
                cost = (in_tok * p["in"] + out_tok * p["out"]) / 1_000_000
                _record_usage(model, agent, in_tok, out_tok, cost)
                logger.debug(
                    "call_model OK model=%s agent=%s %.1fs in=%d out=%d",
                    model,
                    agent,
                    elapsed,
                    in_tok,
                    out_tok,
                )
                return out
            # throttle/429 감지 (Max 정액제 5시간 윈도우 한도)
            stderr = (r.stderr or "")[:300]
            if (
                "429" in stderr
                or "rate" in stderr.lower()
                or "throttle" in stderr.lower()
                or "overloaded" in stderr.lower()
            ):
                logger.error(
                    "THROTTLE model=%s agent=%s rc=%s stderr=%s",
                    model,
                    agent,
                    r.returncode,
                    stderr,
                )
                _record_throttle(model, agent, stderr)
                # throttle은 짧게 백오프 후 재시도
                time.sleep(min(30, 5 * (attempt + 1)))
                continue
            logger.warning(
                "call_model rc=%s stderr=%s (attempt %d/%d)",
                r.returncode,
                stderr,
                attempt + 1,
                max_retries + 1,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "call_model TIMEOUT model=%s agent=%s (attempt %d/%d)",
                model,
                agent,
                attempt + 1,
                max_retries + 1,
            )
        except Exception as e:
            logger.warning("call_model exception: %s", e)
    return None


def to_sqlite_text(v):
    """LLM 응답을 SQLite TEXT 컬럼에 안전 적재 (FLR-005 patch-C 공통 헬퍼).

    - None → None (NULL 적재)
    - str / int / float / bool → 원형 보존 (sqlite 네이티브 지원)
    - dict / list / tuple / set → json.dumps (default=str로 nested 객체도 안전)
    - 기타 객체 → str() 폴백

    재발 방지: 모든 LLM-derived 컬럼 적재 시 to_sqlite_text(v)로 일괄 래핑.
    sqlite3.InterfaceError("Error binding parameter ...") 방지.
    """
    if v is None:
        return None
    if isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, (dict, list, tuple, set)):
        try:
            return json.dumps(v, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(v)
    return str(v)


# ──────────────────────────────────────────────────────────────────────
# LLM 캐시 (REQ-20260420-REQ-003)
# 같은 입력·같은 모델이면 같은 결과를 보장. 모델 버전 변경 시 자동 invalidate.
# ──────────────────────────────────────────────────────────────────────


def hash_input(payload) -> str:
    """입력을 정규화하여 sha256 해시.

    - dict: sort_keys=True로 직렬화
    - list/tuple: 정렬된 튜플로 직렬화 (순서 무관 캐시 hit 보장)
    - str: 양끝 공백 제거 후 해시
    - 기타: json.dumps 폴백
    """
    import hashlib

    if isinstance(payload, str):
        norm = payload.strip()
    elif isinstance(payload, (list, tuple)):
        # 원소 자체를 정렬 가능한 형태로: 문자열은 그대로, dict면 직렬화
        items = []
        for el in payload:
            if isinstance(el, (dict, list, tuple)):
                items.append(json.dumps(el, sort_keys=True, ensure_ascii=False))
            else:
                items.append(str(el))
        items.sort()
        norm = json.dumps(items, ensure_ascii=False)
    elif isinstance(payload, dict):
        norm = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    else:
        norm = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _cache_lookup(
    domain: str, target_id: str, input_hash: str, model_version: str
) -> str | None:
    """캐시 조회. HIT이면 response_json 반환 + hit_count 증가."""
    try:
        from .db import _ensure_llm_cache, connect

        with connect() as conn:
            _ensure_llm_cache(conn)
            row = conn.execute(
                """SELECT id, response_json FROM llm_cache
                   WHERE domain=? AND target_id=? AND input_hash=? AND model_version=?""",
                (domain, target_id, input_hash, model_version),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE llm_cache SET hit_count = hit_count + 1 WHERE id=?",
                (row["id"],),
            )
            conn.commit()
            return row["response_json"]
    except Exception as e:
        logger.debug("cache lookup FAIL: %s", e)
        return None


# ──────────────────────────────────────────────────────────────────────
# Schema validation — Q-20260511-FIX-A-LLM-CACHE
# 자연어 응답 / sub-agent persona 응답을 캐시 store 단계에서 차단.
# Root cause: claude CLI가 sub-agent 컨텍스트에서 prompt를 받으면 LLM 직접
# 응답이 아닌 sub-agent persona ("타치코마"/"이시카와") 자연어 응답을 반환할 수 있음.
# → 그 자연어가 캐시 store → hit_count 누적 재사용 → DB UPDATE 0건.
# Fix: JSON 응답 의무 도메인은 store 전 JSON 파싱 가능성 검증. 실패 시 store 거부.
# 자연어 prefix 감지: "응답 시각" / "대표님" / "작업 판단" / "마스터 큐" 등.
# ──────────────────────────────────────────────────────────────────────

# JSON 응답 의무 도메인 — 이 도메인 중 하나면 store 전 schema 검증
_JSON_REQUIRED_DOMAINS = frozenset(
    [
        "ishikawa_news",
        "ishikawa_structural",
        "togusa_news",
        "togusa_missed",
        "togusa_recheck",
        "extract_macros",
        "wire_news_ko",  # 미장 wire 한국어 해석 (Q-20260612-154, scripts/wire_collector/interpret_wire.py)
    ]
)

# 자연어 응답 prefix/marker (sub-agent persona) — 발견 시 store 거부
_PERSONA_MARKERS = (
    "응답 시각:",
    "대표님",
    "[작업 판단]",
    "마스터 큐",
    "MASTER-QUEUE",
    "안녕하세요",
    "세션 브리핑",
    "[타치코마]",
    "[이시카와]",
    "메시지에 의도가",
    "요청 내용이 비어",
    "메시지 본문이",
    "분석 대상 없는",
    "사용자 요청이 명시",
    "요청 의도가 모호",
    "내용이 누락",
)


def _validate_cache_response(domain: str, response: str) -> tuple[bool, str]:
    """캐시 store 전 응답 유효성 검증.

    Args:
        domain: 캐시 도메인 (ishikawa_news 등)
        response: LLM 응답 문자열

    Returns:
        (is_valid, reason) — valid면 store 진행, invalid면 store 거부
    """
    if not response or not response.strip():
        return False, "empty response"

    # 자연어 persona marker 사전 차단 (JSON 의무 도메인 한정)
    if domain in _JSON_REQUIRED_DOMAINS:
        # 응답 앞 200자 안에 persona marker가 있으면 sub-agent 응답으로 판정
        head = response[:300]
        for marker in _PERSONA_MARKERS:
            if marker in head:
                return False, f"persona marker detected: {marker!r}"

        # JSON 파싱 가능성 확인 — extract_json 활용
        # extract_macros는 list 응답, 나머지는 dict 응답
        if domain == "extract_macros":
            # list 응답 — 코드펜스 안의 [...] 도 허용
            parsed_ok = False
            # 코드펜스 시도
            for m in _JSON_OBJ_FENCE_RE.finditer(response):
                try:
                    arr = json.loads(m.group(1).strip())
                    if isinstance(arr, list):
                        parsed_ok = True
                        break
                except (json.JSONDecodeError, ValueError):
                    continue
            # 단순 [..] 시도
            if not parsed_ok:
                s = response.find("[")
                e = response.rfind("]")
                if s >= 0 and e > s:
                    try:
                        arr = json.loads(response[s : e + 1])
                        if isinstance(arr, list):
                            parsed_ok = True
                    except (json.JSONDecodeError, ValueError):
                        pass
            if not parsed_ok:
                return False, "JSON list parse FAIL"
        else:
            # dict 응답 — extract_json 활용
            obj = extract_json(response)
            if obj is None:
                return False, "JSON object parse FAIL"

    return True, "ok"


def _cache_store(
    domain: str,
    target_id: str,
    input_hash: str,
    model_version: str,
    response_json: str,
) -> bool:
    """캐시 저장 (UNIQUE 충돌 시 무시).

    Q-20260511-FIX-A-LLM-CACHE — store 전 schema validation 추가.
    JSON 응답 의무 도메인에서 자연어/sub-agent persona 응답 감지 시 store 거부.
    """
    # Schema validation — Q-20260511-FIX-A
    is_valid, reason = _validate_cache_response(domain, response_json)
    if not is_valid:
        logger.warning(
            "cache STORE REJECTED domain=%s target=%s reason=%s preview=%r",
            domain,
            target_id,
            reason,
            response_json[:80] if response_json else "",
        )
        _CACHE_STATS["store_fail"] += 1
        return False

    try:
        from .db import _ensure_llm_cache, connect

        with connect() as conn:
            _ensure_llm_cache(conn)
            conn.execute(
                """INSERT OR IGNORE INTO llm_cache
                   (domain, target_id, input_hash, model_version, response_json)
                   VALUES(?,?,?,?,?)""",
                (domain, target_id, input_hash, model_version, response_json),
            )
            conn.commit()
        return True
    except Exception as e:
        logger.warning("cache store FAIL domain=%s target=%s: %s", domain, target_id, e)
        _CACHE_STATS["store_fail"] += 1
        return False


def call_model_cached(
    prompt: str,
    model: str,
    *,
    domain: str,
    target_id: str,
    input_hash: str,
    agent: str = "unknown",
    timeout: int = 60,
    max_retries: int = 2,
    ignore_cache: bool = False,
) -> str | None:
    """캐시 적용 LLM 호출 (REQ-20260420-REQ-003).

    Args:
        prompt: 모델에 전달할 프롬프트
        model: 모델 식별자 (model_version으로 캐시 키 일부)
        domain: 캐시 분리 도메인 (ishikawa_news / togusa_news / extract_macros / ...)
        target_id: 도메인 내 식별자 (종목코드, 날짜 등)
        input_hash: 정규화된 입력 해시 (hash_input 헬퍼 사용 권장)
        ignore_cache: True면 강제 MISS + 호출 결과로 캐시 갱신

    Returns:
        LLM 응답 문자열 (캐시 HIT/MISS 무관 동일 형식). 호출 실패 시 None.
    """
    force_miss = ignore_cache or _IGNORE_CACHE_ENV
    if not force_miss:
        cached = _cache_lookup(domain, target_id, input_hash, model)
        if cached is not None:
            _CACHE_STATS["hit"] += 1
            logger.debug(
                "cache HIT domain=%s target=%s model=%s", domain, target_id, model
            )
            return cached
    _CACHE_STATS["miss"] += 1
    response = call_model(
        prompt, model, agent=agent, timeout=timeout, max_retries=max_retries
    )
    if response is not None:
        _cache_store(domain, target_id, input_hash, model, response)
    return response


def cache_stats() -> dict:
    """이번 프로세스 누적 HIT/MISS/store_fail 카운터."""
    return dict(_CACHE_STATS)


def reset_cache_stats() -> None:
    """단위 테스트용 — 카운터 초기화."""
    _CACHE_STATS["hit"] = 0
    _CACHE_STATS["miss"] = 0
    _CACHE_STATS["store_fail"] = 0


def db_cache_summary(date_str: str | None = None) -> dict:
    """DB에 누적된 캐시 통계.

    - total_entries: 전체 캐시 항목 수
    - total_hits: hit_count 합계
    - by_domain: 도메인별 집계
    - reuse_ratio: total_hits / (total_entries + total_hits) — 재사용률
    """
    try:
        from .db import _ensure_llm_cache, connect

        with connect() as conn:
            _ensure_llm_cache(conn)
            agg = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(hit_count),0) AS h FROM llm_cache"
            ).fetchone()
            by_domain = conn.execute(
                """SELECT domain, COUNT(*) AS entries, COALESCE(SUM(hit_count),0) AS hits
                   FROM llm_cache GROUP BY domain ORDER BY hits DESC"""
            ).fetchall()
        total_entries = agg["n"] or 0
        total_hits = agg["h"] or 0
        denom = total_entries + total_hits
        ratio = (total_hits / denom) if denom else 0.0
        return {
            "total_entries": total_entries,
            "total_hits": total_hits,
            "reuse_ratio": round(ratio, 4),
            "by_domain": [dict(r) for r in by_domain],
        }
    except Exception as e:
        logger.debug("db_cache_summary FAIL: %s", e)
        return {
            "total_entries": 0,
            "total_hits": 0,
            "reuse_ratio": 0.0,
            "by_domain": [],
        }


_JSON_OBJ_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL)


def extract_json(text: str) -> dict | None:
    """LLM 응답에서 JSON 객체를 견고하게 추출 (4단 fallback).

    trackC themes parse_llm_json_array 패턴 차용 (FLR patch-B 보강).

    우선순위:
      1) ```json ... ``` 또는 ``` ... ``` 모든 코드 펜스 후보
      2) `{` ~ 매칭되는 `}` 균형 매칭으로 발견된 모든 최상위 객체 후보
      3) 단순 `{` 첫 인덱스 ~ `}` 마지막 인덱스 폴백
      4) 모든 후보를 순서대로 시도, dict 파싱 성공 시 즉시 반환

    페르소나 prefix("이시카와입니다. ..."), 끝의 부연설명, 멀티 코드블록 등에 안전.
    실패 시 None.
    """
    if not text:
        return None

    candidates: list[str] = []

    # 1) 코드 펜스 후보 모두 수집
    for m in _JSON_OBJ_FENCE_RE.finditer(text):
        candidates.append(m.group(1).strip())

    # 2) 균형 매칭으로 최상위 객체 모두 수집 (in-string / escape 인지)
    depth = 0
    start_idx = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
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
        if ch == "{":
            if depth == 0:
                start_idx = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start_idx >= 0:
                    candidates.append(text[start_idx : i + 1])
                    start_idx = -1

    # 3) 단순 first/last brace 폴백 (멀티라인 JSON에 안전망)
    s = text.find("{")
    e = text.rfind("}")
    if s >= 0 and e > s:
        candidates.append(text[s : e + 1])

    # 4) 후보 순서대로 시도, dict 성공 시 반환
    for c in candidates:
        try:
            parsed = json.loads(c)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError) as exc:
            logger.debug("extract_json candidate parse FAIL: %s", exc)
            continue
    return None


def daily_cost_summary(date_str: str | None = None) -> dict:
    """오늘(또는 지정일) LLM 비용·호출 집계."""
    from .db import connect

    today = date_str or datetime.now().strftime("%Y-%m-%d")
    with connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS usage_llm (
                date TEXT NOT NULL, model TEXT NOT NULL, agent TEXT NOT NULL,
                call_count INTEGER DEFAULT 0, input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0, est_cost_usd REAL DEFAULT 0.0,
                PRIMARY KEY(date,model,agent)
            )"""
        )
        rows = conn.execute(
            "SELECT model, agent, call_count, input_tokens, output_tokens, est_cost_usd "
            "FROM usage_llm WHERE date=? ORDER BY est_cost_usd DESC",
            (today,),
        ).fetchall()
    total = sum(r["est_cost_usd"] for r in rows)
    calls = sum(r["call_count"] for r in rows)
    return {
        "date": today,
        "total_usd": round(total, 4),
        "total_calls": calls,
        "breakdown": [dict(r) for r in rows],
    }


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "cost":
        print(json.dumps(daily_cost_summary(), ensure_ascii=False, indent=2))
    elif len(sys.argv) > 1 and sys.argv[1] == "cache":
        print(json.dumps(db_cache_summary(), ensure_ascii=False, indent=2))
    else:
        print(f"TOP_MODEL={TOP_MODEL}")
        print(f"ISHIKAWA_MODEL={ISHIKAWA_MODEL}")
        print(f"TOGUSA_MODEL={TOGUSA_MODEL}")
        print(f"IGNORE_CACHE={_IGNORE_CACHE_ENV}")
