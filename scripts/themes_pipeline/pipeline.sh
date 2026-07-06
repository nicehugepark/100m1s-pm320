#!/bin/bash
# 테마뉴스 파이프라인 (Phase 5 / REQ-20260420-REQ-004)
# 메인과 5분 위상 시프트.
#
# 락 직렬화: 외부 with_lock.sh가 처리 (macOS는 flock 명령이 없음 — REQ-002 결정).
# launchd plist에서 다음과 같이 호출:
#   bash scripts/news_pipeline/with_lock.sh /tmp/100m1s-themes.lock -- bash scripts/themes_pipeline/pipeline.sh
# 락 파일은 메인(`/tmp/100m1s-pipeline.lock`)과 분리 — themes는 별도 락.

set -uo pipefail

# S5 자립화 (DOC-20260707-REQ-001): 메인 레포 절대경로 → 이 스크립트 기준 pm320 레포 루트.
ROOT="${M1S_COMPANY:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)}"
LOG_DIR="$ROOT/scripts/themes_pipeline/logs"
mkdir -p "$LOG_DIR"

LOG="$LOG_DIR/pipeline-$(date +%Y%m%d).log"

cd "$ROOT" || exit 1

echo "=== themes-pipeline start $(date '+%F %T') ===" >> "$LOG"

# .env load (메인과 동일 키 사용 — GEMINI_API_KEY, DART_API_KEY 등)
if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

PY=${PY:-python3}

# 1. 이시카와 1차 테마 해석 (캐시 자동)
$PY -m scripts.themes_pipeline.interpret_themes >> "$LOG" 2>&1
RC1=$?

# 2. 토구사 검증 (캐시 자동)
$PY -m scripts.themes_pipeline.verify_themes >> "$LOG" 2>&1
RC2=$?

# 3. 테마맵 + 매크로 요약 빌드 (LLM 없음, write-only)
$PY -m scripts.themes_pipeline.build_theme_map >> "$LOG" 2>&1
RC3=$?

echo "=== themes-pipeline done rc=$RC1/$RC2/$RC3 $(date '+%F %T') ===" >> "$LOG"
exit 0
