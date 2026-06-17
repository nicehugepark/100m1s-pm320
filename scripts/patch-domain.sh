#!/usr/bin/env bash
#
# PM320 서브도메인 직접 서빙 전환 — 도메인 절대경로 갱신 (sync-from-homepage.sh 후행)
#
# sync 후 pm320 repo 안의 절대경로 https://100m1s.com/... 를 pm320.100m1s.com 으로 교체.
# (og:url / og:image / canonical / 브랜드 텍스트). 상대경로(/data, /js, /pm320-assets)는
# 도메인 무관하게 동작하므로 건드리지 않는다.
#
# 🔴 실행은 lead GO 후. 실 sed -i (in-place 수정)이므로 sync 완료된 DST에서만.
#
# 사용:
#   bash scripts/patch-domain.sh          # dry-run (대상 grep만)
#   bash scripts/patch-domain.sh --apply  # 실 in-place 교체 (lead GO 후)
#
set -euo pipefail

DST="/Users/seongjinpark/company/100m1s-pm320"
APPLY=0
[ "${1:-}" = "--apply" ] && APPLY=1

OLD="https://100m1s.com"
NEW="https://pm320.100m1s.com"

echo "=== 도메인 절대경로 갱신 (apply=$APPLY) ==="
echo "$OLD  →  $NEW"
echo ""

# --- 1) index.html + pm320.html: og:url / og:image / twitter:image ---
#   pm320.html:35-40 = og:url=https://100m1s.com/pm320.html / og:image=https://100m1s.com/pm320-assets/...
#   직접 서빙에선 og:url 을 pm320 루트로. (/pm320.html → / 로 정규화하되 호환 위해 둘 다 유지 가능)
for f in index.html pm320.html; do
  echo "--- $f 대상 ---"
  grep -nE "https://100m1s\.com" "$DST/$f" 2>/dev/null || echo "  (없음)"
  if [ "$APPLY" = "1" ]; then
    # og:url 은 직접 서빙 루트로 정규화: /pm320.html → / (index 서빙)
    sed -i '' \
      -e "s#${OLD}/pm320.html#${NEW}/#g" \
      -e "s#${OLD}/pm320-assets#${NEW}/pm320-assets#g" \
      -e "s#${OLD}#${NEW}#g" \
      "$DST/$f"
    echo "  [apply] 교체 완료"
  fi
done

# --- 2) pm320/ share stub (276개): og:image=https://100m1s.com/og/news/stock/... ---
#   og 이미지를 pm320 repo가 직접 서빙 → 도메인만 pm320 로.
echo "--- pm320/ stub (og:image 도메인) 대상 수 ---"
grep -lE "${OLD}/og" "$DST"/pm320/*.html 2>/dev/null | wc -l | xargs echo "  대상 파일:"
if [ "$APPLY" = "1" ]; then
  # GNU/BSD sed 호환: 파일별 in-place
  for f in "$DST"/pm320/*.html; do
    grep -qE "${OLD}" "$f" 2>/dev/null && sed -i '' "s#${OLD}#${NEW}#g" "$f"
  done
  echo "  [apply] pm320/ stub 교체 완료"
fi

# --- 3) js/renderer.js 브랜드 텍스트 "PM320 · 100m1s.com" (도메인 표기) ---
#   사용자 노출 텍스트. 직접 서빙이면 "PM320 · pm320.100m1s.com" 또는 유지 — 대표/브랜딩 결정 영역.
#   🔴 코드 직접 수정은 개발팀 위임 영역(AGENT.md §금지). 여기선 대상만 표시, 실 교체 0.
echo "--- renderer.js 브랜드 텍스트 (코드 — 개발팀 위임, 미교체) ---"
grep -nE "PM320 · 100m1s\.com" "$DST/js/renderer.js" 2>/dev/null || echo "  (없음)"
echo "  ⚠️ 브랜드 텍스트 변경은 branding/개발팀 결정 — 본 스크립트 미교체"

# --- 4) sw.js CACHE_NAME bump (전환 = 신 origin → 캐시 무효화 필수) ---
echo "--- sw.js CACHE_NAME ---"
grep -nE "^const CACHE_NAME" "$DST/sw.js" 2>/dev/null
if [ "$APPLY" = "1" ]; then
  CUR=$(grep -oE "news-v[0-9]+" "$DST/sw.js" | head -1)
  NUM=$(echo "$CUR" | grep -oE "[0-9]+")
  NEWCACHE="news-v$((NUM+1))"
  sed -i '' "s#${CUR}#${NEWCACHE}#g" "$DST/sw.js"
  echo "  [apply] CACHE_NAME ${CUR} → ${NEWCACHE}"
fi

echo ""
echo "=== 완료 (apply=$APPLY) ==="
