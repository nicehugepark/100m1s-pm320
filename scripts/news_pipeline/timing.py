"""파이프라인 구간별 + interpret 스레드별 소요 시간 측정 (대표 지시 2026-05-28 11:53 KST).

대표 지시 2종:
  1. 설정 일원화(INTERPRET_CONCURRENCY 등)가 동시성 성능을 저해하는지 측정 데이터 확보.
  2. 파이프라인 구간마다 + interpret 스레드마다 소요 시간을 측정하는 로그/메트릭 신설.

설계 원칙 (기존 동작 영향 0건):
  - 측정 코드는 **추가 로그/append 만** 수행. 기존 로직(반환값·예외 흐름)은 일절 건드리지 않음.
  - 메트릭 파일 write 실패는 절대 파이프라인을 깨면 안 됨 → 모든 write 를 try/except 로 격리.
  - jsonl append (한 줄 = 한 측정) — 분석 도구가 라인 단위 stream 파싱.
  - 파일 위치: config.LOG_DIR (scripts/news_pipeline/logs/, .gitignore 대상).
    워크트리·cron-isolation 환경에서도 config.ROOT 가 자동 resolve → 경로 일관.

분석 활용 (대표 지시 1 — 동시성 성능 저해 여부):
  - interpret 스레드별 elapsed 합(sum_elapsed) / 전체 wall(wall) = 유효 병렬도(effective_parallelism).
  - 동시성 N 으로 캡 됐다면 effective_parallelism ≈ N (직렬에 가까우면 1).
  - INTERPRET_CONCURRENCY=50 vs 3 비교 시: 같은 종목 집합에서 wall 비교 + effective_parallelism
    비교로 설정 일원화가 처리량을 떨어뜨렸는지 정량 판정.
"""

from __future__ import annotations

import json
import time
from datetime import datetime

from .config import LOG_DIR

INTERPRET_TIMING_LOG = LOG_DIR / "interpret-timing.jsonl"
STAGE_TIMING_LOG = LOG_DIR / "stage-timing.jsonl"


def _append_jsonl(path, record: dict) -> None:
    """jsonl 1줄 append. 어떤 실패도 호출측으로 전파하지 않음 (측정이 파이프라인을 깨면 안 됨)."""
    try:
        line = json.dumps(record, ensure_ascii=False)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001 — 측정 실패는 silent (기존 동작 영향 0건 원칙)
        pass


def record_interpret_timing(
    *,
    code: str,
    elapsed_sec: float,
    ok: bool,
    error: str | None,
    concurrency: int,
    run_id: str,
) -> None:
    """interpret 단일 종목(=단일 worker thread) 소요 시간 1건 박제.

    Args:
        code: 종목 코드 (6자리).
        elapsed_sec: 해당 종목 interpret() wrapper 소요 시간 (monotonic).
        ok: verdict 적재 성공 여부.
        error: 예외 사유 (없으면 None).
        concurrency: 본 run 의 INTERPRET_CONCURRENCY (동시성 성능 분석 기준값).
        run_id: 본 interpret_loop 실행 식별자 (같은 run 의 스레드들을 묶어 wall 계산).
    """
    _append_jsonl(
        INTERPRET_TIMING_LOG,
        {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "run_id": run_id,
            "code": code,
            "elapsed_sec": round(elapsed_sec, 2),
            "ok": ok,
            "error": (error[:200] if error else None),
            "concurrency": concurrency,
        },
    )


def record_interpret_run_summary(
    *,
    run_id: str,
    concurrency: int,
    wall_sec: float,
    sum_elapsed_sec: float,
    submitted: int,
    ok: int,
    skipped: int,
    errors: int,
) -> None:
    """interpret_loop 전체 run 요약 1건 박제 (동시성 성능 분석의 핵심 레코드).

    effective_parallelism = sum_elapsed_sec / wall_sec
      → 동시성 N 캡 시 ≈ N (직렬에 가까우면 ≈ 1). 설정 일원화가 동시성을
        저해했는지 = 같은 종목 집합에서 wall 증가 + effective_parallelism 하락으로 판정.
    """
    eff = round(sum_elapsed_sec / wall_sec, 2) if wall_sec > 0 else 0.0
    _append_jsonl(
        INTERPRET_TIMING_LOG,
        {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "run_id": run_id,
            "kind": "run_summary",
            "concurrency": concurrency,
            "wall_sec": round(wall_sec, 2),
            "sum_elapsed_sec": round(sum_elapsed_sec, 2),
            "effective_parallelism": eff,
            "submitted": submitted,
            "ok": ok,
            "budget_skipped": skipped,
            "errors": errors,
        },
    )


def new_run_id() -> str:
    """interpret_loop 1회 실행 식별자 (epoch-ms 기반, 같은 run 의 스레드 묶음용)."""
    return f"interp-{int(time.time() * 1000)}"
