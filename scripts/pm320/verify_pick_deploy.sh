#!/usr/bin/env bash
# PM320 픽 발행일 배포 검증 + 자가 복구 가드 (FLR-20260612-TEC-001 재발 방지 (a)).
#
# 사고 (2026-06-12 15:20 체인, 한 사고 세 단면):
#   ① STEP2 build_card_history --require-sync 가 judge-minutes 게이트 graceful exit(0건 생성)
#      에도 exit 0 → wrapper 가 "STEP2 PASS (카드 생성+배포 완료)" 거짓 표기 → 카톡 발송.
#      (가드 신설 8a3f20d9 2026-06-11 19:33 이후 15:20 fire 는 구조적으로 게이트 통과 불가 —
#       judge snapshot 첫 생성 = minute-backfill routine 15:31 fire)
#   ② 종목 카드 HTML pm320/{date}/{code}.html 이 kiwoom_cron 끝 push 화이트리스트
#      `git add pm320/*.html` (top-level glob) 에 미포함 → 영구 untracked → 카톡 버튼 404.
#   ③ 화면 추천 영역: 데이터 404 + 폴링 종료 시 소멸.
#
# 동작 (single-shot, launchd 가 15:25/15:35/15:45/15:55/16:05 평일 다회 fire = 자연 retry):
#   1. 픽 발행일 판단: projects/pm320/data/daily/picks/{오늘}.json 존재 (부재=휴장 → exit 0)
#   2. 라이브 검증: data/pm320_history/{date}.json + pm320/{date}/{code}.html HTTP 200
#   3. 둘 다 200 → OK exit 0
#   4. 404 발견 → cron WT 산출물 명시 화이트리스트 add → commit → safe rebase push (자가 복구)
#      - §11.27 race-guard: 외부 staged change 존재 시 push SKIP (다음 fire 재시도)
#      - 로컬 산출물 자체 부재 시 add 0건 → 다음 fire 재시도 (15:31/15:41 routine 이 생성)
#   5. 마지막 fire(16:05) 까지 404 지속 → osascript CRITICAL notify
#
# 금지: stash / reset --hard / push --force / --no-verify (lead-meta §6 — 사용·시도 금지)
# 중복 fire 가드: launchd plist 가 with_lock.sh (fcntl, macOS flock 부재 대체 — REQ-002 옵션 B)
#   로 래핑 — /tmp/100m1s-pm320-deploy-verify.lock. 본 스크립트 자체 lock 없음.
# 설치: launchctl bootstrap 은 lead 결정 후 별건 (plist: com.100m1s.pm320-deploy-verify.plist)

set -u

REPO_ROOT="${M1S_COMPANY:-/Users/seongjinpark/company/100m1s}"
HOMEPAGE_DIR="${M1S_HOMEPAGE:-$HOME/company/100m1s-homepage-cron}"
BASE_URL="https://100m1s.com"
FINAL_FIRE_HHMM="16:05"

DATE_KST="$(TZ=Asia/Seoul date +%F)"
NOW_HHMM="$(TZ=Asia/Seoul date +%H:%M)"
CB="$(date +%s)"  # cache-buster (GH Pages CDN 캐시된 404/200 회피)

ts() { TZ=Asia/Seoul date '+%Y-%m-%d %H:%M:%S KST'; }
log() { echo "[pick-deploy-verify] $(ts) $1"; }
notify() {
  # macOS notification (best-effort, pipeline.sh L88 verbatim 패턴)
  osascript -e "display notification \"$1\" with title \"100m1s PM320 CRITICAL\"" 2>/dev/null || true
}

# --- 1. 픽 발행일 판단 ---
PICKS_JSON="${REPO_ROOT}/projects/pm320/data/daily/picks/${DATE_KST}.json"
if [ ! -f "$PICKS_JSON" ]; then
  log "OK: picks 부재 (휴장/미발행, date=${DATE_KST}) — 검증 불요"
  exit 0
fi

PICK_CODE="$(/usr/bin/python3 -c "
import json,sys
try:
    d=json.load(open(sys.argv[1]))
    print((d.get('picked') or {}).get('code') or '')
except Exception:
    print('')
" "$PICKS_JSON")"

# --- 2. 라이브 검증 ---
DATA_URL="${BASE_URL}/data/pm320_history/${DATE_KST}.json?v=${CB}"
DATA_HTTP="$(curl -s -o /dev/null -m 20 -w '%{http_code}' "$DATA_URL" || echo 000)"

CARD_HTTP="200"  # pick code 부재 시 카드 검증 생략 (데이터만)
if [ -n "$PICK_CODE" ]; then
  CARD_URL="${BASE_URL}/pm320/${DATE_KST}/${PICK_CODE}.html?v=${CB}"
  CARD_HTTP="$(curl -s -o /dev/null -m 20 -w '%{http_code}' "$CARD_URL" || echo 000)"
fi

log "검증: data=${DATA_HTTP} card=${CARD_HTTP} (date=${DATE_KST} code=${PICK_CODE:-none})"

if [ "$DATA_HTTP" = "200" ] && [ "$CARD_HTTP" = "200" ]; then
  log "OK: 라이브 정상 (data+card 200)"
  exit 0
fi

# --- 3. 자가 복구: cron WT 산출물 명시 화이트리스트 add → commit → safe push ---
if [ ! -d "${HOMEPAGE_DIR}/.git" ] && [ ! -f "${HOMEPAGE_DIR}/.git" ]; then
  log "FAIL: HOMEPAGE_DIR git 부재 (${HOMEPAGE_DIR}) — 자가 복구 불가"
  notify "pick-deploy-verify: HOMEPAGE_DIR git 부재 — 수동 확인 필요"
  exit 1
fi

cd "$HOMEPAGE_DIR" || exit 1

# §11.27 race-guard: 외부(lead/dev) staged change 존재 시 본 가드 push 금지
PRE_STAGED="$(git diff --cached --name-only | wc -l | tr -d ' ')"
if [ "$PRE_STAGED" != "0" ]; then
  log "SKIP push: 외부 staged change ${PRE_STAGED}건 (§11.27 race-guard) — 다음 fire 재시도"
  exit 0
fi

# 명시 화이트리스트만 add (§11.27 — 무차별 glob 금지). 존재하는 경로만.
[ -f "data/pm320_history/${DATE_KST}.json" ] && git add "data/pm320_history/${DATE_KST}.json" 2>/dev/null
[ -f "data/pm320_history/summary.json" ]    && git add "data/pm320_history/summary.json" 2>/dev/null
[ -d "pm320/${DATE_KST}" ]    && git add "pm320/${DATE_KST}" 2>/dev/null
[ -f "pm320/${DATE_KST}.html" ] && git add "pm320/${DATE_KST}.html" 2>/dev/null
[ -d "og/pm320/${DATE_KST}" ] && git add "og/pm320/${DATE_KST}" 2>/dev/null
[ -f "og/pm320/${DATE_KST}.png" ] && git add "og/pm320/${DATE_KST}.png" 2>/dev/null

STAGED="$(git diff --cached --name-only | wc -l | tr -d ' ')"
if [ "$STAGED" = "0" ]; then
  # 로컬 산출물 자체가 아직 없거나 이미 커밋됨 (커밋됨 + 라이브 404 = Pages 배포 지연)
  if [ "$NOW_HHMM" \> "$FINAL_FIRE_HHMM" ] || [ "$NOW_HHMM" = "$FINAL_FIRE_HHMM" ]; then
    log "CRITICAL: 라이브 404 지속 + add 할 신규 산출물 0건 (마지막 fire) — 수동 개입 필요"
    notify "PM320 ${DATE_KST} 픽 배포 404 지속 (data=${DATA_HTTP} card=${CARD_HTTP}) — 수동 확인"
    exit 2
  fi
  log "WAIT: add 할 신규 산출물 0건 (routine 15:31/15:41 생성 대기 또는 Pages 배포 중) — 다음 fire 재시도"
  exit 0
fi

log "자가 복구: ${STAGED}건 staged → commit+push 진입 (data=${DATA_HTTP} card=${CARD_HTTP})"

if ! git commit -m "chore(pm320,deploy-verify,FLR-20260612-TEC-001): ${DATE_KST} 픽 배포 자가 복구 — 라이브 404 detect (data=${DATA_HTTP} card=${CARD_HTTP})"; then
  log "FAIL: git commit 실패 — 다음 fire 재시도"
  exit 1
fi

# safe push: pull --rebase --autostash → push HEAD:main (force 없음 — sync_to_homepage_main §6 verbatim 패턴)
if ! git pull --rebase --autostash origin main; then
  git rebase --abort 2>/dev/null || true
  log "FAIL: pull --rebase 실패 (rebase abort) — 다음 fire 재시도"
  notify "pick-deploy-verify: rebase 실패 — 다음 fire 재시도 (${DATE_KST})"
  exit 1
fi

if git push origin HEAD:main; then
  log "PASS: 자가 복구 push 완료 (${STAGED}건) — Pages 배포 1~2분 후 다음 fire 가 재검증"
  notify "PM320 ${DATE_KST} 픽 배포 자가 복구 push 완료 (${STAGED}건)"
  exit 0
else
  log "FAIL: git push 실패 — 다음 fire 재시도 (commit 은 로컬 보존, idempotent)"
  notify "pick-deploy-verify: push 실패 — 다음 fire 재시도 (${DATE_KST})"
  exit 1
fi
