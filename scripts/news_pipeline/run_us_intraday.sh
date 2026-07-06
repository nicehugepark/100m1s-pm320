#!/usr/bin/env bash
# 미 지수 선물 + 뉴스 장중 경량 갱신 — Q-20260605-103 Phase 4.
#
# 동작: build_us_digest --intraday (선물 ES/NQ/YM 갱신 + 신규 기사 있을 때만
#       news_chips LLM 재생성). 아침 빌드 산출물(지수 마감·range_240d·daily_expanded·
#       지수별 카드뉴스)은 그대로 보존 — LLM 비용 절제.
# 시점: 국내장 중 10분 주기 (평일 08:50~15:30 KST, plist; Q-20260608-139 15→10분).
#       종목카드 장중 갱신 주기와 정합. 선물은 윈도우 내내 거래 중(CME), 휴장은 윈도우 밖.
# stale: as_of_kst 갱신 → 프론트 "N분 전 기준" 표시. fetch 실패 시 직전 JSON 유지.
# push: 생성만(cron worktree write). 배포는 기존 kiwoom_cron push add-set 흡수.

set -uo pipefail

cd "$(dirname "$0")/../.."
export PYTHONPATH="$PWD"
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:$PATH"
export M1S_HOMEPAGE="${M1S_HOMEPAGE:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)}"

if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

LOG_DIR="$PWD/scripts/news_pipeline/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/us-intraday-$(date +%Y-%m-%d).log"

echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') us-intraday start ===" >> "$LOG"
python3 -m scripts.news_pipeline.build_us_digest --intraday >> "$LOG" 2>&1
rc=$?
echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') us-intraday end rc=$rc ===" >> "$LOG"

# ──────────────────────────────────────────────────────────────────────
# self-deploy (Q-20260608: 선물 as_of 신선도 ≤ 10분 보장 / 격리 worktree 재설계).
# 기존: 산출만(cron worktree write) → 배포는 kiwoom_cron push add-set 흡수 → 라이브
#   반영이 kiwoom cadence·SSOT-guard SKIP 에 종속 → 선물 as_of 최대 20분 stale
#   (대표 2026-06-08: "10분 주기인데 어째서 20분 전 조회라고").
# 1차 변경(공유 락+cron worktree push): 공유 pipeline lock 점유 시 graceful SKIP →
#   push 안 됨 → 라이브 선물 59분+ stale + cron worktree(cron-isolation) divergence
#   반복(FLR-20260608-TEC-002).
# 본 변경: us_intraday_deploy.sh 가 **origin/main 기준 격리 임시 worktree**에서 배포
#   (lead 우회 검증 41b11f25c 패턴 정식화) → 공유 락·cron worktree·divergence 무관.
#   → 공유 lock(with_lock.sh) 래퍼 제거: 격리 worktree 는 kiwoom_cron 과 index 충돌 없음.
#
# 🔴 race-safety:
#   - 배포는 독립 임시 worktree(PID+timestamp suffix, origin/main detached)에서만 수행 →
#     cron worktree·메인 worktree·SSOT 4종 구조적 무접촉.
#   - cron worktree git 상태는 read-only(cp source)로만 접근 — 절대 add/commit 안 함.
#   - 명시 1-glob(data/us-indices/*.json)만 add. force/--no-verify/reset --hard 미사용.
#   - 메인레포 100m1s-homepage(divergent) 미사용. 서빙 = origin/main.
# rc != 0(수집 실패) 시 산출물 미변경 → 격리 worktree 에서 diff 0 → push 생략.
if [ "$rc" -eq 0 ] && [ -e "$M1S_HOMEPAGE/.git" ]; then
  bash "$PWD/scripts/news_pipeline/us_intraday_deploy.sh" >> "$LOG" 2>&1
fi

exit "$rc"
