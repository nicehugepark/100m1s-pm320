#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# PM320 레포 격리 — 신 launchd plist 초안 생성기 (S5 준비, DOC-20260707-REQ-001 §2)
#
# 목적: 현 라이브 launchd(~/Library/LaunchAgents/com.100m1s.*)를 pm320 레포 기준으로
#       재작성한 *초안*을 launchd/drafts/ 에 출력. **적재(launchctl load) 절대 안 함.**
#
# 자립화 원칙: pm320 레포는 코드(scripts/)+데이터가 동일 레포에 자립하므로
#   WorkingDirectory = M1S_HOMEPAGE = M1S_COMPANY = $PM320_REPO (한 곳).
#   실 flip(S5b) 시 $PM320_REPO 를 배포 경로(예: ~/company/100m1s-pm320)로 지정.
#
# 폐지 대상(초안 생성 안 함): dual-write(pm320-push env M1S_PM320_INDEPENDENT 제거),
#   pm320-subdomain-sync / -sync-1520 / -pick-guard (미러 배선 — pm320 정본화 시 불요).
#
# 가역성: 원본 라이브 plist·launchd 무변경. 초안 파일만 생성.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# 실 flip 시 여기만 바꾸면 됨(현재는 초안 검증용으로 배포 예정 경로 지정).
PM320_REPO="${PM320_REPO:-/Users/seongjinpark/company/100m1s-pm320}"
LA="$HOME/Library/LaunchAgents"
OUT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/drafts"
mkdir -p "$OUT_DIR"

OLD_MAIN="/Users/seongjinpark/company/100m1s"
OLD_HOMEPAGE_CRON="/Users/seongjinpark/company/100m1s-homepage-cron"
OLD_HOMEPAGE="/Users/seongjinpark/company/100m1s-homepage"

# 재작성 대상(유지) — pm320/공유 파이프라인. 미이관 의존은 로그로 경고.
KEEP_LABELS=(
  com.100m1s.pm320-push
  com.100m1s.pm320-minute-backfill
  com.100m1s.kiwoom-scraper
  com.100m1s.news-pipeline
  com.100m1s.themes-pipeline
  com.100m1s.macro-indicators
  com.100m1s.kr-index-intraday
  com.100m1s.market-anomaly-sensor
  com.100m1s.nxt-roster
  com.100m1s.wire-collector
  com.100m1s.us-digest
  com.100m1s.us-intraday
  com.100m1s.news-recovery
  com.100m1s.kakao-token-rotate
  com.100m1s.cafe-scraper
)

echo "=== PM320 launchd 초안 생성 (배포 경로 = $PM320_REPO) ==="
for label in "${KEEP_LABELS[@]}"; do
  src="$LA/$label.plist"
  if [ ! -f "$src" ]; then
    echo "  ○ SKIP $label — 라이브 plist 부재"
    continue
  fi
  dst="$OUT_DIR/$label.plist"
  # 경로 치환: 옛 메인/cron WT/homepage → pm320 레포 루트.
  #   ⚠️ 접두사 충돌 방지: OLD_MAIN(/…/100m1s)은 OLD_HOMEPAGE(/…/100m1s-homepage)·
  #      PM320_REPO(/…/100m1s-pm320)의 접두사 → 나이브 sed 시 100m1s-pm320-pm320 오염.
  #   해법: 경로 뒤 경계(뒤에 / 또는 " 또는 < 또는 EOL)를 캡처해 치환 + 긴 것 먼저.
  #      각 OLD 를 "뒤에 경로문자([-/A-Za-z0-9]) 없는" 경우만 매치(word-boundary 근사).
  sed -E \
    -e "s#${OLD_HOMEPAGE_CRON}([/\"<]|\$)#${PM320_REPO}\1#g" \
    -e "s#${OLD_HOMEPAGE}([/\"<]|\$)#${PM320_REPO}\1#g" \
    -e "s#${OLD_MAIN}([/\"<]|\$)#${PM320_REPO}\1#g" \
    "$src" > "$dst"

  # dual-write env 제거(pm320-push): M1S_PM320_INDEPENDENT* 2줄 삭제(초안에서 dual 폐지).
  if [ "$label" = "com.100m1s.pm320-push" ]; then
    /usr/bin/python3 - "$dst" <<'PYEOF'
import sys, re
f = sys.argv[1]
src = open(f).read()
# <key>M1S_PM320_INDEPENDENT...</key>\n<string>...</string> 페어 제거
src = re.sub(r'\s*<key>M1S_PM320_INDEPENDENT[^<]*</key>\s*<string>[^<]*</string>', '', src)
open(f, 'w').write(src)
print(f"    [dual-write 제거] {f}")
PYEOF
  fi

  # plutil 검증(초안 XML 유효성) — 미적재.
  if /usr/bin/plutil -lint "$dst" >/dev/null 2>&1; then
    ok="✅ plutil OK"
  else
    ok="❌ plutil FAIL"
  fi

  # 미이관 의존 경고: ProgramArguments 의 실제 스크립트가 pm320 레포에 존재하나.
  warn=""
  progs=$(/usr/bin/python3 - "$dst" <<'PYEOF'
import sys, plistlib
d = plistlib.load(open(sys.argv[1], 'rb'))
for a in d.get('ProgramArguments', []):
    if a.startswith('/') and (a.endswith('.sh') or a.endswith('.py')):
        print(a)
PYEOF
)
  while IFS= read -r p; do
    [ -z "$p" ] && continue
    [ -e "$p" ] || warn="$warn [미이관:$p]"
  done <<< "$progs"

  echo "  ● $label → $ok$warn"
done

echo ""
echo "=== 폐지 대상(초안 미생성) ==="
echo "  - com.100m1s.pm320-subdomain-sync       (미러 rsync 10분 — pm320 정본화 시 자기→자기 불요)"
echo "  - com.100m1s.pm320-subdomain-sync-1520   (미러 최후 하한)"
echo "  - com.100m1s.pm320-subdomain-pick-guard  (15:21 라이브 검증-재시도 — 서빙 단일화 후 불요)"
echo "  - pm320-push env M1S_PM320_INDEPENDENT   (dual-write — 초안에서 제거 완료)"
echo ""
echo "초안 위치: $OUT_DIR (적재 안 됨 — 실 flip S5b 시 launchctl load)"
