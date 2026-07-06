#!/usr/bin/env bash
# 키움 조건검색 10분마다 실행 + build_daily 자동 호출
# launchd plist에서 호출
#
# Phase 1 (REQ-002): launchd plist는 with_lock.sh 경유로 호출 (pipeline.sh와 락 공유).
# 직접 실행 시 동시 실행 방지하려면:
#   bash scripts/news_pipeline/with_lock.sh /tmp/100m1s-pipeline.lock -- bash scripts/kiwoom_cron.sh

set -euo pipefail
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin"

# S5 자립화 (DOC-20260707-REQ-001): 메인 레포 절대경로 → 스크립트 자기 위치 기준 pm320 레포 루트.
#   이 스크립트는 <repo>/scripts/kiwoom_cron.sh → 조상 2단계 = pm320 레포 루트.
#   pm320 레포는 코드+데이터가 같은 레포에 자립하므로 MAIN_REPO=HOMEPAGE=repo 루트가 기본.
#   env(M1S_COMPANY/M1S_HOMEPAGE) 명시 시 오버라이드(옛 cron WT 분리 배선과의 호환).
_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_REPO_ROOT="$(cd "$_SCRIPT_DIR/.." && pwd)"
MAIN_REPO="${M1S_COMPANY:-$_REPO_ROOT}"
# HOMEPAGE = 데이터·서빙 홈. config.py 의 HOMEPAGE = Path(os.environ.get("M1S_HOMEPAGE")) 경유.
#   pm320 자립: 데이터가 코드와 동일 레포 → 기본값 = repo 루트. cron pipeline 의 모든 file I/O 가
#   이 경로 대상 (race condition 봉쇄는 pm320 레포 자체의 물리 격리로 유지).
HOMEPAGE="${M1S_HOMEPAGE:-$_REPO_ROOT}"
export M1S_HOMEPAGE="$HOMEPAGE"
# cycle25 env-unification (2026-05-28): split-brain .env 봉쇄.
# 이전: projects/pm320/poc/.env (4/15 mtime, 5/27 lead .env fix 0건 반영 → opus-4-6 옛값 + concurrency=50 default).
# 현재: 메인 .env 단일 source 일원화. KIWOOM_* 7종 메인 .env 보강 (2026-05-28).
ENV_FILE="$MAIN_REPO/.env"
LOG="/tmp/kiwoom-cron.log"

# ──────────────────────────────────────────────────────────────────────
# 파이프라인 구간(stage)별 소요 시간 측정 (2026-05-28 대표 지시 11:53 KST):
# "파이프라인의 구간마다 시간을 측정할 수 있도록 메트릭을 만들던가 로그를 심던가".
#
# pipeline.sh 의 검증된 _stage 패턴(STAGE name=.. dur=..s rc=..)을 차용 + jsonl 박제.
# 동작: `_stage_timed <name> -- <cmd> [args...]` → cmd 실행, start/end epoch + rc 측정,
#   (a) $LOG 에 사람이 읽는 라인 `[stage-timing] stage=<name> elapsed=<dur>s rc=<rc>`,
#   (b) scripts/news_pipeline/logs/stage-timing.jsonl 에 jsonl 1줄 append.
# 기존 동작 영향 0건: rc 비0 이어도 기존 `|| echo FAIL` 패턴과 동일하게 항상 0 반환
#   (set -e 하에서 stage 실패가 cron 전체를 abort 시키지 않도록). 측정 jsonl write
#   실패도 silent (파이프라인 미영향). cmd 의 stdout/stderr 는 종전대로 $LOG 로 redirect.
STAGE_TIMING_JSONL="$MAIN_REPO/scripts/news_pipeline/logs/stage-timing.jsonl"
STAGE_RUN_ID="cron-$(date +%s)"
STAGE_LAST_RC=0
_stage_timed() {
  local name="$1"; shift
  [ "$1" = "--" ] && shift
  local start_epoch end_epoch dur rc ts
  start_epoch=$(date +%s)
  set +e
  "$@" >> "$LOG" 2>&1
  rc=$?
  set -e
  end_epoch=$(date +%s)
  dur=$((end_epoch - start_epoch))
  ts=$(date -Iseconds)
  echo "[stage-timing] stage=${name} elapsed=${dur}s rc=${rc}" >> "$LOG"
  # jsonl 박제 (실패 silent — 측정이 파이프라인을 깨면 안 됨).
  echo "{\"ts\":\"${ts}\",\"run_id\":\"${STAGE_RUN_ID}\",\"stage\":\"${name}\",\"elapsed_sec\":${dur},\"rc\":${rc}}" >> "$STAGE_TIMING_JSONL" 2>/dev/null || true
  STAGE_LAST_RC="$rc"
  return 0  # 기존 `|| echo FAIL` 패턴 동형 — stage 실패가 cron abort 미유발
}

# Phase 1 (REQ-002): DB 백업 (.backup, 7개 리텐션)
bash "$MAIN_REPO/scripts/news_pipeline/backup_db.sh" >> "$LOG" 2>&1 || true

# 시간 체크: 09:00~15:40 (KST) 장중만 실행
# (2026-05-30 대표 지시: "15:30 종가가 안 보이더라" — 정규 cron 수집 윈도우를 15:40 까지
#  연장 → 15:30 마감 확정 종가가 정규 cycle 에 잡힘. 마감 시각은 여전히 15:30, 수집 윈도우만 연장.)
HOUR=$(date +%H)
MIN=$(date +%M)
DOW=$(date +%u)  # 1=Mon, 7=Sun

# 주말 스킵
if [ "$DOW" -ge 6 ]; then
  echo "$(date -Iseconds) SKIP: weekend" >> "$LOG"
  exit 0
fi

# 장 시간외: SKIP 디폴트 + catch-up trigger 분기 (Q-CYCLE21-008 §11.33 D 후행 자동화)
#
# 장 마감 후 (15:30 이후) backend pipeline source change push 시 cron 자연 cycle 의존 시
# 다음 영업일 09:00까지 stale 데이터 위험 (cycle20 14:01 cascade fix → 16:25 라이브 stale 54분 사례).
#
# 본 분기: 장 시간외 진입 시 (a) 새 backend pipeline source commit 감지 + (b) catch-up marker 부재 시
# → 1회 catch-up trigger (장 시간 흐름 그대로 — kiwoom_scrape 포함, 단 ka10032 당일 데이터 부재 시
#  파이프라인 후속 단계가 기존 stocks.db row 기반으로 build_daily 처리).
# marker로 동일 commit 중복 trigger 방지. 다음 영업일 09:00 자연 cycle 진입 시 marker reset.
#
# 외부 spec 검증: `git log --since=DATE -- PATH` 공식 git 문서 (since/until + pathspec, gitcli(7) 안정 stable)
# 경로 필터 = backend pipeline source만 (docs/cron/frontend commit 제외, false-positive 회피)
CATCHUP_MARKER="/tmp/100m1s-kiwoom-catchup.marker"
CATCHUP_PIPELINE_PATHS=(
  "scripts/news_pipeline/build_daily.py"
  "scripts/news_pipeline/interpret_stocks.py"
  "scripts/news_pipeline/verify_trades.py"
  "scripts/news_pipeline/final_gate.py"
  "scripts/news_pipeline/backfill_dailybars_bulk.py"
  "scripts/news_pipeline/backfill_dailybars.py"
  "scripts/news_pipeline/themes_pipeline.py"
)
CATCHUP_TRIGGERED=0
if [ "$HOUR" -lt 9 ] || [ "$HOUR" -gt 15 ] || ([ "$HOUR" -eq 15 ] && [ "$MIN" -gt 40 ]); then
  # catch-up trigger 검토 (장 마감 후만 — 장 시작 전 09:00 미만은 자연 cycle 대기)
  TRY_CATCHUP=0
  if [ "$HOUR" -gt 15 ] || ([ "$HOUR" -eq 15 ] && [ "$MIN" -gt 40 ]); then
    TRY_CATCHUP=1
  fi

  if [ "$TRY_CATCHUP" -eq 1 ]; then
    # 1) 새 backend pipeline source commit 감지 (오늘 15:40 KST 이후, MAIN_REPO 기준)
    #    (2026-05-30) 정규 수집 윈도우를 15:40 까지 연장 → 15:31~15:40 commit 은 정규 실행이
    #    처리. catch-up 은 정규 윈도우 종료(15:40) 이후 backend commit 만 감지 (경계 정합).
    SINCE_TS="$(date +%Y-%m-%d) 15:40:00"
    NEW_BACKEND_COMMIT=$(git -C "$MAIN_REPO" log -1 --since="$SINCE_TS" --pretty=format:"%H" -- "${CATCHUP_PIPELINE_PATHS[@]}" 2>/dev/null | head -1)

    if [ -n "$NEW_BACKEND_COMMIT" ]; then
      # 2) catch-up marker 확인 (동일 commit 중복 trigger 방지)
      MARKER_COMMIT=""
      if [ -f "$CATCHUP_MARKER" ]; then
        MARKER_COMMIT=$(cat "$CATCHUP_MARKER" 2>/dev/null || echo "")
      fi

      if [ "$MARKER_COMMIT" != "$NEW_BACKEND_COMMIT" ]; then
        # 3) catch-up 1회 trigger
        echo "$(date -Iseconds) CATCH-UP TRIGGER: outside market hours ($HOUR:$MIN) + new backend commit ${NEW_BACKEND_COMMIT:0:7} → 1회 build_daily rerun + push (§11.33 D, Q-CYCLE21-008)" >> "$LOG"
        echo "$NEW_BACKEND_COMMIT" > "$CATCHUP_MARKER"
        CATCHUP_TRIGGERED=1
        # Fix E-1-V3 (2026-05-22): catch-up trigger 본문 collect_dailybars force 재호출.
        # 본 시점 (16:30 cron fire 마지막 + pipeline.sh 30분 cycle 다음 18:30~19:00) 사이에서
        # stale dailybars row 정정 본질 필요. M1S_FORCE_DAILYBARS_RECOLLECT=1 본문 본질:
        #   - already_today skip 비활성화
        #   - market_close_dt threshold 무시 (16:00 이후 본 시점 무조건 force)
        # 후행 작업: pipeline.sh 다음 30분 cycle에 정상 진행 (collect_dailybars + build_daily).
        export M1S_FORCE_DAILYBARS_RECOLLECT=1
        # SSOT-guard 검사는 본 분기 통과 후 진행 (기존 흐름 유지)
      else
        echo "$(date -Iseconds) SKIP: outside market hours ($HOUR:$MIN) + catch-up already done for ${NEW_BACKEND_COMMIT:0:7}" >> "$LOG"
        exit 0
      fi
    else
      echo "$(date -Iseconds) SKIP: outside market hours ($HOUR:$MIN) + no new backend commit since 15:40" >> "$LOG"
      exit 0
    fi
  else
    # 09:00 이전 = 자연 cycle 대기 (catch-up 불요, marker reset)
    rm -f "$CATCHUP_MARKER" 2>/dev/null || true
    echo "$(date -Iseconds) SKIP: outside market hours ($HOUR:$MIN) — pre-market, awaiting natural cycle" >> "$LOG"
    exit 0
  fi
fi

# 장 시작 시 (09:00~09:10) catch-up marker reset — 자연 cycle 진입 정합 (다음 영업일 첫 cycle)
if [ "$HOUR" -eq 9 ] && [ "$MIN" -lt 10 ]; then
  if [ -f "$CATCHUP_MARKER" ]; then
    echo "$(date -Iseconds) CATCH-UP MARKER RESET (natural cycle 09:00 진입)" >> "$LOG"
    rm -f "$CATCHUP_MARKER" 2>/dev/null || true
  fi
fi

# DOC-20260504-PLAN-001 §3.1 SSOT-guard (DOC-20260507-FLR-001 sequel — INDEX 3종 제외)
# 메인 레포 Tier 1 SSOT 가 dirty 면 lead/DevOps 작업 중 — cron SKIP.
# FLR-20260504-FLR-003 (recurring critical, 2회차) 영구 차단.
#
# ⚠️ 제외 (FLR-20260507 sequel, b800ef9 동형): records/INDEX.md, records/failures/INDEX.md, records/YYYY-MM/INDEX.md
# 위 3종은 PostToolUse hook + scripts/index-rebuild.sh 가 자동 갱신 (timestamp 라인 flip).
# 사람 직접 편집이 아니므로 SSOT-guard 대상에서 제외. 미제외 시 cron 영구 SKIP (35h 사고 발생).
SSOT_GUARD_FILES=(
  "records/queue/MASTER-QUEUE.md"
  "MEMORY.md"
  "CLAUDE.md"
)
SSOT_GUARD_DIRS=(
  "rules"
  "agents"
)
cd "$MAIN_REPO"
SKIP_MARKER="/tmp/100m1s-kiwoom-ssot-skip.marker"

# Q-20260519-CYCLE17-004 — SSOT-guard 명시 dedicated log channel
SSOT_GUARD_LOG="$MAIN_REPO/records/optimization/ssot-guard-log/$(date +%Y-%m-%d).log"
mkdir -p "$(dirname "$SSOT_GUARD_LOG")"
_ssot_guard_log() {
  local branch="$1"
  local detail="$2"
  local ts
  ts=$(date -Iseconds)
  echo "${ts} [kiwoom_cron.sh] ${branch}: ${detail}" >> "$SSOT_GUARD_LOG"
}

# lead 가시화 flag — SSOT 디렉토리 밖 안정 경로(이미 .gitignore, cron add 화이트리스트 밖).
# 세션/SessionStart 가 이 깃발을 읽어 "거래대금 cron 정지 중" 을 인지(FLR-20260615-FLR-001).
SKIP_FLAG="$MAIN_REPO/records/optimization/ssot-guard-log/SKIP-ACTIVE.flag"
# 알람 쿨다운 marker — 60분 도달 후 매 5분 스팸 방지(동일 문구 19회 무뎌짐 사고).
ALARM_COOLDOWN_MARKER="/tmp/100m1s-kiwoom-ssot-alarm.cooldown"
ALARM_INTERVAL_SEC=1800  # 60분 도달 후 알람 재발송 최소 간격(30분)

_ssot_skip_check() {
  # 반환: 0 = SKIP 유지(60분 미만, lead/DevOps 단기 작업 보호) / 2 = force-run 진입(60분+ 잠적 판정).
  #
  # 옵션 B (2026-06-16, DOC-20260616 dev cycle — DOC-20260615-FLR-001 §4 P1 채택):
  #   기존(FLR-20260507/615): elapsed>3600 시 알람 + lead flag 만 쓰고 호출부가 exit 0 (SKIP 유지).
  #   문제: 멀티세션 staged 잠적·lead 장기작업으로 dirty 가 60분+ 지속되면 세션이 알람을 못 봐
  #         거래대금 cron 무한 정지(2026-06-15 3h47m SKIP → 15:20 픽 HL만도 오선정).
  #   변경: 60분 도달 = "잠적" 판정 → SKIP 대신 force-run(return 2) 으로 승격. FLR-615 §3 핵심
  #         ("감지·알람만·SKIP 유지 = 무한정지 안티패턴") 해소. 60분 미만은 기존대로 SKIP(return 0).
  #   안전: force-run 의 git add 는 본 cron 의 격리 worktree($HOMEPAGE = homepage-cron, 별도
  #         git tree) 안에서만 발생(L633-662 add-set 전부 data/·og/·news/·pm320/, SSOT 0건)
  #         → 메인 레포 SSOT 를 구조적으로 touch 불가(§11.32 인프라 격리). push 는
  #         --force-with-lease 로 lead/dev hotfix race 보호 유지.
  # 강화 유지: (1) 알람 문구에 dirty label + 경과(분) + staged 세션 목록 + 구체 조치
  #            (2) lead flag write (3) 60분 도달 첫 알람 + 이후 30분 간격만(쿨다운, 스팸 억제).
  local label="$1"
  echo "[FLR-003 SSOT-guard] $label dirty in main repo → cron SKIP 검토 (lead/DevOps active)" >> "$LOG"
  _ssot_guard_log "ssot-skip" "label=$label (dirty in main repo, lead/DevOps active)"
  local NOW
  NOW=$(date +%s)
  if [ -f "$SKIP_MARKER" ]; then
    local FIRST_SKIP ELAPSED ELAPSED_MIN STAGED_SESSIONS
    FIRST_SKIP=$(cat "$SKIP_MARKER" 2>/dev/null || echo "$NOW")
    ELAPSED=$((NOW - FIRST_SKIP))
    ELAPSED_MIN=$((ELAPSED / 60))
    if [ "$ELAPSED" -gt 3600 ]; then
      # dirty 원인 staged 파일 목록 (FLR-615 Action P1 "dirty 원인 macOS 알람").
      STAGED_SESSIONS=$(git diff --cached --name-only 2>/dev/null | head -8 | tr '\n' ',' | sed 's/,$//')
      [ -z "$STAGED_SESSIONS" ] && STAGED_SESSIONS="(staged 없음 — working-tree-only dirty)"
      # lead 가시화 flag 갱신(매 SKIP, 세션이 최신 상태를 읽도록).
      mkdir -p "$(dirname "$SKIP_FLAG")" 2>/dev/null || true
      printf '%s\n' \
        "SSOT-guard 60분+ → FORCE-RUN 승격 — 거래대금 kiwoom cron 재개 (옵션 B, FLR-20260615-FLR-001)" \
        "dirty: $label" \
        "staged 세션: $STAGED_SESSIONS" \
        "since: $(date -r "$FIRST_SKIP" -Iseconds 2>/dev/null) (경과 ${ELAPSED_MIN}분)" \
        "updated: $(date -Iseconds)" \
        "조치: git status 로 위 dirty(${label}) 확인 → commit/stash → SSOT clean 시 정상 SKIP-guard 복귀" \
        "주의: force-run 중 — 거래대금 수집은 재개됐으나 메인 repo dirty 는 여전히 lead 정리 필요" \
        > "$SKIP_FLAG" 2>/dev/null || true
      echo "[FLR-003 SSOT-guard] 🔴 60분+ → FORCE-RUN 승격 (elapsed=${ELAPSED}s/${ELAPSED_MIN}분, since=$(date -r "$FIRST_SKIP" -Iseconds), dirty=${label}, staged=${STAGED_SESSIONS}) — SKIP 대신 거래대금 수집 강행" >> "$LOG"
      _ssot_guard_log "ssot-force-run" "elapsed=${ELAPSED}s, label=$label, staged=${STAGED_SESSIONS} — 60분+ 잠적 판정, force-run 승격"
      # 알람 쿨다운: 마지막 알람 후 ALARM_INTERVAL_SEC 경과 시에만 osascript 재발송.
      local LAST_ALARM ALARM_GAP
      LAST_ALARM=0
      [ -f "$ALARM_COOLDOWN_MARKER" ] && LAST_ALARM=$(cat "$ALARM_COOLDOWN_MARKER" 2>/dev/null || echo 0)
      ALARM_GAP=$((NOW - LAST_ALARM))
      if [ "$ALARM_GAP" -ge "$ALARM_INTERVAL_SEC" ]; then
        osascript -e "display notification \"거래대금 cron 60분+ → FORCE-RUN 재개 (경과 ${ELAPSED_MIN}분, dirty: ${label}). git status→commit/stash 로 dirty 정리 필요\" with title \"100m1s 거래대금 FORCE-RUN\"" 2>/dev/null || true
        echo "$NOW" > "$ALARM_COOLDOWN_MARKER"
        _ssot_guard_log "ssot-force-run-alarm" "osascript 발송(쿨다운 통과, gap=${ALARM_GAP}s), elapsed_min=${ELAPSED_MIN}"
      fi
      return 2  # force-run 진입
    fi
  else
    echo "$NOW" > "$SKIP_MARKER"
  fi
  return 0  # 60분 미만 — SKIP 유지
}
# 옵션 B: SSOT dirty 시 _ssot_skip_check 반환값으로 분기.
#   rc=0 → exit 0 (SKIP 유지) / rc=2 → SSOT_FORCE_RUN=1 설정 후 루프 탈출(가드 통과, 수집 강행).
#   set -e 안전: rc 즉시 캡처(|| true 로 set -e 미발동), [ -eq 2 ] 비교는 if-condition 컨텍스트.
SSOT_FORCE_RUN=0
for f in "${SSOT_GUARD_FILES[@]}"; do
  if ! git diff --quiet HEAD -- "$f" 2>/dev/null; then
    _ssot_skip_check "$f" && SSOT_GUARD_RC=0 || SSOT_GUARD_RC=$?
    if [ "${SSOT_GUARD_RC:-0}" -eq 2 ]; then SSOT_FORCE_RUN=1; break; fi
    exit 0
  fi
done
if [ "$SSOT_FORCE_RUN" -ne 1 ]; then
  for d in "${SSOT_GUARD_DIRS[@]}"; do
    if ! git diff --quiet HEAD -- "$d" 2>/dev/null; then
      _ssot_skip_check "$d/" && SSOT_GUARD_RC=0 || SSOT_GUARD_RC=$?
      if [ "${SSOT_GUARD_RC:-0}" -eq 2 ]; then SSOT_FORCE_RUN=1; break; fi
      exit 0
    fi
  done
fi
# SSOT clean — SKIP marker + lead flag + 알람 쿨다운 일괄 정리(정지 해소 인지).
# 단 force-run(SSOT_FORCE_RUN=1) 진입 시엔 dirty 가 여전히 active → marker/flag 보존 의무.
#   marker 를 지우면 60분 elapsed 클럭이 리셋되어 다음 fire 가 다시 60분 SKIP 부터 시작 →
#   force-run 무력화(매 fire 마다 "정지→재개" 진동). dirty 가 lead 정리로 clean 될 때만 정리.
if [ "$SSOT_FORCE_RUN" -ne 1 ]; then
  if [ -f "$SKIP_MARKER" ] || [ -f "$SKIP_FLAG" ]; then
    _ssot_guard_log "ssot-clean" "SKIP marker/flag cleared (SSOT clean, cron proceed)"
  fi
  rm -f "$SKIP_MARKER" "$SKIP_FLAG" "$ALARM_COOLDOWN_MARKER" 2>/dev/null || true
fi

echo "=== $(date -Iseconds) kiwoom scrape start ===" >> "$LOG"

# .env 로드
if [ -f "$ENV_FILE" ]; then
  set -a; . "$ENV_FILE"; set +a
fi

# 조건검색 실행
cd "$HOMEPAGE"
SCRAPE_START_EPOCH=$(date +%s)
_stage_timed scrape -- /usr/bin/python3 scripts/kiwoom-scraper/main.py
SCRAPE_RC="$STAGE_LAST_RC"

# ──────────────────────────────────────────────────────────────────────
# P0 스냅샷 early deploy (2026-05-27, FLR-20260527-TEC-001 후속 — kiwoom_cron 경로 누락 봉쇄):
# "절대 멈추지 마" / 장중 카드 stale 차단.
#
# 근본 사고 (evidence): 기존 homepage push (L240 부근) 가 cycle 맨 끝, interpret_stocks/
# verify_trades/final_gate(opus) **뒤** 에 위치. opus call_model TIMEOUT 누적 →
# with_lock.sh CHILD_TIMEOUT_SEC=540s 소진 → 프로세스 그룹 SIGTERM → push 미도달.
# 실측 (2026-05-27 /tmp/kiwoom-cron.log): 마지막 "kiwoom scrape end"(=L245, push 후) =
# 2026-05-26 09:24 — 이후 24h+ 매 cycle SIGTERM 으로 push 미도달. main.py scrape 자체는
# 매 cycle ~25~40s 안정 성공 (스냅샷 #6~#9 저장 확인). 즉 스냅샷은 매 10분 로컬 기록되나
# opus 뒤 push 가 한 번도 도달 못 해 라이브 stale → renderer.js _computeMarketHardGuard
# (now-last_snapshot_at > 30분) 가 카드 차단.
# a1f3715 의 early-deploy 는 pipeline.sh(news-pipeline) 에만 추가됐고 본 kiwoom_cron.sh
# (kiwoom-scraper launchd, 스냅샷 생산 경로) 에는 누락 → 동일 ROOT 미봉쇄.
#
# 본 fix: main.py scrape (last_snapshot_at SoT 생산) 직후 + opus 단계 **직전** 에
# data/kiwoom 스냅샷 배포(add + commit + push) 1회. opus 가 SIGTERM 돼도 스냅샷은 매 cycle
# 무조건 배포 → 30분 guard 한참 안쪽 유지. 끝 push(L240) 는 enrich본(interpreted/themes) 유지.
# add 범위·exclusion·self-recovery(stash/rebase/pop)·race-guard(§11.27) 는 끝 push 와 동형.
# 스냅샷 파일 부재/무효 시 early deploy SKIP (scrape 실패 graceful).
# ──────────────────────────────────────────────────────────────────────
SNAP_DATE="$(date +%Y-%m-%d)"
SNAP_EARLY="$HOMEPAGE/data/kiwoom/${SNAP_DATE}.json"
SNAP_INDEX_EARLY="$HOMEPAGE/data/kiwoom/index.json"
SNAP_LATEST_EARLY="$HOMEPAGE/data/kiwoom/latest.json"
SNAP_EARLY_MTIME=0
SNAP_INDEX_MTIME=0
SNAP_LATEST_MTIME=0
if [ -e "$SNAP_EARLY" ]; then
  SNAP_EARLY_MTIME=$(/usr/bin/stat -f %m "$SNAP_EARLY" 2>/dev/null || echo 0)
fi
if [ -e "$SNAP_INDEX_EARLY" ]; then
  SNAP_INDEX_MTIME=$(/usr/bin/stat -f %m "$SNAP_INDEX_EARLY" 2>/dev/null || echo 0)
fi
if [ -e "$SNAP_LATEST_EARLY" ]; then
  SNAP_LATEST_MTIME=$(/usr/bin/stat -f %m "$SNAP_LATEST_EARLY" 2>/dev/null || echo 0)
fi
if [ "$SCRAPE_RC" -eq 0 ] \
  && [ -s "$SNAP_EARLY" ] && [ -s "$SNAP_INDEX_EARLY" ] && [ -s "$SNAP_LATEST_EARLY" ] \
  && [ "$SNAP_EARLY_MTIME" -ge "$SCRAPE_START_EPOCH" ] \
  && [ "$SNAP_INDEX_MTIME" -ge "$SCRAPE_START_EPOCH" ] \
  && [ "$SNAP_LATEST_MTIME" -ge "$SCRAPE_START_EPOCH" ] \
  && /usr/bin/python3 -c "import json,sys; snap=sys.argv[1]; daily=json.load(open(sys.argv[2])); idx=json.load(open(sys.argv[3])); latest=json.load(open(sys.argv[4])); assert daily.get('date') == snap; assert latest.get('date') == snap; dates = idx.get('dates') or []; assert dates and dates[0] == snap" "$SNAP_DATE" "$SNAP_EARLY" "$SNAP_INDEX_EARLY" "$SNAP_LATEST_EARLY" >/dev/null 2>&1; then
  echo "[snap-early-deploy] $(date -Iseconds) 스냅샷 JSON 유효 → opus 단계 전 선배포 진입 ($SNAP_EARLY)" >> "$LOG"
  cd "$HOMEPAGE"
  # (a) 멈춰있는 rebase abort (이전 cron 잔여물 정리) — 끝 push 와 동형
  if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; then
    echo "[snap-early-deploy][FLR-007 self-recovery] stale rebase detected → abort" >> "$LOG"
    git rebase --abort >> "$LOG" 2>&1 || true
  fi
  # (b) stash + pull --rebase — 실패 시 origin/main reset (자기 회복)
  SNAP_STASH_BEFORE=$(git rev-parse -q --verify refs/stash 2>/dev/null || true)
  git stash push --include-untracked -m "snap-early-deploy $(date -Iseconds)" >> "$LOG" 2>&1 || true
  SNAP_STASH_AFTER=$(git rev-parse -q --verify refs/stash 2>/dev/null || true)
  SNAP_HAS_STASH=0
  if [ -n "$SNAP_STASH_AFTER" ] && [ "$SNAP_STASH_AFTER" != "$SNAP_STASH_BEFORE" ]; then
    SNAP_HAS_STASH=1
  fi
  if ! git pull --rebase origin main >> "$LOG" 2>&1; then
    echo "[snap-early-deploy][FLR-007 self-recovery] git pull FAIL → rebase --abort + reset --hard origin/main" >> "$LOG"
    git rebase --abort >> "$LOG" 2>&1 || true
    git fetch origin main >> "$LOG" 2>&1 && git reset --hard origin/main >> "$LOG" 2>&1
  fi
  # (c) stash pop — 이번 블록이 새로 만든 stash 가 있을 때만 pop (old stash 오염 방지)
  if [ "$SNAP_HAS_STASH" -eq 1 ]; then
    if ! git stash pop >> "$LOG" 2>&1; then
      echo "[snap-early-deploy][FLR-007 self-recovery] stash pop conflict → checkout --theirs (incoming/stash 우선)" >> "$LOG"
      git diff --name-only --diff-filter=U | xargs -I{} git checkout --theirs {} 2>>"$LOG" || true
      git diff --name-only --diff-filter=U | xargs -I{} git add {} 2>>"$LOG" || true
      git stash drop >> "$LOG" 2>&1 || true
    fi
  fi
  SNAP_POST_MTIME=$(/usr/bin/stat -f %m "$SNAP_EARLY" 2>/dev/null || echo 0)
  SNAP_INDEX_POST_MTIME=$(/usr/bin/stat -f %m "$SNAP_INDEX_EARLY" 2>/dev/null || echo 0)
  SNAP_LATEST_POST_MTIME=$(/usr/bin/stat -f %m "$SNAP_LATEST_EARLY" 2>/dev/null || echo 0)
  SNAP_POST_VALID=0
  if [ -s "$SNAP_EARLY" ] && [ -s "$SNAP_INDEX_EARLY" ] && [ -s "$SNAP_LATEST_EARLY" ] \
    && [ "$SNAP_POST_MTIME" -ge "$SCRAPE_START_EPOCH" ] \
    && [ "$SNAP_INDEX_POST_MTIME" -ge "$SCRAPE_START_EPOCH" ] \
    && [ "$SNAP_LATEST_POST_MTIME" -ge "$SCRAPE_START_EPOCH" ] \
    && /usr/bin/python3 -c "import json,sys; snap=sys.argv[1]; daily=json.load(open(sys.argv[2])); idx=json.load(open(sys.argv[3])); latest=json.load(open(sys.argv[4])); assert daily.get('date') == snap; assert latest.get('date') == snap; dates = idx.get('dates') or []; assert dates and dates[0] == snap" "$SNAP_DATE" "$SNAP_EARLY" "$SNAP_INDEX_EARLY" "$SNAP_LATEST_EARLY" >/dev/null 2>&1; then
    SNAP_POST_VALID=1
  fi
  if [ "$SNAP_POST_VALID" -ne 1 ]; then
    echo "[snap-early-deploy] SKIP: post-pull/pop JSON validation failed or stale (kiwoom_mtime=${SNAP_POST_MTIME}/${SNAP_INDEX_POST_MTIME}/${SNAP_LATEST_POST_MTIME}) — 끝 push 에서 재시도" >> "$LOG"
  else
    # (d) FLR-20260519-TEC-001 §11.27 race-guard — 외부 staged change 시 early push SKIP (끝 push 동형)
    SNAP_EXTERNAL_STAGED=$(git diff --cached --name-only 2>/dev/null | wc -l | tr -d ' ')
    if [ "$SNAP_EXTERNAL_STAGED" -gt 0 ]; then
      echo "[snap-early-deploy][§11.27 race-guard] SKIP early push: external staged change (count=$SNAP_EXTERNAL_STAGED) — 끝 push 에서 재시도" >> "$LOG"
      git diff --cached --name-only 2>/dev/null | head -20 >> "$LOG"
    else
      # (e) 자기 산출물 화이트리스트 add — 끝 push (L235-238) 와 동일 범위. 스냅샷 당일 + 메타만.
      #     SSOT 4종·코드·stocks.db 절대 제외 (.gitignore + cron-fallback 정책 정합).
      git add "data/kiwoom/${SNAP_DATE}.json" 2>/dev/null || true
      git add data/kiwoom/index.json data/kiwoom/latest.json 2>/dev/null || true
      if git diff --cached --quiet; then
        echo "[snap-early-deploy] no staged change after validation — push skip" >> "$LOG"
      elif git commit -m "chore(kiwoom): $(date +%Y-%m-%dT%H:%M) snapshot early-deploy (opus 전 선배포, FLR-20260527-TEC-001)" >> "$LOG" 2>&1; then
        # cycle25 cron-push-fix (2026-05-28): cron-isolation 브랜치 → origin/main 명시 refspec.
        # ROOT: 기본 `git push origin main` 은 로컬 `main` 브랜치를 찾으나 cron worktree HEAD 는
        # cron-isolation 이라 src 모호 + 동시 fetch 시 non-fast-forward 거부 (cron 로그 매 cycle FAIL).
        # 신규: HEAD (cron-isolation) → refs/heads/main 명시 + --force-with-lease 로 race 안전.
        #       lease=origin/main 마지막 fetch 시점 SHA → 다른 actor 끼어들면 거부 (lead/dev hotfix 보호).
        git push --force-with-lease=main origin HEAD:refs/heads/main >> "$LOG" 2>&1 \
          || echo "[snap-early-deploy] git push FAIL (끝 push 에서 재시도)" >> "$LOG"
      else
        echo "[snap-early-deploy] git commit FAIL (끝 push 에서 재시도)" >> "$LOG"
      fi
    fi
  fi
else
  echo "[snap-early-deploy] $(date -Iseconds) SKIP: scrape rc=${SCRAPE_RC}, 스냅샷 JSON 부재·무효·stale (kiwoom_mtime=${SNAP_EARLY_MTIME}/${SNAP_INDEX_MTIME}/${SNAP_LATEST_MTIME}) — opus 단계 진행, 끝 push 에서 배포" >> "$LOG"
fi

# ──────────────────────────────────────────────────────────────────────
# P0 단일 파이프라인 + LLM tail 통합 (2026-05-27, DOC-20260527-DEC-001 분리→통합
# 번복, 대표 직접 결정 "분리 지금 제거해"):
#
# 배경: 2026-05-27 오전에 opus 해석을 별도 cron (interpret_cron.sh +
# com.100m1s.interpret.plist, 별도 lock) 으로 분리했었다 (dab51cc). 그러나 같은 날
# INTERPRET_CONCURRENCY 를 50 으로 올리면서 (df68b5d) interpret_loop wall-time 이
# budget (~300s) 안에 들어오게 되어 분리가 불필요해졌다 → 대표가 "분리 지금 제거해"
# 결정. opus 를 다시 본 10분 배포 cron 에 tail 로 통합하되, 중복(build_daily/push)
# 도 함께 정리한다.
#
# 통합 후 본 cron 단일 흐름 (LLM 은 early-deploy 뒤 tail):
#   scan(main.py) → catch-up collect_dailybars → build_daily(카드) →
#   스냅샷 early-deploy(L210-249, opus 앞 = 안전망) →
#   ↓↓ 여기부터 ↓↓
#   LLM tail (interpret_loop ishikawa → verify_trades togusa → final_gate 휴지,
#            INTERPRET_CONCURRENCY=50, budget 도달 시 graceful skip) →
#   build_daily #2 (verdict/grade enrich — DB SELECT, LLM 콜 0건) → 끝 push.
#
# 안전 (대표 remove-now 승인이나 리스크 낮춤):
#   - early-deploy(스냅샷) 가 LLM tail **앞** 에 위치 → opus 가 늦거나 SIGTERM
#     돼도 스냅샷은 매 cycle 무조건 배포 (renderer.js stale guard 30분 한참 안쪽).
#     이 순서는 절대 유지.
#   - 단일 lock (/tmp/100m1s-pipeline.lock): LLM(~300s) + 배포(~80s) ≈ 380s
#     < with_lock CHILD_TIMEOUT_SEC=540s budget < 600s interval → lock 이 다음
#     fire 전 해제. cold-start 실측으로 확인 (proxy 금지).
#   - interpret_loop budget 도달 시 미처리 종목 graceful skip → build_daily #2 가
#     DB 에 기록된 verdict 만 enrich (없는 종목은 다음 fire 에서 캐시 HIT 보완).
#
# 통합으로 제거 (중복 0):
#   - interpret_cron.sh + com.100m1s.interpret.plist (오늘 추가한 분리, launchctl
#     unload 는 배포 시 DevOps/lead 수행).
#   - pipeline.sh 의 interpret_loop(opus) + build_daily + 끝 push (중복) — pipeline.sh
#     는 뉴스/공시(haiku) 수집 + og/지표 생성만 남고, build_daily/push 는 본 cron 단일.
#     pipeline.sh 가 생성한 og/news/limit-up-trend 산출물도 본 cron 끝 push add-set 에
#     포함 (SSOT 대칭 — 아래 add 범위 확장 참조).
# ──────────────────────────────────────────────────────────────────────
cd "$MAIN_REPO"
export PYTHONPATH="$MAIN_REPO"

# ──────────────────────────────────────────────────────────────────────
# DB 스키마 검증 게이트 (DOC-20260617-FLR-001 §4 P0):
# 2026-06-16 cron 격리 worktree 전환 시 신규 stocks.db 에 dailybars 등 테이블이
# 누락된 채 초기화 → 6/17 build_daily `no such table: dailybars` 크래시 → 라이브
# 0종목 (6시간 행). 격리(isolation)는 race condition 봉쇄에 성공했으나 DB 스키마
# 동등성 검증을 누락한 설계 공백 (FLR-001 5 Whys 근본 원인).
#
# 본 게이트: collect_dailybars / build_daily **직전** 에 cron DB 필수 테이블 존재를
# sqlite_master 로 검증 → 부재 시 메인 serving DB (schema SSOT) DDL 로 빈 테이블
# 자동 복구 (데이터는 후속 collect stage 가 적재) + 알림 marker. 복구 불가
# (메인 DB 부재 등) 시 exit 2 → build_daily 전 stage SKIP (크래시·6h 행 대신 graceful).
# read-only 검증 + 빈 테이블 CREATE 만. 기존 데이터 행 0 touch.
DB_SCHEMA_OK=1
_stage_timed verify_cron_db_schema -- /usr/bin/python3 -m scripts.news_pipeline.verify_cron_db_schema
if [ "$STAGE_LAST_RC" -ne 0 ]; then
  DB_SCHEMA_OK=0
  echo "$(date -Iseconds) [DB-SCHEMA-GATE] FAIL rc=$STAGE_LAST_RC — cron DB 필수 테이블 복구 불가 → collect_dailybars/build_daily SKIP (DOC-20260617-FLR-001 §4). marker: /tmp/100m1s-cron-db-schema.marker" >> "$LOG"
else
  echo "$(date -Iseconds) [DB-SCHEMA-GATE] PASS — cron DB 스키마 정합 (또는 복구 완료)" >> "$LOG"
fi

# Fix E-1-V3 (2026-05-22): catch-up trigger 본문 collect_dailybars 호출 추가.
# pipeline.sh 30분 cycle 에 자연 실행되나, catch-up trigger = 본 시점 stale dailybars
# row 정정 강제 → collect_dailybars force 호출.
# M1S_FORCE_DAILYBARS_RECOLLECT=1: already_today 무시 + market_close_dt threshold 무시.
if [ "$CATCHUP_TRIGGERED" -eq 1 ] && [ "$DB_SCHEMA_OK" -eq 1 ]; then
  echo "[CATCH-UP] collect_dailybars force 호출 (M1S_FORCE_DAILYBARS_RECOLLECT=1) — stale dailybars row 정정" >> "$LOG"
  # env 로 환경변수 전달 (함수 호출 prefix 는 함수가 실행하는 외부 cmd 까지 전파 보장 X).
  _stage_timed collect_dailybars_catchup -- \
    env M1S_FORCE_DAILYBARS_RECOLLECT=1 /usr/bin/python3 -m scripts.news_pipeline.collect_dailybars
fi

# 상한가 collect_limit_up stage 제거 (2026-06-17, D축 cutover 후속):
# D축 cutover (merge 7e18635f)로 상한가 SoT가 ka10017 별도조회 → v1 조건검색 등락률
# (>= 29.79%)로 통일됨. collect_kiwoom_limit_up.py는 DEPRECATED guard(main() return)로
# no-op이 되었고 com.100m1s.kiwoom-limit-up.plist도 unload됨. build_daily가 v1 목록으로
# 상한가를 자체 산출하므로 본 stage 불요 → 제거. 상세: DSN-001 §2.3.

# build_daily 실행 (DB 적재 + JSON 빌드) — 카드/집계 무조건 산출 (LLM 콜 0건).
# 스냅샷 early-deploy (위 L210-249) 가 이미 스냅샷을 배포했고, 끝 push 가 본 카드를 배포.
BUILD_DAILY_CARDS_START_EPOCH=$(date +%s)
if [ "$DB_SCHEMA_OK" -eq 1 ]; then
  _stage_timed build_daily_cards -- /usr/bin/python3 -m scripts.news_pipeline.build_daily
  BUILD_DAILY_CARDS_RC="$STAGE_LAST_RC"
else
  # DB 스키마 게이트 FAIL — build_daily SKIP (no such table 크래시·6h 행 회피).
  # RC 비0 으로 세워 card early-deploy 가 stale 카드 배포를 안 하도록 (기존 SKIP 분기 정합).
  echo "$(date -Iseconds) [build_daily_cards] SKIP — DB 스키마 게이트 FAIL (DOC-20260617-FLR-001 §4)" >> "$LOG"
  BUILD_DAILY_CARDS_RC=1
fi

# ──────────────────────────────────────────────────────────────────────
# P0 카드 early deploy (2026-06-11, R30 조니 2심 후속):
# raw `data/kiwoom` 스냅샷만 LLM 앞에서 배포하면, Claude/LLM tail 지연·토큰 리밋 시
# 라이브가 최신 raw는 갖고도 `data/interpreted/stock-*.json` 카드는 stale인 상태가 된다.
# 대표 기준의 "10분 파이프라인은 Claude 사용량 제한과 분리"를 충족하려면 build_daily_cards
# 직후 생성된 카드/캘린더 JSON도 LLM tail **앞**에서 한 번 배포해야 한다.
#
# 끝 push는 verdict/grade enrich와 og/news 배포를 계속 담당한다. 본 블록은 매매 판단에 필요한
# freshness path만 선배포한다.
CARD_EARLY="$HOMEPAGE/data/interpreted/stock-${SNAP_DATE}.json"
CAL_EARLY="$HOMEPAGE/data/calendar/index.json"
KIWOOM_DAILY_EARLY="$HOMEPAGE/data/kiwoom/${SNAP_DATE}.json"
KIWOOM_INDEX_EARLY="$HOMEPAGE/data/kiwoom/index.json"
KIWOOM_LATEST_EARLY="$HOMEPAGE/data/kiwoom/latest.json"
CARD_EARLY_MTIME=0
CAL_EARLY_MTIME=0
KIWOOM_DAILY_MTIME=0
KIWOOM_INDEX_MTIME=0
KIWOOM_LATEST_MTIME=0
if [ -e "$CARD_EARLY" ]; then
  CARD_EARLY_MTIME=$(/usr/bin/stat -f %m "$CARD_EARLY" 2>/dev/null || echo 0)
fi
if [ -e "$CAL_EARLY" ]; then
  CAL_EARLY_MTIME=$(/usr/bin/stat -f %m "$CAL_EARLY" 2>/dev/null || echo 0)
fi
if [ -e "$KIWOOM_DAILY_EARLY" ]; then
  KIWOOM_DAILY_MTIME=$(/usr/bin/stat -f %m "$KIWOOM_DAILY_EARLY" 2>/dev/null || echo 0)
fi
if [ -e "$KIWOOM_INDEX_EARLY" ]; then
  KIWOOM_INDEX_MTIME=$(/usr/bin/stat -f %m "$KIWOOM_INDEX_EARLY" 2>/dev/null || echo 0)
fi
if [ -e "$KIWOOM_LATEST_EARLY" ]; then
  KIWOOM_LATEST_MTIME=$(/usr/bin/stat -f %m "$KIWOOM_LATEST_EARLY" 2>/dev/null || echo 0)
fi
if [ "$SCRAPE_RC" -eq 0 ] && [ "$BUILD_DAILY_CARDS_RC" -eq 0 ] \
  && [ -s "$CARD_EARLY" ] && [ -s "$CAL_EARLY" ] \
  && [ -s "$KIWOOM_DAILY_EARLY" ] && [ -s "$KIWOOM_INDEX_EARLY" ] && [ -s "$KIWOOM_LATEST_EARLY" ] \
  && [ "$CARD_EARLY_MTIME" -ge "$BUILD_DAILY_CARDS_START_EPOCH" ] \
  && [ "$CAL_EARLY_MTIME" -ge "$BUILD_DAILY_CARDS_START_EPOCH" ] \
  && [ "$KIWOOM_DAILY_MTIME" -ge "$SCRAPE_START_EPOCH" ] \
  && [ "$KIWOOM_INDEX_MTIME" -ge "$SCRAPE_START_EPOCH" ] \
  && [ "$KIWOOM_LATEST_MTIME" -ge "$SCRAPE_START_EPOCH" ] \
  && /usr/bin/python3 -c "import json,sys; snap=sys.argv[1]; card=json.load(open(sys.argv[2])); cal=json.load(open(sys.argv[3])); daily=json.load(open(sys.argv[4])); idx=json.load(open(sys.argv[5])); latest=json.load(open(sys.argv[6])); assert daily.get('date') == snap; assert latest.get('date') == snap; dates = idx.get('dates') or []; assert dates and dates[0] == snap" "$SNAP_DATE" "$CARD_EARLY" "$CAL_EARLY" "$KIWOOM_DAILY_EARLY" "$KIWOOM_INDEX_EARLY" "$KIWOOM_LATEST_EARLY" >/dev/null 2>&1; then
  echo "[card-early-deploy] $(date -Iseconds) build_daily 카드 JSON 유효 → LLM tail 전 선배포 진입 ($CARD_EARLY)" >> "$LOG"
  cd "$HOMEPAGE"
  if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; then
    echo "[card-early-deploy][FLR-007 self-recovery] stale rebase detected → abort" >> "$LOG"
    git rebase --abort >> "$LOG" 2>&1 || true
  fi
  CARD_PRE_STAGED=$(git diff --cached --name-only 2>/dev/null | wc -l | tr -d ' ')
  if [ "$CARD_PRE_STAGED" -gt 0 ]; then
    echo "[card-early-deploy][§11.27 race-guard] SKIP early push: pre-existing staged change (count=$CARD_PRE_STAGED) — 끝 push 에서 재시도" >> "$LOG"
    git diff --cached --name-only 2>/dev/null | head -20 >> "$LOG"
  else
    CARD_STASH_BEFORE=$(git rev-parse -q --verify refs/stash 2>/dev/null || true)
    git stash push --include-untracked -m "card-early-deploy $(date -Iseconds)" >> "$LOG" 2>&1 || true
    CARD_STASH_AFTER=$(git rev-parse -q --verify refs/stash 2>/dev/null || true)
    CARD_HAS_STASH=0
    if [ -n "$CARD_STASH_AFTER" ] && [ "$CARD_STASH_AFTER" != "$CARD_STASH_BEFORE" ]; then
      CARD_HAS_STASH=1
    fi
    if ! git pull --rebase origin main >> "$LOG" 2>&1; then
      echo "[card-early-deploy][FLR-007 self-recovery] git pull FAIL → rebase --abort + reset --hard origin/main" >> "$LOG"
      git rebase --abort >> "$LOG" 2>&1 || true
      git fetch origin main >> "$LOG" 2>&1 && git reset --hard origin/main >> "$LOG" 2>&1
    fi
    if [ "$CARD_HAS_STASH" -eq 1 ]; then
      if ! git stash pop >> "$LOG" 2>&1; then
        echo "[card-early-deploy][FLR-007 self-recovery] stash pop conflict → checkout --theirs (incoming/stash 우선)" >> "$LOG"
        git diff --name-only --diff-filter=U | xargs -I{} git checkout --theirs {} 2>>"$LOG" || true
        git diff --name-only --diff-filter=U | xargs -I{} git add {} 2>>"$LOG" || true
        git stash drop >> "$LOG" 2>&1 || true
      fi
    fi
    CARD_EARLY_POST_MTIME=$(/usr/bin/stat -f %m "$CARD_EARLY" 2>/dev/null || echo 0)
    CAL_EARLY_POST_MTIME=$(/usr/bin/stat -f %m "$CAL_EARLY" 2>/dev/null || echo 0)
    KIWOOM_DAILY_POST_MTIME=$(/usr/bin/stat -f %m "$KIWOOM_DAILY_EARLY" 2>/dev/null || echo 0)
    KIWOOM_INDEX_POST_MTIME=$(/usr/bin/stat -f %m "$KIWOOM_INDEX_EARLY" 2>/dev/null || echo 0)
    KIWOOM_LATEST_POST_MTIME=$(/usr/bin/stat -f %m "$KIWOOM_LATEST_EARLY" 2>/dev/null || echo 0)
    CARD_POST_VALID=0
    if [ -s "$CARD_EARLY" ] && [ -s "$CAL_EARLY" ] \
      && [ -s "$KIWOOM_DAILY_EARLY" ] && [ -s "$KIWOOM_INDEX_EARLY" ] && [ -s "$KIWOOM_LATEST_EARLY" ] \
      && [ "$CARD_EARLY_POST_MTIME" -ge "$BUILD_DAILY_CARDS_START_EPOCH" ] \
      && [ "$CAL_EARLY_POST_MTIME" -ge "$BUILD_DAILY_CARDS_START_EPOCH" ] \
      && [ "$KIWOOM_DAILY_POST_MTIME" -ge "$SCRAPE_START_EPOCH" ] \
      && [ "$KIWOOM_INDEX_POST_MTIME" -ge "$SCRAPE_START_EPOCH" ] \
      && [ "$KIWOOM_LATEST_POST_MTIME" -ge "$SCRAPE_START_EPOCH" ] \
      && /usr/bin/python3 -c "import json,sys; snap=sys.argv[1]; card=json.load(open(sys.argv[2])); cal=json.load(open(sys.argv[3])); daily=json.load(open(sys.argv[4])); idx=json.load(open(sys.argv[5])); latest=json.load(open(sys.argv[6])); assert daily.get('date') == snap; assert latest.get('date') == snap; dates = idx.get('dates') or []; assert dates and dates[0] == snap" "$SNAP_DATE" "$CARD_EARLY" "$CAL_EARLY" "$KIWOOM_DAILY_EARLY" "$KIWOOM_INDEX_EARLY" "$KIWOOM_LATEST_EARLY" >/dev/null 2>&1; then
      CARD_POST_VALID=1
    fi
    if [ "$CARD_POST_VALID" -ne 1 ]; then
      echo "[card-early-deploy] SKIP: post-pull/pop JSON validation failed or stale (card_mtime=${CARD_EARLY_POST_MTIME}, cal_mtime=${CAL_EARLY_POST_MTIME}, kiwoom_mtime=${KIWOOM_DAILY_POST_MTIME}/${KIWOOM_INDEX_POST_MTIME}/${KIWOOM_LATEST_POST_MTIME}) — 끝 push 에서 재시도" >> "$LOG"
    else
      CARD_EXTERNAL_STAGED=$(git diff --cached --name-only 2>/dev/null | wc -l | tr -d ' ')
      if [ "$CARD_EXTERNAL_STAGED" -gt 0 ]; then
        echo "[card-early-deploy][§11.27 race-guard] SKIP early push: external staged change (count=$CARD_EXTERNAL_STAGED) — 끝 push 에서 재시도" >> "$LOG"
        git diff --cached --name-only 2>/dev/null | head -20 >> "$LOG"
      else
        if git add "data/interpreted/stock-${SNAP_DATE}.json" data/calendar/index.json \
          "data/kiwoom/${SNAP_DATE}.json" data/kiwoom/index.json data/kiwoom/latest.json \
          data/themes/theme-tree.json >> "$LOG" 2>&1; then
          if git diff --cached --quiet; then
            echo "[card-early-deploy] no staged change after validation — push skip" >> "$LOG"
          elif git commit -m "chore(kiwoom): $(date +%Y-%m-%dT%H:%M) card early-deploy (LLM tail 전 선배포)" >> "$LOG" 2>&1; then
            git push --force-with-lease=main origin HEAD:refs/heads/main >> "$LOG" 2>&1 \
              || echo "[card-early-deploy] git push FAIL (끝 push 에서 재시도)" >> "$LOG"
          else
            echo "[card-early-deploy] git commit FAIL (끝 push 에서 재시도)" >> "$LOG"
          fi
        else
          echo "[card-early-deploy] git add FAIL: validated card/kiwoom file set not staged — 끝 push 에서 재시도" >> "$LOG"
        fi
      fi
    fi
  fi
else
  echo "[card-early-deploy] $(date -Iseconds) SKIP: scrape rc=${SCRAPE_RC}, build_daily rc=${BUILD_DAILY_CARDS_RC}, card_mtime=${CARD_EARLY_MTIME}, cal_mtime=${CAL_EARLY_MTIME}, kiwoom_mtime=${KIWOOM_DAILY_MTIME}/${KIWOOM_INDEX_MTIME}/${KIWOOM_LATEST_MTIME}, 카드/캘린더/키움 JSON 부재·무효·stale — 끝 push 에서 배포" >> "$LOG"
fi

# ──────────────────────────────────────────────────────────────────────
# LLM tail (interpret_loop ishikawa(opus) → verify_trades togusa → final_gate 휴지).
# early-deploy(스냅샷) + build_daily(카드) **뒤**, 끝 push **앞**. opus interpret_loop 이
# 늦거나 SIGTERM 돼도 카드/스냅샷은 이미 배포된 상태 (never-stop = 배포가 critical path 앞).
#
# LLM 호출 stage = interpret_loop **단 하나** (ishikawa opus). verify_trades 는 룰 기반
# (LLM 콜 0건 — togusa_verdicts INSERT OR REPLACE), final_gate 도 LLM 콜 0건
# (hugepark_gate grade 규칙 부여). 따라서 tail wall-time 의 변수는 interpret_loop budget.
#
# budget hard-cap 정합 (단일 lock 안전 — 대표 remove-now 승인이나 리스크 낮춤):
#   with_lock CHILD_TIMEOUT_SEC=540s 안에 [scan~40s + (catch-up collect~24s) +
#   build_daily~56s + early push~10s + LLM tail + build_daily enrich~56s + 끝 push~10s]
#   가 들어와야 함. interpret_loop budget=300s 로 명시 cap → 비-LLM 합 ~196~220s +
#   300s ≈ 496~520s < 540s. budget default 420s 면 ~616~640s 로 초과 → **명시 export
#   의무** (kiwoom-scraper.plist 가 INTERPRET_BUDGET_SEC 미설정 → 미명시 시 420 default).
#   budget 도달 시 미처리 종목 graceful skip → 다음 fire 캐시 HIT + resume (DEC-001 정책).
#   INTERPRET_CONCURRENCY=50 (df68b5d, 대표 승인) — claude CLI subprocess 라 GIL 무관.
#
# interpret_loop 직접 호출 이유: build_daily 동일 union fanout(load_fanout_codes) +
# budget + 동시성 이미 검증 (FLR-20260406-TEC-001 한쪽-fix 회피, interpret 경로 단일화).
# ──────────────────────────────────────────────────────────────────────
export INTERPRET_BUDGET_SEC="${INTERPRET_BUDGET_SEC:-300}"
export INTERPRET_CONCURRENCY="${INTERPRET_CONCURRENCY:-50}"
# DB 스키마 게이트 FAIL 시 LLM tail + enrich 전체 SKIP (DOC-20260617-FLR-001 §4).
# interpret_loop/verify_trades/final_gate/build_daily 모두 stocks.db SELECT 의존 →
# 테이블 부재 시 동일 크래시. 끝 push 는 진행 (직전 정상 cycle 산출물 유지·배포).
if [ "$DB_SCHEMA_OK" -eq 1 ]; then
  echo "[llm-tail] $(date -Iseconds) interpret_loop → verify_trades → final_gate 진입 (concurrency=${INTERPRET_CONCURRENCY}, budget=${INTERPRET_BUDGET_SEC}s)" >> "$LOG"
  _stage_timed interpret_loop -- /usr/bin/python3 -m scripts.news_pipeline.interpret_loop
  _stage_timed verify_trades -- /usr/bin/python3 -m scripts.news_pipeline.verify_trades
  _stage_timed final_gate -- /usr/bin/python3 -m scripts.news_pipeline.final_gate

  # build_daily #2 — LLM verdict/grade enrichment 반영 (DB SELECT, LLM 콜 0건).
  # interpret_loop 가 SIGTERM 으로 죽어도 카드/스냅샷은 이미 early-deploy/끝 push 로
  # 보장 → 09:10 카드 표시 요구 충족. 정상 cycle 은 본 stage 까지 완료되어 verdict 최신화.
  _stage_timed build_daily_enrich -- /usr/bin/python3 -m scripts.news_pipeline.build_daily
else
  echo "[llm-tail] $(date -Iseconds) SKIP — DB 스키마 게이트 FAIL (interpret/verify/final_gate/build_daily_enrich 전체, DOC-20260617-FLR-001 §4)" >> "$LOG"
fi

# homepage 자동 푸시 (데이터만)
# FLR-006: stash + pull --rebase 패턴으로 news-pipeline cron과의 push race 해소
cd "$HOMEPAGE"
# (a) 멈춰있는 rebase 있으면 abort
if [ -d .git/rebase-merge ] || [ -d .git/rebase-apply ]; then
  echo "[FLR-007 self-recovery] stale rebase detected → abort" >> "$LOG"
  git rebase --abort >> "$LOG" 2>&1 || true
fi
# (b) stash (kiwoom-scraper가 만든 data/kiwoom/* 변경 보호)
FINAL_STASH_BEFORE=$(git rev-parse -q --verify refs/stash 2>/dev/null || true)
git stash push --include-untracked -m "final-push $(date -Iseconds)" >> "$LOG" 2>&1 || true
FINAL_STASH_AFTER=$(git rev-parse -q --verify refs/stash 2>/dev/null || true)
FINAL_HAS_STASH=0
if [ -n "$FINAL_STASH_AFTER" ] && [ "$FINAL_STASH_AFTER" != "$FINAL_STASH_BEFORE" ]; then
  FINAL_HAS_STASH=1
fi
# (c) pull --rebase — 실패 시 origin/main으로 reset (자기 회복)
if ! git pull --rebase origin main >> "$LOG" 2>&1; then
  echo "[FLR-007 self-recovery] git pull FAIL → rebase --abort + reset --hard origin/main" >> "$LOG"
  git rebase --abort >> "$LOG" 2>&1 || true
  git fetch origin main >> "$LOG" 2>&1 && git reset --hard origin/main >> "$LOG" 2>&1
fi
# (d) stash pop — 이번 블록이 새로 만든 stash 가 있을 때만 pop (old stash 오염 방지)
if [ "$FINAL_HAS_STASH" -eq 1 ]; then
  if ! git stash pop >> "$LOG" 2>&1; then
    echo "[FLR-007 self-recovery] stash pop conflict → checkout --theirs (incoming/stash 우선)" >> "$LOG"
    git diff --name-only --diff-filter=U | xargs -I{} git checkout --theirs {} 2>>"$LOG" || true
    git diff --name-only --diff-filter=U | xargs -I{} git add {} 2>>"$LOG" || true
    git stash drop >> "$LOG" 2>&1 || true
  fi
fi
git add data/interpreted/stock-*.json data/calendar/index.json data/themes/*.json data/kiwoom/*.json 2>/dev/null || true
# cycle22 Q-CYCLE22-001 Phase 3: per-stock dailybars 240영업일 JSON
# (DSN-arch-pipeline §7.7 + REQ-001 §3 Phase 3). SSOT 비대칭 회피 의무 (pipeline.sh 동형 add).
git add data/dailybars/*.json 2>/dev/null || true
# Q-20260605-103 Phase 3: 야간 미국증시 요약 us-indices JSON (run_us_digest.sh 산출).
# us-digest plist(07:00 KST)가 cron worktree data/us-indices/{kstDate}.json 생성 →
# 본 cron 다음 fire 가 배포 (자기 산출물 명시 화이트리스트만, §11.27 무차별 add 금지).
git add data/us-indices/*.json 2>/dev/null || true
# 단일 파이프라인 통합 (2026-05-27, DOC-20260527-DEC-001 분리→통합 번복): pipeline.sh
# 가 build_daily/push 를 더 이상 안 하므로, pipeline.sh 가 생성하던 og/news/지표/
# limit-up-trend 산출물도 본 단일 push 가 배포 (SSOT 대칭, pipeline.sh 끝 push add-set
# 흡수). pipeline.sh 는 뉴스/공시(haiku) 수집 + og/지표 생성만 → 본 cron 다음 fire 가 배포.
git add og/ news/ data/limit-up-trend.json 2>/dev/null || true
# Q-20260606-119 / FLR-20260610: PM320 날짜 공유·OG 리다이렉트 페이지 pm320/{date}.html.
# generate_og.py:324 가 write 만 하고 add/push 0 → og/ news/ 화이트리스트에 pm320/ 부재
# 시 그날 마지막 cron 이후 생성분(예 23:50) 매일 untracked → 라이브 404 (6/9·6/10 재발).
# og/pm320/{date}.png 의 동반 HTML 이므로 og/ 와 동일 add-set (SSOT 대칭, §11.27 명시 화이트리스트).
git add pm320/*.html 2>/dev/null || true
# FLR-20260612-TEC-001 ② — 날짜 서브디렉토리 카드 HTML(pm320/{date}/{code}.html)이 위
# 글롭에 영구 미포함 → 신규 생성/재생성분 미배포 = 카톡 카드 404의 ROOT. 서브디렉토리
# HTML 만 명시 add (bare pm320/*/ 는 pm320/data/ 의 untracked .bak 잔재까지 publish — §11.27 금지).
git add pm320/*/*.html 2>/dev/null || true
# 🔴 P0-2 (FLR-20260605-TEC-001) — 공유 링크 존재 보장 manifest 동기 갱신.
#   news/stock/{date}/{code}.html 실파일을 스캔해 data/page-manifest.json 생성 → manifest 가
#   "지금 배포되는 그 페이지 집합" 과 동기 (배포-manifest skew 봉쇄). 종목페이지 add 직후 실행.
#   renderer.js 공유 URL 생성부가 이 manifest 로 대상 페이지 존재 검증 → 404 URL 봉쇄.
#   실패해도 cron abort 금지(set -e 하 || true) — manifest 부재/stale 시 renderer 는 보수적 폴백.
if command -v node >/dev/null 2>&1; then
  node "$HOMEPAGE/scripts/build-page-manifest.js" >> "$LOG" 2>&1 || echo "[page-manifest] build FAIL (non-fatal)" >> "$LOG"
  git add data/page-manifest.json 2>/dev/null || true
fi
if git diff --cached --quiet; then
  echo "[final-push] no staged change — push skip" >> "$LOG"
elif git commit -m "chore(kiwoom): $(date +%Y-%m-%dT%H:%M) condition search update" >> "$LOG" 2>&1; then
  # cycle25 cron-push-fix (2026-05-28): cron-isolation 브랜치 → origin/main 명시 refspec (early-deploy 동형).
  # pull --rebase (L354) 직후 push 직전 race window 봉쇄 + --force-with-lease 로 lead/dev hotfix 보호.
  git push --force-with-lease=main origin HEAD:refs/heads/main >> "$LOG" 2>&1 || echo "git push FAIL" >> "$LOG"
else
  echo "[final-push] git commit FAIL (post-deploy verify still scheduled)" >> "$LOG"
fi

# 배포 후 자동 검증 (백그라운드 — cron 블로킹 방지)
nohup bash "$MAIN_REPO/scripts/qa/post-deploy-verify.sh" >> "$LOG" 2>&1 &

echo "=== $(date -Iseconds) kiwoom scrape end ===" >> "$LOG"
