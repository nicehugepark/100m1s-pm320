#!/usr/bin/env bash
# us-intraday 산출물(data/us-indices/*.json) self-deploy — Q-20260608 (격리 worktree 재설계).
#
# 🔴 재설계 배경 (FLR-20260608-TEC-002 봉쇄):
#   기존 방식(cron worktree cron-isolation 브랜치에서 stash/pull --rebase/push)은
#     (1) 공유 pipeline lock(/tmp/100m1s-pipeline.lock) 점유 시 호출측 graceful SKIP →
#         push 안 됨 → 라이브 선물 59분+ stale,
#     (2) cron worktree(cron-isolation)가 origin/main 과 반복 divergence(378/382) →
#         rebase 경합·실패 빈발.
#   본 스크립트는 배포를 **origin/main 기준 격리 임시 worktree**에서 수행 →
#     공유 락·cron worktree·divergence 와 무관하게 깨끗이 배포.
#     (lead 우회 검증 41b11f25c 패턴 정식화.)
#
# 동작:
#   1. cron worktree(읽기 전용 source)의 data/us-indices/*.json 산출물을 읽어,
#   2. `git worktree add <tmp> origin/main` 으로 origin/main 기준 신선 임시 worktree 생성,
#   3. 해당 파일 cp → `git add data/us-indices/*.json` → (변경 있을 때만) commit,
#   4. `git fetch + pull --rebase origin main` → `git push origin HEAD:main`,
#   5. 임시 worktree 항상 cleanup(trap, PID/timestamp suffix 충돌 방지).
#
# 🔴 안전:
#   - 공유 락(SKIP) 게이트 제거: 임시 worktree 는 cron worktree·타 파이프라인 git 과
#     충돌 안 함(독립 worktree, 독립 index).
#   - force / --no-verify / reset --hard 미사용. pull --rebase 로 동시 push 흡수.
#   - 메인레포 100m1s-homepage(divergent) 절대 미사용. 서빙 = origin/main.
#   - 임시 worktree 경로 = PID + timestamp suffix → 동시 fire 충돌 방지.
#   - cron worktree git 상태(status/index)는 read-only(cp source)로만 사용 — 절대 add/commit 안 함.
#     (393 backfill 등 cron worktree 동시 작업과 무충돌.)
set -uo pipefail
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:$PATH"

# source = cron worktree (us-indices 산출물 생성처). read-only 로만 사용.
SRC_HOMEPAGE="${M1S_HOMEPAGE:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)}"
SRC_USINDICES="$SRC_HOMEPAGE/data/us-indices"

cd "$SRC_HOMEPAGE" || exit 0

echo "--- $(date '+%Y-%m-%d %H:%M:%S %Z') us-intraday self-deploy (isolated worktree) ---"

if [ ! -d "$SRC_USINDICES" ]; then
  echo "[us-intraday self-deploy] source us-indices 디렉토리 없음 — 생략"
  exit 0
fi

# 배포 대상 산출물 (당일 + 직전 영업일까지 커버되도록 us-indices 전체 *.json 동기화).
shopt -s nullglob
SRC_FILES=("$SRC_USINDICES"/*.json)
shopt -u nullglob
if [ "${#SRC_FILES[@]}" -eq 0 ]; then
  echo "[us-intraday self-deploy] source us-indices/*.json 0건 — 생략"
  exit 0
fi

# origin/main 최신화 (격리 worktree 기준점).
git fetch origin main 2>&1 || true

# 격리 임시 worktree 경로 — PID+timestamp suffix 로 동시 fire 충돌 방지.
TMP_WT="/tmp/m1s-us-intraday-deploy-$$-$(date +%s)"

cleanup() {
  # 임시 worktree 항상 제거 (정상/비정상 종료 모두). cron worktree·메인 worktree 무접촉.
  if [ -d "$TMP_WT" ]; then
    git -C "$SRC_HOMEPAGE" worktree remove --force "$TMP_WT" 2>/dev/null \
      || rm -rf "$TMP_WT" 2>/dev/null || true
  fi
  git -C "$SRC_HOMEPAGE" worktree prune 2>/dev/null || true
}
trap cleanup EXIT

# (a) origin/main 기준 격리 임시 worktree 생성 (detached HEAD).
if ! git worktree add --detach "$TMP_WT" origin/main 2>&1; then
  echo "[us-intraday self-deploy] worktree add FAIL — 생략(다음 cycle 재시도)"
  exit 0
fi

# (b) cron worktree 산출물 → 임시 worktree 로 cp (read-only source).
mkdir -p "$TMP_WT/data/us-indices"
cp -f "${SRC_FILES[@]}" "$TMP_WT/data/us-indices/" 2>&1 || true

cd "$TMP_WT" || { echo "[us-intraday self-deploy] tmp worktree cd FAIL"; exit 0; }

# (c) 명시 1-glob add → 변경 있을 때만 commit (SSOT 4종·코드 무접촉).
git add data/us-indices/*.json 2>/dev/null || true
if git diff --cached --quiet; then
  echo "[us-intraday self-deploy] origin/main 대비 us-indices 변경 0건 — push 생략"
  exit 0
fi
git commit -m "chore(us-intraday): $(date +%Y-%m-%dT%H:%M) 선물 as_of 갱신 self-deploy" 2>&1 || {
  echo "[us-intraday self-deploy] commit FAIL — 생략"
  exit 0
}

# (d) fetch → pull --rebase origin main → push 재시도 루프 (최대 3회).
#   격리 worktree 는 detached HEAD(origin/main 기준)이므로 rebase 가 항상 깨끗.
#   다른 세션/cron 이 동시 push 하면 remote main 이동 → reject → 매 시도 rebase 재흡수.
#   force / --no-verify 미사용. 모두 실패 시 다음 cycle 자연 재시도(임시 worktree 는 cleanup).
PUSH_OK=0
for attempt in 1 2 3; do
  git fetch origin main 2>&1 || true
  if ! git rebase origin/main 2>&1; then
    echo "[us-intraday self-deploy] rebase conflict (attempt $attempt) → abort"
    git rebase --abort 2>&1 || true
    break
  fi
  if git push origin HEAD:refs/heads/main 2>&1; then
    PUSH_OK=1
    break
  fi
  echo "[us-intraday self-deploy] push reject (attempt $attempt) — remote 이동, 재시도"
done

if [ "$PUSH_OK" -eq 1 ]; then
  echo "[us-intraday self-deploy] push OK — origin/main us-indices 갱신 완료"
else
  echo "[us-intraday self-deploy] push FAIL 3회 — 다음 cron 재시도(임시 worktree cleanup)"
fi
