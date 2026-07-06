#!/usr/bin/env bash
# PM320 매일 15:20 KST 단일 launchd fire — 4단계 cascade.
#
# 대표 verbatim (2026-06-05 15:24~15:25 KST):
#   "카톡 cron 순서가 거꾸로 — 카톡이 카드 생성+배포보다 먼저라 미완성 카드를 본다.
#    순서: 픽 → 카드+매매버튼 생성 → homepage 배포(push) 완료 → 그 다음 카톡."
#   "카톡을 보내는 크론 로직에서 다 같이 처리하면 되잖아."
#
# 통합 직전 (별도 cron 2개 + race window):
#   - 15:20 wrapper: select_daily_pick → send_kakao_message (카톡 먼저 fire)
#   - 15:25 별도 cron: build_card_history.py (당일 카드 생성 + homepage push)
#   결과: 카톡(15:20) > 카드 생성+배포(15:25). 사용자가 미완성/미배포 카드 열람.
#
# 통합 직후 (본 wrapper 단일 cascade 4단계):
#   (1) select_daily_pick.py            exit 0 confirm → 픽 적재
#   (2) build_card_history.py --require-sync
#                                       exit 0 confirm → 당일 카드+매매버튼 생성
#                                                        + homepage main repo push(배포) 완료
#       (build_card_history 내부 sync_to_homepage_main 이 cp+commit+push 수행.
#        --require-sync 시 push 실패 = exit 2 → 카톡 보류 trigger.)
#   (3) send_kakao_message.py           ← (2) 배포 완료 후에만 fire (카톡 마지막)
#   결정적 cascade 순서 보장: 사용자가 푸시 받는 시점엔 라이브 카드 이미 반영.
#
# 별도 15:25 history-build cron(당일 trigger)은 본 wrapper 흡수로 제거 대상
#   (launchctl bootout + plist 삭제 = DevOps 적용. 본 wrapper commit 후 별건).
#   과거 카드 일괄 재생성(backfill)은 build_card_history.py 수동/별 trigger로 유지.
#
# with_lock.sh 래핑: 15:15 fire와 동일한 lock SoT 유지
#   (/tmp/100m1s-pm320-daily-pick.lock — 직전 daily-pick.plist 가 사용한 path verbatim).
#
# 실패 분기 (각 단계 exit 0 confirm 후 다음 — race 봉쇄):
#   - select_daily_pick 실패 → osascript notification + 이후 단계 skip
#       (picks 미적재 상태 카드 생성·카톡 회피).
#   - build_card_history 실패 (생성 실패 또는 배포 실패) → osascript notification
#       + send_kakao_message skip (미완성/미배포 카드 푸시 금지, 대표 결정 2026-06-05).
#   - send_kakao_message 실패는 자체 graceful exit (.env 키 / API 에러 / picks 미존재).
#
# usage (수동 dry-run):
#   bash scripts/pm320/run_daily_pick_and_push.sh
#
# launchd plist ProgramArguments:
#   /bin/bash
#   /Users/seongjinpark/company/100m1s/scripts/pm320/run_daily_pick_and_push.sh

set -u

REPO_ROOT="${M1S_COMPANY:-/Users/seongjinpark/company/100m1s}"
LOCK_PATH="/tmp/100m1s-pm320-daily-pick.lock"
WITH_LOCK="${REPO_ROOT}/scripts/news_pipeline/with_lock.sh"

notify() {
  # macOS notification (best-effort, pipeline.sh L88 verbatim 패턴)
  osascript -e "display notification \"$1\" with title \"100m1s PM320 CRITICAL\"" 2>/dev/null || true
}

echo "[$(date '+%Y-%m-%d %H:%M:%S %z')] BEGIN run_daily_pick_and_push.sh"

# Step 1: select_daily_pick (lock 보호)
echo "[$(date '+%Y-%m-%d %H:%M:%S %z')] STEP1 select_daily_pick"
bash "${WITH_LOCK}" "${LOCK_PATH}" -- \
  /usr/bin/env python3 -m scripts.pm320.select_daily_pick
SELECT_EXIT=$?

if [ "${SELECT_EXIT}" -ne 0 ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S %z')] FAIL select_daily_pick exit=${SELECT_EXIT} — 이후 단계 skip"
  notify "select_daily_pick exit=${SELECT_EXIT} — picks 미적재, 카드 생성·카톡 skip"
  exit "${SELECT_EXIT}"
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S %z')] STEP1 PASS"

# Step 2: build_card_history (당일 카드+매매버튼 생성 + homepage 배포)
#   --require-sync: 배포(push) 실패 시 exit 2 → 카톡 보류 (미배포 카드 푸시 금지).
echo "[$(date '+%Y-%m-%d %H:%M:%S %z')] STEP2 build_card_history --require-sync"
/usr/bin/env python3 "${REPO_ROOT}/scripts/pm320/build_card_history.py" --require-sync
CARD_EXIT=$?

if [ "${CARD_EXIT}" -ne 0 ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S %z')] FAIL build_card_history exit=${CARD_EXIT} — send_kakao_message skip (미완성/미배포 카드 푸시 금지)"
  notify "build_card_history exit=${CARD_EXIT} — 카드 생성/배포 미완료, 카톡 보류"
  exit "${CARD_EXIT}"
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S %z')] STEP2 PASS (카드 생성 + homepage 배포 완료)"

# Step 3: send_kakao_message (배포 완료 후 카톡 — cascade 마지막)
echo "[$(date '+%Y-%m-%d %H:%M:%S %z')] STEP3 send_kakao_message"
/usr/bin/env python3 "${REPO_ROOT}/scripts/pm320/send_kakao_message.py"
PUSH_EXIT=$?

if [ "${PUSH_EXIT}" -ne 0 ]; then
  echo "[$(date '+%Y-%m-%d %H:%M:%S %z')] WARN send_kakao_message exit=${PUSH_EXIT} (graceful exit)"
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S %z')] END run_daily_pick_and_push.sh"
exit "${PUSH_EXIT}"
