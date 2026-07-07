#!/usr/bin/env bash
# 카페 스크래퍼 로컬 실행 래퍼 (launchd 용, 재설계 v2 · 2026-07-06)
#
# 역할:
#   1. 같은 디렉토리의 .env(NAVER_COOKIES / GOOGLE_AI_API_KEY)를 환경변수로 로드
#      (main.py 는 os.environ 만 읽음 — GHA 는 secret 주입, 로컬은 .env source)
#   2. with_lock.sh 로 카페 전용 lock 획득 후 main.py 실행 (기존 pipeline lock 과 분리)
#
# 적재: lead/DevOps 후속 (launchctl load ~/Library/LaunchAgents/com.100m1s.cafe-scraper.plist)
# .env 는 커밋 금지 (쿠키·API 키 포함).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/.env"
LOCK_PATH="/tmp/100m1s-cafe-scraper.lock"
# pm320 자립 (S5b-2 이관): with_lock.sh 는 pm320 레포에 존재 → 스크립트 위치 기준 해석
# (SCRIPT_DIR = scripts/cafe-scraper → 조상 2단계 = pm320 레포 루트). M1S_COMPANY override 우선.
_CAFE_REPO_ROOT="${M1S_COMPANY:-$(cd "${SCRIPT_DIR}/../.." && pwd)}"
WITH_LOCK="${_CAFE_REPO_ROOT}/scripts/news_pipeline/with_lock.sh"

# .env 로드 (KEY='...' / KEY="..." / KEY=... 모두 지원, 값 로그 노출 금지)
if [[ -f "${ENV_FILE}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
  set +a
else
  echo "[cafe-scraper] .env 없음: ${ENV_FILE}" >&2
  exit 2
fi

# 카페 전용 lock — 다른 파이프라인 lock 과 독립
if [[ -x "${WITH_LOCK}" ]]; then
  exec bash "${WITH_LOCK}" "${LOCK_PATH}" -- /usr/bin/env python3 "${SCRIPT_DIR}/main.py"
else
  # with_lock.sh 부재 시 직접 실행 (fallback)
  exec /usr/bin/env python3 "${SCRIPT_DIR}/main.py"
fi
