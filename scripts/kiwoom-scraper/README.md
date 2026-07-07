# 키움 '500억이상' 조건검색 스크레이퍼

매 10분 (KST 10:00 ~ 21:50) 키움 OpenAPI 조건검색 '500억이상'을 실행하여
거래대금 500억+ 종목 목록을 수집한다.

## 파이프라인

```
GitHub Actions cron (*/10 1-12 * * 1-5, UTC)
  ↓
main.py
  ├─ 1. 키움 토큰 발급 (REST au10001)
  ├─ 2. 조건검색 목록 조회 (WebSocket ka10171)
  ├─ 3. '500억이상' 이름 매칭 → seq 추출
  ├─ 4. 조건검색 실행 (WebSocket ka10172)
  ├─ 5. 종목 파싱 + 거래대금 desc 정렬 + rank 부여
  └─ 6. data/kiwoom/<date>.json 누적 갱신 + latest.json + index.json
  ↓
git commit + push (newzy-bot)
  ↓
news.html 가 fetch → 날짜 그룹 위에 "거래대금 500억+ 상위 종목" 표시
```

## 사전 준비 (대표가 직접)

### 1. 영웅문에서 조건검색 등록
- 영웅문4 → 조건검색 → 새 조건식 만들기
- 이름: 정확히 **`500억이상`** (스크레이퍼가 이름으로 매칭)
- 조건: 거래대금 500억 이상 + 원하는 추가 필터
- 저장 → 클라우드 동기화 (API에서 조회 가능하려면 클라우드 저장 필수)

### 2. GitHub repo secrets 추가
`https://github.com/nicehugepark/100m1s-homepage/settings/secrets/actions`

| Secret | 값 |
|--------|-----|
| `KIWOOM_APPKEY` | 키움 OpenAPI 앱키 |
| `KIWOOM_SECRETKEY` | 시크릿키 |
| `KIWOOM_BASE_URL` | `https://mockapi.kiwoom.com` (모의) 또는 `https://api.kiwoom.com` (실전) |

기존 시크릿은 그대로 두면 되고 위 3개만 추가.

## 출력 스키마

### `data/kiwoom/<YYYY-MM-DD>.json`
```json
{
  "date": "2026-04-08",
  "condition_name": "500억이상",
  "first_snapshot_at": "2026-04-08T10:00:01+09:00",
  "last_snapshot_at": "2026-04-08T20:50:01+09:00",
  "snapshot_count": 66,
  "latest_stocks": [
    {
      "rank": 1,
      "ticker": "005930",
      "name": "삼성전자",
      "price": 75000,
      "change_pct": 2.34,
      "trade_amount": 152300000000
    }
  ],
  "daily_top": [
    {
      "ticker": "005930",
      "name": "삼성전자",
      "max_trade_amount": 152300000000,
      "max_change_pct": 2.34,
      "min_change_pct": 0.5,
      "first_seen": "10:00",
      "last_seen": "20:50",
      "appearances": 66,
      "last_price": 75000
    }
  ],
  "accumulated_stocks": {}
}
```

### `data/kiwoom/latest.json`
```json
{
  "date": "2026-04-08",
  "fetched_at": "...",
  "snapshot_count": 66,
  "stocks": [...]  // 최근 스냅샷의 top 30
}
```

### `data/kiwoom/index.json`
```json
{
  "dates": ["2026-04-08", "2026-04-07", ...],
  "updated_at": "..."
}
```

## 로컬 실행

```bash
cd scripts/kiwoom-scraper
pip install -r requirements.txt
export KIWOOM_APPKEY=...
export KIWOOM_SECRETKEY=...
export KIWOOM_BASE_URL=https://mockapi.kiwoom.com
python main.py
```

## 알려진 위험

1. **조건검색 미등록** — 영웅문에서 클라우드 저장 안 했으면 API에서 안 보임. main.py가 등록된 조건식 목록을 출력하니 확인 가능.
2. **WebSocket 연결 불안정** — 키움 WebSocket 가끔 끊김. main.py는 단일 시도 후 종료. 다음 cron(10분 후)이 재시도.
3. **장 시간외 결과 0건** — 정상. 0건 반환 시 파일 변경 없음.
4. **모의 vs 실전 키 만료** — `agents/jooju/...` 또는 메모리 참조. 만료 2주 전 알림 필수.
5. **commit 빈도** — 평일 12시간 × 6/시간 = 72 commit/일. 누적 빠름. 필요 시 batch commit으로 전환.
6. **rate limit** — 키움 OpenAPI 분당 호출 제한 있음. 10분 1회는 안전.

## TODO

- [ ] 같은 종목이 카페 게시글과 키움 둘 다 등장 시 cross-link 표시
- [ ] 시간대별 거래대금 변화 그래프 (intraday chart)
- [ ] 종목별 알림 (특정 임계값 돌파 시 텔레그램 봇)
- [ ] 종목명 → 카페 뉴스 자동 매칭
