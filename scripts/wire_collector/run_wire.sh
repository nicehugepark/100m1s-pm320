#!/usr/bin/env bash
# wire collector launchd entry — 수집(rc=0) 시에만 self-deploy.
# 로그: plist StandardOutPath/StandardErrorPath (~/Library/Logs/100m1s/wire-collector-*.log).
set -uo pipefail

cd "$(dirname "$0")/../.."
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:$PATH"
export M1S_HOMEPAGE="${M1S_HOMEPAGE:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/../.." && pwd)}"

echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') wire-collector start ==="
python3 scripts/wire_collector/collect_wire.py
rc=$?
echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') wire-collector end rc=$rc ==="

if [ "$rc" -eq 0 ] && [ -e "$M1S_HOMEPAGE/.git" ]; then
  bash scripts/wire_collector/wire_deploy.sh
fi

exit "$rc"
