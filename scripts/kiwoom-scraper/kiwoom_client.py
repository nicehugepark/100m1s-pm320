"""
키움증권 REST + WebSocket API 클라이언트
PM320 POC - 토큰 발급, 조건검색(WebSocket), 시세 조회(REST) 검증

키움 API 프로토콜:
  - 시세/종목정보/주문/계좌: REST (POST https://[mock]api.kiwoom.com/api/dostk/...)
  - 조건검색/실시간시세:     WebSocket (wss://[mock]api.kiwoom.com:10000/api/dostk/websocket)

WebSocket 조건검색 호출 순서 (필수):
  1. LOGIN (토큰 전송)
  2. CNSRLST (조건검색 목록 조회 — 반드시 검색 전 1회 호출)
  3. CNSRREQ (조건검색 실행)
"""

import asyncio
import json
import os
import ssl

import requests

BASE_URL = os.getenv("KIWOOM_BASE_URL", "https://mockapi.kiwoom.com")
APPKEY = os.getenv("KIWOOM_APPKEY")
SECRETKEY = os.getenv("KIWOOM_SECRETKEY")


class KiwoomClient:
    def __init__(self):
        self.base_url = BASE_URL
        self.appkey = APPKEY
        self.secretkey = SECRETKEY
        self.token = None
        self.token_expires = None

    def _rest_headers(self, api_id):
        """REST API 공통 헤더"""
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "api-id": api_id,
        }
        if self.token:
            headers["authorization"] = f"Bearer {self.token}"
        return headers

    @property
    def _ws_url(self):
        """WebSocket URL 생성 (REST 도메인에서 파생)"""
        domain = self.base_url.replace("https://", "").replace("http://", "")
        return f"wss://{domain}:10000/api/dostk/websocket"

    # ═══════════════════════════════════════════════════════
    # REST API
    # ═══════════════════════════════════════════════════════

    def get_token(self):
        """접근토큰 발급 (au10001)"""
        url = f"{self.base_url}/oauth2/token"
        body = {
            "grant_type": "client_credentials",
            "appkey": self.appkey,
            "secretkey": self.secretkey,
        }
        resp = requests.post(
            url, json=body, headers={"Content-Type": "application/json;charset=UTF-8"}
        )
        data = resp.json()

        if resp.status_code == 200 and "token" in data:
            self.token = data["token"]
            self.token_expires = data.get("expires_dt")
            print(f"[토큰] 발급 성공 (만료: {self.token_expires})")
        else:
            print(f"[토큰] 발급 실패: {data}")

        return data

    def revoke_token(self):
        """접근토큰 폐기 (au10002)"""
        url = f"{self.base_url}/oauth2/revoke"
        body = {
            "token": self.token,
            "appkey": self.appkey,
            "secretkey": self.secretkey,
        }
        resp = requests.post(url, json=body)
        print(f"[토큰] 폐기: {resp.json().get('return_msg', '')}")
        return resp.json()

    def get_stock_info(self, stk_cd):
        """주식기본정보요청 (ka10001) — 현재가, 등락률, 거래량 등"""
        url = f"{self.base_url}/api/dostk/stkinfo"
        resp = requests.post(
            url,
            json={"stk_cd": stk_cd},
            headers=self._rest_headers("ka10001"),
        )
        return resp.json()

    def get_daily_chart(self, stk_cd, count=240):
        """일봉 데이터 조회 (ka10081) — MA/MACD/RSI 계산용

        Returns:
            list: [{"date","open","high","low","close","volume"}, ...] 최근 N일
        """
        url = f"{self.base_url}/api/dostk/chart"
        resp = requests.post(
            url,
            json={"stk_cd": stk_cd, "period": "D", "count": str(count)},
            headers=self._rest_headers("ka10081"),
        )
        data = resp.json()
        if data.get("return_code") != 0:
            return []
        return data.get("data", [])

    def get_today_trade_amount(self, stk_cd):
        """오늘 일봉 row의 trde_prica (거래대금) 반환 — ka10081.

        Q-20260519-CYCLE11-004 본질 fix: ka10172 조건검색 응답 field 14
        (거래대금)가 영구 부재(빈 문자열) → ka10081 추가 호출로 정합화.

        패턴: LU collector (collect_kiwoom_limit_up.py
        fetch_dailybars_trade_amount) 동형 — cycle11 Q-001과 동일 구조.

        Args:
            stk_cd: 종목코드 6자리 ("001430") 또는 "A001430" 형식.

        Returns:
            int: trde_prica 원 단위 (백만원 × 1_000_000). 실패/부재 시 None.

        근거: ka10081 trde_prica는 KRX 정식 누적 거래대금 (원본).
        price × volume 단순곱은 평균체결가 ≠ cur_prc인 경우 부정확
        (cycle11 5/19 영웅문 catch 26억 차이 본질 검증).
        """
        import time as _time
        from datetime import datetime as _dt

        code = stk_cd if str(stk_cd).startswith("A") else f"A{stk_cd}"
        today = _dt.now().strftime("%Y%m%d")
        url = f"{self.base_url}/api/dostk/chart"
        # LU collector 동형 retry — 429 rate limit 시 2^attempt 백오프 (1s,2s,4s,8s)
        for attempt in range(4):
            try:
                resp = requests.post(
                    url,
                    json={"stk_cd": code, "base_dt": today, "upd_stkpc_tp": "1"},
                    headers=self._rest_headers("ka10081"),
                    timeout=20,
                )
            except Exception:
                return None
            if resp.status_code == 429:
                _time.sleep(2 ** (attempt + 1))
                continue
            if resp.status_code != 200:
                return None
            data = resp.json()
            if data.get("return_code") != 0:
                return None
            rows = data.get("stk_dt_pole_chart_qry", [])
            for r in rows:
                if r.get("dt") == today:
                    try:
                        return int(r.get("trde_prica", "0")) * 1_000_000
                    except (ValueError, TypeError):
                        return None
            return None
        return None

    def get_stock_list(self, mrkt_tp="0"):
        """종목정보 리스트 (ka10099) — 0:코스피, 10:코스닥"""
        url = f"{self.base_url}/api/dostk/stkinfo"
        resp = requests.post(
            url,
            json={"mrkt_tp": mrkt_tp},
            headers=self._rest_headers("ka10099"),
        )
        return resp.json()

    def get_trade_amount_ranking(self, mrkt_tp="000", pages=2):
        """거래대금상위 (ka10032) — 전 시장, 관리종목 제외

        Args:
            mrkt_tp: "000"=전체, "001"=코스피, "101"=코스닥
            pages: 페이지 수 (1페이지=100건)

        Returns:
            list: [{"stk_cd", "stk_nm", "cur_prc", "flu_rt", "trde_prica", ...}, ...]
        """
        all_stocks = []
        headers = self._rest_headers("ka10032")
        body = {"mrkt_tp": mrkt_tp, "mang_stk_incls": "0", "stex_tp": "1"}

        for _page in range(pages):
            resp = requests.post(
                f"{self.base_url}/api/dostk/rkinfo",
                json=body,
                headers=headers,
            )
            data = resp.json()
            stocks = data.get("trde_prica_upper", [])
            all_stocks.extend(stocks)
            # 연속조회
            if resp.headers.get("cont-yn") == "Y" and resp.headers.get("next-key"):
                headers["cont-yn"] = "Y"
                headers["next-key"] = resp.headers["next-key"]
            elif data.get("cont-yn") == "Y" and data.get("next-key"):
                headers["cont-yn"] = "Y"
                headers["next-key"] = data["next-key"]
            else:
                break

        return all_stocks

    # ═══════════════════════════════════════════════════════
    # WebSocket API — 조건검색
    # ═══════════════════════════════════════════════════════

    async def _ws_connect(self):
        """WebSocket 연결 + SSL 설정"""
        try:
            import websockets
        except ImportError:
            raise RuntimeError("websockets 패키지 필요: pip install websockets")

        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        return websockets.connect(self._ws_url, ssl=ssl_ctx, open_timeout=15)

    async def ws_condition_list(self):
        """조건검색 목록 조회 (ka10171, WebSocket)

        Returns:
            list: [[seq, name], ...] 형태의 조건검색식 목록
        """
        async with await self._ws_connect() as ws:
            # 1. LOGIN
            await ws.send(json.dumps({"trnm": "LOGIN", "token": self.token}))
            login = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if login.get("return_code") != 0:
                raise RuntimeError(f"WebSocket LOGIN 실패: {login}")

            # 2. CNSRLST
            await ws.send(json.dumps({"trnm": "CNSRLST"}))
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if resp.get("return_code") != 0:
                raise RuntimeError(f"조건검색 목록 조회 실패: {resp}")

            return resp.get("data", [])

    async def ws_condition_search(self, seq, search_type="0", stex_tp="K"):
        """조건검색 실행 (ka10172, WebSocket)

        호출 순서: LOGIN → CNSRLST → CNSRREQ (순서 필수)

        Args:
            seq: 조건검색식 일련번호 (문자열)
            search_type: "0"=일반검색, "1"=일반+실시간
            stex_tp: "K"=KRX

        Returns:
            list: [{"9001": 종목코드, "302": 종목명, "10": 현재가, ...}, ...]
        """
        async with await self._ws_connect() as ws:
            # 1. LOGIN
            await ws.send(json.dumps({"trnm": "LOGIN", "token": self.token}))
            login = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if login.get("return_code") != 0:
                raise RuntimeError(f"WebSocket LOGIN 실패: {login}")

            # 2. CNSRLST (검색 전 반드시 호출)
            await ws.send(json.dumps({"trnm": "CNSRLST"}))
            await asyncio.wait_for(ws.recv(), timeout=10)

            # 3. CNSRREQ
            req = {
                "trnm": "CNSRREQ",
                "seq": str(seq),
                "search_type": search_type,
                "stex_tp": stex_tp,
                "cont_yn": "N",
                "next_key": "",
            }
            await ws.send(json.dumps(req))

            all_stocks = []
            while True:
                try:
                    r = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                except TimeoutError:
                    break

                if r.get("return_code") not in (0, None):
                    raise RuntimeError(f"조건검색 실패: {r}")

                stocks = r.get("data", [])
                if isinstance(stocks, list):
                    all_stocks.extend(stocks)

                # 연속조회
                if r.get("cont_yn") == "Y" and r.get("next_key"):
                    cont_req = {**req, "cont_yn": "Y", "next_key": r["next_key"]}
                    await ws.send(json.dumps(cont_req))
                else:
                    break

            return all_stocks

    def condition_list(self):
        """조건검색 목록 (동기 래퍼)"""
        return asyncio.run(self.ws_condition_list())

    def condition_search(self, seq, **kwargs):
        """조건검색 실행 (동기 래퍼)"""
        return asyncio.run(self.ws_condition_search(seq, **kwargs))


def parse_kiwoom_int(val):
    """키움 API 숫자 문자열 파싱 (부호, 패딩 제거)"""
    if not val:
        return 0
    return int(val.replace("+", "").replace("-", "").lstrip("0") or "0")


def run_poc():
    """POC 전체 플로우 실행"""
    from datetime import datetime

    print("=" * 60)
    print("PM320 키움 REST + WebSocket API POC")
    print(f"실행 시각: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"REST: {BASE_URL}")
    print("=" * 60)

    if not APPKEY or not SECRETKEY:
        print("\nKIWOOM_APPKEY, KIWOOM_SECRETKEY를 .env에 설정하세요.")
        return

    client = KiwoomClient()

    # Step 1: 토큰 발급
    print("\n─── STEP 1: 토큰 발급 ───")
    client.get_token()
    if not client.token:
        return

    # Step 2: 조건검색 목록 (WebSocket)
    print("\n─── STEP 2: 조건검색 목록 (WebSocket ka10171) ───")
    conditions = client.condition_list()
    print(f"  조건식 {len(conditions)}개")
    for seq, name in conditions:
        print(f"    [{seq}] {name}")

    # Step 3: 조건검색 실행 (첫 번째 조건식)
    if conditions:
        seq, name = conditions[0]
        print(f"\n─── STEP 3: 조건검색 실행 [{seq}] {name} (WebSocket ka10172) ───")
        stocks = client.condition_search(seq)
        print(f"  결과: {len(stocks)}종목")
        for s in stocks[:5]:
            code = s.get("9001", "").replace("A", "")
            print(f"    {code} {s.get('302', '')}")

    # Step 4: 시세 조회 (REST)
    print("\n─── STEP 4: 시세 조회 (REST ka10001) ───")
    info = client.get_stock_info("005930")
    if info.get("return_code") == 0:
        print(f"  {info['stk_nm']} 현재가: {info['cur_prc']}원 ({info['flu_rt']}%)")

    # 정리
    print("\n─── CLEANUP ───")
    client.revoke_token()
    print("\nPOC 완료")


if __name__ == "__main__":
    run_poc()
