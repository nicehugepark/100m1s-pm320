#!/usr/bin/env bash
# stocks.db 자동 백업 + 7개 리텐션
# pipeline.sh / kiwoom_cron.sh 진입 직후 호출
# Phase 1 (REQ-002): SQLite .backup 명령 사용 (WAL/락 안전)
#
# DB 실 위치: config.py의 DATA_DIR=HOMEPAGE/data/stocks.db
#   → ../100m1s-homepage/data/stocks.db (메인 레포 옆)
# 백업 위치: 본 레포 scripts/news_pipeline/backups/ (운영 편의)
# (REQ-002 후속 핫픽스: 초안 가정 SCRIPT_DIR/stocks.db 가 잘못된 경로였음)

set -u

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
HOMEPAGE_DB="$REPO_ROOT/../100m1s-homepage/data/stocks.db"
DB="$(cd "$(dirname "$HOMEPAGE_DB")" 2>/dev/null && pwd)/$(basename "$HOMEPAGE_DB")"
BACKUP_DIR="$SCRIPT_DIR/backups"
mkdir -p "$BACKUP_DIR"

if [ ! -f "$DB" ]; then
  echo "$(date -Iseconds) backup_db: SKIP (no $DB)" >&2
  exit 0
fi
TS="$(date +%Y%m%d-%H%M)"
TARGET="$BACKUP_DIR/stocks.db.bak.$TS"

# SQLite .backup은 WAL/락 안전. cp는 쓰기 중 깨질 위험 있음.
if sqlite3 "$DB" ".backup '$TARGET'" 2>&1; then
  echo "$(date -Iseconds) backup_db: OK $TARGET"
else
  echo "$(date -Iseconds) backup_db: FAIL $TARGET" >&2
  exit 1
fi

# 최근 7개만 유지 (오래된 것 삭제, macOS xargs는 -r 미지원이므로 -I 사용)
# 글롭 ????????-???? = YYYYMMDD(8) + '-' + HHMM(4) 정확 매치.
# SQLite가 만드는 -wal/-shm 파일 제외 (REQ-002 후속)
ls -1t "$BACKUP_DIR"/stocks.db.bak.????????-???? 2>/dev/null | tail -n +8 | xargs -I{} rm -f {}
# WAL/SHM 잔여물도 함께 정리 (활성 백업과 짝이 맞지 않으면 안전 제거)
for ext in wal shm; do
  for f in "$BACKUP_DIR"/stocks.db.bak.*-"$ext"; do
    [ -e "$f" ] || continue
    base="${f%-$ext}"
    [ -e "$base" ] || rm -f "$f"
  done
done

exit 0
