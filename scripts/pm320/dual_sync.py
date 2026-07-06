#!/usr/bin/env python3
"""PM320 독립 마이그레이션 S2 — 보조(독립 repo) 동기화 공용 헬퍼.

DOC-20260614-DSN-002 S2 (데이터 배선·dual-write).

배경
----
매일 15:20 픽 데이터는 두 writer 가 홈페이지 cron WT(서빙 정본)로 push 한다:
  1. push_pick_preview.py :: sync_push()          — 15:20:50 경량 선공개 (브랜드 약속)
  2. build_card_history.py :: sync_to_homepage_main() — 15:35경 상세 카드

S2 목표 = 위 홈 push 를 **무중단 유지**하면서, 동일 산출물을 독립 repo
(100m1s-pm320) cron WT 로도 **dual-write** (양쪽 동기). 독립 repo 가 실패해도
홈 픽은 절대 끊기지 않는다 (failure isolation = 브랜드 약속 최우선).

본 모듈은 두 writer 가 공유한다 — "한쪽 코드만 고치고 다른 쪽 누락"
(FLR-20260406-TEC-001 / FLR-20260428-TEC-001 동형) 재발 방지 공통 모듈화.

설계 원칙 (필독)
----------------
- **failure isolation (최우선)**: 본 함수는 *예외를 절대 밖으로 던지지 않고*,
  실패해도 호출자 push exit code 에 영향 주지 않는다. 반환값은 진단/관찰용 로그
  목적의 SyncResult 일 뿐, 호출자는 이를 자기 exit code 로 승격하면 안 된다.
  → 독립 repo push 가 깨져도 홈 픽 cascade(카드·카톡)는 정상 진행 (무중단).
- **opt-in (기본 OFF)**: 환경변수 ``M1S_PM320_INDEPENDENT`` 가 *없으면* 완전 무동작.
  S2 활성화 = lead 가 cron plist 에 env 추가하는 시점 (그 전까지 현행 동작 0 변화).
- **dry-run**: ``M1S_PM320_INDEPENDENT_NO_SYNC=1`` → write 만, git push skip.
- **git 안전 (홈 sync 와 동형, §11.27)**:
  - pre-staged 존재 시 add/commit SKIP (타 actor staged 보호) — 독립 repo 도 동일 가드.
  - add 는 본 산출물 경로만 (무차별 glob 금지).
  - push = pull --rebase --autostash origin main → push HEAD:main (plain, force 금지),
    race(거절) 시 재시도 1회. force / reset / stash 조작 0.
  - FLR-20260612-TEC-002 (untracked 전이 충돌) 대비: 본 cron WT 는 전용 격리 브랜치라
    동일 경로 untracked 충돌면이 없도록 신설 시 1회 pull 선행을 운영 체크리스트화.

대상 경로
---------
설계 원안(S1) = ``<독립 cron WT>/site`` 하위. 단 **실제 컷오버본(2026-07-05,
100m1s-pm320 repo)은 GitHub Pages 루트 서빙 = 루트에 data/ 직접 배치** —
서브디렉토리는 env ``M1S_PM320_INDEPENDENT_SITE_SUBDIR`` 로 조정한다
(기본 "site" 유지 = 기존 동작 0 변화 / 컷오버본 대상 활성 시 ``.`` 지정).
데이터 fetch 경로는 홈과 동일한 root-absolute ``/data/...`` 규약.

  - 본 픽:    <subdir>/data/pm320_history/{date}.json
  - preview:  <subdir>/data/pm320_history/preview/{date}.json

env
---
  M1S_PM320_INDEPENDENT          독립 repo cron WT 루트 (예: ~/company/100m1s-pm320-cron).
                                 미설정 시 본 모듈 전체 무동작 (S2 비활성).
  M1S_PM320_INDEPENDENT_SITE_SUBDIR   서빙 자산 서브디렉토리 (기본 "site").
                                 컷오버본(루트 배치)은 "." 지정 → <WT>/data/... 착지.
  M1S_PM320_INDEPENDENT_NO_SYNC=1   write 만 수행, git add/commit/push skip (dry-run).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# 독립 repo 서빙 자산 루트 서브디렉토리 (설계 원안 = site/ 하위, 컷오버본 = 루트 ".").
# 데이터 물리 경로 = <WT>/<subdir>/data/... (fetch 는 root-absolute /data/...).
INDEPENDENT_SITE_SUBDIR_DEFAULT = "site"
INDEPENDENT_SITE_SUBDIR_ENV = "M1S_PM320_INDEPENDENT_SITE_SUBDIR"
INDEPENDENT_ENV = "M1S_PM320_INDEPENDENT"
INDEPENDENT_NO_SYNC_ENV = "M1S_PM320_INDEPENDENT_NO_SYNC"


@dataclass
class SyncResult:
    """독립 repo dual-write 결과 (진단/관찰용 — 호출자 exit code 로 승격 금지)."""

    status: str  # "ok" | "skip" | "fail"
    detail: str

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def independent_root() -> Path | None:
    """독립 repo cron WT 루트. env 미설정 시 None (S2 비활성)."""
    raw = os.environ.get(INDEPENDENT_ENV)
    if not raw:
        return None
    return Path(raw)


def independent_data_dir(root: Path) -> Path:
    """독립 repo 의 data/ 물리 경로 (기본 <WT>/site/data, subdir "." 시 <WT>/data).

    컷오버본(100m1s-pm320, 2026-07-05)은 루트에 data/ 직접 배치 —
    env ``M1S_PM320_INDEPENDENT_SITE_SUBDIR="."`` 로 지정 (경로 mismatch 봉쇄,
    2026-07-06 데이터 공백 사고 후속).
    """
    subdir = os.environ.get(
        INDEPENDENT_SITE_SUBDIR_ENV, INDEPENDENT_SITE_SUBDIR_DEFAULT
    ).strip()
    if subdir in ("", "."):
        return root / "data"
    return root / subdir / "data"


def _git(
    root: Path, args: list[str], timeout: int = 120
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _git_push_one(root: Path, rel: str, commit_msg: str, log) -> SyncResult:
    """산출물 1파일 add → commit → rebase+push (race 재시도 1회). force 금지."""
    # 타 actor staged 보호 (§11.27) — pre-staged 존재 시 SKIP (독립 repo 도 동일).
    pre = _git(root, ["diff", "--cached", "--name-only"], timeout=30)
    if pre.returncode != 0:
        return SyncResult("fail", f"git diff --cached rc={pre.returncode}")
    if pre.stdout.strip():
        return SyncResult(
            "skip",
            f"pre-existing staged ({len(pre.stdout.splitlines())} files) — 다음 fire 재시도",
        )

    status = _git(root, ["status", "--porcelain", "--", rel], timeout=30)
    if status.returncode != 0:
        return SyncResult("fail", f"git status rc={status.returncode}")
    if not status.stdout.strip():
        return SyncResult("ok", f"no diff (idempotent, {rel})")

    if _git(root, ["add", rel], timeout=30).returncode != 0:
        return SyncResult("fail", "git add")
    if _git(root, ["commit", "-m", commit_msg], timeout=60).returncode != 0:
        return SyncResult("fail", "git commit")

    # post-commit staged 잔존 검사 (홈 sync 동형 안전장치)
    post = _git(root, ["diff", "--cached", "--name-only"], timeout=30)
    if post.returncode == 0 and post.stdout.strip():
        return SyncResult(
            "fail", f"commit left staged ({len(post.stdout.splitlines())})"
        )

    # fetch + rebase 2단계 분리: cron WT upstream(cron-isolation)과 origin/main 모호성 제거
    # (pull --rebase origin main 는 upstream 추적 브랜치가 별도 설정된 경우
    #  "Cannot rebase onto multiple branches" fatal 발생 — 2026-06-29 근본 수정)
    for attempt in (1, 2):
        fetch = _git(root, ["fetch", "origin", "main"], timeout=30)
        if fetch.returncode != 0:
            log(
                f"INDEP SYNC retry (fetch attempt {attempt}): "
                f"{fetch.stderr.strip()[:160]}"
            )
            if attempt == 2:
                return SyncResult("fail", f"fetch: {fetch.stderr.strip()[:160]}")
            time.sleep(3)
            continue
        pull = _git(root, ["rebase", "--autostash", "origin/main"])
        if pull.returncode != 0:
            log(
                f"INDEP SYNC retry (rebase attempt {attempt}): "
                f"{pull.stderr.strip()[:160]}"
            )
            if attempt == 2:
                return SyncResult("fail", f"rebase: {pull.stderr.strip()[:160]}")
            time.sleep(3)
            continue
        push = _git(root, ["push", "origin", "HEAD:main"])
        if push.returncode == 0:
            return SyncResult("ok", f"push → origin main ({rel}, attempt {attempt})")
        log(
            f"INDEP SYNC retry (push rejected attempt {attempt}): {push.stderr.strip()[:160]}"
        )
        if attempt == 2:
            return SyncResult("fail", f"push rejected: {push.stderr.strip()[:160]}")
        time.sleep(3)
    return SyncResult("fail", "push exhausted")


def dual_write(
    source_path: Path,
    rel_data_path: str,
    date_str: str,
    log,
    commit_label: str,
) -> SyncResult:
    """홈 push 성공 후, 동일 산출물을 독립 repo 로도 dual-write (failure isolated).

    🔴 호출자 계약: 본 함수는 **예외를 절대 밖으로 던지지 않으며**, 반환 SyncResult
    는 진단/관찰용일 뿐 호출자 exit code 로 승격하면 안 된다. 독립 repo 가
    실패해도 홈 픽은 무중단 (브랜드 약속 최우선).

    Parameters
    ----------
    source_path : 복사 원본 (이미 sanitize 된 공개판 — 홈 push 와 동일 파일).
    rel_data_path : data/ 기준 상대 경로 (예: "pm320_history/2026-06-15.json").
                    독립 repo 물리 경로 = <WT>/site/data/<rel_data_path>.
    date_str : 날짜(또는 "summary") — 커밋 메시지·로그용.
    log : 호출자 로거 (callable[[str], None]).
    commit_label : 커밋 메시지 식별 라벨 (예: "preview" / "history").

    Returns
    -------
    SyncResult : status ∈ {ok, skip, fail}. 호출자는 로그만 남기고 무시한다.
    """
    try:
        root = independent_root()
        if root is None:
            return SyncResult("skip", f"{INDEPENDENT_ENV} 미설정 (S2 비활성)")
        if not root.exists() or not (root / ".git").exists():
            log(f"INDEP SYNC SKIP: 독립 repo WT 부재 ({root})")
            return SyncResult("skip", f"WT 부재 ({root})")
        if not source_path.exists() or source_path.stat().st_size == 0:
            log(f"INDEP SYNC SKIP: source 부재/빈파일 ({source_path})")
            return SyncResult("skip", "source 부재/빈파일")

        target = independent_data_dir(root) / rel_data_path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(source_path), str(target))
            log(f"INDEP SYNC: cp {source_path.name} → {target}")
        except Exception as exc:  # noqa: BLE001 — 격리: 호출자에 전파 금지
            log(f"INDEP SYNC FAIL (cp): {type(exc).__name__}: {exc}")
            return SyncResult("fail", f"cp: {type(exc).__name__}")

        if os.environ.get(INDEPENDENT_NO_SYNC_ENV) == "1":
            log("INDEP SYNC SKIP: NO_SYNC=1 (write-only dry-run)")
            return SyncResult("skip", "NO_SYNC=1 (write-only)")

        rel = str(target.relative_to(root))
        commit_msg = (
            f"data(pm320,independent,{commit_label},{date_str}): "
            f"S2 dual-write (dual_sync.py)"
        )
        result = _git_push_one(root, rel, commit_msg, log)
        if result.ok:
            log(f"INDEP SYNC: {result.detail}")
        else:
            log(f"INDEP SYNC {result.status.upper()}: {result.detail}")
        return result
    except Exception as exc:  # noqa: BLE001 — 최종 안전망: 어떤 예외도 홈 push 막지 않음
        log(f"INDEP SYNC FAIL (isolated): {type(exc).__name__}: {exc}")
        return SyncResult("fail", f"isolated: {type(exc).__name__}")
