/* ───── data-loader.js — 데이터 fetch/캐시 ───── */

// ── KST 시각 헬퍼 (FLR-20260624-TEC: 해외 접속 시 시장-시간 로직 로컬 TZ 오판 봉쇄) ──
//   대표가 해외(로컬 TZ ≠ KST)에서 접속 시, 시장-시간 로직(getMarketState·15:20 픽 공개·"오늘"
//   판정·캐시 키·_dataBust·픽 공개 폴링)이 브라우저 로컬 시각(new Date().getHours() 등)으로 계산되어
//   "아직 15:20 전"으로 오판 → 오늘 카드 fetch skip + pending 표시 → 픽 미공개.
//   _kstNow(now): 브라우저 로컬 TZ 무관하게, 반환 Date 의 로컬 필드 메서드(getHours/getFullYear/...)가
//   KST wall-clock 을 반환하도록 보정한 Date. (now.getTime() + (540 + getTimezoneOffset())*60000)
//     - KST 브라우저(offset −540): 가산 0 → 무변화(무회귀).
//     - US Eastern(offset +240): 가산 +780분 → get*() = KST wall-clock.
//   검증: TZ=Asia/Seoul·America/New_York·America/Los_Angeles·UTC 4종에서 get*() 동일 KST 시각 확인.
//   사용처: 시장-시간 용도의 now||new Date() / new Date() 를 _kstNow(now) / _kstNow() 로 치환.
//   호출부 시그니처 불변(함수 내부 변환). renderer.js 기존 KST-correct 패턴(nowMs+9h+getUTC*)은
//   _kstNow().get*() 와 동치이므로 그대로 유지(중복 교체 금지).
//   주의: new Date(y, m, d, H, M, ...) 생성자에 _kstNow() 의 get*() 를 넘기면 로컬 TZ 로 해석되지만,
//   비교 대상 _now 도 동일 좌표계의 가짜 시각이라 target.getTime()-_now.getTime() 차이는 KST 기준 정확.
function _kstNow(now) {
  const _n = now || new Date();
  return new Date(_n.getTime() + (540 + _n.getTimezoneOffset()) * 60000);
}
if (typeof window !== 'undefined') { window._kstNow = _kstNow; }

let themesData = null;
let _themeTreeCache = null;

async function loadThemes() {
  try {
    const res = await fetch('/data/themes/themes.json');
    if (!res.ok) throw new Error('themes.json HTTP ' + res.status);
    return await res.json();
  } catch (e) { return null; }
}


// v6 (묶음 3): kiwoom index 캐시 — 404 소음 제거
let _kiwoomIndexCache = null;
let _kiwoomIndexPromise = null;

async function loadKiwoomIndex() {
  if (_kiwoomIndexCache) return _kiwoomIndexCache;
  if (_kiwoomIndexPromise) return _kiwoomIndexPromise;
  _kiwoomIndexPromise = (async () => {
    try {
      const res = await fetch('/data/kiwoom/index.json');
      if (!res.ok) throw new Error('kiwoom/index.json HTTP ' + res.status);
      const d = await res.json();
      _kiwoomIndexCache = d;
      return d;
    } catch (e) {
      return null;
    } finally {
      _kiwoomIndexPromise = null;
    }
  })();
  return _kiwoomIndexPromise;
}

// DSN-frontend §3.6.2.2 (2026-05-28 대표 직접 발화) — 오늘 view + 장 시작 후 어제 데이터 fallback 차단.
// 대표 verbatim: "9시부터 종목검색 결과가 있을 때까지 어제걸 보여주는게 과연 맞는걸까? 무슨 이점이 있지?"
//                "없으면 없다고 알려주는게 더 신뢰도나 활용면에서 좋자않나?"
// 본 helper — date === today (KST) + 09:00 이후 일 때 true.
// PRE_MARKET (09:00 미만)은 renderPreMarketEmpty의 사용자 opt-in 토글 path가 별도로 처리하므로 본 guard 대상 아님.
// 과거 viewDate (date < today)는 정상적으로 해당일 데이터 표시 (fallback 자체가 부적절).
// timezone: 브라우저 로컬 가정 (KST 사용자 기준). 5/27 freshness 라벨 _computeFreshnessLabel과 동일 가정.
function _isTodayPastOpen(date) {
  const now = _kstNow(); // 해외 접속 TZ 무관 KST wall-clock (get*() = KST)
  const y = now.getFullYear();
  const m = String(now.getMonth() + 1).padStart(2, '0');
  const d = String(now.getDate()).padStart(2, '0');
  const todayKst = `${y}-${m}-${d}`;
  if (date !== todayKst) return false;
  return now.getHours() >= 9; // KST 장 시작 시각
}

// fix/pick-reveal (2026-06-12) — 일자 데이터 fetch cache-bust 토큰.
//   평시: 날짜 고정 해시(YYYYMMDD) → 같은 URL 재사용으로 CDN 캐시 활용 (기존 동작 동일).
//   픽 공개 감지(renderer.js _startPickRevealPoll) 후: window._pm320PickRevealBust 부착 → 신규 URL 로
//   Pages CDN(max-age 600)·브라우저 HTTP 캐시 동시 우회. 같은 URL 재요청은 픽 이전 stale JSON 을
//   최대 10분 서빙하므로(location.reload 도 동일) URL 자체를 바꿔야 한다 (FLR-20260605-TEC-001 동형).
//   sw.js 데이터 fetch 는 network-first 라 본 토큰과 독립 (이중 안전).
function _dataBust(date) {
  const base = date.replace(/-/g, '');
  const b = (typeof window !== 'undefined' && window._pm320PickRevealBust) ? window._pm320PickRevealBust : null;
  if (b) return `${base}-${b}`;
  // 보유픽(running) current_pnl 은 장중 10분 단위로 갱신되는데, prior-day 카드가 안정 dateHash 로 캐시되면
  //   브라우저가 옛 카드를 서빙(보유픽 손익 stale·% 누락). 현재 시각이 장중(OPEN)이면 10분 버킷을 붙여 refetch.
  //   (2026-06-23 대표 catch — 제주반도체 등락률 미표시·10분 준실시간 미갱신. 모든 카드 공통 — 카드 작음)
  try {
    const _n = _kstNow(); // KST wall-clock — 해외 접속 시 OPEN 오판 봉쇄
    const _today = `${_n.getFullYear()}-${String(_n.getMonth() + 1).padStart(2, '0')}-${String(_n.getDate()).padStart(2, '0')}`;
    if (date === _today) {
      // 당일 데이터 = 가변(픽 공개·재판정·인트라데이). 60초 버킷으로 상시 신선 fetch —
      // 고정 dateHash + Pages max-age=600 조합이 픽 공개를 최대 10분 stale 노출하던 결함 봉쇄
      // (2026-07-07 16:00 리허설 실측, FLR-20260707-TEC-003 후속. 과거 날짜는 불변이라 base 유지).
      return `${base}-s${Math.floor(Date.now() / 60000)}`;
    }
  } catch (_) { /* graceful — 기본 dateHash */ }
  return base;
}

async function loadKiwoomDate(date) {
  const dateHash = _dataBust(date);
  // v6: index 선행 조회하여 존재하지 않는 날짜는 fetch 자체를 건너뛴다 (404 소음 제거).
  const idx = await loadKiwoomIndex();
  const idxDates = idx && Array.isArray(idx.dates) ? idx.dates : null;
  const dateExists = idxDates ? idxDates.includes(date) : true; // index 없으면 기존 동작 유지
  if (dateExists) {
    try {
      const res = await fetch(`/data/kiwoom/${date}.json?v=${dateHash}`);
      if (res.ok) return await res.json();
    } catch (e) { /* fall through */ }
  }
  // 폴백 1: stock-*.json에서 종목 리스트 추출 (당일)
  // R18 P0 (콘솔 404 0err화) — PRE_MARKET + 오늘 view 시 오늘 interpreted JSON 은 미생성(확정 404).
  //   kiwoom 본파일도 없고 본 fallback 도 확정 404 → 둘 다 건너뛴다 (장전엔 본 데이터 미사용).
  // R51 P2 (정직성 적발 — 휴장 콘솔 404) — 휴장일(주말/KRX 휴장)은 거래 자체가 없어 stock-{date}.json
  //   확정 부재 → fetch 시 콘솔 빨간 404. isMarketClosed(date) 시(과거/오늘 무관) 본 fallback 생략해
  //   콘솔 청정. loadCalDayData L379~387 의 _closedMarket interpreted 생략과 동형(휴장 데이터 무존재 전제).
  let _kiwoomStockSkip = false;
  try {
    const _n = _kstNow(); // KST wall-clock — 해외 접속 시 오늘/PRE_MARKET 오판 봉쇄
    const _t = `${_n.getFullYear()}-${String(_n.getMonth() + 1).padStart(2, '0')}-${String(_n.getDate()).padStart(2, '0')}`;
    const _preMarket = (date === _t) && (typeof getMarketState === 'function') && getMarketState(date, _n) === 'PRE_MARKET';
    const _closed = (typeof isMarketClosed === 'function') && isMarketClosed(date);
    _kiwoomStockSkip = _preMarket || _closed;
  } catch (_) { _kiwoomStockSkip = false; }
  try {
    const fb = _kiwoomStockSkip ? null : await fetch(`/data/interpreted/stock-${date}.json?v=${dateHash}`);
    if (fb && fb.ok) {
      const d = await fb.json();
      if (d.stocks && d.stocks.length > 0) {
        return { daily_top: d.stocks.map(s => ({
          ticker: s.code, name: s.name, rank: s.rank,
          max_trade_amount: s.trade_amount, max_change_pct: s.change_pct
        })) };
      }
    }
  } catch (e) { /* ignore */ }
  // 폴백 2: index의 가장 최근 날짜로 kiwoom 데이터 폴백 (오늘 파이프라인 수집 전)
  // DSN-frontend §3.6.2.2 (2026-05-28) — 오늘 view + 09:00 이후 시 본 fallback 차단.
  //   기존 동작 (어제 데이터 표시) → 사용자 매매 판단 misread risk.
  //   휴장일/과거 viewDate에 한해 fallback 유지 (해당 날짜에 데이터 부재 시 가장 가까운 데이터 보충).
  if (!dateExists && idxDates && idxDates.length > 0 && !_isTodayPastOpen(date)) {
    const latest = [...idxDates].sort().pop();
    if (latest && latest !== date) {
      try {
        const latestHash = latest.replace(/-/g, '');
        const res = await fetch(`/data/kiwoom/${latest}.json?v=${latestHash}`);
        if (res.ok) {
          const d = await res.json();
          d._fallback_date = latest;
          return d;
        }
      } catch (e) { /* ignore */ }
    }
  }
  return null;
}

async function loadCalendarIndex() {
  try {
    const res = await fetch('/data/calendar/index.json');
    if (!res.ok) return null;
    return await res.json();
  } catch (e) { return null; }
}

async function loadHolidayData() {
  try {
    const res = await fetch('/data/holidays.json');
    if (!res.ok) return null;
    return await res.json();
  } catch (e) { return null; }
}

// DOC-20260603-DSN-001 §1 — PM320 추천/결과 데이터 fetch (별 path 격리).
// 본문: build_card_history.py + backfill_card_history.py 산출 (메인 worktree projects/pm320/data/history/)
//       → cron worktree 미러 /data/pm320_history/{date}.json 사이트 도메인 serve.
// 404 graceful (PICK 부재 일자) → null. Phase 1 dev 위임 본문 (2026-06-03 dev-pm320-frontend-implementation).
async function loadPm320History(date) {
  const dateHash = _dataBust(date);  // fix/pick-reveal — 픽 공개 후 신선 fetch (평시 날짜 해시 동일)
  try {
    const res = await fetch(`/data/pm320_history/${date}.json?v=${dateHash}`);
    if (!res.ok) return null;
    return await res.json();
  } catch (e) { return null; }
}

// PM320-D6 (손님 판정 R1, 대표 결정 2026-06-10) — 4/8 이후 PICK 승률 summary.
//   build_card_history.py build_summary() 산출 (서빙 history 전수 집계, 매일 15:20 자동 갱신).
//   schema: { since, total_picks, settled, running, take_profit, expired_loss, expired_gain, win_rate, _basis }
//   MDD 병기(2026-06-10, 손님 정직성): { worst_mdd_pct, avg_mdd_pct, take_profit_target_pct } 선택 필드.
//     이 셋은 history 전수 transversal 산출값(하드코딩 0) — 부재/비-number 시 MDD 줄만 생략(승률 카드는 렌더).
//   404/파싱 실패/필수 필드 부재 시 null → 승률 카드 미렌더 (FLR-AGT-002 거짓 충실성 차단 — 추정 표시 금지).
async function loadPm320Summary() {
  try {
    const res = await fetch(`/data/pm320_history/summary.json?v=${Date.now()}`);
    if (!res.ok) return null;
    const d = await res.json();
    // schema validation — 승률 산출 핵심 필드 존재 + 타입 확인 (캐시 오염·구버전 차단).
    if (!d || typeof d.settled !== 'number' || typeof d.take_profit !== 'number'
        || typeof d.win_rate !== 'number') {
      return null;
    }
    return d;
  } catch (e) { return null; }
}

// Q-20260605-103 Phase 3 — 야간 미국증시 요약 (us-indices/{kstDate}.json).
//   DSN §3.6.9. 부재/파싱 실패/schema 불일치 시 null → 섹션 전체 미렌더 (FLR-AGT-002 거짓 충실성 차단).
//   schema: { trade_date_local, indices:[{name, point, change_pct, spark[], candle:{o,h,l,c}}], news_chips:[{summary, source, url}] }
//
// Q-20260608-133 (FLR-20260606-TEC-001 정합) — 직전 미장 거래일 fallback.
//   미 일요일 휴장 + 월요일 모닝 빌드 부재(us-digest plist Weekday 2~6) → 오늘(KST) us-indices 파일 부재 → 404.
//   이때 섹션 전체가 사라지면 "지난 금요장(현지 목요 마감)" 미장 정보까지 월요일 내내 미표시 (사용자 손실).
//   대응: 오늘 파일 부재/무효 시 직전 거래일로 최대 N일 역탐색 (주말·미 휴장 자연 건너뜀 — 파일 존재 = 거래일).
//   trade_date_local 라벨이 실제 마감일(예 금요장)을 정직 표기하므로 stale 오인 위험 없음.
//   선물(futures)은 renderer 가 거래일 범위 게이트(미래시각 + 좀비 ~49h 초과 차단)로 별도 판정 → 묵은 fallback
//   선물도 거래일 범위면 "N분 전 기준"+"지연" 배지로 상시 표시(Q-20260608-145, 숨김 폐기). 수일 좀비만 미렌더.
async function loadNightlyUsSummary(date) {
  // 1) 오늘 파일 우선 — 유효하면 그대로 사용 (fallback 미발동).
  const today = _parseNightlyUs(await _fetchUsIndices(date));
  if (today) return today;
  // 2) 오늘 파일 부재/무효 → 직전 거래일 역탐색 (최대 7 캘린더일: 주말 + 단일 휴장 커버).
  //    날짜 산술은 로컬(KST 사용자 가정) getFullYear/getMonth/getDate 사용 — loadKiwoomDate 폴백과 동일 가정.
  const base = new Date(`${date}T00:00:00`);
  for (let i = 1; i <= 7; i++) {
    const dt = new Date(base);
    dt.setDate(dt.getDate() - i);
    const y = dt.getFullYear();
    const m = String(dt.getMonth() + 1).padStart(2, '0');
    const d = String(dt.getDate()).padStart(2, '0');
    const prevDate = `${y}-${m}-${d}`;
    const prev = _parseNightlyUs(await _fetchUsIndices(prevDate));
    if (prev) {
      prev._fallback_date = prevDate;  // 렌더/디버그용 (UI 라벨은 trade_date_local 사용)
      return prev;
    }
  }
  return null;
}

// us-indices/{date}.json fetch — 404/네트워크 오류 시 null (호출부에서 fallback 판정).
// R44 #10 (조니 2심, 2026-06-12) — 과거 뷰 연쇄 404 가드: us-indices 데이터 시작일(2026-06-05,
//   us-digest 운영 개시) 이전 날짜는 파일이 영구 부재 → 직접 조회 + 7일 역탐색 fallback 까지
//   최대 8연쇄 확정 404 가 콘솔을 채움. epoch 미만은 fetch 자체 생략 (확정 404 0건 — R18 P0 동형).
//   ISO 문자열 비교 = 날짜 비교 (zero-pad 고정 포맷).
const _US_INDICES_EPOCH = '2026-06-05';
async function _fetchUsIndices(date) {
  if (!date || date < _US_INDICES_EPOCH) return null;
  const dateHash = _dataBust(date);  // fix/pick-reveal — 픽 공개 후 신선 fetch (평시 날짜 해시 동일)
  try {
    const res = await fetch(`/data/us-indices/${date}.json?v=${dateHash}`);
    if (!res.ok) return null;
    return await res.json();
  } catch (e) { return null; }
}

// raw us-indices 페이로드 → 검증된 구조 (무효 시 null). loadNightlyUsSummary + fallback 공용.
function _parseNightlyUs(raw) {
  // schema validation — 최소 필수 필드 미충족 시 미신뢰 → null (빈 카드/mock 노출 차단)
  if (!raw || typeof raw !== 'object') return null;
  if (typeof raw.trade_date_local !== 'string' || !raw.trade_date_local) return null;
  if (!Array.isArray(raw.indices) || raw.indices.length === 0) return null;
  const validIndices = raw.indices.filter(ix =>
    ix && typeof ix === 'object'
    && typeof ix.name === 'string' && ix.name
    && typeof ix.point === 'number'
    && typeof ix.change_pct === 'number'
  );
  if (validIndices.length === 0) return null;
  // news_chips는 선택 — 부재/형식불일치 시 빈 배열 (지수 카드는 정상 렌더)
  const newsChips = Array.isArray(raw.news_chips)
    ? raw.news_chips.filter(c =>
      c && typeof c === 'object'
      && typeof c.summary === 'string' && c.summary
      && typeof c.source === 'string' && c.source
      && typeof c.url === 'string' && c.url)
    : [];
  // Phase 4 (Q-20260605-103, 백엔드 commit 3969bf9) — 장중 미 선물 (선택).
  //   schema: { as_of_kst, session_open?, futures:[{name, label_note, point, change_pct, spark[]}] }.
  //   부재/형식불일치/유효 선물 0건 시 null → 선물 줄 미렌더 (장외/주말/미합류 graceful, FLR-AGT-002).
  //   신선도(stale) 판정은 렌더 시점(renderer)에서 as_of_kst vs now — 로더는 형식만 검증.
  //   Q-20260608-140 (A안 페어 카드) — session_open(섹션 단위 거래중/마감) 통과. 부재 시 undefined → 도트 미렌더.
  let futures = null;
  if (raw.futures && typeof raw.futures === 'object'
    && typeof raw.futures.as_of_kst === 'string' && raw.futures.as_of_kst
    && Array.isArray(raw.futures.futures)) {
    const vf = raw.futures.futures.filter(f =>
      f && typeof f === 'object'
      && typeof f.name === 'string' && f.name
      && typeof f.point === 'number'
      && typeof f.change_pct === 'number');
    if (vf.length > 0) {
      futures = {
        as_of_kst: raw.futures.as_of_kst,
        session_open: (typeof raw.futures.session_open === 'boolean') ? raw.futures.session_open : undefined,
        futures: vf
      };
    }
  }
  return {
    trade_date_local: raw.trade_date_local,
    indices: validIndices,
    news_chips: newsChips,
    futures,  // null이면 선물 줄 미렌더 (장외/stale/미합류)
    // Q-20260606-113 — 야간 미국증시 생성 시각 (us-indices/{date}.json built_at_kst).
    //   주말·휴장일 국내장 generated_at 부재 시 헤더 "마지막 갱신" 폴백 소스로 사용.
    built_at_kst: (typeof raw.built_at_kst === 'string') ? raw.built_at_kst : ''
  };
}

// feat/market-context (조니 확정 spec 2026-06-12) — 시장 컨텍스트 단일 파일 4종 로더.
//   /pm320/data/ 하위 날짜 무관 단일 라이브 파일: kr_indices(시장 지수 ①) / wire_news(기관 와이어 ⑤)
//   / macro_indicators(장중 글로벌 지표 ② — usdkrw·wti 2종, ust10y 제외 확정 조니 미니 단정 17:06)
//   / nxt_roster(NXT 동시상장 ③·시총 ④). 각 파일 부재/파싱 실패/schema 미달 시 해당 키 null →
//   소비측 무렌더 (FLR-AGT-002 거짓 충실성 차단 — 폴백·대시·mock 절대 금지, "빈자리는 정직하다").
//   캘린더가 날짜별 loadCalDayData 를 다건 호출하므로 TTL 5분 단일 Promise 캐시(과다 fetch 회피 +
//   장중 10~15분 수집 주기 대비 신선도 유지). 검증은 최소 필수 필드만 — 상세 stale 가드는 렌더 시점.
let _marketCtxCache = { at: 0, p: null };
function _loadMarketContext() {
  const now = Date.now();
  if (_marketCtxCache.p && (now - _marketCtxCache.at) < 5 * 60 * 1000) return _marketCtxCache.p;
  const bust = String(Math.floor(now / (5 * 60 * 1000)));  // 5분 단위 cache-bust (CDN 304 친화)
  const _get = (path) => fetch(`${path}?v=${bust}`).then(r => (r.ok ? r.json() : null)).catch(() => null);
  const p = Promise.all([
    _get('/pm320/data/kr_indices.json').then(_parseKrIndices),
    _get('/pm320/data/wire_news.json').then(_parseWireNews),
    _get('/pm320/data/macro_indicators.json').then(_parseMacroIndicators),
    _get('/pm320/data/nxt_roster.json').then(_parseNxtRoster),
  ]).then(([krIndices, wireNews, macroIndicators, nxtRoster]) => (
    { krIndices, wireNews, macroIndicators, nxtRoster }
  )).catch(() => ({ krIndices: null, wireNews: null, macroIndicators: null, nxtRoster: null }));
  _marketCtxCache = { at: now, p };
  return p;
}

// kr_indices.json — { KOSPI:{name,value,prev_close,change_pct,candles_10m[],range_240d{high,low,days},
//   trade_date,asof,session}, KOSDAQ:{…} }. 지수별 독립 검증 — 유효 0건이면 null.
function _parseKrIndices(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const out = [];
  for (const key of ['KOSPI', 'KOSDAQ']) {  // 표시 순서 고정 (코스피 → 코스닥)
    const e = raw[key];
    if (!e || typeof e !== 'object') continue;
    if (typeof e.name !== 'string' || !e.name) continue;
    if (typeof e.value !== 'number' || !isFinite(e.value)) continue;
    if (typeof e.change_pct !== 'number' || !isFinite(e.change_pct)) continue;
    if (typeof e.trade_date !== 'string' || !e.trade_date) continue;
    out.push(e);
  }
  return out.length > 0 ? { list: out } : null;
}

// wire_news.json — { items:[{published_at, source, title, url}] }. 4필드 전부 필수 (직링크 의무 — 법무).
function _parseWireNews(raw) {
  if (!raw || typeof raw !== 'object' || !Array.isArray(raw.items)) return null;
  const items = raw.items.filter(it =>
    it && typeof it === 'object'
    && typeof it.title === 'string' && it.title
    && typeof it.source === 'string' && it.source
    && typeof it.url === 'string' && /^https?:\/\//i.test(it.url)
    && typeof it.published_at === 'string' && it.published_at);
  return items.length > 0 ? { items } : null;
}

// macro_indicators.json — { as_of, indicators:{ usdkrw:{label,value,prev_close,change_pct,bar_asof},
//   wti:{…}, ust10y:{label,value,change_bp,bar_asof} } }. 항목별 독립(부분 산출 가용 항목만).
//   usdkrw·wti = change_pct(%) 축 / ust10y = 미10년물 금리 = change_bp(bp) 축 (renderer.js _buildGlobalStatsHtml
//   ust10y 전용 분기 L566~574 와 동형 — value=yield(%)·delta=change_bp). Q-20260613-165 ② 배선 완결:
//   종전 화이트리스트가 [usdkrw,wti] 2종이라 ust10y 가 렌더 직전에 탈락 → 렌더 분기 도달 불가(죽은 경로).
//   본 파서가 ust10y 통과시켜 월요일 cron 산출 시 노출(주말은 데이터 부재 → out 미포함 → graceful 무렌더).
function _parseMacroIndicators(raw) {
  if (!raw || typeof raw !== 'object' || !raw.indicators || typeof raw.indicators !== 'object') return null;
  const out = {};
  for (const key of ['usdkrw', 'wti']) {
    const e = raw.indicators[key];
    if (!e || typeof e !== 'object') continue;
    if (typeof e.label !== 'string' || !e.label) continue;
    if (typeof e.value !== 'number' || !isFinite(e.value)) continue;
    if (typeof e.change_pct !== 'number' || !isFinite(e.change_pct)) continue;
    if (typeof e.bar_asof !== 'string' || !e.bar_asof) continue;  // 항목별 60분 stale 가드 축 — 필수
    out[key] = e;
  }
  // ust10y (미10년물 금리) — change_pct 가 아니라 change_bp(bp) 축이라 별 분기. renderer ust10y 분기
  //   (value + change_bp + bar_asof 요구, L567~569)와 동일 계약으로 검증 → 통과분만 운반.
  const u = raw.indicators.ust10y;
  if (u && typeof u === 'object'
    && typeof u.label === 'string' && u.label
    && typeof u.value === 'number' && isFinite(u.value)
    && typeof u.change_bp === 'number' && isFinite(u.change_bp)
    && typeof u.bar_asof === 'string' && u.bar_asof) {
    out.ust10y = u;
  }
  return Object.keys(out).length > 0 ? { as_of: (typeof raw.as_of === 'string' ? raw.as_of : ''), indicators: out } : null;
}

// nxt_roster.json — { fetched_at, snapshots:{ "YYYY-MM-DD": { codes_nxt:[…], list_count:{code:int} } } }.
//   시점 왜곡 금지 — 소비측(renderer)이 "표시 날짜의 스냅샷만" 사용 + fetched_at 7거래일+ 경과 시 suppress.
function _parseNxtRoster(raw) {
  if (!raw || typeof raw !== 'object') return null;
  if (typeof raw.fetched_at !== 'string' || !raw.fetched_at) return null;
  if (!raw.snapshots || typeof raw.snapshots !== 'object') return null;
  return raw;
}

async function loadCalDayData(date) {
  // §3.6.2.3 (FLR-20260605-TEC-001 P1-2) — 세션 구간 키로 read/write (calendar.js _cacheKey 단일 출처).
  //   장 시작/마감 경계를 넘으면 키 불일치 → cache miss → 네트워크 재로드(이전 구간 데이터 재사용 0).
  const _key = (typeof _cacheKey === 'function') ? _cacheKey(date) : date;
  if (calDayCache[_key]) return calDayCache[_key];
  // kiwoom + stock-daily + pm320_history 병렬 fetch
  // DOC-20260603-DSN-001 §1 — pm320_history는 별 path (메인 worktree → cron 미러), 404 graceful (PICK 부재 일자)
  const dateHash = _dataBust(date);  // fix/pick-reveal — 픽 공개 후 신선 fetch (평시 날짜 해시 동일)
  // R18 P0 (콘솔 404 0err화) — PRE_MARKET(장 시작 전) + 오늘 view 시 오늘 interpreted/pm320_history JSON
  //   은 아직 생성 전(09:00 이후 빌드) → 확정 404. 장전엔 본 데이터를 쓰지도 않으므로(renderPreMarketEmpty
  //   는 nightlyUs/전일 픽만 사용) fetch 자체를 건너뛰어 콘솔 빨간 에러를 없앤다. 09:00 전환 시
  //   onCalCellClick 재호출 → 캐시 키(_cacheKey) 가 장경계로 무효화돼 fresh fetch (무회귀).
  let _todayPreMarket = false;
  let _todayBeforePick = false;
  try {
    const _n = _kstNow(); // KST wall-clock — 해외 접속 시 오늘/PRE_MARKET/15:20 게이트 오판 봉쇄
    const _t = `${_n.getFullYear()}-${String(_n.getMonth() + 1).padStart(2, '0')}-${String(_n.getDate()).padStart(2, '0')}`;
    _todayPreMarket = (date === _t) && (typeof getMarketState === 'function') && getMarketState(date, _n) === 'PRE_MARKET';
    // R44 #10 (조니 2심, 2026-06-12) — 당일 pm320_history/{date}.json 은 15:20 픽 발행 후 생성.
    //   09:00~15:20 오늘 view 의 history fetch 는 확정 404 (콘솔 빨간 에러) → 시간 가드로 생략.
    //   15:20 경계 후 첫 로드는 정상 fetch (파일 배포 지연 시 404 graceful null 기존 동작 유지).
    //   interpreted JSON 은 장중에도 갱신되므로 본 가드 미적용 (PRE_MARKET/휴장 가드만).
    const _hm = _n.getHours() * 60 + _n.getMinutes();
    _todayBeforePick = (date === _t) && _hm < (15 * 60 + 20);
  } catch (_) { _todayPreMarket = false; _todayBeforePick = false; }
  const _closedMarket = (typeof isMarketClosed === 'function') ? isMarketClosed(date) : false;
  // WAVE6-R29 — 휴장일 클릭은 국내장/PM320 산출물이 원칙적으로 없다.
  // 화면은 kiwoom snapshot + renderer 휴장 안내로 충분하므로 확정 404가 되는 interpreted/pm320_history 요청은 건너뛴다.
  const [kiwoom, stockDailyDirect, pm320Data, nightlyUs, pm320Summary, marketCtx] = await Promise.all([
    loadKiwoomDate(date),
    (_todayPreMarket || _closedMarket)
      ? Promise.resolve(null)
      : fetch(`/data/interpreted/${calCategory}-${date}.json?v=${dateHash}`).then(r => r.ok ? r.json() : null).catch(() => null),
    (_todayPreMarket || _closedMarket || _todayBeforePick) ? Promise.resolve(null) : loadPm320History(date),
    loadNightlyUsSummary(date),
    loadPm320Summary(),
    _loadMarketContext()  // feat/market-context — kr_indices/wire_news/macro_indicators/nxt_roster (각 null graceful)
  ]);
  // pm320 stocks → code 기반 lookup map 신축 (interpretedByName 합성 시 사용)
  // schema: { code, name, pm320_pick: { is_pick, entry_price, watering_target_price, ... } }
  const pm320ByCode = new Map();
  if (pm320Data && Array.isArray(pm320Data.stocks)) {
    for (const ps of pm320Data.stocks) {
      if (ps.code) pm320ByCode.set(ps.code, ps.pm320_pick);
    }
  }
  // DSN-frontend §3.6.8 (2026-06-05) — 당일 PM320 추천 부재(보류일) 일-레벨 마커.
  //   통합 모델 보류일(선제거로 잔존<2 → PICK 0건, 예: 4/16)에는 picked_code=null + 모든
  //   stocks[].pm320_pick.is_pick=false 다. 이 경우 renderer 가 "추천 종목 없음" 안내를 띄운다.
  //   3상태: true=추천 부재 확정(보류일) / false=추천 존재 / null=데이터 미신뢰(404·미생성).
  //   FLR-AGT-002 거짓 충실성 차단 — pm320_history 404(미생성)는 null 로 두어 추정 고지 금지.
  let pm320NoPick = null;
  if (pm320Data && Array.isArray(pm320Data.stocks)) {
    // 보류일 = pm320_history 가 로드됐고(404 아님) is_pick=true 종목이 0건.
    //   라이브 데이터(4/16)는 picked_code=null 도 동반하나, is_pick 합산이 단일 출처(SoT)다.
    pm320NoPick = !pm320Data.stocks.some(
      (ps) => ps && ps.pm320_pick && ps.pm320_pick.is_pick === true,
    );
  }
  let stockDailyData = stockDailyDirect;
  // REQ-055 P0 — 당일 stock JSON이 stocks=[] 빈 데이터인 경우도 fallback 대상.
  //   배포 직후/장 시작 전 build_daily.py가 빈 stocks=[] 파일을 생성하면 truthy로 평가되어
  //   fallback이 동작하지 않고 카드/sparkline/themes_chip이 모두 비어 보이는 결함 (4/28 07:50 KST 사례).
  //   stocks가 1건이라도 있어야 해석으로 인정. macro_events/generated_at만 있는 빈 파일은 무시.
  const _hasStockEntries = (sd) => !!(sd && Array.isArray(sd.stocks) && sd.stocks.length > 0);
  // 당일 데이터 없거나 stocks 비었으면 최근 7일 이내 이전 날짜 fallback (병렬)
  // 단, 휴장일/주말은 fallback 자체를 비활성화 (옵션 A: 휴장 안내만 표시)
  // DSN-frontend §3.6.2.2 (2026-05-28) — 오늘 view + 09:00 이후 시 본 fallback 차단.
  //   기존 동작 (어제 stock-daily 데이터 표시) → 사용자 매매 판단 misread risk.
  //   renderer.js empty-state path가 시간대별 메시지 ("데이터 수집 중" / "수집 이상")로 정직하게 고지.
  if (!_hasStockEntries(stockDailyData) && !isMarketClosed(date) && !_isTodayPastOpen(date)) {
    // REQ-055 P0 — toISOString()는 KST→UTC 변환되어 하루 전 날짜를 반환하는 버그.
    //   `new Date('2026-04-28T00:00:00')` (KST 자정) → UTC `2026-04-27T15:00:00Z`
    //   → `setDate(-1)` 후 toISOString() → '2026-04-26' (4/27 건너뜀, 4/24가 첫 PASS로 잡힘 사례).
    //   날짜 산술은 로컬 시간 기준 getFullYear/getMonth/getDate 사용.
    const _localYmd = (dt) => {
      const y = dt.getFullYear();
      const m = String(dt.getMonth() + 1).padStart(2, '0');
      const d = String(dt.getDate()).padStart(2, '0');
      return `${y}-${m}-${d}`;
    };
    const d = new Date(date + 'T00:00:00');
    const fallbackFetches = [];
    for (let i = 1; i <= 7; i++) {
      const prev = new Date(d);
      prev.setDate(prev.getDate() - i);
      const prevStr = _localYmd(prev);
      // R18 P0 (콘솔 404 0err화) — 주말·휴장일은 interpreted JSON 이 애초에 생성 안 됨
      //   → fetch 시 확정 404(콘솔 빨간 에러 박제). market-closed 일자는 fetch 자체를 건너뛴다
      //   (탐색은 i 루프로 더 과거 거래일까지 계속 진행 → fallback 동작 무회귀).
      if (typeof isMarketClosed === 'function' && isMarketClosed(prevStr)) continue;
      const prevHash = prevStr.replace(/-/g, '');
      fallbackFetches.push(
        fetch(`/data/interpreted/${calCategory}-${prevStr}.json?v=${prevHash}`)
          .then(r => r.ok ? r.json().then(j => _hasStockEntries(j) ? { date: prevStr, data: j } : null) : null)
          .catch(() => null)
      );
    }
    const results = (await Promise.all(fallbackFetches)).filter(Boolean);
    if (results.length > 0) {
      // 가장 최근 날짜 우선 (i=1부터 순서대로 fetch했지만 Promise.all 순서 보장 — 첫 entry가 가장 가까운 과거)
      stockDailyData = results[0].data;
      stockDailyData._fallback_date = results[0].date;
    }
  }
  // 종목명 → 해석 stock 객체 병합 맵
  const interpretedByName = new Map();
  let macroEvents = [];
  // Phase 1 뉴스 파이프라인 산출물 (stock-YYYY-MM-DD.json) — DB 종목 마스터 + 이시카와/토구사 해석
  try {
    if (stockDailyData) {
      const stockDaily = stockDailyData;
      for (const st of (stockDaily.stocks || [])) {
        if (!st.name) continue;
        // 기존 해석이 있으면 themes/theme_paths만 병합 (cafe에 테마 없을 수 있음)
        if (interpretedByName.has(st.name)) {
          const existing = interpretedByName.get(st.name);
          const stThemes = (st.themes || []).map(t => typeof t === 'string' ? { name: t } : t);
          if (stThemes.length > 0 && (!existing.themes || existing.themes.length === 0)) {
            existing.themes = stThemes;
          }
          if ((st.theme_paths || []).length > 0 && (!existing.theme_paths || existing.theme_paths.length === 0)) {
            existing.theme_paths = st.theme_paths;
          }
          continue;
        }
        {
          // stock-*.json 형식 → 기존 렌더러 호환 변환
          // 가비지 뉴스 필터: VI발동, 신고가, 단순 등락률 로봇 기사 제거
          const garbageRe = /[+-]?\d[\d.]*%\s*(VI\s*발동|\d+주\s*신[고저]가|상한가|하한가)|거래량\s*(폭발|급증|돌파)/;
          const newsItems = (st.news || [])
            .filter(n => !(n.newzy_verdict || '').startsWith('반대'))  // 이시카와 판정 컬럼 (legacy: newzy_verdict)
            .filter(n => !garbageRe.test(n.title || ''));
          const topNews = newsItems[0];
          const pp = st.prev_pick;
          const industryLabel = st.industry ? `업종: ${st.industry}` : '';
          const sectorLabel = st.sector ? (() => {
            // 괄호 밖의 첫 콤마에서만 자르기 (괄호 안 콤마는 무시)
            let depth = 0, cutIdx = -1;
            for (let i = 0; i < st.sector.length; i++) {
              if (st.sector[i] === '(') depth++;
              else if (st.sector[i] === ')') depth--;
              else if (st.sector[i] === ',' && depth === 0) { cutIdx = i; break; }
            }
            return cutIdx >= 0 ? st.sector.slice(0, cutIdx).trim() : st.sector.trim();
          })() : '';
          // causal_chain이 있는 뉴스를 우선 탐색 (첫 번째 뉴스에 없을 수 있음)
          const chainNews = newsItems.find(n => n.causal_chain) || null;
          const causalText = chainNews ? chainNews.causal_chain : '';
          const diffParts = [
            causalText,
            !causalText && industryLabel ? industryLabel : '',
            !causalText && sectorLabel ? `주요제품: ${sectorLabel}` : '',
          ].filter(Boolean);
          // 테마: theme_paths 우선, 없으면 themes, 없으면 industry 폴백
          const trimIndustry = (s) => s.replace(/\s*(제조업|업)$/, '').replace(/기타\s*/, '');
          let themes = (st.themes || []).map(t => typeof t === 'string' ? { name: t } : t);
          const themePaths = st.theme_paths || [];
          // industry 폴백 제거 (산업분류 ≠ 테마, 대표 결정)

          interpretedByName.set(st.name, {
            name: st.name,
            themes,
            theme_paths: themePaths,
            causal_chain: causalText ? [causalText] : (st.causal_chain ? [st.causal_chain] : []),
            differentiator: diffParts.join(' · ') || st.causal_chain || '',
            macro_event: topNews?.macro_event || null,
            news_digest: newsItems.map(n => ({ url: n.url, inferred_title: n.title, source: n.source })),
            industry: st.industry,
            sector: st.sector,
            fallback: st.fallback,
            fallback_date: st.fallback_date,
            pick_count: st.pick_count,
            prev_pick: pp,
            disclosures: st.disclosures || [],
            credit_risk: !!st.credit_risk,
            credit_reason: st.credit_reason || null,
            close_price: st.close_price || null,
            open_price: st.open_price || null,
            high_price: st.high_price || null,
            low_price: st.low_price || null,
            change_pct: st.change_pct ?? null,
            trade_amount: st.trade_amount ?? null,
            rank: st.rank ?? null,
            code: st.code || null,
            // REQ-055 P0 — fallback 시점에도 분봉 데이터는 해당 날짜의 정합 자료이므로 null화 금지.
            //   기존 로직은 fallback 데이터의 분봉을 일괄 null 처리해 sparkline이 회색으로만 표시되어
            //   "차트 안 그려짐" 결함을 유발 (4/28 사례). 데이터 안내 chip이 이미 fallback_date를 명시하므로 혼동 없음.
            intraday: st.intraday || null,
            status_badges: st.status_badges || [],
            range_240d: st.range_240d || null,
            // REQ-pm320-ux-cycle #3 P0 fix (FLR-20260429-FLR-002) — 20영업일 일봉 OHLC 패스스루.
            // build_daily.py가 stocks[N].daily_20 부착하지만 본 패스스루 누락 시 renderer.js
            // it.interp.daily_20 undefined → cal-candles20-empty 100% 회색 박스. 모바일은 sparkline
            // display:none이라 매매 직결 정보 100% 손실.
            daily_20: st.daily_20 || null,
            // REQ-048 — 강세 배지 데이터 패스스루 (build_daily.py REQ-039 entry 루트 → interp 합성).
            // 이 필드 누락이 라이브 화면 강세 배지 미노출의 진짜 본질 (대표 발화 02:45 KST).
            bullish_today: !!st.bullish_today,
            bullish_streak: st.bullish_streak ?? 0,
            // P0-23 Fix-79 (2026-05-21 19:38 KST 대표 verbatim
            //   "제주반도체 일봉캔들 영웅문을 보면 강세 날짜가 상당히 많다. 그런데 오늘 하루만 강세로 표시가 된다"):
            //   - P0-21 backend rollout (commit c263408 / merge d0ab219) bullish_dates list[str] emit 라이브 visible PASS
            //     (curl https://100m1s.com/data/interpreted/stock-2026-05-21.json 본문 080220 제주반도체 → bullish_dates: 4건 ['2026-04-24','2026-05-14','2026-05-18','2026-05-21'])
            //   - P0-18 Fix-61 backward derive (streak=N → daily_20 마지막 N건) 본질 폐기 → SoT (backend) 직접 사용
            //   - 영웅문 본문 정합 본질: 강세 발생 모든 영업일 (날짜 array) 본문 분홍 vertical line visible
            //   §11.15 외부 spec 사전 검증 PASS:
            //     - build_daily.py L2509-2609 _prefetch_bullish_info 본문 verbatim grep (bullish_dates list[str] 본질)
            //     - build_daily.py L3188 entry["bullish_dates"] = bullish_dates verbatim grep
            //     - curl 라이브 evidence (제주반도체 4건 visible)
            bullish_dates: Array.isArray(st.bullish_dates) ? st.bullish_dates : null,
            // DOC-20260603-DSN-001 §1 — PM320 추천/결과 합성 패스스루.
            // pm320_history 별 path → code 기반 lookup → interp.pm320_pick 합성.
            // 부재 시 null (404 / non-PICK universe 아닌 일자 / IPO 전 일자 등) → renderer.js graceful.
            pm320_pick: (st.code && pm320ByCode.get(st.code)) || null,
          });
        }
      }
      // 매크로 이벤트 보충
      if (Array.isArray(stockDaily.macro_events) && macroEvents.length === 0) {
        macroEvents = stockDaily.macro_events;
      }
      // fallback 날짜 표시를 위해 안내 이벤트 삽입
      if (stockDailyData._fallback_date) {
        macroEvents.unshift({
          keyword: '데이터 안내',
          summary: `최신 분석 데이터 준비 중 — ${stockDailyData._fallback_date} 기준 데이터를 표시합니다.`,
          source_count: 0
        });
      }
    }
  } catch (e) { console.warn('stock-daily merge:', e); }

  // data_source: stock JSON에 포함된 소스 태그 (kiwoom / kiwoom_ranking)
  const dataSource = (stockDailyData && stockDailyData.data_source) || 'kiwoom';
  // REQ-033 — 마지막 업데이트 시각 (build_daily.py generated_at). SPEC-001 §I.4.
  const generatedAt = (stockDailyData && stockDailyData.generated_at) || '';
  // Q-CYCLE20-P2 (2026-05-20) — kiwoom raw 마지막 폴링 시각 (SPEC-001 §I.4 확장).
  //   장중 stale 데이터 가시화. kiwoom 폴백 path(stocks 합성 객체)는 last_snapshot_at 없음 →
  //   자연 미표시 (FLR-AGT-002 거짓 충실성 차단).
  const lastSnapshotAt = (kiwoom && typeof kiwoom.last_snapshot_at === 'string') ? kiwoom.last_snapshot_at : '';
  // DSN-frontend §3.6.2.2 (2026-06-05 P0 라이브 재발) — fallback 여부 명시 마커.
  //   data-loader 의 _isTodayPastOpen 가드는 fetch 시점에만 fallback 을 차단한다. 그러나
  //   PRE_MARKET(09:00 미만) 에 정상 생성된 fallback 결과가 localStorage(calDayCache)에 박제된 뒤,
  //   사용자가 09:00 이후 reload 하면 calendar.js 의 stage-3 동기 렌더(L241)가 이 stale fallback 을
  //   "오늘 데이터인 양" 즉시 표시 → 어제 뉴스/PM320 종목카드 노출 (대표 catch 09:05/09:15).
  //   본 마커로 calendar.js 가 OPEN 시점에 fallback 캐시 엔트리를 식별·차단한다.
  //   소스: kiwoom._fallback_date (L92) 또는 stockDailyData._fallback_date (L187).
  const fallbackDate =
    (stockDailyData && stockDailyData._fallback_date) ||
    (kiwoom && kiwoom._fallback_date) ||
    null;
  const result = {
    kiwoom,
    cafePosts: [],
    narratives: [],
    interpretedByName,
    macroEvents,
    dataSource,
    generatedAt,
    lastSnapshotAt,
    _fallbackDate: fallbackDate,
    pm320NoPick,
    nightlyUs,  // Q-20260605-103 Phase 3 — null이면 섹션 미렌더 (FLR-AGT-002)
    pm320Summary,  // PM320-D6 — 4/8 이후 승률 summary (null이면 승률 카드 미렌더, FLR-AGT-002)
    // feat/market-context — 시장 컨텍스트 4종 (각 null = 해당 표시 무렌더, FLR-AGT-002 "빈자리는 정직하다")
    krIndices: marketCtx ? marketCtx.krIndices : null,
    wireNews: marketCtx ? marketCtx.wireNews : null,
    macroIndicators: marketCtx ? marketCtx.macroIndicators : null,
    nxtRoster: marketCtx ? marketCtx.nxtRoster : null
  };
  // §3.6.2.3 — read 와 동일 세션 구간 키로 write (장경계 무효화 정합).
  calDayCache[_key] = result;
  _persistCache();

  // 전일 전파는 비동기 (초기 렌더 차단 안 함)
  setTimeout(() => _propagatePrevDay(date, result), 50);

  return result;
}

// 전일 해석 전파 — 별도 비동기 함수 (렌더 이후 백그라운드)
async function _propagatePrevDay(date, result) {
  try {
    const interpretedByName = result.interpretedByName;
    const prevPickDates = new Set();
    for (const [name, curr] of interpretedByName) {
      if (curr.prev_pick && curr.prev_pick.date) {
        const chain = curr.causal_chain || [];
        if (chain.length === 0) prevPickDates.add(curr.prev_pick.date);
      }
    }
    for (const prevDate of prevPickDates) {
      // 전일 해석 전파: 캐시 우선, 없으면 stock JSON만 직접 fetch (재귀 방지)
      // §3.6.2.3 — 동일 키 스킴으로 조회(전일은 과거일이므로 flat date 키, 정합 유지).
      let prevData = calDayCache[(typeof _cacheKey === 'function') ? _cacheKey(prevDate) : prevDate];
      if (!prevData) {
        const prevHash = prevDate.replace(/-/g, '');
        const prevStock = await fetch(`/data/interpreted/${calCategory}-${prevDate}.json?v=${prevHash}`).then(r => r.ok ? r.json() : null).catch(() => null);
        if (prevStock) {
          const prevMap = new Map();
          for (const st of (prevStock.stocks || [])) {
            if (!st.name) continue;
            const chainNews = (st.news || []).find(n => n.causal_chain) || null;
            prevMap.set(st.name, {
              causal_chain: chainNews ? [chainNews.causal_chain] : [],
              differentiator: chainNews ? chainNews.causal_chain : '',
              macro_event: (st.news || [])[0]?.macro_event || null,
              news_digest: (st.news || []).map(n => ({ url: n.url, inferred_title: n.title, source: n.source })),
            });
          }
          prevData = { interpretedByName: prevMap };
        }
      }
      if (!prevData || !prevData.interpretedByName) continue;
      for (const [name, prevInterp] of prevData.interpretedByName) {
        if (!interpretedByName.has(name)) continue;
        const curr = interpretedByName.get(name);
        if ((curr.causal_chain || []).length > 0) continue;
        if ((prevInterp.causal_chain || []).length > 0) {
          // curr를 base로 두고, prev에서 분석성 필드만 보충 (당일 가격/공시/분봉/신용 등 보존)
          const merged = Object.assign({}, curr, {
            causal_chain: prevInterp.causal_chain,
            differentiator: prevInterp.differentiator || curr.differentiator,
            macro_event: prevInterp.macro_event || curr.macro_event,
            news_digest: prevInterp.news_digest || curr.news_digest,
          });
          interpretedByName.set(name, merged);
        }
      }
    }
    // 전파 완료 후 캐시 업데이트
    _persistCache();
  } catch (e) { console.warn('prev-day propagation:', e); }
}
