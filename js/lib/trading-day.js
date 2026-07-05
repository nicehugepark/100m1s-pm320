/* ───── lib/trading-day.js — 거래일·휴장 캘린더 산출 (utils.js 추출) ─────
   분리 commit: REQ-001 §3 Phase 1. DSN: DOC-20260430-DSN-001-arch-frontend §3.1.
   IIFE + window 전역 등록 (SW cache 호환). 동작 100% 동일 보존.
   추출 함수: formatYMD, getNextTradingDay, dsnV9FormatMD, computeTradingDayDiff, getShortTermDayOffset.
   참조 데이터: window.KOREA_HOLIDAYS (data-loader.js 주입).
*/
(function (root) {
  'use strict';

  function formatYMD(date) {
    // Date → YYYY-MM-DD
    const y = date.getFullYear();
    const m = String(date.getMonth() + 1).padStart(2, '0');
    const d = String(date.getDate()).padStart(2, '0');
    return `${y}-${m}-${d}`;
  }

  function getNextTradingDay(dateStr) {
    // §E.4 renderer 측 안전망. 우선순위: build_daily.py 산출 next_trading_day_for_predicted 신뢰.
    // 본 함수는 view_date+1 거래일 비교용(getPredictedTenseVariant 내부) 또는 build_daily 미산출 케이스 폴백.
    // 이시카와 P0 — 연 경계 가드: holidays.json은 2026 단년. 2027+ view_date 산출 시 캘린더+1 폴백 + warn (FLR-20260425).
    // KOREA_HOLIDAYS estimated 등급 hit 시 console.warn 1회 (FLR-20260423-FLR-002 verified 절차).
    if (!dateStr) return '';
    const holidaysData = (typeof window !== 'undefined' && window.KOREA_HOLIDAYS) || null;
    const holidaysYear = holidaysData && holidaysData.year ? Number(holidaysData.year) : null;
    const holidaysSet = holidaysData && holidaysData.holidays ? new Set(Object.keys(holidaysData.holidays)) : null;
    const marketClosedSet = holidaysData && holidaysData.market_closed ? new Set(Object.keys(holidaysData.market_closed)) : null;
    const isEstimated = holidaysData && holidaysData.verification_status === 'estimated';

    const date = new Date(dateStr);
    if (isNaN(date.getTime())) return '';
    let next = new Date(date);
    let safety = 14;
    while (safety-- > 0) {
      next.setDate(next.getDate() + 1);
      const nextYear = next.getFullYear();
      // 이시카와 P0 연 경계 가드 — holidays.json 데이터 연도 초과 시 캘린더+1 폴백 (주말만 스킵)
      if (holidaysYear && nextYear > holidaysYear) {
        if (typeof console !== 'undefined') {
          console.warn(`[DSN-v9.1] getNextTradingDay: ${nextYear}+ holidays data missing (loaded year ${holidaysYear}), fallback to calendar+1 weekday only (FLR-20260425). build_daily.py 산출 신뢰 권고.`);
        }
        const dowFb = next.getDay();
        if (dowFb === 0 || dowFb === 6) continue;
        return formatYMD(next);
      }
      const dow = next.getDay();
      if (dow === 0 || dow === 6) continue;
      const ymd = formatYMD(next);
      if (holidaysSet && holidaysSet.has(ymd)) {
        if (isEstimated && typeof console !== 'undefined') {
          console.warn(`[DSN-v9.1] getNextTradingDay: holidays.json estimated grade hit (${ymd}). build_daily.py 산출 신뢰 권고.`);
        }
        continue;
      }
      if (marketClosedSet && marketClosedSet.has(ymd)) {
        if (isEstimated && typeof console !== 'undefined') {
          console.warn(`[DSN-v9.1] getNextTradingDay: holidays.json estimated grade hit (${ymd}). build_daily.py 산출 신뢰 권고.`);
        }
        continue;
      }
      return ymd;
    }
    return '';
  }

  function dsnV9FormatMD(dateStr) {
    // YYYY-MM-DD → M/D
    if (!dateStr) return '';
    const m = String(dateStr).match(/^(\d{4})-(\d{1,2})-(\d{1,2})/);
    if (!m) return dateStr;
    return `${parseInt(m[2], 10)}/${parseInt(m[3], 10)}`;
  }

  function computeTradingDayDiff(startDate, viewDate) {
    // v9.3 §III.2 — 영업일 차이 산출 (휴장 제외). startDate=D+0, viewDate가 D+N이면 N 반환.
    // 음수=발효 전, 0+=발효 후. KOREA_HOLIDAYS·marketClosed 의존 (getNextTradingDay와 동일 데이터 셋).
    if (!startDate || !viewDate) return null;
    if (startDate === viewDate) return 0;
    const sd = new Date(startDate);
    const vd = new Date(viewDate);
    if (isNaN(sd.getTime()) || isNaN(vd.getTime())) return null;
    // 발효 전 (음수)
    if (vd < sd) {
      return -computeTradingDayDiff(viewDate, startDate); // 재귀로 부호 반전
    }
    // 영업일 카운트 (start 다음 영업일부터 view까지)
    const holidaysData = (typeof window !== 'undefined' && window.KOREA_HOLIDAYS) || null;
    const holidaysSet = holidaysData && holidaysData.holidays ? new Set(Object.keys(holidaysData.holidays)) : null;
    const marketClosedSet = holidaysData && holidaysData.market_closed ? new Set(Object.keys(holidaysData.market_closed)) : null;
    let cur = new Date(sd);
    let n = 0;
    let safety = 30;
    while (safety-- > 0) {
      cur.setDate(cur.getDate() + 1);
      const dow = cur.getDay();
      const ymd = formatYMD(cur);
      if (dow === 0 || dow === 6) {
        if (ymd === formatYMD(vd)) return n; // viewDate가 휴일이어도 자기 위치 0 반환 (보수)
        continue;
      }
      if (holidaysSet && holidaysSet.has(ymd)) {
        if (ymd === formatYMD(vd)) return n;
        continue;
      }
      if (marketClosedSet && marketClosedSet.has(ymd)) {
        if (ymd === formatYMD(vd)) return n;
        continue;
      }
      n += 1;
      if (ymd === formatYMD(vd)) return n;
    }
    return null;
  }

  function getShortTermDayOffset(badge, viewDate) {
    // v9.3 §III.2 + 사이클 2.5 — 단기과열 트랙 D 결정. D+0/D+1 분리 (효과 부재 동일 처리이나 디버그·메트릭 추적용).
    // 발효 전 → 'd+0' (badge.start 미도래 — viewDate < start 케이스. 사이클 2.5 정정: 지정 당일=days=0)
    // days=0 (지정 당일=D+0 — viewDate==badge.start) / days=1 → 'd+1' (D+1) / days=2 → 'd+2' / 3~5 → 'd+3-5' / 6+ → 'd+6+'
    if (!badge || !badge.start || !viewDate) return 'unknown';
    const days = computeTradingDayDiff(badge.start, viewDate);
    if (days === null) return 'unknown';
    if (days < 0) return 'd+0';      // 발효 전 (badge.start 미도래) — D+0과 동일 처리(효과 부재)
    if (days === 0) return 'd+0';    // 지정 당일 (D+0)
    if (days === 1) return 'd+1';    // D+1
    if (days === 2) return 'd+2';    // D+2 (거래정지 1일)
    if (days >= 3 && days <= 5) return 'd+3-5';  // D+3~D+5 (단일가매매)
    return 'd+6+';                   // D+6+ (자동 해제)
  }

  // 전역 등록 (utils.js·renderer.js 잔존 호출부 호환)
  root.formatYMD = formatYMD;
  root.getNextTradingDay = getNextTradingDay;
  root.dsnV9FormatMD = dsnV9FormatMD;
  root.computeTradingDayDiff = computeTradingDayDiff;
  root.getShortTermDayOffset = getShortTermDayOffset;
})(typeof window !== 'undefined' ? window : this);
