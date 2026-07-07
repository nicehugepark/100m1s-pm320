#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
# 한국 주식시장 달력 유틸리티
# ═══════════════════════════════════════════════════════════
# 사용:
#   bash scripts/market-calendar.sh today              # 오늘이 거래일? (true/false)
#   bash scripts/market-calendar.sh status             # 장전/장중/장후/휴장/주말
#   bash scripts/market-calendar.sh last               # 직전 거래일 (YYYY-MM-DD)
#   bash scripts/market-calendar.sh next               # 다음 거래일 (YYYY-MM-DD)
#   bash scripts/market-calendar.sh is-holiday 2026-04-14  # 특정일 휴장 여부
#   bash scripts/market-calendar.sh context            # YAML용 market_context + last_trading_day
#
# 의존: holidays.json (100m1s-homepage/data/holidays.json)
# ═══════════════════════════════════════════════════════════
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# holidays.json 경로. pm320 자립 (S5b-2 이관): 옛 ../100m1s-homepage 하드코딩 fallback →
# pm320 레포 로컬 data/holidays.json 로 자립화. 호출자(subdomain-pick-guard.sh)가
# HOLIDAYS_JSON 을 cron WT SSOT 로 주입하면 그것이 우선 (env override 유지).
HOLIDAYS_JSON="${HOLIDAYS_JSON:-$PROJECT_ROOT/data/holidays.json}"

CMD="${1:-}"
ARG="${2:-}"

if [ -z "$CMD" ]; then
    echo "사용법: bash scripts/market-calendar.sh <today|status|last|next|is-holiday|context> [date]" >&2
    exit 2
fi

# market_closed 키에 날짜가 있으면 휴장
is_market_closed() {
    local date="$1"
    if [ ! -f "$HOLIDAYS_JSON" ]; then
        # holidays.json 없으면 주말만 체크
        local dow
        dow=$(date -j -f "%Y-%m-%d" "$date" "+%u" 2>/dev/null || date -d "$date" "+%u" 2>/dev/null)
        [ "$dow" -ge 6 ] && return 0 || return 1
    fi
    python3 -c "
import json, sys
with open('$HOLIDAYS_JSON') as f:
    data = json.load(f)
mc = data.get('market_closed', {})
sys.exit(0 if '$date' in mc else 1)
"
}

get_closed_reason() {
    local date="$1"
    if [ ! -f "$HOLIDAYS_JSON" ]; then
        echo "unknown"
        return
    fi
    python3 -c "
import json
with open('$HOLIDAYS_JSON') as f:
    data = json.load(f)
mc = data.get('market_closed', {})
reason = mc.get('$date', '')
if not reason:
    print('거래일')
elif '토요일' in reason or '일요일' in reason:
    print('주말')
else:
    print('휴장')
"
}

# 직전 거래일 찾기
find_last_trading_day() {
    local ref_date="${1:-$(date +%Y-%m-%d)}"
    python3 -c "
import json, datetime
ref = datetime.date.fromisoformat('$ref_date')
try:
    with open('$HOLIDAYS_JSON') as f:
        data = json.load(f)
    closed = set(data.get('market_closed', {}).keys())
except:
    closed = set()

d = ref - datetime.timedelta(days=1)
for _ in range(30):
    ds = d.isoformat()
    if ds not in closed and d.weekday() < 5:
        print(ds)
        break
    d -= datetime.timedelta(days=1)
else:
    print(d.isoformat())
"
}

# 다음 거래일 찾기
find_next_trading_day() {
    local ref_date="${1:-$(date +%Y-%m-%d)}"
    python3 -c "
import json, datetime
ref = datetime.date.fromisoformat('$ref_date')
try:
    with open('$HOLIDAYS_JSON') as f:
        data = json.load(f)
    closed = set(data.get('market_closed', {}).keys())
except:
    closed = set()

d = ref + datetime.timedelta(days=1)
for _ in range(30):
    ds = d.isoformat()
    if ds not in closed and d.weekday() < 5:
        print(ds)
        break
    d += datetime.timedelta(days=1)
else:
    print(d.isoformat())
"
}

# 현재 시장 상태
get_market_status() {
    local today
    today=$(date +%Y-%m-%d)

    if is_market_closed "$today"; then
        get_closed_reason "$today"
        return
    fi

    # 거래일 — 시간대별 판단
    local hour minute
    hour=$(date +%H)
    minute=$(date +%M)
    local time_val=$((10#$hour * 60 + 10#$minute))

    if [ $time_val -lt 540 ]; then
        echo "장전"
    elif [ $time_val -le 930 ]; then
        echo "장중"
    else
        echo "장후"
    fi
}

case "$CMD" in
    today)
        TODAY=$(date +%Y-%m-%d)
        if is_market_closed "$TODAY"; then
            echo "false"
        else
            echo "true"
        fi
        ;;
    status)
        get_market_status
        ;;
    last)
        REF="${ARG:-$(date +%Y-%m-%d)}"
        find_last_trading_day "$REF"
        ;;
    next)
        REF="${ARG:-$(date +%Y-%m-%d)}"
        find_next_trading_day "$REF"
        ;;
    is-holiday)
        if [ -z "$ARG" ]; then
            echo "사용법: bash scripts/market-calendar.sh is-holiday <YYYY-MM-DD>" >&2
            exit 2
        fi
        if is_market_closed "$ARG"; then
            echo "true"
            REASON=$(get_closed_reason "$ARG")
            echo "$REASON" >&2
        else
            echo "false"
        fi
        ;;
    context)
        # YAML frontmatter용 market_context + last_trading_day
        STATUS=$(get_market_status)
        TODAY=$(date +%Y-%m-%d)
        if is_market_closed "$TODAY"; then
            LAST=$(find_last_trading_day "$TODAY")
        else
            # 장전이면 전일이 last trading day
            HOUR=$(date +%H)
            MINUTE=$(date +%M)
            TIME_VAL=$((10#$HOUR * 60 + 10#$MINUTE))
            if [ $TIME_VAL -lt 540 ]; then
                LAST=$(find_last_trading_day "$TODAY")
            else
                LAST="$TODAY"
            fi
        fi
        echo "market_context: \"$STATUS\""
        echo "last_trading_day: \"$LAST\""
        ;;
    *)
        echo "알 수 없는 명령: $CMD" >&2
        exit 2
        ;;
esac
