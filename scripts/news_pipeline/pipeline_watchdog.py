"""파이프라인 카드/집계 stale 감지 + 자동 복구 (2026-05-26 LLM 회복력 §B1).

대표 지시 ("llm 죽을 수 있는 상황 방지·대비"): 장중(09:00~15:35 KST)에 카드/집계가
멈추면(LLM hang → 파이프라인 SIGTERM → build_daily 미실행) 자동 복구한다.

동작:
  1) 장중이 아니면 즉시 종료 (주말·공휴일·장외 시간은 stale 정상 → 복구 불필요).
  2) 오늘 stock-{date}.json 의 generated_at 을 읽어 stale(기본 15분) 여부 판정.
     파일 부재도 stale 로 간주 (장중인데 오늘 빌드가 없음).
  3) stale 이면 build_daily 를 1회 실행해 카드/집계만 재발행한다 (LLM 미경유 —
     build_daily 는 키움 스냅샷 + 상한가 union + DB SELECT enrichment 만 사용,
     interpret_loop 호출 안 함). 복구 1회 후 osascript 알림.

회복력 설계 (개발팀 비판 가드):
  - 메인 파이프라인 lock(/tmp/100m1s-pipeline.lock)을 공유. lock 이 점유 중이면
    파이프라인이 지금 돌고 있다는 뜻 → stale 이 곧 해소됨 → 복구 SKIP (중복·race 방지).
    이 watchdog 은 launchd 가 with_lock.sh 경유로 호출하므로 lock 점유는 launchd 가
    보장 (별도 fcntl 코드 불필요 — DRY).
  - git push 안 함 (메인 pipeline.sh 가 다음 cycle 에 정상 push). 복구는 "로컬
    카드 JSON 재발행"까지만 → cron race(FLR-003 cluster) 원천 회피.
  - INTERPRET 미실행 → 복구 자체가 다시 hang 할 위험 0.
  - 어떤 실패에도 비0 exit 안 함 (launchd cascade 방지). 복구 시도 결과만 로그.
"""

from __future__ import annotations

import json

# generated_at 이 이 분(minute)보다 오래되면 stale. env STALE_THRESHOLD_MIN 로 조정.
import os
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import HOMEPAGE, is_market_hours, pipeline_date

_STALE_THRESHOLD_MIN = int(os.environ.get("WATCHDOG_STALE_MIN", "15"))


def _today_generated_at(target_date: str) -> datetime | None:
    """오늘 published stock-{date}.json 의 generated_at(naive KST) 반환. 없으면 None."""
    f = HOMEPAGE / "data" / "interpreted" / f"stock-{target_date}.json"
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        ts = data.get("generated_at")
        if not ts:
            return None
        # build_daily 는 datetime.now().isoformat() (naive KST) 로 기록.
        return datetime.fromisoformat(ts)
    except (json.JSONDecodeError, ValueError, OSError):
        return None


def _staleness_minutes(target_date: str, now_kst_naive: datetime) -> float | None:
    """카드 stale 분(minute). 파일/timestamp 부재면 None(= 무한 stale 취급은 호출부)."""
    gen = _today_generated_at(target_date)
    if gen is None:
        return None
    return (now_kst_naive - gen).total_seconds() / 60.0


def _notify(msg: str) -> None:
    """osascript 알림 (best-effort, 기존 pipeline.sh/with_lock.sh 패턴 재사용)."""
    try:
        subprocess.run(  # noqa: S603 — 고정 명령·자체 메시지(외부 입력 아님)
            [
                "/usr/bin/osascript",
                "-e",
                f'display notification "{msg}" with title "100m1s WATCHDOG"',
            ],
            capture_output=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        pass


def _recover_build_daily() -> bool:
    """build_daily 1회 실행 (LLM 미경유 카드/집계 재발행). 성공 시 True."""
    try:
        r = subprocess.run(  # noqa: S603 — sys.executable(절대경로) + 고정 모듈명
            [sys.executable, "-m", "scripts.news_pipeline.build_daily"],
            capture_output=True,
            text=True,
            timeout=180,  # build_daily 정상 7~40s. 180s 면 충분 + cron budget 내.
        )
        if r.returncode == 0:
            print("[watchdog] build_daily 복구 성공")
            return True
        print(f"[watchdog] build_daily 복구 rc={r.returncode} stderr={r.stderr[:300]}")
        return False
    except subprocess.TimeoutExpired:
        print("[watchdog] build_daily 복구 TIMEOUT(180s)")
        return False
    except Exception as e:  # noqa: BLE001 — 회복력: 어떤 실패도 비0 exit 금지
        print(f"[watchdog] build_daily 복구 예외: {e}")
        return False


def main() -> None:
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    now_naive = now.replace(tzinfo=None)  # build_daily generated_at 이 naive KST

    if not is_market_hours(now):
        print(f"[watchdog] {now:%Y-%m-%d %H:%M} KST 장외/휴장 → SKIP (stale 정상)")
        return

    target_date = pipeline_date()
    stale_min = _staleness_minutes(target_date, now_naive)

    if stale_min is None:
        # 오늘 빌드 파일이 아예 없음 — 장중인데 카드 부재 = critical stale.
        print(f"[watchdog] 🔴 stock-{target_date}.json 부재 (장중) → 복구 시도")
        ok = _recover_build_daily()
        _notify(
            f"카드 파일 부재(장중) → build_daily 복구 "
            f"{'성공' if ok else '실패(로그 확인)'}"
        )
        return

    if stale_min > _STALE_THRESHOLD_MIN:
        print(
            f"[watchdog] 🔴 카드 stale {stale_min:.1f}분 "
            f"(> {_STALE_THRESHOLD_MIN}분, 장중) → 복구 시도"
        )
        ok = _recover_build_daily()
        # 복구 후 재측정
        after = _staleness_minutes(
            target_date, datetime.now(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)
        )
        _notify(
            f"카드 {stale_min:.0f}분 stale → build_daily 복구 "
            f"{'성공' if ok else '실패'}"
            + (f" (현재 {after:.0f}분)" if after is not None else "")
        )
    else:
        print(
            f"[watchdog] 카드 fresh {stale_min:.1f}분 "
            f"(<= {_STALE_THRESHOLD_MIN}분) → 복구 불필요"
        )


if __name__ == "__main__":
    main()
