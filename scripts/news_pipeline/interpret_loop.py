"""거래대금 리스트 전량 순회 LLM 해석 (REQ-003 캐시 적용).

Q-20260511-FIX-A (P0, dev-audit-v2 root cause A):
- 기존: latest.json (조건검색 30종목)만 fan-out
- 결함: build_daily의 상한가 union 결과(LU +N건) 미해석 → themes=[] 발생
- 정정: latest.json ∪ 상한가 union(stock_status_badges) 통합 fan-out
  (build_daily.py:2600~2664 의 union 로직과 동일 SQL 적용)
- 의존 역전 회피: build_daily 이전 stage라도 stock_status_badges는
  collect_kiwoom_limit_up.py가 이미 적재했으므로 read 가능
"""

from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import HOMEPAGE, pipeline_date
from .db import connect
from .interpret_stocks import _load_theme_dictionary, interpret
from .llm_client import cache_stats
from .timing import (
    new_run_id,
    record_interpret_run_summary,
    record_interpret_timing,
)

# P0 (2026-05-26, 카드·집계 decouple): interpret_loop 는 30분 cron 의 build_daily
# 직전 stage. LLM (ishikawa/togusa) call_model 이 종목별로 hang/throttle 하면
# (timeout 60s × 3 attempt = 180s/종목) 누적되어 interpret_loop dur 가 540s
# (with_lock.sh CHILD_TIMEOUT_SEC) 를 초과 → with_lock 이 프로세스 그룹 전체 SIGTERM
# → 직후 build_daily 미실행 → 카드/집계 미발행.
#
# 증거 (2026-05-26 pipeline log): interpret_loop dur 최대 479s, call_model TIMEOUT
# model=claude-opus-4-7 agent=ishikawa 47회, interpret_loop 53 starts / build_daily
# 52 completions (1회 누락) + with_lock CHILD TIMEOUT 11회.
#
# 본 budget: deadline 도달 시 남은 종목 graceful skip → build_daily 에 충분한 시간
# (기본 420s = 540s - build_daily ~40s - generate_og/stock_og ~3s - 안전 마진 ~75s)
# 남김. LLM 해석은 opportunistic — 이번 cycle 못 한 종목은 다음 cycle 또는 캐시 HIT
# 시 자연 보완. 캐시 정책 (call_model_cached) 유지 — HIT 종목은 거의 0초이므로
# budget 영향 미미 (정상 cycle 은 65s 내 완료, budget 미발동).
_INTERPRET_BUDGET_SEC = int(os.environ.get("INTERPRET_BUDGET_SEC", "420"))

# 옵션 B (2026-05-26, 대표 지시 "opus 유지 + 호출 최소화"): interpret 동시 실행.
# 근본: ishikawa 를 opus 로 되돌리면 캐시 MISS 종목당 opus 호출이 60s+ 지연 →
# 순차 처리 시 N종목 × 60s 가 budget(420s)/CHILD_TIMEOUT(540s) 를 초과 → SIGTERM.
# (캐시 HIT 종목은 거의 0초 — interpret_stocks 의 call_model_cached 가 동일 입력해시
#  재사용. live stocks.db ishikawa_news opus-4-7 maxhit=424 = 캐시 자체는 잘 작동.)
# 동시 실행 = wall-time 을 N분의 1 로 단축 → opus 품질 유지하면서 budget 안에 처리.
#
# 안전:
#  - interpret_disclosures.py 가 이미 동일 ThreadPoolExecutor 패턴으로 검증됨
#    (interpret_disclosures.py:674, max_workers=2 기본). 본 loop 도 동일 패턴 차용
#    (FLR-20260406-TEC-001 recurring 교훈: 한쪽만 고치고 다른 쪽 누락 회피 — 동일
#     동시성 정책을 stocks 해석에도 일관 적용).
#  - claude CLI 는 subprocess (별도 프로세스) → GIL 영향 없음. db.connect() 는 호출
#    마다 새 connection + busy_timeout=5000 (db.py:28) → thread-safe.
#  - rate limit 안전 범위: 기본 워커 50 (2026-05-27, 대표 승인 DOC-20260527-DEC-001).
#    종목 40~50 넘는 경우 드물어 사실상 전 종목 동시. 근거: opus 20개 병렬 REAL 호출
#    실측 20/20 성공·429 0건·wall 21.6s·per-call avg 19.5s (개발팀 직접 테스트).
#    직렬에 가까운 동시성 3 (108콜 ~1548s) 으로 5/27 아침 cold-start 가 540s
#    CHILD_TIMEOUT 초과 → 홈페이지 1h+ stale (FLR-20260527-TEC-001). 동시성 50 으로
#    cold-start 를 budget(420s)/CHILD_TIMEOUT(540s) 안쪽으로 단축. 50 동시 일부 429
#    발생해도 call_model 이 429/rate/throttle/overloaded 를 retryable 로 잡아 backoff
#    재시도 (llm_client.py:313-330) + None 반환 graceful → 카드 decouple(473ba38) 로 보호.
#  - budget guard 유지: deadline 도달 시 미제출 종목 graceful skip + 진행 중 future
#    는 완료 대기하되 추가 submit 중단. (동시성 50 이면 cold-start 도 budget 미발동.)
_INTERPRET_CONCURRENCY = max(1, int(os.environ.get("INTERPRET_CONCURRENCY", "50")))


def load_fanout_codes(target_date: str | None = None) -> list[str]:
    """interpret fan-out 종목 코드 = daily_top(영웅식) ∪ latest.json(ranking) ∪ 상한가 union.

    Q-20260511-FIX-A: build_daily 와 동일한 union 룰을 interpret 단계에도 적용.
    공통 모듈로 분리하여 interpret_loop / nightly_recovery 양쪽 동시 적용.

    SoT 우선순위 (build_daily.load_kiwoom_volume_list 동일):
        (1) data/kiwoom/{target_date}.json  daily_top  (영웅문식 누적 — primary)
        (2) data/kiwoom/latest.json         stocks     (ka10032 ranking — 폴백/병합)
        (3) stock_status_badges 상한가      LU union   (build_daily 와 동일 SQL)

    daily_picks(date=target_date, source='kiwoom') 도 보완 안전망으로 read.

    Returns:
        ticker 6자리 코드 list (중복 제거, 순서: daily_top → latest → 상한가 → daily_picks).
    """
    today = target_date or pipeline_date()
    codes: list[str] = []
    seen: set[str] = set()
    kiwoom_dir = HOMEPAGE / "data" / "kiwoom"

    def _add(code: str) -> bool:
        if code and code not in seen:
            codes.append(code)
            seen.add(code)
            return True
        return False

    # 1) daily_top 영웅문식 누적 (data/kiwoom/{date}.json) — primary SoT
    date_file = kiwoom_dir / f"{today}.json"
    if date_file.exists():
        try:
            data = json.loads(date_file.read_text(encoding="utf-8"))
            stocks = data.get("daily_top") or data.get("stocks") or []
            added = 0
            for s in stocks:
                if _add(s.get("ticker") or s.get("code")):
                    added += 1
            print(f"[fanout] daily_top({today}.json): +{added}건 → total={len(codes)}")
        except Exception as e:
            print(f"[fanout] {date_file.name} 로드 실패: {e}")

    # 2) latest.json (ka10032 ranking — 거래대금 최신 30종목, daily_top 누락 보완)
    latest = kiwoom_dir / "latest.json"
    if latest.exists():
        try:
            data = json.loads(latest.read_text(encoding="utf-8"))
            added = 0
            for s in data.get("stocks", []):
                if _add(s.get("ticker") or s.get("code")):
                    added += 1
            if added:
                print(f"[fanout] latest.json: +{added}건 → total={len(codes)}")
        except Exception as e:
            print(f"[fanout] latest.json 로드 실패: {e}")

    # 3) 상한가 union (stock_status_badges 활성 상한가)
    #    build_daily.py:2600~2664 와 동일 SQL — date=today AND badge_type='상한가'
    #    AND active_until IS NULL. heroshik strict override는 build_daily 만 적용.
    try:
        with connect() as conn:
            rows = conn.execute(
                """SELECT DISTINCT stock_code FROM stock_status_badges
                   WHERE date=? AND badge_type='상한가'
                     AND active_until IS NULL""",
                (today,),
            ).fetchall()
            added_lu = 0
            for r in rows:
                if _add(r["stock_code"]):
                    added_lu += 1
            if added_lu:
                print(f"[fanout] 상한가 union: +{added_lu}건 → total={len(codes)}")

            # 4) daily_picks 보완 안전망 — 동일 source='kiwoom' rows 가 있다면 union
            #    (자기 재귀 우려 — daily_picks 는 build_daily 출력 SoT 인데 interpret 이전 stage
            #    부재 보장. recovery / 재실행 시에만 효과적이며 normal cron path 영향 없음.)
            try:
                rows2 = conn.execute(
                    """SELECT DISTINCT stock_code FROM daily_picks
                       WHERE date=? AND source='kiwoom'""",
                    (today,),
                ).fetchall()
                added_dp = 0
                for r in rows2:
                    if _add(r["stock_code"]):
                        added_dp += 1
                if added_dp:
                    print(
                        f"[fanout] daily_picks(kiwoom) 보완: +{added_dp}건 "
                        f"→ total={len(codes)} (이전 빌드 잔존 — recovery 경로)"
                    )
            except Exception:
                pass
    except Exception as e:
        print(f"[fanout] 상한가 union/daily_picks 조회 실패: {e}")

    return codes


def _interpret_one(
    code: str,
    ignore_cache: bool,
    *,
    run_id: str = "",
    concurrency: int = 0,
) -> tuple[str, bool, str | None, float]:
    """단일 종목 interpret() 래퍼 (worker thread 실행).

    회복력 (2026-05-26): 단일 종목 interpret() 예외가 stage 전체를 abort 시키지
    않도록 per-stock try/except. interpret() 는 LLM 실패 시 None 반환(정상)이지만,
    예기치 못한 예외(DB 락/네트워크/파싱)도 그 종목만 건너뛴다.
    (interpret_stocks.py 내부에도 부분 try/except 있으나, 함수 경계 안전망 유지.)

    측정 (2026-05-28 대표 지시): worker thread = 종목 1개 = 측정 단위. interpret()
    소요 시간(monotonic)을 재서 jsonl 1줄 박제 + 로그 1줄 출력. 측정은 추가 로그만
    이며 반환 흐름·예외 처리는 종전과 동일 (기존 동작 영향 0건). elapsed 는 호출측
    (main) 이 sum_elapsed 집계 → effective_parallelism 계산에 쓰도록 함께 반환.

    Returns:
        (code, ok, error_msg, elapsed_sec) — ok=True면 verdict 적재 성공,
        error_msg는 예외 시 사유, elapsed_sec는 본 종목 interpret() 소요 시간.
    """
    t0 = time.monotonic()
    ok = False
    err: str | None = None
    try:
        result = interpret(code, ignore_cache=ignore_cache)
        ok = bool(result)
    except Exception as e:  # noqa: BLE001 — cron 회복력: 어떤 예외도 stage 미중단
        err = str(e)
    elapsed = time.monotonic() - t0
    # 측정 박제 (실패해도 silent — 파이프라인 미영향).
    print(
        f"[interpret-timing] code={code} elapsed={elapsed:.1f}s "
        f"ok={ok} concurrency={concurrency}"
    )
    record_interpret_timing(
        code=code,
        elapsed_sec=elapsed,
        ok=ok,
        error=err,
        concurrency=concurrency,
        run_id=run_id,
    )
    return code, ok, err, elapsed


def main(ignore_cache: bool = False):
    codes = load_fanout_codes()
    if not codes:
        print("no fanout codes (latest.json + 상한가 union 모두 빈 결과)")
        return
    print(
        f"interpreting {len(codes)} stocks "
        f"(ignore_cache={ignore_cache}, budget={_INTERPRET_BUDGET_SEC}s, "
        f"concurrency={_INTERPRET_CONCURRENCY})"
    )
    ok = 0
    skipped = 0
    errors = 0
    # 측정 (2026-05-28 대표 지시): run_id 로 본 실행의 스레드들을 묶고, wall_start 로
    # 전체 wall-time, sum_elapsed 로 종목별 소요 시간 합을 집계. effective_parallelism
    # = sum_elapsed / wall 로 동시성 성능(저해 여부)을 정량 판정.
    run_id = new_run_id()
    wall_start = time.monotonic()
    sum_elapsed = 0.0
    submitted = 0
    deadline = time.monotonic() + _INTERPRET_BUDGET_SEC

    # Q-20260527-001 (옵션 B): CALL_WALL 동적 캡 — interpret deadline 환경변수 export.
    # llm_client._effective_wall_budget(remaining_seconds) 이 잔여 BUDGET 시점 조회 →
    # _CALL_WALL_BUDGET_SEC static vs 잔여 BUDGET 중 작은 값으로 per-call wall 캡.
    # budget 임박 시 in-flight worker 의 retry/backoff 자연 truncate → with_lock
    # CHILD_TIMEOUT 540s 안쪽 강제 (worst-case wall ≤ INTERPRET_BUDGET_SEC + opus
    # 1회 attempt cap). 환경변수 전달 이유: ThreadPoolExecutor worker 가 별 thread 라도
    # os.environ 동일 process 공유 → IPC 불요. subprocess (claude CLI) 도 부모 environ
    # 상속하지만 child 자체는 미사용 — Python-side llm_client 가 부모 process 내부에서
    # 잔여 BUDGET 시점 계산. budget 0 또는 미설정 시 export 생략 → llm_client 가 static
    # _CALL_WALL_BUDGET_SEC 그대로 사용 (하위호환 보존).
    if _INTERPRET_BUDGET_SEC > 0:
        os.environ["INTERPRET_DEADLINE_MONOTONIC"] = str(deadline)

    # 동시성 race 사전 봉쇄: interpret_stocks 의 module-level theme dictionary lazy-load
    # (_CANONICAL_THEMES / _INDUSTRY_SEEDS, interpret_stocks.py:45-59) 를 pool spawn 전
    # 메인 스레드에서 1회 pre-warm → worker 들이 동시에 lazy-load 진입하는 부분-상태
    # 읽기 가능성 제거 (idempotent 하지만 명시 봉쇄).
    _load_theme_dictionary()

    # 동시성 1 = 기존 순차 거동 (regression-free 경로 보존, recovery 시 환경변수로 강제 가능).
    if _INTERPRET_CONCURRENCY <= 1:
        for idx, code in enumerate(codes):
            if _INTERPRET_BUDGET_SEC > 0 and time.monotonic() >= deadline:
                skipped = len(codes) - idx
                print(
                    f"[budget] INTERPRET_BUDGET_SEC={_INTERPRET_BUDGET_SEC}s 초과 "
                    f"→ 남은 {skipped}종목 graceful skip (build_daily 시간 확보, "
                    f"다음 cycle/캐시 HIT 시 보완)"
                )
                break
            _, got, err, elapsed = _interpret_one(
                code,
                ignore_cache,
                run_id=run_id,
                concurrency=_INTERPRET_CONCURRENCY,
            )
            submitted += 1
            sum_elapsed += elapsed
            if err is not None:
                errors += 1
                print(f"[interpret-error] {code} 예외 → skip (다음 종목 진행): {err}")
            elif got:
                ok += 1
        print(
            f"interpreted: {ok}/{len(codes)} "
            f"(budget-skipped={skipped}, errors={errors})"
        )
        record_interpret_run_summary(
            run_id=run_id,
            concurrency=_INTERPRET_CONCURRENCY,
            wall_sec=time.monotonic() - wall_start,
            sum_elapsed_sec=sum_elapsed,
            submitted=submitted,
            ok=ok,
            skipped=skipped,
            errors=errors,
        )
        print(f"[cache] {cache_stats()}")
        return

    # 옵션 B: ThreadPoolExecutor 동시 실행 — 단, in-flight 작업을 워커 수로 bound.
    #
    # ⚠️ 핵심 (sandbox TEST3 catch): ex.submit() 은 즉시 반환(non-blocking)이므로
    # 단순 submit 루프는 deadline 점검 전에 전 종목을 큐에 밀어넣는다 → 풀이 모두를
    # 순차 소화하며 budget 을 초과 → SIGTERM. 따라서 submit 시점 deadline 점검만으로는
    # budget 이 전혀 강제되지 않는다.
    #
    # 봉쇄: 동시에 떠 있는 future 를 max_workers 개로 제한(bounded) — 하나 완료될 때마다
    # 다음 1건 submit. submit 직전 deadline 점검 → 진행 중인 workers 가 완료되면 추가
    # 제출 중단(graceful). 이렇게 하면 "초과로 큐잉된 작업" 0건 → budget 실효.
    # 진행 중(이미 시작된) worker 는 자연 완료 대기(중단 불가, claude subprocess).
    # call_model 의 wall-budget(_CALL_WALL_BUDGET_SEC=200s, llm_client.py)이 개별 종목의
    # 최대 지연을 제한하므로 worker 가 무한정 매달리지 않는다.
    it = iter(codes)
    with ThreadPoolExecutor(max_workers=_INTERPRET_CONCURRENCY) as ex:
        futures: dict = {}

        def _submit_next() -> bool:
            """budget 여유 시 다음 종목 1건 submit. 제출하면 True, 소진/budget 시 False."""
            nonlocal submitted
            if _INTERPRET_BUDGET_SEC > 0 and time.monotonic() >= deadline:
                return False
            try:
                code = next(it)
            except StopIteration:
                return False
            futures[
                ex.submit(
                    _interpret_one,
                    code,
                    ignore_cache,
                    run_id=run_id,
                    concurrency=_INTERPRET_CONCURRENCY,
                )
            ] = code
            submitted += 1
            return True

        # 초기 충전: 워커 수만큼 in-flight 채움 (budget 내).
        for _ in range(_INTERPRET_CONCURRENCY):
            if not _submit_next():
                break

        # 완료될 때마다 결과 집계 + 다음 1건 보충 (budget 허용 시).
        while futures:
            done = next(as_completed(futures))
            code = futures.pop(done)
            _, got, err, elapsed = done.result()
            sum_elapsed += elapsed
            if err is not None:
                errors += 1
                print(f"[interpret-error] {code} 예외 → skip (다음 종목 진행): {err}")
            elif got:
                ok += 1
            _submit_next()  # budget 소진 시 False — 추가 제출 없이 잔여 future 만 소화

    # 미제출(budget skip) 종목 수 = 전체 - 제출.
    skipped = len(codes) - submitted
    if skipped > 0:
        print(
            f"[budget] INTERPRET_BUDGET_SEC={_INTERPRET_BUDGET_SEC}s 도달 "
            f"→ {skipped}종목 미제출 graceful skip (제출 {submitted}건 완료, "
            f"build_daily 시간 확보, 다음 cycle/캐시 HIT 시 보완)"
        )
    print(
        f"interpreted: {ok}/{len(codes)} "
        f"(submitted={submitted}, budget-skipped={skipped}, errors={errors})"
    )
    record_interpret_run_summary(
        run_id=run_id,
        concurrency=_INTERPRET_CONCURRENCY,
        wall_sec=time.monotonic() - wall_start,
        sum_elapsed_sec=sum_elapsed,
        submitted=submitted,
        ok=ok,
        skipped=skipped,
        errors=errors,
    )
    print(f"[cache] {cache_stats()}")


if __name__ == "__main__":
    main(ignore_cache="--ignore-cache" in sys.argv[1:])
