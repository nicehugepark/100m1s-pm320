#!/usr/bin/env bash
# 미국 야간 미장요약(US overnight digest) 통합 entry — Q-20260605-103 Phase 3.
#
# 동작: 지수 수집(Yahoo) + 미장 RSS/한경글로벌 뉴스 LLM 요약 → JSON 빌드 →
#       cron worktree homepage 산출물 data/us-indices/{kstDate}.json 생성.
# 시점: 미장 마감 후 아침 1회 (07:00 KST plist, 법무 ③ 아침 1회 빌드 정합).
# push: 본 스크립트는 *생성만* (build_us_digest 가 cron worktree 에 write).
#       배포(git add/push)는 기존 kiwoom_cron.sh push add-set 가 data/ 흡수
#       (FLR-20260605-TEC-001 §2 handoff window — us-indices/ 화이트리스트 추가는
#       lead/DevOps 후속 wiring, 본 스크립트는 직접 push 0건).
#
# 사용:
#   bash scripts/news_pipeline/run_us_digest.sh
#   with_lock.sh 경유 권장 (kiwoom_cron 락 공유):
#     bash scripts/news_pipeline/with_lock.sh /tmp/100m1s-us-digest.lock -- \
#       bash scripts/news_pipeline/run_us_digest.sh

set -uo pipefail

cd "$(dirname "$0")/../.."
export PYTHONPATH="$PWD"
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:$PATH"

# cron worktree 격리 (lead-meta §11.32) — config.py HOMEPAGE 를 cron-isolation 으로.
# pipeline.sh:18 / kiwoom_cron.sh:18 동일 값. 미설정 시 메인 레포에 산출물 생성 →
# cron push 가 빈 us-indices/ 배포. plist EnvironmentVariables 로도 주입되나 직접
# 실행 fallback 대비 명시.
export M1S_HOMEPAGE="${M1S_HOMEPAGE:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)}"

# .env 로드 (NAVER_CLIENT_*, ANTHROPIC 등 LLM/검색 키)
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

LOG_DIR="$PWD/scripts/news_pipeline/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/us-digest-$(date +%Y-%m-%d).log"

echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') us-digest start (M1S_HOMEPAGE=$M1S_HOMEPAGE) ===" >> "$LOG"

python3 -m scripts.news_pipeline.build_us_digest >> "$LOG" 2>&1
rc=$?

echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') us-digest end rc=$rc ===" >> "$LOG"

# ──────────────────────────────────────────────────────────────────────
# self-deploy (FLR-20260615-TEC 봉쇄 — us_digest 배포 누락 root fix).
#
# 근본 사고: us-digest plist(07:00 KST)가 산출물 생성 후 배포 step 없음 →
#   kiwoom_cron push add-set(L610)이 흡수 예정이었으나 kiwoom_cron은 09:00~15:40
#   장중에만 실행 → 생성(07:00)~배포(09:00+) 사이 2~8h 공백 → 라이브 stale.
#   (run_us_digest.sh 원 주석 "배포는 lead/DevOps 후속 wiring" = 미완 상태로 운영됨)
#
# 동형 패턴: run_us_intraday.sh(L51-53) → us_intraday_deploy.sh(격리 임시 worktree).
#   us_intraday_deploy.sh 는 data/us-indices/*.json 전체 동기화 →
#   digest 산출물({kstDate}.json)도 포함됨. 별도 deploy 스크립트 신설 불필요.
#
# 🔴 안전 (us_intraday_deploy.sh 동형):
#   - 격리 임시 worktree(PID+timestamp)에서만 배포 — cron worktree·메인·SSOT 무접촉.
#   - 명시 1-glob(data/us-indices/*.json)만 add. force/--no-verify/reset --hard 미사용.
#   - rc != 0(수집 실패) 시 us_intraday_deploy.sh diff 0 → push 자동 생략.
# ──────────────────────────────────────────────────────────────────────
if [ "$rc" -eq 0 ] && [ -e "$M1S_HOMEPAGE/.git" ]; then
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') us-digest self-deploy 진입 ===" >> "$LOG"
  bash "$(dirname "$0")/us_intraday_deploy.sh" >> "$LOG" 2>&1
  deploy_rc=$?
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') us-digest self-deploy end rc=$deploy_rc ===" >> "$LOG"
fi

exit "$rc"
