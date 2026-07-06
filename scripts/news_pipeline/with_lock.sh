#!/usr/bin/env bash
# Phase 1 (REQ-002): Python fcntl 기반 lock 래퍼
# 사용: bash with_lock.sh <LOCK_PATH> -- <CMD> [ARGS...]
# 락 미획득 시 종료 코드 0 + "SKIP: lock held" 출력 (cron 자동 SKIP)
# macOS에 flock 명령이 없어 fcntl로 대체 (옵션 B, REQ-002 결정)
#
# cycle20 P1 (2026-05-20): stale lock detect 추가
# - lock 미획득 시 holder PID alive check + lock 파일 mtime 5분+ stale 검사
# - 두 조건 모두 충족 시 force release (unlink + 재획득)
# - 9h stale lock 사고 (2026-05-19 12:19 ~ 2026-05-20 09:09 KST) 재발 방지
# - 사고 근거: /tmp/kiwoom-launchd-stdout.log "2026-05-20T09:09:31+09:00 SKIP: lock held"
#
# cycle20 P1 강화 (2026-05-20 09:52~): 3 layer 봉쇄
# - A: 자식 프로세스 wall-clock timeout (CHILD_TIMEOUT_SEC=540, GRACE_KILL_SEC=60)
#   SIGTERM → 60초 grace → SIGKILL → lock 자동 해제. 다음 10분 cycle 진입 보장.
#   launchd ExitTimeOut 키 macOS man page 명시 없어 wrapper 단 구현 (lead 권고 검증 결과).
# - B: lsof 기반 holder 검증 — 좀비 프로세스는 lsof 미점유 (`ps`만으로 alive 오인 차단)
#   + long-running stuck 감지: lock fd 점유자 없으면 (PID ps alive 와 무관하게) stale 판정
# - C: SKIP false-positive 차단 — holder lsof 점유 정상 SKIP 시 카운트 reset (false-positive 알람 차단)
#   stale SKIP (lsof 미점유) 만 카운트 ↑ → 알람

set -u

if [ "$#" -lt 3 ]; then
  echo "usage: $0 <LOCK_PATH> -- <CMD> [ARGS...]" >&2
  exit 2
fi

LOCK_PATH="$1"; shift
if [ "$1" != "--" ]; then
  echo "usage: $0 <LOCK_PATH> -- <CMD> [ARGS...]" >&2
  exit 2
fi
shift

# Python fcntl.flock(LOCK_EX|LOCK_NB)으로 획득 시도.
# 성공: fork-exec로 명령 실행, 종료 후 락 해제
# 실패 분기:
#   - lsof로 holder 검증 → 점유자 있음 (정상 long-running) → SKIP, false-positive 차단 신호 (exit 98)
#   - lsof로 holder 검증 → 점유자 없음 (좀비/abnormal exit) → stale → force release 1회 → 그래도 실패 시 99 (race SKIP)
# cycle20 P1: STALE_THRESHOLD_SEC = 300 (5분) — lsof 점유 없을 때 mtime 5분+ 일 때만 force (race 보호)
python3 - "$LOCK_PATH" "$@" <<'PY'
import fcntl, os, sys, errno, time, subprocess, signal

STALE_THRESHOLD_SEC = 300  # 5분 (B: lsof 점유자 0 인 경우 force release 임계)
CHILD_TIMEOUT_SEC = 540    # A: 9분 — 다음 10분 cycle 전 SIGTERM 보장
GRACE_KILL_SEC = 60        # A: SIGTERM 후 60초 grace → SIGKILL

lock_path = sys.argv[1]
argv = sys.argv[2:]

def _try_acquire():
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd, None
    except OSError as e:
        os.close(fd)
        return None, e

def _read_holder_pid():
    try:
        with open(lock_path, 'r') as f:
            content = f.read().strip()
        return int(content.split('\n')[0]) if content else 0
    except (OSError, ValueError):
        return 0

def _lock_mtime_age():
    try:
        return time.time() - os.path.getmtime(lock_path)
    except OSError:
        return 0

def _lsof_holder_pids(path):
    """lsof로 file lock 점유 중인 PID 집합 반환 (좀비/dead PID 자동 배제).

    좀비 프로세스는 file descriptor 를 잃기 때문에 lsof 에 안 잡힘 → 가장 신뢰성 있는 alive 검증.
    ps/kill(0) 보다 강함 (ps 는 좀비 표시, kill(0) 은 좀비도 alive 반환 가능).
    """
    try:
        # -t = terse (PID only), 명시적 path 인자
        out = subprocess.run(
            ['lsof', '-t', path],
            capture_output=True, text=True, timeout=5,
        )
        pids = set()
        for line in out.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                pids.add(int(line))
        return pids
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        # lsof 없거나 timeout → 보수적 처리 (점유자 있다고 가정, SKIP)
        return None  # None = lsof 사용 불가 신호

# 1차 시도
fd, err = _try_acquire()
if err is not None:
    if err.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
        raise err

    # cycle20 P1: lsof 기반 holder 검증 (좀비 PID 자동 배제)
    holder_pid = _read_holder_pid()
    mtime_age = _lock_mtime_age()
    lsof_pids = _lsof_holder_pids(lock_path)

    if lsof_pids is None:
        # lsof 사용 불가 → 보수적 SKIP (기존 동작)
        sys.stderr.write(
            f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} lsof unavailable, conservative SKIP "
            f"holder_pid={holder_pid} mtime_age={mtime_age:.0f}s\n"
        )
        sys.exit(99)

    if len(lsof_pids) > 0:
        # 정상 long-running: lsof 가 점유자 식별 → 카운트 reset 신호 (exit 98)
        sys.stderr.write(
            f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} normal SKIP: lsof holder alive "
            f"pids={sorted(lsof_pids)} mtime_age={mtime_age:.0f}s ({lock_path})\n"
        )
        sys.exit(98)

    # lsof 점유자 0 = 좀비/abnormal exit. mtime 보호 후 force release
    if mtime_age >= STALE_THRESHOLD_SEC:
        sys.stderr.write(
            f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} STALE LOCK detect: "
            f"lsof_pids=0 (no holder) recorded_pid={holder_pid} "
            f"mtime_age={mtime_age:.0f}s (>= {STALE_THRESHOLD_SEC}s) "
            f"→ force release ({lock_path})\n"
        )
        try:
            os.unlink(lock_path)
        except OSError:
            pass  # 동시 unlink race tolerate
        fd, err2 = _try_acquire()
        if err2 is not None:
            # race condition: 다른 프로세스가 이미 획득. stale SKIP 신호
            sys.exit(99)
    else:
        # lsof 점유자 0 인데 아직 mtime 5분 미만 → race window 보호 stale SKIP
        sys.stderr.write(
            f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} stale SKIP (race protect): "
            f"lsof_pids=0 mtime_age={mtime_age:.0f}s (< {STALE_THRESHOLD_SEC}s) ({lock_path})\n"
        )
        sys.exit(99)

# 락 보유 PID 기록 (디버깅용)
try:
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n".encode())
except OSError:
    pass

# A: 자식 프로세스 wall-clock timeout
# - 자식 PGID 생성 (setsid) → 자식 + 손자 일괄 SIGTERM 가능
# - SIGTERM 후 GRACE_KILL_SEC 지나도 살아있으면 SIGKILL
# - fcntl flock 은 lock fd 가 닫히면 자동 해제 → 다음 cycle 진입 가능
pid = os.fork()
if pid == 0:
    # 자식: 새 프로세스 그룹 leader → 일괄 신호 전달 가능
    try:
        os.setsid()
    except OSError:
        pass
    os.execvp(argv[0], argv)

# 부모: timeout 감시 루프
deadline = time.monotonic() + CHILD_TIMEOUT_SEC
sigterm_sent_at = None

while True:
    waited_pid, status = os.waitpid(pid, os.WNOHANG)
    if waited_pid == pid:
        if os.WIFEXITED(status):
            sys.exit(os.WEXITSTATUS(status))
        sys.exit(1)

    now = time.monotonic()
    if sigterm_sent_at is None and now >= deadline:
        # 9분 경과 → SIGTERM 송신 (자식 PGID 전체)
        sys.stderr.write(
            f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} CHILD TIMEOUT (>{CHILD_TIMEOUT_SEC}s): "
            f"pid={pid} pgid={pid} → SIGTERM ({lock_path})\n"
        )
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        sigterm_sent_at = now
    elif sigterm_sent_at is not None and now - sigterm_sent_at >= GRACE_KILL_SEC:
        # SIGTERM 후 60초 grace → SIGKILL
        sys.stderr.write(
            f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} GRACE EXPIRED ({GRACE_KILL_SEC}s): "
            f"pid={pid} → SIGKILL ({lock_path})\n"
        )
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        # 마지막 reap 시도
        try:
            _, status = os.waitpid(pid, 0)
        except OSError:
            pass
        sys.exit(124)  # GNU timeout 관례 (124 = timeout)

    time.sleep(1)
PY

rc=$?

SKIP_COUNTER="${LOCK_PATH}.skip-count"
ALARM_FLAG="${LOCK_PATH}.skip-alarm-sent"

if [ "$rc" -eq 98 ]; then
  # cycle20 P1: 정상 SKIP (lsof holder alive) — false-positive 차단
  # 카운트 reset + alarm flag clear (정상 long-running 은 알람 송신 금지)
  echo "$(date -Iseconds) SKIP: lock held normally ($LOCK_PATH) — holder alive (lsof)"
  rm -f "$SKIP_COUNTER" "$ALARM_FLAG" 2>/dev/null || true
  exit 0
fi

if [ "$rc" -eq 124 ]; then
  # cycle20 P1 A: 자식 9분+ wall-clock timeout → SIGTERM/SIGKILL 완료
  # lock 자동 해제 (fd close on process exit) → 다음 cycle 진입 가능
  echo "$(date -Iseconds) TIMEOUT: child killed (>9m) ($LOCK_PATH)"
  # 카운트는 reset (stale 아님, 단순 timeout — 다음 cycle 정상 진입 예상)
  rm -f "$SKIP_COUNTER" "$ALARM_FLAG" 2>/dev/null || true

  # 즉시 알람 (timeout 자체가 비정상 — 작업 분량 과다 또는 hang)
  osascript -e "display notification \"with_lock.sh CHILD TIMEOUT 9m+ — $LOCK_PATH 작업 hang 의심. 로그 확인 권고.\" with title \"100m1s WARN\"" 2>/dev/null || true
  exit 124
fi

if [ "$rc" -eq 99 ]; then
  # stale SKIP (lsof 미점유 또는 race) — 카운트 누적, 알람 후보
  echo "$(date -Iseconds) SKIP: lock held STALE ($LOCK_PATH)"

  # cycle20 P1: stale SKIP 연속 3회+ osascript 알람
  # counter 파일 = lock 파일 별 (kiwoom + news 분리)
  CUR_COUNT=$(cat "$SKIP_COUNTER" 2>/dev/null || echo 0)
  CUR_COUNT=$((CUR_COUNT + 1))
  echo "$CUR_COUNT" > "$SKIP_COUNTER"
  if [ "$CUR_COUNT" -ge 3 ] && [ ! -f "$ALARM_FLAG" ]; then
    # 기존 osascript 패턴 재사용 (kiwoom_cron.sh L81 / pipeline.sh L84 동형)
    osascript -e "display notification \"with_lock.sh STALE SKIP 연속 ${CUR_COUNT}회 — stale lock 의심. ${LOCK_PATH} 확인 권고.\" with title \"100m1s CRITICAL\"" 2>/dev/null || true
    touch "$ALARM_FLAG"
    echo "$(date -Iseconds) ALARM SENT: STALE SKIP=${CUR_COUNT} ($LOCK_PATH) — osascript notification" >&2
  fi

  exit 0
fi

# 정상 실행 (lock 획득 성공) → counter + alarm flag reset
rm -f "$SKIP_COUNTER" "$ALARM_FLAG" 2>/dev/null || true

exit "$rc"
