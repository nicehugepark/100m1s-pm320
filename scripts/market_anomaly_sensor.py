"""코스피·코스닥 10분 이상치 내부 센서 — 조니 판정 1단계 (2026-06-12 16:02 조건부 GO).

화면 노출 0픽셀. 내부 센서 + 대표 로컬 알림만. 2단계(조사 자동 연결)는 범위 외.

데이터 소스 (가용 최선):
  로컬에 지수 분봉 없음 (index_dailybars=일봉만, intraday_snapshot=종목 전용 — 2026-06-12 grep 확인).
  → 키움 ka20005 (업종분봉조회요청) 직접 조회. read-only 실측 probe 검증 (2026-06-12 16:07 KST):
    * POST /api/dostk/chart, api-id=ka20005, body {"inds_cd": "001"|"101", "tic_scope": "10"}
    * HTTP 200, return_code=0, 응답 키 inds_min_pole_qry, 1페이지 900행(≈22거래일, 연속조회 불요)
    * 필드: cur_prc, open_pric, high_pric, low_pric, trde_qty, cntr_tm(YYYYMMDDHHMMSS, 내림차순)
    * cntr_tm = 봉 시작 라벨 (0900..1520 + 1530 종가 단일 프린트 = 40봉/일 실측)
    * 스케일 = 실지수 × 100 (ka20006와 동일): 분봉 20260612 1530 close 812362/100=8123.62
      == index_dailybars KOSPI 2026-06-12 close 8123.62 교차검증 PASS.
      FLR-20260406-TEC-001 (/1000 오인) 재발 방지.
  외부 API 사전 검증: FLR-20260408-TEC-001 — 위 probe가 인증·endpoint·스키마 실측.
  rate limit: 기존 collector 동일 endpoint 계열, 10분당 2호출 (KOSPI+KOSDAQ) — 부하 무시 수준.

탐지 (적응형 — 고정 % 금지, 조니):
  10분 수익률 z-score. 베이스라인 = 직전 BASELINE_DAYS 거래일의 장중 10분 수익률 분포
  (robust: median + MAD). |z| >= Z_THRESHOLD AND |ret| >= NOISE_FLOOR_PCT 시 트리거.
  NOISE_FLOOR는 초저변동 구간에서 무의미 z-트리거를 막는 노이즈 플로어일 뿐, 판정 주체는 z.

발사 규칙 (조니 고정): 1일 상한 2회 + 쿨다운 60분 (지수 합산 글로벌).

알림: triggers.jsonl 기록 + last_alert.json + macOS osascript notification + 카카오 self-memo.
  카카오 = 대표 본인 talk/memo/default/send 만 (broadcast/타인 발송 endpoint 구조적 부재, legal P0).
  endpoint·스키마 = scripts/pm320/send_kakao_message.py (작동 검증 채널) verbatim 동일.
  메시지 행동 지시어 0: "[긴급 시장 분석 후보] 코스피 10분 -X.X% (zN.N) — 조사 트리거"

캘리브레이션: evals.jsonl에 매 fire 전수 기록 (발사율·오탐 측정, 2주). --backtest / --calibrate 제공.

사용:
  python3 scripts/market_anomaly_sensor.py                  # live 1회 평가 (launchd 10분 간격)
  python3 scripts/market_anomaly_sensor.py --backtest 20260612   # 당일 재현 — 첫 트리거 시각
  python3 scripts/market_anomaly_sensor.py --calibrate --days 7  # 임계 스윕 오탐 시뮬
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - py<3.9 미지원 환경 없음
    ZoneInfo = None

import requests

# env 로드 — 메인 .env 단일 source (collect_kiwoom_indices.py 패턴 동일, shell export 우선)
MAIN_ENV = Path("/Users/seongjinpark/company/100m1s/.env")
if MAIN_ENV.exists():
    for line in MAIN_ENV.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

# 패키지 import 경로 보정 (단독 실행 시)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.news_pipeline.kiwoom_client import get_token  # noqa: E402

KIWOOM_BASE = os.environ.get("KIWOOM_LIVE_BASE_URL") or os.environ.get(
    "KIWOOM_BASE_URL", "https://api.kiwoom.com"
)
KIWOOM_APPKEY = os.environ.get("KIWOOM_LIVE_APPKEY") or os.environ.get("KIWOOM_APPKEY")
KIWOOM_SECRETKEY = os.environ.get("KIWOOM_LIVE_SECRETKEY") or os.environ.get(
    "KIWOOM_SECRETKEY"
)

# (표시명, inds_cd) — ka20005
INDEX_TARGETS = [("코스피", "001"), ("코스닥", "101")]

# --- 카카오 self-memo (대표 본인 알림 전용) ---
# 🔴 no-broadcast 게이트 (legal P0): URL 은 'talk/memo/default/send' 하드코딩 단일 —
#   talk/friends / broadcast / 타인 발송 endpoint 는 코드에 존재 자체가 없다 (구조적 봉쇄).
#   유료회원 푸시는 별건. 본 센서는 self-memo 만.
# endpoint·스키마 = scripts/pm320/send_kakao_message.py (작동 검증된 채널) verbatim 동일.
# token refresh·.env atomic 갱신 = scripts/pm320/kakao_token.py 공용 모듈 위임
#   (2026-07-06 통합 — FLR-20260706-PRC-001 rotate race 봉쇄, kakao_token_rotate 동형).
sys.path.insert(0, str(Path(__file__).resolve().parent / "pm320"))

KAKAO_MEMO_SEND_URL = "https://kapi.kakao.com/v2/api/talk/memo/default/send"
# PM320 페이지 링크 (종목 카드 부재 → pm320.html 직접)
KAKAO_LINK_URL = "https://100m1s.com/pm320.html"

# --- 실시간 원인 분석 (breach 시 1콜, 대표 카톡 개인용) ---
# claude CLI (Max 정액제, API 키 과금 0) + --allowed-tools WebSearch 로 실시간 뉴스 원인 도출.
#   scripts/news_pipeline/llm_client.py call_model 은 WebSearch 미지원(--allowed-tools 없음) →
#   별도 경로. cache 우회(매 breach 실시간). 실패/타임아웃/빈응답 시 빈 문자열 → 원인 줄 생략,
#   기존 알림(지수+z)은 그대로 발송 (graceful — 절대 알림 자체를 막지 않는다).
# 타임아웃: 헤드리스 WebSearch 실측 소요 40~71s (2026-06-24 dry-run 5회, sonnet) →
#   발사 일일 cap 2 + 쿨다운 60분이 호출수 bound, launchd 10분 주기 별 프로세스라 다음
#   cycle 과 충돌 없음 → 75s 상향이 안전(graceful 이라 실패해도 무해).
# 🔴 정확성 가드 (2026-06-24 대표 catch — 이시카와 환율 1560대 오보 = FLR-AGT-002 거짓 정밀성):
#   LLM/WebSearch 는 숫자(환율·지수·등락률·금액)를 지어내면 틀린다 → LLM 은 "왜"(서술)만,
#   모든 숫자는 권위 소스. 지수 = 센서가 가진 breach 값, 환율 = macro_indicators.json (Yahoo).
CLAUDE_CMD = "/Users/seongjinpark/.local/bin/claude"
CAUSE_MODEL = "claude-sonnet-4-6"
CAUSE_TIMEOUT_SEC = 75
CAUSE_MAX_LEN = 110  # 후처리 hard truncate (카톡 200자 가드 - 지수줄·환율줄 여유 확보)

# 환율 권위 소스 — PM320 Yahoo KRW=X (macro_indicators.json). env M1S_HOMEPAGE 로 경로 override.
#   cron WT 가 최신(분 단위 갱신). graceful: 파일/필드 부재 시 환율 줄 생략.
MACRO_INDICATORS_PATH = (
    Path(
        os.environ.get(
            "M1S_HOMEPAGE", str(Path.home() / "company/100m1s-homepage-cron")
        )
    )
    / "pm320/data/macro_indicators.json"
)

# 스케일·sanity — collect_kiwoom_indices.py 실측 검증값 동일 (FLR-20260406-TEC-001)
SCALE_DIVISOR = 100.0
SANITY_RANGE = {"001": (1800.0, 20000.0), "101": (400.0, 5000.0)}

# 탐지 파라미터 (2026-06-12 캘리브레이션: --backtest 20260612 + --calibrate 산출 근거는 커밋 메시지)
BASELINE_DAYS = 10  # 베이스라인 거래일 수 (적응형 분포)
Z_THRESHOLD = (
    5.0  # robust z 임계 — 2026-06-12 캘리브레이션: 6.0은 당일 급락(z-5.1) 미포착,
)
# 5.0은 당일 14:40 포착 + 평시일(5/27·5/29·6/9 등) 오탐 0 (12거래일 스윕)
NOISE_FLOOR_PCT = 0.25  # 노이즈 플로어 (판정 주체는 z)
MAX_FIRES_PER_DAY = 2  # 조니 고정
COOLDOWN_MIN = 60  # 조니 고정

# 상태·캘리브레이션 데이터 — git 비추적 경로 (기존 Logs/100m1s 패턴)
SENSOR_DIR = Path.home() / "Library/Logs/100m1s/market-sensor"
STATE_PATH = SENSOR_DIR / "state.json"
TRIGGERS_PATH = SENSOR_DIR / "triggers.jsonl"
EVALS_PATH = SENSOR_DIR / "evals.jsonl"
LAST_ALERT_PATH = SENSOR_DIR / "last_alert.json"

KST = ZoneInfo("Asia/Seoul") if ZoneInfo else None


def now_kst() -> datetime:
    return datetime.now(KST) if KST else datetime.now()


def _parse_price(val) -> float | None:
    """키움 cur_prc 파싱 — 부호/콤마 제거 후 /100 스케일."""
    if val is None:
        return None
    s = str(val).replace(",", "").replace("+", "").replace("-", "").strip()
    if not s:
        return None
    try:
        return int(s) / SCALE_DIVISOR
    except ValueError:
        return None


def fetch_index_minutes(token: str, inds_cd: str) -> list[tuple[str, float]]:
    """ka20005 업종 10분봉 1페이지 (900행 ≈ 22거래일 — 베이스라인 10일 + 여유 충분).

    반환: [(cntr_tm 14자리, close), ...] 오름차순. sanity 위반 시 RuntimeError.
    """
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "authorization": f"Bearer {token}",
        "api-id": "ka20005",
    }
    body = {"inds_cd": inds_cd, "tic_scope": "10"}
    last_err = "unknown"
    for attempt in range(3):
        try:
            r = requests.post(
                f"{KIWOOM_BASE}/api/dostk/chart", json=body, headers=headers, timeout=20
            )
        except Exception as e:  # noqa: BLE001 - 네트워크 일시 오류 재시도
            last_err = f"exception {e}"
            time.sleep(2 ** (attempt + 1))
            continue
        if r.status_code == 429:
            last_err = "http 429"
            time.sleep(2 ** (attempt + 1))
            continue
        if r.status_code != 200:
            raise RuntimeError(
                f"ka20005 {inds_cd} http {r.status_code}: {r.text[:200]}"
            )
        data = r.json()
        if data.get("return_code") != 0:
            raise RuntimeError(f"ka20005 {inds_cd} rc={data.get('return_code')}")
        rows = data.get("inds_min_pole_qry") or []
        out: list[tuple[str, float]] = []
        lo, hi = SANITY_RANGE[inds_cd]
        for row in rows:
            dt = str(row.get("cntr_tm", "")).strip()
            close = _parse_price(row.get("cur_prc"))
            if len(dt) != 14 or close is None:
                continue
            if not (lo <= close <= hi):
                raise RuntimeError(
                    f"ka20005 {inds_cd} scale sanity 위반: {close} not in [{lo},{hi}]"
                )
            out.append((dt, close))
        out.sort()
        return out
    raise RuntimeError(f"ka20005 {inds_cd} 재시도 소진: {last_err}")


def returns_by_day(bars: list[tuple[str, float]]) -> dict[str, list[tuple[str, float]]]:
    """일자별 장중 10분 수익률. {date8: [(slot4=봉시작라벨, ret_pct), ...]}.

    오버나이트 갭 제외 — 같은 날짜 내 연속 봉 close 간 수익률만.
    """
    days: dict[str, list[tuple[str, float]]] = {}
    prev_date, prev_close = None, None
    for dt, close in bars:
        date8, slot4 = dt[:8], dt[8:12]
        if prev_date == date8 and prev_close:
            ret = (close / prev_close - 1.0) * 100.0
            days.setdefault(date8, []).append((slot4, ret))
        prev_date, prev_close = date8, close
    return days


def robust_z(value: float, pool: list[float]) -> float | None:
    """median + MAD 기반 z (1.4826 보정). 분포 퇴화 시 표본표준편차 fallback."""
    if len(pool) < 60:  # 베이스라인 표본 과소 → 판정 불가 (거짓 트리거 방지)
        return None
    med = statistics.median(pool)
    mad = statistics.median(abs(x - med) for x in pool)
    scale = mad * 1.4826
    if scale < 1e-6:
        scale = statistics.pstdev(pool)
        if scale < 1e-6:
            return None
    return (value - med) / scale


def baseline_pool(
    days: dict[str, list[tuple[str, float]]], eval_date: str, n_days: int
) -> list[float]:
    """eval_date 직전 n_days 거래일의 수익률 풀 (당일 제외 — look-ahead 차단)."""
    prior = sorted(d for d in days if d < eval_date)[-n_days:]
    return [ret for d in prior for _, ret in days[d]]


def slot_completed(date8: str, slot4: str, now: datetime) -> bool:
    """봉(시작 라벨) 완성 여부 — slot 시작 + 10분 경과."""
    start = datetime(
        int(date8[:4]),
        int(date8[4:6]),
        int(date8[6:8]),
        int(slot4[:2]),
        int(slot4[2:]),
        tzinfo=now.tzinfo,
    )
    return now >= start + timedelta(minutes=10)


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:  # noqa: BLE001 - 최초 실행/손상 시 초기화
        return {}


def save_state(state: dict) -> None:
    SENSOR_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False))


def append_jsonl(path: Path, record: dict) -> None:
    SENSOR_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def notify_macos(title: str, message: str) -> None:
    """macOS 알림 — osascript display notification (실패해도 센서 진행)."""
    script = 'display notification "{}" with title "{}" sound name "Sosumi"'.format(
        message.replace('"', "'"), title.replace('"', "'")
    )
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e", script], timeout=10, capture_output=True
        )
    except Exception as e:  # noqa: BLE001
        print(f"[sensor] osascript 실패 (무시): {e}", file=sys.stderr)


def _kakao_refresh_token() -> str | None:
    """refresh_token → access_token 갱신 — kakao_token 공용 모듈 위임.

    2026-07-06 통합: 자체 refresh 로직 제거, scripts/pm320/kakao_token.py 단일 SSOT
    (FLR-20260706-PRC-001 — .env atomic 갱신 경로 이원화 = rotate race 사슬 단절 봉쇄).
    카카오가 새 refresh_token 을 rotate 발급하면 .env atomic 반영 (기존 코드는 폐기했음).
    실패 시 None (센서 진행 — fire 자체는 osascript + 기록으로 이미 완료, graceful 보존).
    키 본문 stdout/stderr 0건 (rules/security.md §2 — 공용 모듈이 값 미노출 보장).
    """
    # 함수-로컬 import — autoflake 회피 (send_kakao_message._kakao_token_module 동형)
    import kakao_token as _kt

    try:
        env = _kt.read_kakao_env(MAIN_ENV)
    except _kt.KakaoTokenError as exc:
        print(f"[sensor] 카카오 env 로드 실패 — self-memo skip: {exc}", file=sys.stderr)
        return None
    try:
        body = _kt.request_token_refresh(env)
    except _kt.KakaoTokenError as exc:
        print(f"[sensor] 카카오 토큰 갱신 실패: {exc}", file=sys.stderr)
        return None
    except Exception as exc:  # noqa: BLE001 - 알림 실패해도 센서 본연 기능 지속
        print(f"[sensor] 카카오 토큰 갱신 실패: {type(exc).__name__}", file=sys.stderr)
        return None
    new_rt = body.get("refresh_token")
    if new_rt:
        try:
            _kt.write_env_refresh_token(MAIN_ENV, new_rt)
            print("[sensor] 카카오 신규 refresh_token .env 반영 (atomic)")
        except _kt.KakaoTokenError as exc:
            # 저장 실패해도 이번 발송은 access_token 으로 진행 (graceful)
            print(f"[sensor] .env rewrite 실패 (발송 진행): {exc}", file=sys.stderr)
    return body.get("access_token")


def send_kakao_self_memo(text: str) -> bool:
    """카카오 self-memo 발송 (대표 본인 talk/memo/default/send).

    🔴 self-memo 만 — broadcast/타인 발송 endpoint 구조적 부재 (legal P0).
    200자 가드 (feed template 4줄 trim 사고 회피, FLR 2026-06-04 정합).
    실패 시 False (센서 fire 는 이미 osascript + triggers.jsonl 로 완료, graceful).
    """
    if len(text) > 200:
        print(
            f"[sensor] 카카오 텍스트 200자 초과 ({len(text)}) — send 중단",
            file=sys.stderr,
        )
        return False
    access_token = _kakao_refresh_token()
    if not access_token:
        return False
    template_object = {
        "object_type": "text",
        "text": text,
        "link": {"web_url": KAKAO_LINK_URL, "mobile_web_url": KAKAO_LINK_URL},
        "button_title": "PM320 보기",
    }
    data = urllib.parse.urlencode(
        {"template_object": json.dumps(template_object, ensure_ascii=False)}
    ).encode("utf-8")
    # S310: 고정 HTTPS kakao endpoint (사용자 입력 URL 아님 — false-positive)
    req = urllib.request.Request(KAKAO_MEMO_SEND_URL, data=data, method="POST")  # noqa: S310
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8")
        except Exception:  # noqa: BLE001
            err_body = "<unreadable>"
        print(
            f"[sensor] 카카오 send HTTP {exc.code}: {err_body[:200]}", file=sys.stderr
        )
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"[sensor] 카카오 send 실패: {type(exc).__name__}", file=sys.stderr)
        return False
    result_code = body.get("result_code", -1)
    if result_code == 0:
        print("[sensor] 카카오 self-memo 발송 OK (result_code=0)")
        return True
    print(f"[sensor] 카카오 send result_code={result_code}", file=sys.stderr)
    return False


def _read_usdkrw() -> str:
    """환율 권위 소스 read — macro_indicators.json indicators.usdkrw.value (Yahoo).

    🔴 LLM 이 환율 숫자를 지어내지 않도록 권위 소스에서 직접 read (2026-06-24 대표 catch).
    graceful: 파일/필드 부재·파싱 실패 시 빈 문자열 (환율 줄 생략). 절대 raise 안 함.
    반환 예: '원/달러 1536.98' (값 있을 때) / '' (없을 때).
    """
    try:
        d = json.loads(MACRO_INDICATORS_PATH.read_text())
        u = d.get("indicators", {}).get("usdkrw", {})
        val = u.get("value")
        if not isinstance(val, (int, float)):
            return ""
        label = u.get("label") or "원/달러"
        return f"{label} {val:.2f}"
    except Exception:  # noqa: BLE001 - 파일 부재/손상 시 환율 줄 생략 (graceful)
        return ""


def _sanitize_cause(raw: str) -> str:
    """LLM 원인 응답 후처리 — 첫 비어있지 않은 줄 + 마크다운/머리표 제거 + 길이 truncate.

    sonnet 이 마크다운(별표·인용·머리표)·출처 리스트·부가설명을 붙이는 경향(dry-run 확인) →
    카톡 200자 가드(send_kakao_self_memo) 통과 위해 1줄 순수 텍스트로 정규화.
    """
    for line in raw.splitlines():
        s = line.strip()
        # 마크다운 머리표/인용/출처 라벨 제거 후 평가
        s = s.lstrip("*->#·•—– \t").strip()
        if s.lower().startswith(("sources:", "참고:", "출처:", "http")):
            continue
        s = s.replace("**", "").replace("__", "").strip()
        if s:
            if len(s) > CAUSE_MAX_LEN:
                s = s[: CAUSE_MAX_LEN - 1].rstrip() + "…"
            return s
    return ""


def fetch_cause(breaches: list[tuple[str, float, float]]) -> str:
    """breach 시 실시간 뉴스 원인 1줄 도출 (claude CLI + WebSearch, 1콜).

    graceful: 실패/타임아웃/빈응답/rc≠0 → 빈 문자열 반환 (호출부가 원인 줄 생략하고
    기존 알림은 그대로 발송). 절대 예외 raise 하지 않는다 (알림 차단 금지).
    cache 우회 — 매 breach 실시간 검색 (call_model_cached 미사용).
    """
    if not breaches:
        return ""
    # 가장 큰 낙폭(절대값) 지수를 프롬프트 컨텍스트로 (코스피/코스닥 중)
    name, ret, _z = max(breaches, key=lambda b: abs(b[1]))
    direction = "급락" if ret < 0 else "급등"
    prompt = (
        f"한국 {name} 지수가 방금 10분간 약 {ret:+.1f}% {direction}했습니다. "
        "원인을 웹에서 한 번만 검색하고 즉시 답하세요.\n"
        "규칙(엄수): 원인을 80~110자 한국어 한 문장으로만 출력. 오직 원인 문장 한 줄만.\n"
        "🔴 숫자(환율·지수 수치·종목 등락률·금액·포인트)는 절대 지어내지 말 것. "
        "원인의 서술(왜)만 제공하라. 특정 수치가 필요하면 그 수치는 생략하고 서술로만 답하라.\n"
        "마크다운(별표·머리표·인용) 금지. 출처 URL 나열 금지. '참고:' 등 부가설명 금지. "
        "사실 위주, 불확실하면 문장 끝에 '(추정)'. 투자권유·헤지 문구 금지. 검색은 1회로 제한."
    )
    # --setting-sources user 의무 (FLR-20260511-DAT-002): cwd 의 회사 CLAUDE.md 페르소나
    #   inject 차단. project 로컬 설정/메모리 무시, user-level 만 로드.
    cmd = [
        CLAUDE_CMD,
        "--setting-sources",
        "user",
        "-p",
        prompt,
        "--allowed-tools",
        "WebSearch",
        "--model",
        CAUSE_MODEL,
    ]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=CAUSE_TIMEOUT_SEC
        )
    except subprocess.TimeoutExpired:
        print(
            f"[sensor] 원인 분석 timeout ({CAUSE_TIMEOUT_SEC}s) — 원인 줄 생략",
            file=sys.stderr,
        )
        return ""
    except Exception as e:  # noqa: BLE001 - 어떤 실패에도 알림은 발송 (graceful)
        print(f"[sensor] 원인 분석 실패 (무시): {type(e).__name__}", file=sys.stderr)
        return ""
    if r.returncode != 0 or not r.stdout.strip():
        print(
            f"[sensor] 원인 분석 rc={r.returncode} 빈응답 — 원인 줄 생략",
            file=sys.stderr,
        )
        return ""
    return _sanitize_cause(r.stdout)


def format_alert_line(name: str, ret: float, z: float) -> str:
    # 행동 지시어 0 — 조니 판정 메시지 포맷 고정
    return (
        f"[긴급 시장 분석 후보] {name} 10분 {ret:+.1f}% (z{abs(z):.1f}) — 조사 트리거"
    )


def format_kakao_alert(
    breaches: list[tuple[str, float, float]],
    fire_id: str,
    cause: str = "",
    usdkrw: str = "",
) -> str:
    """카카오 self-memo compact 텍스트 (200자 가드 정합, 행동 지시어 0).

    fire_id = 'HHMM' → 'HH:MM' 표기. breaches 1~2건 (코스피/코스닥).
    🔴 모든 숫자는 권위 소스 (2026-06-24 대표 catch): 지수=breach(센서) / 환율=usdkrw(Yahoo).
       cause(LLM) = 서술만, 숫자 미포함.
    🔴 형식 (2026-06-24 대표 지시): 지수 line 은 등락률만(z-score·"내부 센서"·헤지 모두 제거),
       마지막 줄은 시각만.
    cause = 실시간 뉴스 서술 원인 1줄 (빈 문자열이면 줄 생략). 지수 line 다음 삽입.
    usdkrw = '원/달러 1536.98' 형태 (빈 문자열이면 줄 생략).
    예) '🔴 PM320 시장 급변\n코스피 -2.09%\n원/달러 1536.98\n원인: ...\n14:40'
    """
    hhmm = f"{fire_id[:2]}:{fire_id[2:]}" if len(fire_id) == 4 else fire_id
    lines = ["🔴 PM320 시장 급변"]
    # 지수 line: 등락률만 (z-score 제거 — 2026-06-24 대표 지시). z 는 센서 내부 판정 주체로 유지.
    lines += [f"{name} {ret:+.2f}%" for name, ret, _z in breaches]
    if usdkrw:
        lines.append(usdkrw)
    if cause:
        lines.append(f"원인: {cause}")
    lines.append(hhmm)  # 마지막 줄 = 시각만 ("· 내부 센서" 제거 — 2026-06-24 대표 지시)
    return "\n".join(lines)


def run_live() -> int:
    now = now_kst()
    # 장중 가드 09:05~15:45 (15:40 fire가 1530 종가봉 평가) + 주말 가드 (launchd Weekday와 이중)
    if now.weekday() >= 5:
        return 0
    hm = now.hour * 100 + now.minute
    if not (905 <= hm <= 1545):
        return 0

    token = get_token(KIWOOM_BASE, KIWOOM_APPKEY, KIWOOM_SECRETKEY)
    today8 = now.strftime("%Y%m%d")
    fire_id = now.strftime("%H%M")

    state = load_state()
    if state.get("date") != today8:
        state = {"date": today8, "fires": 0, "last_fire_epoch": 0}

    breaches: list[tuple[str, float, float]] = []
    for name, inds_cd in INDEX_TARGETS:
        try:
            bars = fetch_index_minutes(token, inds_cd)
        except RuntimeError as e:
            # 실패 시 상태 기록 없음 → 다음 fire 자연 재시도 (cache-mark 함정 회피)
            print(f"[sensor] {name} 수집 실패: {e}", file=sys.stderr)
            continue
        days = returns_by_day(bars)
        if today8 not in days:
            # 휴장일 (당일 봉 없음) — 조용히 종료
            return 0
        completed = [
            (slot, ret)
            for slot, ret in days[today8]
            if slot_completed(today8, slot, now)
        ]
        if not completed:
            continue
        slot, ret = completed[-1]
        pool = baseline_pool(days, today8, BASELINE_DAYS)
        z = robust_z(ret, pool)
        breach = bool(
            z is not None and abs(z) >= Z_THRESHOLD and abs(ret) >= NOISE_FLOOR_PCT
        )
        append_jsonl(
            EVALS_PATH,
            {
                "ts": now.isoformat(timespec="seconds"),
                "fire": fire_id,
                "index": name,
                "bar": slot,
                "ret_pct": round(ret, 4),
                "z": round(z, 2) if z is not None else None,
                "baseline_n": len(pool),
                "breach": breach,
            },
        )
        if breach and z is not None:
            breaches.append((name, ret, z))

    if not breaches:
        return 0

    # 발사 게이트 (글로벌): 1일 2회 + 쿨다운 60분
    suppressed = None
    if state["fires"] >= MAX_FIRES_PER_DAY:
        suppressed = "daily_cap"
    elif time.time() - state.get("last_fire_epoch", 0) < COOLDOWN_MIN * 60:
        suppressed = "cooldown"

    lines = [format_alert_line(name, ret, z) for name, ret, z in breaches]
    record = {
        "ts": now.isoformat(timespec="seconds"),
        "fire": fire_id,
        "breaches": [
            {"index": n, "ret_pct": round(r, 4), "z": round(z, 2)}
            for n, r, z in breaches
        ],
        "message": " / ".join(lines),
        "suppressed": suppressed,
        "fires_today_before": state["fires"],
    }
    append_jsonl(TRIGGERS_PATH, record)

    if suppressed:
        print(f"[sensor] 트리거 억제({suppressed}): {record['message']}")
        return 0

    state["fires"] += 1
    state["last_fire_epoch"] = time.time()
    save_state(state)
    LAST_ALERT_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAST_ALERT_PATH.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    notify_macos("100m1s 시장 센서", " / ".join(lines))
    # 실시간 뉴스 원인 1줄 도출 (발사 확정 후 1회만). 실패 시 빈 문자열 → 원인 줄 생략.
    #   macOS 알림·jsonl 기록은 이미 위에서 완료 → 원인 분석이 늦거나 실패해도 알림 자체는 보존.
    cause = fetch_cause(breaches)
    # 카카오 self-memo 발송 (대표 본인 전용). 실패해도 fire 는 osascript + 기록으로 완료.
    #   compact 텍스트 (osascript "[긴급...조사 트리거]" 보다 짧게, 200자 가드 정합).
    # 환율 = 권위 소스 read (LLM 아님, 2026-06-24 대표 catch). 부재 시 빈 문자열 → 줄 생략.
    #   숫자는 모두 권위 소스: 지수=breach(센서), 환율=usdkrw(Yahoo), cause(LLM)=서술만.
    usdkrw = _read_usdkrw()
    kakao_text = format_kakao_alert(breaches, fire_id, cause, usdkrw)
    send_kakao_self_memo(kakao_text)
    print(f"[sensor] 트리거 발사: {record['message']}")
    return 0


def simulate_day(
    days: dict[str, list[tuple[str, float]]],
    eval_date: str,
    threshold: float,
    verbose: bool = False,
) -> list[dict]:
    """fire 그리드(매 10분) 재현 — look-ahead 없이 당일 트리거 시퀀스 산출."""
    pool = baseline_pool(days, eval_date, BASELINE_DAYS)
    fires = 0
    last_fire_min = -(10**9)
    triggers: list[dict] = []
    for slot, ret in days.get(eval_date, []):
        # slot 봉 완성 직후 fire 시각 = slot + 10분
        fire_min = int(slot[:2]) * 60 + int(slot[2:]) + 10
        fire_hm = f"{fire_min // 60:02d}:{fire_min % 60:02d}"
        z = robust_z(ret, pool)
        breach = bool(
            z is not None and abs(z) >= threshold and abs(ret) >= NOISE_FLOOR_PCT
        )
        if verbose and z is not None and abs(z) >= 3.0:
            print(f"  {fire_hm} fire: bar {slot} ret {ret:+.2f}% z {z:+.1f}")
        if not breach or z is None:
            continue
        suppressed = None
        if fires >= MAX_FIRES_PER_DAY:
            suppressed = "daily_cap"
        elif fire_min - last_fire_min < COOLDOWN_MIN:
            suppressed = "cooldown"
        if not suppressed:
            fires += 1
            last_fire_min = fire_min
        triggers.append(
            {
                "fire": fire_hm,
                "bar": slot,
                "ret_pct": round(ret, 2),
                "z": round(z, 1),
                "suppressed": suppressed,
            }
        )
    return triggers


def run_backtest(date8: str, threshold: float) -> int:
    token = get_token(KIWOOM_BASE, KIWOOM_APPKEY, KIWOOM_SECRETKEY)
    for name, inds_cd in INDEX_TARGETS:
        days = returns_by_day(fetch_index_minutes(token, inds_cd))
        if date8 not in days:
            print(f"== {name}: {date8} 봉 없음 (휴장?)")
            continue
        pool = baseline_pool(days, date8, BASELINE_DAYS)
        print(f"== {name} {date8} (z임계 {threshold}, 베이스라인 {len(pool)}표본)")
        triggers = simulate_day(days, date8, threshold, verbose=True)
        live = [t for t in triggers if not t["suppressed"]]
        for t in triggers:
            tag = f" [억제:{t['suppressed']}]" if t["suppressed"] else " [발사]"
            print(
                f"  TRIGGER {t['fire']} bar {t['bar']} {t['ret_pct']:+.2f}% z{t['z']:+.1f}{tag}"
            )
        first = live[0]["fire"] if live else "없음"
        print(
            f"  → 첫 발사: {first} / 발사 {len(live)}회, 억제 {len(triggers) - len(live)}회"
        )
    return 0


def run_calibrate(days_window: int) -> int:
    token = get_token(KIWOOM_BASE, KIWOOM_APPKEY, KIWOOM_SECRETKEY)
    thresholds = [3.0, 4.0, 5.0, 6.0, 8.0, 10.0]
    for name, inds_cd in INDEX_TARGETS:
        days = returns_by_day(fetch_index_minutes(token, inds_cd))
        dates = sorted(days)[-days_window:]
        print(f"== {name} 최근 {len(dates)}거래일 임계 스윕 (발사/breach 총수)")
        header = "date     " + "".join(f"  z>={t:<4}" for t in thresholds)
        print(header)
        for d in dates:
            cells = []
            for t in thresholds:
                trig = simulate_day(days, d, t)
                live = sum(1 for x in trig if not x["suppressed"])
                cells.append(f"  {live}/{len(trig):<4}")
            print(f"{d}" + "".join(cells))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="코스피·코스닥 10분 이상치 센서 (조니 1단계)"
    )
    p.add_argument(
        "--backtest", metavar="YYYYMMDD", help="해당일 재현 — 첫 트리거 시각"
    )
    p.add_argument("--calibrate", action="store_true", help="임계 스윕 오탐 시뮬")
    p.add_argument("--days", type=int, default=7, help="--calibrate 윈도우 (거래일)")
    p.add_argument("--threshold", type=float, default=Z_THRESHOLD)
    args = p.parse_args()
    if args.backtest:
        return run_backtest(args.backtest, args.threshold)
    if args.calibrate:
        return run_calibrate(args.days)
    return run_live()


if __name__ == "__main__":
    sys.exit(main())
