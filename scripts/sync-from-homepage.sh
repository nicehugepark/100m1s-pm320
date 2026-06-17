#!/usr/bin/env bash
#
# PM320 서브도메인 직접 서빙 전환 — homepage repo → pm320 repo 콘텐츠/데이터 동기화
#
# 목적: pm320.100m1s.com 이 redirect placeholder(→100m1s.com/pm320) 대신
#       실 콘텐츠를 직접 서빙하도록, 100m1s-homepage repo의 PM320 자산을
#       100m1s-pm320 repo로 복사한다.
#
# 근거 진단: records/2026-06/pm320-rebuild/reverse-infra.md §1.2 + 본 devops 단발 실측
#   - PM320 실코드는 100m1s-pm320 repo에 없음(redirect placeholder만) → homepage repo에 있음
#   - data-loader.js가 /data/* 절대경로 fetch → pm320 repo 루트에 /data 필수
#
# 🔴 destructive 주의: 이 스크립트는 SRC(homepage)에서 DST(pm320)로 cp(non-destructive on SRC).
#   DST의 기존 index.html(redirect)은 pm320.html로 대체되며, 구도메인 redirect는 별도 stub로 보존.
#   실행은 lead GO 후. 그 전까지 준비물.
#
# 사용:
#   bash scripts/sync-from-homepage.sh          # dry-run (복사 목록 출력만, 실 cp 0)
#   bash scripts/sync-from-homepage.sh --apply  # 실 cp 실행 (lead GO 후)
#
set -euo pipefail

SRC="/Users/seongjinpark/company/100m1s-homepage"
DST="/Users/seongjinpark/company/100m1s-pm320"
APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

run() {
  if [ "$APPLY" = "1" ]; then
    echo "[apply] $*"
    "$@"
  else
    echo "[dry-run] $*"
  fi
}

echo "=== PM320 콘텐츠/데이터 동기화 (apply=$APPLY) ==="
echo "SRC: $SRC"
echo "DST: $DST"
echo ""

# --- 1) 진입점 + 본 페이지 ---
# /pm320 경로 진입은 pm320/index.html (refresh → /pm320) 이었으나,
# pm320 repo 직접 서빙에선 루트 index.html = pm320 본 페이지로 직접 서빙.
# (pm320.html 을 index.html 로 복사하여 pm320.100m1s.com 진입 즉시 본 페이지)
run cp "$SRC/pm320.html" "$DST/index.html"
run cp "$SRC/pm320.html" "$DST/pm320.html"   # 기존 절대경로 /pm320.html fetch 호환

# --- 2) JS 전체 (data-loader, renderer, utils, calendar, components, lib) ---
run rsync -a --delete "$SRC/js/" "$DST/js/"

# --- 3) CSS + menu.js ---
run cp "$SRC/news.css" "$DST/news.css"
run cp "$SRC/menu.js" "$DST/menu.js"

# --- 4) PM320 자산 (favicon, og 대표 이미지) ---
run rsync -a --delete "$SRC/pm320-assets/" "$DST/pm320-assets/"

# --- 5) 실 서빙 데이터 (data-loader.js 가 fetch 하는 /data/* 하위만 — ~29MB) ---
#   백업/원본(data/backups 382M, stocks.db 등)은 제외 → 라이브 fetch 대상만.
mkdir -p "$DST/data"
for d in pm320_history interpreted themes dailybars kiwoom dailybars-nxt us-indices calendar; do
  run rsync -a --delete "$SRC/data/$d/" "$DST/data/$d/"
done
for f in holidays.json limit-up-trend.json page-manifest.json; do
  run cp "$SRC/data/$f" "$DST/data/$f"
done

# --- 6) Share stub + OG 이미지 (과거 공유 링크 보존, ~11MB) ---
run rsync -a --delete "$SRC/pm320/" "$DST/pm320/"
mkdir -p "$DST/og"
run rsync -a --delete "$SRC/og/pm320/" "$DST/og/pm320/"
run rsync -a --delete "$SRC/og/news/" "$DST/og/news/"   # pm320/ stub 의 og:image=/og/news/stock 참조 호환

# --- 7) sw.js (PWA 캐시) ---
run cp "$SRC/sw.js" "$DST/sw.js"

# --- 8) 정적 페이지 (pm320.html 이 링크하는 /lecture.html /privacy.html /terms.html) ---
for f in lecture.html privacy.html terms.html; do
  [ -f "$SRC/$f" ] && run cp "$SRC/$f" "$DST/$f"
done

echo ""
echo "=== 완료 (apply=$APPLY). 다음: 도메인 절대경로 갱신은 patch-domain.sh ==="
