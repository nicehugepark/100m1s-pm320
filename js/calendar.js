/* ───── calendar.js — 달력/날짜 로직 ───── */

let calIndex = null;           // data/calendar/index.json (없으면 null)
let holidayData = null;        // data/holidays.json (공휴일 + KRX 휴장일)
let calViewYear, calViewMonth; // 현재 보기 연·월 (month: 1~12)
let calSelectedDate = null;    // 'YYYY-MM-DD'
let calCategory = 'stock';     // 'stock' | 'realestate' | 'policy' (Phase 2/3 확장용)
const calDayCache = (() => {
  try {
    const raw = JSON.parse(localStorage.getItem('calDayCache') || '{}');
    // Map 복원: interpretedByName이 배열이면 Map으로 재구성
    for (const [date, entry] of Object.entries(raw)) {
      if (entry && Array.isArray(entry.interpretedByName)) {
        entry.interpretedByName = new Map(entry.interpretedByName);
      }
    }
    return raw;
  } catch { return {}; }
})();

function _persistCache() {
  try {
    // §3.6.2.3 (FLR-20260605-TEC-001 P1-2) — 캐시 키가 'date' 또는 'date@SEGMENT' 혼재.
    //   종래 키 단위 상위 7개 trim 은 오늘 date 가 최대 3구간(PRE/OPEN/POST)을 점유해
    //   유효 보존 거래일이 줄 수 있다. 거래일(키의 '@' 앞 date 부분) 기준 최근 7일을 보존하여
    //   종전 보존 폭(7거래일) 무회귀 + 구간 키 공존.
    const _dateOf = (k) => k.split('@')[0];
    const recentDates = new Set(
      Array.from(new Set(Object.keys(calDayCache).map(_dateOf))).sort().reverse().slice(0, 7)
    );
    const keys = Object.keys(calDayCache).filter((k) => recentDates.has(_dateOf(k)));
    const trimmed = {};
    for (const k of keys) {
      const entry = calDayCache[k];
      if (!entry) continue;
      // Map→Array 직렬화 (JSON.stringify는 Map을 빈 객체로 변환하므로)
      trimmed[k] = {
        ...entry,
        interpretedByName: entry.interpretedByName instanceof Map
          ? Array.from(entry.interpretedByName.entries())
          : entry.interpretedByName
      };
    }
    localStorage.setItem('calDayCache', JSON.stringify(trimmed));
  } catch {}
}

function isHoliday(iso) {
  return holidayData && holidayData.holidays && (iso in holidayData.holidays);
}

function getHolidayName(iso) {
  if (!holidayData || !holidayData.holidays) return null;
  return holidayData.holidays[iso] || null;
}

function isMarketClosed(iso) {
  if (!holidayData || !holidayData.market_closed) return isWeekendDate(iso);
  return iso in holidayData.market_closed;
}

// design-news-time-state-v1 — 시점 4구간 분기 SSOT.
// 본 함수가 PRE_MARKET / OPEN / POST_MARKET / HOLIDAY 단일 출처. renderer.js의
// (hour < 16) 휴리스틱은 04:14에도 true가 되는 결함 → 폐기.
// PRE_MARKET = 거래일 09:00 미만 / OPEN = 09:00~15:30 / POST_MARKET = 15:30 이후
// HOLIDAY = isMarketClosed (주말 + KRX 휴장일)
function getMarketState(iso, now) {
  // now 미명시 시 _kstNow() (해외 접속 TZ 무관 KST wall-clock). now 명시 시 호출부가 KST 보정한
  //   값을 넘긴다는 계약(data-loader/renderer 가 _kstNow() 전달) — 여기서 재가산하지 않음(double-shift 방지).
  const _now = now || _kstNow();
  const todayIso = iso || ymd(_now.getFullYear(), _now.getMonth() + 1, _now.getDate());
  if (isMarketClosed(todayIso)) return 'HOLIDAY';
  const hm = _now.getHours() * 60 + _now.getMinutes();
  if (hm < 9 * 60) return 'PRE_MARKET';
  if (hm < 15 * 60 + 30) return 'OPEN';
  return 'POST_MARKET';
}

// DSN-frontend §3.6.2.3 (2026-06-05 P1-2, FLR-20260605-TEC-001) — 장경계 캐시 키 구조화.
//   stage-3 즉시 캐시 렌더가 장 시작/마감 경계를 넘어 이전 구간 캐시를 "현재인 양" 재표시하던
//   부분상태(stale) 클래스의 구조적 봉쇄. 캐시 키에 세션 구간(getMarketState 단일 출처)을 인코딩하면
//   경계를 넘는 순간 키가 자동 불일치 → 이전 구간 캐시는 자연 폐기(재표시 0). 09:05 stale fix
//   (f27625e66, OPEN+today+fallback 3조건 폐기 휴리스틱)를 일반화·대체한다.
//   원칙: getMarketState(calendar.js)가 PRE_MARKET/OPEN/POST_MARKET/HOLIDAY 단일 출처.
//   - 오늘(KST) 날짜만 세션 구간을 키에 인코딩한다. 오늘은 시각 경과로 구간이 바뀌므로 무효화 필요.
//   - 과거/미래 viewDate는 일자가 이미 확정되어 세션 구간과 무관(getMarketState(pastDate,now)는
//     그 과거일 기준이라 부적절) → date 단일 키 유지 = 과거 카드 캐시 무회귀 + cold load 최소화.
//   - 구 스키마(flat date 키) 잔존 캐시는 read miss + _persistCache trim 으로 자연 폐기(1회 cold load 무해).
function _isTodayIso(iso, now) {
  const _now = now || _kstNow(); // 해외 접속 TZ 무관 KST "오늘" 판정
  return iso === ymd(_now.getFullYear(), _now.getMonth() + 1, _now.getDate());
}

// 보유픽(running) 신선도 윈도우 — "오늘 기준 최근 N영업일 이내 과거일"만 신선도 토큰 부착 대상.
//   근거: PM320 만기 모델 = 매수일 제외 6영업일 보유 → 진입 후 ~D+6 영업일까지 running.
//   _collectRunningPicks fan-out(8) + 만기 여유 = 10영업일(보수적). 11영업일+ 이전 확정 청산일은
//   불변 캐시 유지(과거 카드 캐시 이점 보존·cold load 남발 0). 윈도우 근사가 순환 의존(어느 과거일에
//   running 픽이 있는지 _cacheKey 가 모름)을 회피 — 윈도우 안은 refetch, 밖은 불변(과대 무효화 0).
const _FRESH_WINDOW_BDAYS = 10;

// fromIso(과거일)가 todayIso 기준 최근 _FRESH_WINDOW_BDAYS 영업일 이내인지. 미래일/오늘/계산 불가는 false.
//   isMarketClosed(holidays 미로드 시 주말 폴백)로 영업일만 카운트. 90일 가드로 무한루프 봉쇄.
function _withinFreshWindowBdays(fromIso, todayIso) {
  try {
    if (!fromIso || !todayIso || fromIso >= todayIso) return false; // 미래/오늘은 today 분기에서 처리
    let _d = new Date(todayIso + 'T00:00:00');
    if (!Number.isFinite(_d.getTime())) return false;
    let _bdays = 0;
    for (let _g = 0; _g < 90; _g++) {
      const _iso = `${_d.getFullYear()}-${String(_d.getMonth() + 1).padStart(2, '0')}-${String(_d.getDate()).padStart(2, '0')}`;
      if (_iso <= fromIso) return _bdays <= _FRESH_WINDOW_BDAYS; // fromIso 도달 — 그 사이 영업일 수로 판정
      if (typeof isMarketClosed !== 'function' || !isMarketClosed(_iso)) _bdays++;
      if (_bdays > _FRESH_WINDOW_BDAYS) return false; // 윈도우 초과 — 조기 종료
      _d.setDate(_d.getDate() - 1);
    }
    return false;
  } catch (_) { return false; }
}

function _cacheKey(date, now) {
  // 오늘 날짜만 세션 구간 토큰을 부착. 과거/미래는 date 단일 키.
  if (_isTodayIso(date, now)) {
    const _st = getMarketState(date, now);
    // 15:20 픽 공개 경계 — OPEN 중에도 15:20 이후엔 오늘 카드가 픽/보류로 갱신되므로 키 분리.
    //   (2026-06-23 대표 catch: 15:20~15:30 사이 @OPEN 캐시가 15:20 전 pending 을 고정 서빙 → "15:20 정각" 약속 위반)
    if (_st === 'OPEN') {
      const _n2 = now || _kstNow(); // KST wall-clock — 15:20(=920분) 경계 해외 접속 오판 봉쇄
      if (_n2.getHours() * 60 + _n2.getMinutes() >= 920) return `${date}@OPEN_PICKED`;
    }
    return `${date}@${_st}`;
  }
  // 과거일 카드라도 보유픽(running, 만기 전)의 current_pnl_pct 는 진입일 파일에서 MUTABLE 갱신된다
  //   (forward 종가 기반 — 장중 10분 주기 + 장 마감 후 종가로 1회 반영). calDayCache(localStorage 박제)가
  //   과거일 카드를 date 단일 키로 세션 내내 고정 서빙 → _dataBust 무력화·보유픽 손익 stale.
  //   ── 2026-06-24 대표 catch (제주반도체 080220 등락률 공란, 새로고침 무효):
  //      직전 6/23 fix 는 *현재 OPEN(장중)* 일 때만 10분 버킷으로 무효화 → POST_MARKET/PRE_MARKET 누락(부분상태).
  //      장 마감(18:53) 후 stale 캐시(픽 당일 pnl≈0 박제)가 -14.1% 신선값을 가려 가드(_pnl current_price==null&&v===0)가 0% 숨김.
  //      FLR-20260428-TEC-001 "한쪽 수정·다른 끝 누락" recurring 회피 — 장 상태 전 구간(OPEN/POST/PRE/HOLIDAY) 일반화.
  //   원리: running 픽 진입일은 만기 모델(매수일 제외 6영업일 보유 = 진입 후 최대 ~D+6 영업일까지 running)상
  //     항상 "오늘 기준 최근 N영업일 이내". 그 구간(_FRESH_WINDOW_BDAYS)만 신선도 토큰 부착(refetch),
  //     그 이전 확정 청산일은 date 단일 키 불변 캐시 유지(cold load 남발 회피·과거 카드 캐시 이점 보존).
  //     "어느 과거일에 running 픽이 있는지"를 _cacheKey 가 알 수 없는 순환 의존을 윈도우 근사로 회피.
  //   토큰: 장중(OPEN)=10분 버킷(10분 주기 갱신 추종) / 장후·장전·휴장=KST 날짜 버킷(종가 반영 후 불변, 일 1회 무효화로 충분).
  try {
    const _n = now || _kstNow(); // KST wall-clock — 윈도우/장상태/날짜 버킷 해외 접속 오판 봉쇄
    const _tIso = `${_n.getFullYear()}-${String(_n.getMonth() + 1).padStart(2, '0')}-${String(_n.getDate()).padStart(2, '0')}`;
    if (_withinFreshWindowBdays(date, _tIso)) {
      const _st = (typeof getMarketState === 'function') ? getMarketState(_tIso, _n) : 'POST_MARKET';
      if (_st === 'OPEN') {
        // 장중 — 10분 버킷 (보유픽 current_pnl 10분 주기 갱신 추종, 기존 6/23 동작 유지)
        return `${date}@m${String(_n.getHours()).padStart(2, '0')}${Math.floor(_n.getMinutes() / 10)}`;
      }
      // 장후/장전/휴장 — KST 날짜 버킷. 어제 박제 캐시가 오늘 재방문 시 자연 miss → refetch.
      //   같은 날 내 재방문은 동일 키로 캐시 HIT (cold load 남발 0). _persistCache trim 은 date(@ 앞) 기준이라 무회귀.
      return `${date}@d${_tIso}`;
    }
  } catch (_) { /* graceful — 기본 단일 키 */ }
  return date;
}

function formatKoDate(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  const dow = ['일','월','화','수','목','금','토'][new Date(y, m - 1, d).getDay()];
  return `${m}월 ${d}일 (${dow})`;
}

function isWeekendDate(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  const dow = new Date(y, m - 1, d).getDay();
  return dow === 0 || dow === 6;
}

// 다음 거래일 계산 (최대 10일 탐색)
function getNextTradingDate(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  const dt = new Date(y, m - 1, d);
  for (let i = 0; i < 10; i++) {
    dt.setDate(dt.getDate() + 1);
    const next = ymd(dt.getFullYear(), dt.getMonth() + 1, dt.getDate());
    if (!isMarketClosed(next)) return next;
  }
  return null;
}

function calHasData(date) {
  if (!calIndex || !calIndex.days) return false;
  const entry = calIndex.days[date];
  if (!entry) return false;
  // PM320 핵심 약속 = 일일 픽. 픽(stock_count>=1)만 있으면 클릭 가능.
  // 종전 news_count>=1 동시 요구는 cron news 가 없는 거래일(예 6/9~6/14, 뉴스
  // 수집 이전이라 published 0)을 클릭 불가로 차단 → 픽은 정상 존재하는데 캘린더
  // 비활성 (false-fidelity 동형). news_count 는 day entry 에 표시용으로 유지하되
  // 클릭 게이트 판정에서는 제외 (DSN-arch-frontend §calendar 정합).
  return (entry.stock_count ?? 0) >= 1;
}

// 비거래일이면 테마트리(일자별 카드성)만 숨김 — Q-20260613-158 ③ 확장 (대표 09:24 catch verbatim
//   "테마별 거래대금 추이는 왜 가린거야? 상한가 추이는 안가렸잖아. 이 두 개는 트렌드라서 장마감이나
//   휴장과는 무관"). 거래대금 추이(#theme-trend) = 20영업일 윈도 트렌드 차트, x축이 날짜를 운반 →
//   휴장 선택일에도 항상 표시 (상한가 추이 #limit-up-trend 무가드와 일관). 5/8 04:58 대표 catch fix
//   (initThemeTrend PRE_MARKET 분기 rollback, "휴장 무관 항상 표시")가 본 함수(4/16 출생)의 휴장
//   선택일 display:none 경로를 누락한 recurring 2회차 — FLR-20260428-TEC-001 한쪽 수정·다른 끝 누락 동형.
//   테마트리(해당일 데이터 귀속)는 휴장 숨김 유지 — Q-20260606-118 라벨·데이터 일치 + 5/8 verbatim
//   "PRE_MARKET 분기는 일자별 카드성 데이터(테마트리)에만 유지" 정합.
function toggleThemeSections(iso) {
  const closed = isMarketClosed(iso);
  const tree = document.getElementById('theme-tree');
  if (tree) tree.style.display = closed ? 'none' : '';
}

// R46 P0-2① (조니 2026-06-12 단정 — 캘린더 기본 접힘 격하) — 접힘 헤더 미니요약 1줄.
//   소스 = summary.json backtest_detail.table (renderer 가 window._pm320SummaryCache 로 적재,
//   승률 카드와 동일 fetch — 추가 요청 0). 보는 달의 익절 청산일을 "6월 9/12일 익절"로 요약.
//   데이터 부재·해당 월 익절 0건 시 빈 문자열 유지 (조작·추정 0, FLR-AGT-002).
function _updateTossCalSummary() {
  const el = document.querySelector('#toss-cal .pm320-section-summary');
  if (!el) return;
  const s = (typeof window !== 'undefined') ? window._pm320SummaryCache : null;
  const table = (s && s.backtest_detail && Array.isArray(s.backtest_detail.table)) ? s.backtest_detail.table : null;
  let txt = '';
  if (table) {
    const pfx = `${calViewYear}-${String(calViewMonth).padStart(2, '0')}-`;
    const days = [...new Set(
      table
        .filter(r => r && typeof r.exit_date === 'string' && r.exit_date.indexOf(pfx) === 0 && /익절/.test(r.exit_class || ''))
        .map(r => parseInt(r.exit_date.slice(8, 10), 10))
        .filter(d => Number.isFinite(d))
    )].sort((a, b) => a - b);
    if (days.length > 0) {
      // 미니요약 1줄 폭 보호 — 최근 2일 표기(조니 예시 "6월 9/12일 익절" 형식), 초과분 "외 N일"
      //   (사실 그대로, 축약만 — 390px/데스크탑 사이드바 헤더 1줄 고정).
      const shown = days.slice(-2);
      txt = `${calViewMonth}월 ${shown.join('/')}일 익절${days.length > shown.length ? ` 외 ${days.length - shown.length}일` : ''}`;
    }
  }
  el.textContent = txt;
}

function renderCalendar() {
  const grid = document.getElementById('toss-cal-grid');
  const ymEl = document.getElementById('toss-cal-ym');
  const subEl = document.getElementById('toss-cal-sub');
  const prevBtn = document.getElementById('toss-cal-prev');
  const nextBtn = document.getElementById('toss-cal-next');

  ymEl.textContent = `${calViewYear}년 ${calViewMonth}월`;

  const today = _kstNow(); // 해외 접속 TZ 무관 KST "오늘" — isToday/isFuture/네비 제한 전체의 기준
  const todayY = today.getFullYear();
  const todayM = today.getMonth() + 1;
  const todayD = today.getDate();
  const todayStr = ymd(todayY, todayM, todayD);

  // 서브 텍스트 (해당 월 집계)
  subEl.textContent = '';

  // 네비게이션 제한
  nextBtn.disabled = (calViewYear > todayY) || (calViewYear === todayY && calViewMonth >= todayM);

  // 그리드 렌더
  const firstDow = new Date(calViewYear, calViewMonth - 1, 1).getDay();
  const daysInMonth = new Date(calViewYear, calViewMonth, 0).getDate();
  const dows = ['일','월','화','수','목','금','토'];
  let html = '';
  dows.forEach((d, i) => {
    const cls = i === 0 ? 'sun' : (i === 6 ? 'sat' : '');
    html += `<div class="toss-cal-dow ${cls}">${d}</div>`;
  });
  for (let i = 0; i < firstDow; i++) {
    html += `<div class="toss-cal-cell outside"></div>`;
  }
  for (let d = 1; d <= daysInMonth; d++) {
    const date = ymd(calViewYear, calViewMonth, d);
    const isFuture = (calViewYear > todayY) ||
                     (calViewYear === todayY && calViewMonth > todayM) ||
                     (calViewYear === todayY && calViewMonth === todayM && d > todayD);
    const hasData = isFuture ? false : calHasData(date);
    const dow = new Date(calViewYear, calViewMonth - 1, d).getDay();
    const isWeekend = (dow === 0 || dow === 6);
    const isToday = (date === todayStr);
    const isHol = isHoliday(date);
    const classes = ['toss-cal-cell'];
    const isClosed = isMarketClosed(date);
    const isTodayMarketHours = isToday && !isClosed && (today.getHours() < 16); // today=_kstNow() (KST)
    if (isFuture) classes.push('future');
    else if (!hasData && !isToday && !isTodayMarketHours) classes.push('no-data');
    else if (!hasData && isTodayMarketHours) classes.push('market-hours');
    if (isWeekend) classes.push('weekend');
    if (dow === 0) classes.push('sunday');
    if (dow === 6) classes.push('saturday');
    if (isHol) classes.push('holiday');
    if (isToday) classes.push('today');
    if (date === calSelectedDate) classes.push('selected');
    const holName = getHolidayName(date);
    const aria = `${date}${isToday ? ' (오늘)' : ''}${isTodayMarketHours ? ' (장중)' : ''}${holName ? ' ' + holName : ''}`;
    const isClickable = !isFuture && (hasData || isToday || isClosed);
    html += `<div class="${classes.join(' ')}" data-date="${date}" role="button" tabindex="${isClickable ? 0 : -1}" aria-label="${aria}">${d}</div>`;
  }
  grid.innerHTML = html;

  grid.querySelectorAll('.toss-cal-cell[data-date]').forEach(el => {
    if (el.classList.contains('future')) return;
    el.addEventListener('click', () => onCalCellClick(el.dataset.date));
  });

  // R46 P0-2① — 보는 달 기준 접힘 미니요약 동기 (월 이동 prev/next 마다 재산출).
  _updateTossCalSummary();
}

// Q-20260606-118 결함(B) / Q-20260608-404fix — 해당 날짜의 정적 OG 랜딩 페이지(`/pm320/{date}.html`)가
//   라이브에 실제 배포돼 있는지 판정. 유일 기준 = page-manifest(FLR-20260605-TEC-001 P0-2, 라이브
//   실파일 SSOT)의 `landings` 배열(빌드 스크립트가 디스크의 `pm320/{date}.html` 실파일을 스캔해 박제).
//   🔴 calHasData(데이터 인덱스) 낙관 폴백 제거 — "데이터 존재 ≠ 랜딩 배포". 데이터는 있으나 랜딩이
//   아직 미배포인 날(예: today 랜딩 빌드/배포 지연)에 calHasData 폴백이 true 를 내면 미배포 URL 을
//   pushState → 새로고침 404 (본 버그의 ROOT ①). manifest 미도착·landings 부재·날짜 부재 = 모두 false
//   → URL push 안 함 → base `/pm320.html` 유지 → 새로고침 200 (보수적, 무파손).
//   호환: 구버전 manifest(landings 키 없음) 도착 시도 false(보수적). 정적 랜딩은 데이터 생성일에만
//   빌드되므로 휴장/미생성일은 자연히 landings 에서 빠짐.
function _dateHasStaticPage(date) {
  if (!date || !/^\d{4}-\d{2}-\d{2}$/.test(date)) return false;
  const m = (typeof window !== 'undefined') ? window._pageManifest : null;
  if (m && Array.isArray(m.landings)) {
    return m.landings.indexOf(date) !== -1;
  }
  // manifest 미신뢰/landings 부재 → 보수적 false (미배포 URL push 금지 우선).
  return false;
}

async function onCalCellClick(date, pushState) {
  calSelectedDate = date;
  // Q-20260606-118 결함(A) — suppress 재평가. Q-113 은 클릭 시 무조건 해제했으나, 휴장일(오늘=토 등)을
  //   명시 클릭하면 그 날 국내장 데이터가 없어 직전 거래일 fallback 이 표시되어 "6/6 라벨 + 6/5 카드"
  //   불일치가 발생했다. 클릭 대상이 휴장(isMarketClosed)이면 자동 폴백과 동일하게 suppress 유지,
  //   거래일이면 해제(그 날 국내장 카드 정상 표시 — 무회귀).
  window._pm320SuppressDomesticCards = (typeof isMarketClosed === 'function') ? isMarketClosed(date) : false;
  toggleThemeSections(date);
  // Static URL — /pm320/{date}.html (Q-20260606-119, stock 세그먼트 제거 — "pm320 자체가 주식"). 날짜별 OG 매칭.
  // Q-20260606-118 결함(B) — 정적 페이지 부재 날짜로 URL 갱신 금지 (FLR-20260605-TEC-001 "링크 존재 미보장"
  //   동형 변종). 휴장일/데이터 미생성일(예 6/6 토)은 `/pm320/{date}.html` 빌드 산출물이 없어
  //   새로고침 시 GitHub Pages 404. 정적 페이지 존재가 확인된 날(_dateHasStaticPage)만 URL 갱신하고,
  //   그 외에는 base(`/pm320.html`)로 유지 → 새로고침 200 + 휴장 suppress 진입(initCalendar 경로) 정합.
  if (pushState !== false) {
    if (_dateHasStaticPage(date)) {
      history.pushState(null, '', '/pm320/' + date + '.html');
    } else {
      history.pushState(null, '', '/pm320.html');
    }
  }
  renderCalendar();
  const inner = document.getElementById('cal-content');
  inner.innerHTML = `
    <div class="cal-content-head" role="button" tabindex="0" aria-label="달력으로 이동" data-scroll-to-cal="1">
      <div class="cal-content-date">${formatKoDate(date)}</div>
      <div class="cal-content-meta">불러오는 중…</div>
    </div>
    <div class="cal-empty"><div>데이터 로드 중</div></div>
  `;
  const data = await loadCalDayData(date);
  renderCalExpandContent(date, data);
  // 테마트리도 해당 날짜 기준으로 재렌더링 (휴장일은 안내 메시지)
  initThemeTree(date);
}

async function initCalendar() {
  const meta = document.getElementById('meta');
  if (meta) meta.textContent = '';

  // Q-20260606-118 결함(B) — page-manifest 조기 prefetch (논블로킹). 첫 달력 클릭 전 window._pageManifest
  //   워밍 → _dateHasStaticPage 가 manifest(라이브 SSOT)로 판정. 미도착 시 calHasData 보수 폴백(무파손).
  try { if (typeof _loadPageManifest === 'function') _loadPageManifest(); } catch (_) { /* graceful */ }

  // 1단계: localStorage 캐시에서 즉시 복원 (fetch 0건, ~10ms)
  const cachedCalIndex = (() => { try { return JSON.parse(localStorage.getItem('calIndex') || 'null'); } catch { return null; } })();
  calIndex = cachedCalIndex;
  holidayData = (() => { try { return JSON.parse(localStorage.getItem('holidayData') || 'null'); } catch { return null; } })();
  themesData = (() => { try { return JSON.parse(localStorage.getItem('themesData') || 'null'); } catch { return null; } })();

  const now = _kstNow(); // 해외 접속 TZ 무관 KST "오늘" — 초기 진입 시 today 기준
  const todayStr = ymd(now.getFullYear(), now.getMonth() + 1, now.getDate());
  // URL ?cat= / ?date= 파라미터. cat 기본값 stock.
  const urlParams = new URLSearchParams(window.location.search);
  const urlCat = urlParams.get('cat');
  if (urlCat && ['stock', 'realestate', 'policy'].includes(urlCat)) calCategory = urlCat;
  const urlDate = urlParams.get('date');
  // 해시 앵커(#2026-04-10) 지원 — news/YYYY-MM-DD.html에서 리다이렉트
  const hashDate = window.location.hash.replace('#', '');
  const hasUrlDate = (urlDate && /^\d{4}-\d{2}-\d{2}$/.test(urlDate))
    || (hashDate && /^\d{4}-\d{2}-\d{2}$/.test(hashDate));
  let initialDate = hasUrlDate ? (urlDate || hashDate) : todayStr;
  // Q-20260606-113 (대표 verbatim "국내장 종목은 토요일에는 안보이게 해야지") — 주말·휴장일 국내장 카드 비노출.
  //   판정 = 단순 요일이 아닌 isMarketClosed(주말 + KRX market_closed 휴장일 SSOT). 오늘이 비거래일이고
  //   URL 명시 날짜가 없을 때(=자동 폴백 진입)만 suppress 플래그를 세운다. 사용자가 달력에서 과거 거래일을
  //   직접 클릭하면(onCalCellClick) 플래그 해제 → 그 날 국내장 카드는 정상 표시(무회귀). 야간 미국증시 섹션은
  //   본 플래그와 무관(renderer _nightlyUsHtml 별 path) → 토요일 아침 최신 미장 데이터는 항상 유지.
  const _todayClosed = (typeof isMarketClosed === 'function')
    ? isMarketClosed(todayStr)
    : (isWeekendDate(todayStr) || isHoliday(todayStr));
  window._pm320SuppressDomesticCards = (!hasUrlDate && _todayClosed);
  // Q-20260606-121 (대표 verbatim "6월 6일인데 6월 5일로 기본 선택되고 ... 일치가 안되는 중") —
  //   주말·휴장 기본 진입 시 기본 선택 날짜 = 오늘(todayStr) 유지. 종래엔 직전 거래일(6/5)로 폴백했으나
  //   suppress 로 국내장 카드를 어차피 숨기므로 직전 거래일 데이터가 불필요하고, 라벨/달력 하이라이트가
  //   "6월 5일 (금) 주말·휴장" 으로 표시되어 라벨(거래일)·동작(숨김) 불일치 + 미장(토 아침 최신) 시제 어긋남.
  //   기본 날짜를 오늘로 두면 헤더 "6월 6일 (토) 주말·휴장" + 국내장 suppress + 미장 6/6 데이터(us-indices
  //   /{today}.json) 로 시제 정합. 직전 거래일 데이터는 안내문구대로 달력에서 선택(Q-118 명시 클릭 무회귀).
  //   (구 폴백 로직 제거 — 평일은 종전대로 todayStr 유지, 휴장도 todayStr 유지.)
  const [iy, im] = initialDate.split('-').map(Number);
  calViewYear = iy;
  calViewMonth = im;
  calSelectedDate = initialDate;

  // 2단계: 달력 UI 즉시 렌더 (캐시 기반, fetch 안 기다림)
  renderCalendar();

  // R46 P0-2① — 캘린더 접힘 상태 복원 (localStorage 'pm320SectionExpand' 키 'toss-cal').
  //   기본 = 접힘(정적 markup), 사용자가 펼쳐둔 적 있으면 복원 — 다른 접힘 섹션과 동일 문법.
  if (typeof _applySectionCollapse === 'function') {
    _applySectionCollapse(document.getElementById('toss-cal'), 'toss-cal');
  }

  // 3단계: 캐시된 당일 데이터로 즉시 카드 렌더 (있으면)
  // DSN-frontend §3.6.2.2 (2026-06-05 P0 라이브 재발) — OPEN 시점 stale fallback 캐시 차단.
  //   PRE_MARKET 에 박제된 fallback 캐시(_fallbackDate)를 09:00 이후 reload 시 stage-3 동기 렌더가
  //   "오늘 데이터인 양" 즉시 표시하던 결함(어제 뉴스/PM320 종목카드 노출, 대표 catch 09:05/09:15).
  //   data-loader.js 의 fetch-time 가드(_isTodayPastOpen)와 동일 기준으로, 캐시 엔트리도 OPEN+today+fallback
  //   3조건 동시 충족 시 stage-3 렌더를 건너뛰고 캐시를 폐기 → 4단계 네트워크 갱신(정직한 빈 상태)으로 위임.
  //   과거 viewDate / 휴장 / PRE_MARKET 의 정상 fallback 표시는 영향 없음. PRE_MARKET opt-in 토글도 별 path 유지.
  // §3.6.2.3 — 세션 구간 키로 조회. 장경계를 넘었으면 이전 구간 키와 불일치 → cache miss(재표시 0).
  const _initialKey = _cacheKey(initialDate);
  const _cachedEntry = calDayCache[_initialKey];
  // _fallbackDate 마커 (신규 캐시) 또는 macroEvents 안내 배너 (구 스키마 캐시) 양쪽으로 fallback 탐지.
  //   구 캐시는 _fallbackDate 필드가 없으므로 "기준 데이터를 표시" 배너 문구로 보강 탐지.
  const _cacheIsFallback =
    _cachedEntry &&
    (!!_cachedEntry._fallbackDate ||
      (Array.isArray(_cachedEntry.macroEvents) &&
        _cachedEntry.macroEvents.some(m => m && typeof m.summary === 'string' && m.summary.includes('기준 데이터를 표시'))));
  const _isStaleOpenFallback =
    _cacheIsFallback &&
    typeof _isTodayPastOpen === 'function' &&
    _isTodayPastOpen(initialDate);
  // §3.6.2.3 — 세션 구간 키 구조화가 stale OPEN fallback 을 구조적으로 봉쇄(PRE_MARKET fallback 은
  //   date@PRE_MARKET 키에 박제 → OPEN 시 date@OPEN 조회 = miss). 본 3조건 가드는 구 스키마 잔존
  //   캐시(flat date 키 = _cacheKey가 오늘이면 date@SEGMENT 반환하므로 자연 miss이나, 만약을 위한)
  //   defense-in-depth 로 유지. 폐기 대상은 실제 조회 키(_initialKey).
  if (_isStaleOpenFallback) {
    delete calDayCache[_initialKey];
    _persistCache();
  }
  if (_cachedEntry && !_isStaleOpenFallback) {
    toggleThemeSections(initialDate);
    renderCalExpandContent(initialDate, _cachedEntry);
  } else {
    // 캐시 없음 — 로딩 표시
    toggleThemeSections(initialDate);
    const inner = document.getElementById('cal-content');
    if (inner) inner.innerHTML = '<div class="cal-content-head" role="button" tabindex="0" aria-label="달력으로 이동" data-scroll-to-cal="1"><div class="cal-content-date">' + formatKoDate(initialDate) + '</div><div class="cal-content-meta">불러오는 중\u2026</div></div><div class="cal-empty"><div>데이터 로드 중</div></div>';
  }

  // 4단계: 비동기 네트워크 갱신 (사용자가 기다리지 않음)
  _refreshDataAsync(initialDate);

  // 이벤트 리스너 (동기, 즉시)
  window.addEventListener('popstate', () => {
    const p = new URLSearchParams(window.location.search);
    const d = p.get('date');
    const h = window.location.hash.replace('#', '');
    const date = d || (h && /^\d{4}-\d{2}-\d{2}$/.test(h) ? h : null);
    const c = p.get('cat');
    if (c && ['stock', 'realestate', 'policy'].includes(c)) calCategory = c;
    if (date && /^\d{4}-\d{2}-\d{2}$/.test(date)) onCalCellClick(date, false);
  });

  document.getElementById('toss-cal-prev').addEventListener('click', () => {
    calViewMonth--;
    if (calViewMonth < 1) { calViewMonth = 12; calViewYear--; }
    renderCalendar();
  });
  document.getElementById('toss-cal-next').addEventListener('click', () => {
    const now2 = _kstNow(); // 해외 접속 TZ 무관 KST "이번 달" — 미래월 네비 제한 기준
    if (calViewYear > now2.getFullYear() ||
        (calViewYear === now2.getFullYear() && calViewMonth >= now2.getMonth() + 1)) return;
    calViewMonth++;
    if (calViewMonth > 12) { calViewMonth = 1; calViewYear++; }
    renderCalendar();
  });
}

// 비동기 데이터 갱신 — 초기 렌더 후 백그라운드
async function _refreshDataAsync(initialDate) {
  try {
    // 네트워크에서 최신 메타 데이터 fetch (병렬)
    const [calIdx, themes, holidays] = await Promise.all([
      loadCalendarIndex(), loadThemes(), loadHolidayData()
    ]);
    if (calIdx) { calIndex = calIdx; try { localStorage.setItem('calIndex', JSON.stringify(calIdx)); } catch {} }
    if (themes) { themesData = themes; try { localStorage.setItem('themesData', JSON.stringify(themes)); } catch {} }
    if (holidays) { holidayData = holidays; try { localStorage.setItem('holidayData', JSON.stringify(holidays)); } catch {} }

    // 달력 재렌더 (인덱스 업데이트 반영)
    renderCalendar();

    // 당일 데이터 강제 재로드 (캐시 무시) + 카드 렌더
    // §3.6.2.3 — loadCalDayData 가 읽고 쓰는 세션 구간 키를 폐기해야 강제 재로드가 동작.
    delete calDayCache[_cacheKey(initialDate)];
    const data = await loadCalDayData(initialDate);
    renderCalExpandContent(initialDate, data);

    // 테마 트리/트렌드 초기화 (휴장일은 함수 내부에서 안내 메시지 표시)
    initThemeTree(initialDate);
    initThemeTrend();
    initLimitUpTrend();
    initThemeMap();
  } catch (e) {
    console.warn('_refreshDataAsync:', e);
  }
}
