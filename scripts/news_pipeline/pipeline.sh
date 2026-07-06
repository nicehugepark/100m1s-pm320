#!/usr/bin/env bash
# /news Phase 1 파이프라인 오케스트레이션
# 사용법: bash pipeline.sh [YYYY-MM-DD]  (기본: 오늘)
# launchd 30분 주기 호출 대상
#
# Phase 1 (REQ-002): launchd plist는 with_lock.sh 경유로 호출 (kiwoom_cron.sh와 락 공유).
# 직접 실행 시 동시 실행 방지하려면:
#   bash scripts/news_pipeline/with_lock.sh /tmp/100m1s-pipeline.lock -- bash scripts/news_pipeline/pipeline.sh

cd "$(dirname "$0")/../.."
export PYTHONPATH="$PWD"
export PATH="/usr/local/bin:/opt/homebrew/bin:$PATH"
# cron worktree 격리 (lead-meta §11.32) — generate_og/generate_stock_og 가 config.py HOMEPAGE
# 를 cron-isolation 으로 향하게 강제. kiwoom_cron.sh:18 과 동일 값. 미설정 시 config 기본값
# (메인 레포) 에 OG 생성 → kiwoom_cron 의 cron-isolation push 가 빈 og/ 배포 → 라이브 404.
export M1S_HOMEPAGE="/Users/seongjinpark/company/100m1s-homepage-cron"

# Phase 1 (REQ-002): DB 백업 (.backup, 7개 리텐션)
bash "$PWD/scripts/news_pipeline/backup_db.sh" || true

# 날짜 인자 (과거 날짜 재실행 지원)
export PIPELINE_DATE="${1:-$(date +%Y-%m-%d)}"

# .env 로드
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

# 휴장일 체크 — 토/일/공휴일이면 파이프라인 스킵
MARKET_CLOSED=$(python3 -c "
from scripts.news_pipeline.config import is_market_holiday
print('yes' if is_market_holiday('$PIPELINE_DATE') else 'no')
" 2>/dev/null)
if [ "$MARKET_CLOSED" = "yes" ]; then
  echo "=== $(date -Iseconds) SKIP: $PIPELINE_DATE is a market holiday ===" >> "$LOG" 2>/dev/null
  echo "SKIP: $PIPELINE_DATE is a market holiday (weekend or public holiday)"
  exit 0
fi

LOG="$PWD/scripts/news_pipeline/logs/pipeline-${PIPELINE_DATE}.log"
mkdir -p "$(dirname "$LOG")"
echo "=== $(date -Iseconds) pipeline start (date=$PIPELINE_DATE) ===" >> "$LOG"

# Q-20260519-CYCLE17-004 — SSOT-guard 명시 dedicated log channel
# 본질: SSOT-guard SKIP/race-guard 분기 작동 시 별도 log file 박제 (audit 용이).
# 기존 인라인 박제 ($LOG)는 호환성 유지 + dedicated log 동시 박제.
SSOT_GUARD_LOG="$PWD/records/optimization/ssot-guard-log/$(date +%Y-%m-%d).log"
mkdir -p "$(dirname "$SSOT_GUARD_LOG")"
_ssot_guard_log() {
  # 인자: $1=branch (ssot-skip|race-guard|cron-fallback|ssot-clean), $2=detail
  local branch="$1"
  local detail="$2"
  local ts
  ts=$(date -Iseconds)
  echo "${ts} [pipeline.sh] ${branch}: ${detail}" >> "$SSOT_GUARD_LOG"
}

# SSOT-guard 제거 (2026-06-16, DOC-20260616 dev cycle — DOC-20260615-FLR-001 §4 P1 후속).
#
# 제거 근거 (3종 evidence):
#   1) 본 pipeline.sh 는 git push/commit/reset/rebase/stash = 0건 (수집·생성만). 배포는
#      kiwoom_cron.sh 단독 (DOC-20260527-DEC-001 단일 파이프라인 통합). collect_rss 등
#      모든 stage 는 stocks.db(*.db = .gitignore L62, 미추적) 적재 + cron worktree(M1S_HOMEPAGE)
#      파일 생성만 → 메인 레포 git index 를 단 1바이트도 touch 안 함 (dry-run 실측: rc=0,
#      staged 0건). 따라서 SSOT-guard 가 막으려던 FLR-20260504-FLR-003(cron stash/rebase 가
#      메인 SSOT revert)의 가해 경로가 본 스크립트엔 부재 → 가드 효익 0.
#   2) 부작용 100%: 메인 레포 SSOT(CLAUDE.md/agents 등) dirty 1건이 24h·10분 야간 수집을
#      무한 SKIP (2026-06-15 21:40~ 5h+ 연속, 19회/일). FLR-615 §3 "감지·알람만·SKIP 유지
#      = 무한정지 안티패턴".
#   3) §11.32 cron worktree 격리(homepage-cron = 별도 git tree)로 FLR-003 자체가 인프라 layer
#      에서 봉쇄 → 운영 layer 회피책(SSOT-guard)의 명목 효익도 소멸.
#
# cron-fallback(큐 5분+ 자동 stage) 동반 제거: PostToolUse hook
# `scripts/hooks/queue-edit-autostage.sh` 가 Edit 직후 즉시 MASTER-QUEUE.md auto-stage
# (DOC-20260512-FLR-001 primary 봉쇄). 본 cron-fallback 은 그 hook 의 5분-지연 secondary
# backstop 이었으므로 primary(hook) 유지 시 제거 안전. SSOT clean marker 정리도 불요.

# R3 PR2: stage별 시간 측정 헬퍼
# 사용: _stage NAME FAIL_LABEL -- <명령> [args...]
# 출력 (LOG): STAGE name=NAME start=<epoch> end=<epoch> dur=<sec>s rc=<exit>
# 동작: 기존 `<cmd> >> $LOG 2>&1 || echo FAIL_LABEL`과 동등 — rc 비0 시 FAIL_LABEL 기록
_stage() {
  local name="$1"; shift
  local fail_label="$1"; shift
  [ "$1" = "--" ] && shift
  local start_epoch end_epoch dur rc
  start_epoch=$(date +%s)
  "$@" >> "$LOG" 2>&1
  rc=$?
  end_epoch=$(date +%s)
  dur=$((end_epoch - start_epoch))
  echo "STAGE name=$name start=$start_epoch end=$end_epoch dur=${dur}s rc=$rc" >> "$LOG"
  if [ "$rc" -ne 0 ]; then
    echo "$fail_label" >> "$LOG"
  fi
  return 0  # 기존 `|| echo FAIL` 패턴은 항상 0 반환
}

# 국내 RSS 수집 + 종목 매칭
_stage collect_rss    "collect_rss FAIL"    -- python3 -m scripts.news_pipeline.collect_rss
_stage match_stocks   "match_stocks FAIL"   -- python3 -m scripts.news_pipeline.match_stocks

# 글로벌 RSS 수집 (트럼프/관세/지정학 매크로 전용)
_stage global_rss     "global_rss FAIL"     -- python3 -c "
from scripts.news_pipeline.collect_rss import collect_global
from scripts.news_pipeline.match_stocks import store_global_news
items = collect_global()
if items: store_global_news(items)
"

_stage collect_disclosures   "collect_disclosures FAIL"    -- python3 -m scripts.news_pipeline.collect_disclosures
_stage collect_kind          "collect_kind SKIP/FAIL"      -- python3 -m scripts.news_pipeline.collect_kind
_stage interpret_disclosures "interpret_disclosures SKIP"  -- python3 -m scripts.news_pipeline.interpret_disclosures
_stage dump_review           "dump_review SKIP"            -- python3 -m scripts.news_pipeline.dump_review

# REQ-006 R3-PR3 (Candidate A-1): extract_macros 백그라운드 병렬화
# 병목은 extract_macros(평균 44s, claude CLI LLM 대기). collect_credit/intraday/dailybars
# 와 데이터 의존성 無 → 백그라운드로 띄워 LLM 대기 시간과 3개 수집 stage를 겹친다.
# 목표: 전체 cron end-to-end -10% (52s→47s 이하). interpret_loop 이전에 wait로 동기화.
# 회귀 위험: SQLite WAL 동시 write 락 경합 — 머지 후 3회 cron 모니터링 필요 (REF-006 §5).
EXTRACT_MACROS_LOG="$(mktemp -t extract_macros_parallel.XXXXXX.log)"
(
  LOG="$EXTRACT_MACROS_LOG"
  _stage extract_macros "extract_macros FAIL" -- python3 -m scripts.news_pipeline.extract_macros
) &
EXTRACT_MACROS_PID=$!

# 키움 kt20016 신용가능종목 수집 (실전키, 1일 1회, 로컬 고정IP 전제)
_stage collect_credit   "collect_credit SKIP"   -- python3 -m scripts.news_pipeline.collect_credit

# 신규 daily_picks 종목 자동 마스터 등록 + 240d backfill (P0 본질 fix 2026-05-11)
# 반드시 collect_intraday / collect_dailybars **이전** 에 실행해야 함.
# 누락 시 collect_dailybars 의 UPDATE stocks SET ... WHERE code=? 가 0 rows 처리되어
# price_high_240d 영구 NULL → frontend range_240d "--" 결함 (FLR-20260511 root B).
# 마스터에 이미 있는 종목만 있으면 즉시 종료 (no-op, 비용 무시).
_stage seed_master_for_today "seed_master_for_today SKIP" -- python3 -m scripts.news_pipeline.seed_master_for_today

# 분봉 스냅샷 (ka10080, 모의투자, sparkline 렌더용)
_stage collect_intraday "collect_intraday SKIP" -- python3 -m scripts.news_pipeline.collect_intraday

# 일봉 240영업일 min/max (ka10081, REQ-001 Phase 2)
# 종목 단위 트리거 — daily_picks 통째 UPSERT (cron 내 중복은 _RUN_CACHE로 방지)
_stage collect_dailybars "collect_dailybars SKIP" -- python3 -m scripts.news_pipeline.collect_dailybars

# Fix-6 (2026-05-24, DOC-20260522-MIN-001 §21.4 #2):
# collect_dailybars timeout cascade catch — log grep + osascript notification.
#
# 5/22 사고 (wall 8h 46min + 18회 SIGTERM cascade) 동안 alert 0건 → 대표 직접
# catch (08:25 KST) 전까지 인지 부재. 본 fix = alert 부재 ROOT chain 봉쇄
# (DOC-20260522-MIN-001 §12).
#
# 본 분기:
#   (a) collect_dailybars.py 가 timeout_break 시 stderr 에 "TIMEOUT_CASCADE_DETECTED"
#       marker 박제 (_stage 함수 stderr → $LOG 통합 redirect 정합).
#   (b) log tail grep — 직전 _stage 종료 직후 marker 라인 존재 시 cascade catch.
#   (c) osascript display notification — kiwoom_cron.sh L154 / pipeline.sh L84 동형 패턴.
#   (d) $LOG 에 별도 marker "TIMEOUT_CASCADE_DETECTED_CRON" prepend — 후행 audit 용.
#   (e) recurring detect — /tmp/100m1s-pipeline-timeout-cascade.marker 누적 count.
#       2회 이상 연속 시 critical log 강화 (자동 sub-agent spawn 후행 권고).
if tail -200 "$LOG" 2>/dev/null | grep -q "TIMEOUT_CASCADE_DETECTED"; then
  TIMEOUT_MARKER_FILE="/tmp/100m1s-pipeline-timeout-cascade.marker"
  TIMEOUT_NOW=$(date -Iseconds)
  TIMEOUT_COUNT=1
  if [ -f "$TIMEOUT_MARKER_FILE" ]; then
    TIMEOUT_COUNT=$(($(cat "$TIMEOUT_MARKER_FILE" 2>/dev/null | head -1 || echo 0) + 1))
  fi
  echo "$TIMEOUT_COUNT" > "$TIMEOUT_MARKER_FILE"
  echo "[TIMEOUT_CASCADE_DETECTED_CRON] ${TIMEOUT_NOW} collect_dailybars BATCH_LIMIT_SEC 초과 (cascade count=${TIMEOUT_COUNT}) — alert 발화" >> "$LOG"
  _ssot_guard_log "timeout-cascade" "collect_dailybars BATCH_LIMIT_SEC 초과 (cascade count=${TIMEOUT_COUNT}, ts=${TIMEOUT_NOW})"
  # osascript notification (best-effort, 기존 패턴 verbatim 재사용)
  osascript -e "display notification \"collect_dailybars TIMEOUT (count=${TIMEOUT_COUNT}) — pipeline.sh log 점검 + 다음 fire resume 진입 확인\" with title \"100m1s TIMEOUT CASCADE\"" 2>/dev/null || true
  # 2회+ 연속 cascade 시 critical log 강화 (3회+ 시 stale lock 의심 cascade)
  if [ "$TIMEOUT_COUNT" -ge 2 ]; then
    echo "[TIMEOUT_CASCADE_DETECTED_CRON] 🔴 CRITICAL: cascade count=${TIMEOUT_COUNT} (연속 2회+) — _RUN_CACHE resume 실패 의심, 5/22 사고 동형 회귀 의심" >> "$LOG"
    osascript -e "display notification \"collect_dailybars TIMEOUT 연속 ${TIMEOUT_COUNT}회 — 5/22 사고 동형 의심, 즉시 점검\" with title \"100m1s CRITICAL\"" 2>/dev/null || true
  fi
else
  # 정상 cycle — marker reset (timeout cascade 해소 신호)
  rm -f /tmp/100m1s-pipeline-timeout-cascade.marker 2>/dev/null || true
fi

# KOSPI/KOSDAQ 업종 일봉 (ka20006, 임무 C — index_multiple_current 계산용)
# 2회 API 호출만 (inds_cd 001/101) — 부하 낮음. --backfill 5로 최근 영업일만 최신화.
_stage collect_kiwoom_indices "collect_kiwoom_indices SKIP" -- python3 -m scripts.news_pipeline.collect_kiwoom_indices --backfill 5

# extract_macros 백그라운드 동기화 — 3개 수집 stage와 겹쳐 실행된 결과를 wait
wait "$EXTRACT_MACROS_PID" || true
if [ -s "$EXTRACT_MACROS_LOG" ]; then
  cat "$EXTRACT_MACROS_LOG" >> "$LOG"
fi
rm -f "$EXTRACT_MACROS_LOG"

# ──────────────────────────────────────────────────────────────────────
# P0 단일 파이프라인 통합 (2026-05-27, DOC-20260527-DEC-001 분리→통합 번복,
# 대표 직접 결정 "분리 지금 제거해"):
#
# build_daily / homepage git push / interpret_loop(opus) 를 본 30분 cron 에서
# **제거**. 이제 단일 배포 cron = kiwoom_cron.sh (10분) 가 build_daily + LLM tail
# (interpret_loop → verify_trades → final_gate, INTERPRET_CONCURRENCY=50) + 끝
# push 를 전담 → build_daily/push 가 한 곳에만, LLM 도 한 곳에만, lock 하나.
#
# 본 pipeline.sh 는 kiwoom_cron 이 다루지 않는 **고유 수집/생성 stage 만** 남긴다
# (위 _stage 들 + 아래 calc_indicators/generate_og/generate_stock_og):
#   - 뉴스/공시 (haiku) 수집 + 종목 매칭 + 매크로 (collect_rss ~ extract_macros)
#   - 키움 수집 보강 (collect_credit/seed_master/collect_intraday/collect_dailybars
#     /collect_kiwoom_indices)
#   - 산출물 생성 (calc_indicators=기술지표, generate_og/generate_stock_og=OG 이미지)
#
# 이 stage 들은 stocks.db 에 적재하거나 homepage 작업트리에 og/news/지표 파일을
# 생성만 한다. **배포는 안 한다** — kiwoom_cron 의 다음 fire (≤10분) build_daily 가
# DB 를 카드로 렌더하고, kiwoom_cron 끝 push 의 add-set (og/ news/
# data/limit-up-trend.json 흡수) 이 본 cron 산출물까지 함께 배포한다 (SSOT 대칭).
#
# 주의: homepage 작업트리에 생성된 og/news/지표 파일은 다음 kiwoom_cron push 까지
# 미커밋 상태로 대기하나, kiwoom_cron 끝 push 가 stash→pull→pop 후 add 하므로 안전.
# 장 마감 후엔 kiwoom_cron catch-up trigger 가 1회 배포 처리.
# ──────────────────────────────────────────────────────────────────────

# 기술지표 계산 (MA/RSI/MACD — 장 마감 후 1회) — homepage 작업트리에 생성만, push 無.
_stage calc_indicators  "calc_indicators FAIL"  -- python3 -m scripts.news_pipeline.calc_indicators

# OG 이미지 생성 (날짜별 + og-news.png 최신 사본) — homepage 작업트리에 생성만, push 無.
_stage generate_og      "generate_og FAIL"      -- python3 -m scripts.news_pipeline.generate_og

# 종목코드별 OG 이미지 + 메타 HTML (외부 공유용 — 카톡/트위터 썸네일)
# 39종목 ~1.5초, Pillow 템플릿 방식 (Playwright 미사용, cron 부담 최소). 생성만, push 無.
_stage generate_stock_og "generate_stock_og FAIL" -- python3 -m scripts.news_pipeline.generate_stock_og

# ──────────────────────────────────────────────────────────────────────
# 🔴 P1-3 생성단 invariant (FLR-20260605-TEC-001) — "부분상태 노출 클래스" 생성단 봉쇄.
#   generate_stock_og 직후 "카드 코드 = 생성된 페이지/OG 코드" 단언. 누락분이 있으면
#   배포(kiwoom_cron push)가 흡수하기 **전에** 생성단에서 즉시 재생성 → 완결성 확보.
#   판정은 공통 모듈 check_card_page_coverage (P1-1 배포단과 동일 로직 — 한쪽-fix
#   누락 회피, FLR-20260406-TEC-001). 재생성은 누락 코드만 (--codes), 1회 cap
#   (그래도 남으면 다음 30분 cycle 또는 kiwoom_cron P1-1 게이트가 재처리 — 무한 루프 X).
#   비-fatal (set -e 하 || true): 검사/재생성 실패가 cron abort 미유발.
# ──────────────────────────────────────────────────────────────────────
COV_OUT="$(python3 -m scripts.news_pipeline.check_card_page_coverage "$PIPELINE_DATE" 2>>"$LOG" || true)"
echo "[P1-3 coverage] $COV_OUT" >> "$LOG"
COV_MISSING="$(printf '%s\n' "$COV_OUT" | sed -n 's/^MISSING=//p')"
if [ -n "$COV_MISSING" ]; then
  echo "[P1-3 coverage] 누락 코드 재생성: $COV_MISSING" >> "$LOG"
  _stage generate_stock_og_refill "generate_stock_og_refill FAIL" -- \
    python3 -m scripts.news_pipeline.generate_stock_og --codes "$COV_MISSING" "$PIPELINE_DATE"
  # 재생성 후 재검사 (잔존 누락 = 데이터 결손, 로그만 — 다음 cycle/배포단 게이트 처리)
  COV_OUT2="$(python3 -m scripts.news_pipeline.check_card_page_coverage "$PIPELINE_DATE" 2>>"$LOG" || true)"
  COV_MISSING2="$(printf '%s\n' "$COV_OUT2" | sed -n 's/^MISSING=//p')"
  if [ -n "$COV_MISSING2" ]; then
    echo "[P1-3 coverage] ⚠️ 재생성 후 잔존 누락: $COV_MISSING2 — 다음 cycle/배포단 게이트 재처리" >> "$LOG"
  else
    echo "[P1-3 coverage] ✅ 재생성 후 완결 (카드=페이지=OG)" >> "$LOG"
  fi
fi

# REQ-003: LLM 캐시 누적 통계 출력 (HIT/MISS/재사용률)
SCRIPT_BASE="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$SCRIPT_BASE"
echo "--- LLM cache summary ---" >> "$LOG"
python3 -m scripts.news_pipeline.llm_client cache >> "$LOG" 2>&1 || echo "cache stats SKIP" >> "$LOG"

echo "=== $(date -Iseconds) pipeline end (build_daily/push 는 kiwoom_cron 단일 — DOC-20260527-DEC-001) ===" >> "$LOG"
