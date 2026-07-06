#!/usr/bin/env bash
# wire_news.json self-deploy — us_intraday_deploy.sh 격리 worktree 패턴 동형 (FLR-20260608-TEC-002 봉쇄).
#
# 동작:
#   1. cron worktree(읽기 전용 source)의 pm320/data/wire_news.json 을 읽어,
#   2. `git worktree add <tmp> origin/main` 으로 origin/main 기준 격리 임시 worktree 생성,
#   3. cp → 명시 1-file 화이트리스트 add → (변경 있을 때만) commit,
#   4. fetch → rebase origin/main → push (최대 3회 재시도),
#   5. 임시 worktree 항상 cleanup (trap).
#
# 🔴 안전 (lead 사전 명시 승인 범위):
#   - add 는 pm320/data/wire_news.json 한 파일만 — 그 외 add 절대 금지.
#   - force / --no-verify / reset --hard 미사용.
#   - cron worktree·메인 worktree·SSOT 4종 구조적 무접촉 (격리 임시 worktree 독립 index).
set -uo pipefail
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:$PATH"

SRC_HOMEPAGE="${M1S_HOMEPAGE:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)}"
SRC_FILE="$SRC_HOMEPAGE/pm320/data/wire_news.json"
WHITELIST="pm320/data/wire_news.json"

cd "$SRC_HOMEPAGE" || exit 0

echo "--- $(date '+%Y-%m-%d %H:%M:%S %Z') wire self-deploy (isolated worktree) ---"

if [ ! -f "$SRC_FILE" ]; then
  echo "[wire-deploy] source wire_news.json 없음 — 생략"
  exit 0
fi

git fetch origin main 2>&1 || true

TMP_WT="/tmp/m1s-wire-deploy-$$-$(date +%s)"

cleanup() {
  if [ -d "$TMP_WT" ]; then
    git -C "$SRC_HOMEPAGE" worktree remove --force "$TMP_WT" 2>/dev/null \
      || rm -rf "$TMP_WT" 2>/dev/null || true
  fi
  git -C "$SRC_HOMEPAGE" worktree prune 2>/dev/null || true
}
trap cleanup EXIT

if ! git worktree add --detach "$TMP_WT" origin/main 2>&1; then
  echo "[wire-deploy] worktree add FAIL — 생략(다음 cycle 재시도)"
  exit 0
fi

mkdir -p "$TMP_WT/pm320/data"
cp -f "$SRC_FILE" "$TMP_WT/$WHITELIST" 2>&1 || true

cd "$TMP_WT" || { echo "[wire-deploy] tmp worktree cd FAIL"; exit 0; }

git add "$WHITELIST" 2>/dev/null || true
if git diff --cached --quiet; then
  echo "[wire-deploy] origin/main 대비 변경 0건 — push 생략"
  exit 0
fi
git commit -m "chore(wire): $(date +%Y-%m-%dT%H:%M) wire_news 갱신 self-deploy" 2>&1 || {
  echo "[wire-deploy] commit FAIL — 생략"
  exit 0
}

PUSH_OK=0
for attempt in 1 2 3; do
  git fetch origin main 2>&1 || true
  if ! git rebase origin/main 2>&1; then
    echo "[wire-deploy] rebase conflict (attempt $attempt) → abort"
    git rebase --abort 2>&1 || true
    break
  fi
  if git push origin HEAD:refs/heads/main 2>&1; then
    PUSH_OK=1
    break
  fi
  echo "[wire-deploy] push reject (attempt $attempt) — remote 이동, 재시도"
done

if [ "$PUSH_OK" -eq 1 ]; then
  echo "[wire-deploy] push OK — origin/main wire_news 갱신 완료"
else
  echo "[wire-deploy] push FAIL 3회 — 다음 cron 재시도"
fi
