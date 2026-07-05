/* ───── lib/chart-tv/plugins/markers.js — #6 배당락 + RSI 과매도 marker primitive (TradingView v5) ─────
   cycle22 Phase 7c → Phase 7d-1 P0-4 정정 cascade — 영웅문 정합 본질.

   본질 (lead-meta §11.15 외부 spec 사전 검증 PASS):
   - TradingView Lightweight Charts v5에서 `series.setMarkers()` **deprecated**.
   - v5 정합 = `createSeriesMarkers(series, [...])` separate primitive 패턴 의무.
   - WebFetch corroborate:
     * https://tradingview.github.io/lightweight-charts/docs/migrations/from-v4-to-v5
       "Markers moved to separate primitive for optimized bundle size"
     * https://tradingview.github.io/lightweight-charts/tutorials/how_to/series-markers
       v5 정합 패턴: createSeriesMarkers(series, [...markers]) + seriesMarkers.setMarkers([...]) 후속 갱신

   P0-4 영웅문 정합 정정 cascade (2026-05-21 10:02 KST 대표 critical 정정):
   - 분홍 강세 본질 = vertical line primitive (별건 plugins/pink-signal.js 신축)
   - 본 markers.js는 배당락 + RSI 과매도 2종 marker만 처리 (분홍 강세 marker 본문 폐기)

   marker 2종 본질 (P0-4 정정 정합):
   - #6 배당락 marker: position='belowBar', shape='circle', color='#6B7A99' (--neu 회색 정합), text='배당락'
     * 입력 source = options.exDividendDates (Array<string 'YYYY-MM-DD'>)
   - RSI 과매도 marker (P0-4 신축, 영웅문 verbatim): position='aboveBar', shape='arrowDown', color='#1F2937', text='RSI<30'
     * 입력 source = options.rsiOversoldDates (Array<string 'YYYY-MM-DD'>) — RSI 14 < 30 시점 자동 추출 (expanded-chart.js extractRSIOversoldDates 본질)

   §16 self-catch (Phase 7d-1 P0-4):
   - 분홍 강세 marker 본문 (createSeriesMarkers aboveBar arrowUp) 폐기 — 본질 정정 cascade
   - BusinessDay vs UNIX timestamp 본질: TradingView v5 marker time = candle series time 본질과 동일 type 의무.
     candle series가 BusinessDay 객체 {year, month, day} 사용 → marker time도 BusinessDay 객체로 변환 의무.
*/

import { createSeriesMarkers } from 'https://cdn.jsdelivr.net/npm/lightweight-charts@5.0.8/+esm';

// P0-4 영웅문 정합 정정 cascade (2026-05-21 10:02 KST 대표 critical 정정):
//   분홍 강세 본질 = vertical line primitive (별건 plugins/pink-signal.js 신축)
//   본 markers.js는 배당락 + RSI 과매도 2종 marker만 처리 (분홍 강세 PINK_SIGNAL_OPTIONS 폐기)

// P0-20 Fix-65 (2026-05-21 17:46 KST 대표 verbatim "macd 화살표를 더 작게"):
//   본 markers.js 본문 RSI 과매도 arrowDown / 배당락 circle 본문 size 본문 동일 본질 적용 (영웅문 reference 본문 visible 매우 작음 정합).
//   §11.15 외부 spec 사전 검증 PASS — SeriesMarker.size?: number, default 1 (https://tradingview.github.io/lightweight-charts/docs/api/interfaces/SeriesMarkerBar).
const MARKER_SIZE = 0.3;

const EX_DIVIDEND_OPTIONS = {
  position: 'belowBar',
  shape: 'circle',
  color: '#6B7A99',
  text: '배당락',
  size: MARKER_SIZE,
};

// P0-4 영웅문 정합 fix #3 (2026-05-21 10:01 KST 대표 정정 verbatim):
//   "영웅문 차트 캔들 상단의 아래쪽 검은 화살표는 RSI 과매도 신호이다"
//   RSI 14 < 30 시점 = 과매도 본질 (영웅문 verbatim 임계값) → aboveBar arrowDown 검정 marker
// P0-7 fix-11 (2026-05-21 11:19 KST 대표 verbatim
//   "차트에서 RSI<30 이라는 레이블이 검은 화살표위에 붙어있는데 화살표만 있으면 된다. 레이블은 생략해줘. 보기 지져분해져."):
//   text 본문 빈 string 본질 정정 — 화살표만 visible + 라벨 부재 (영웅문 정합)
const RSI_OVERSOLD_OPTIONS = {
  position: 'aboveBar',
  shape: 'arrowDown',
  color: '#1F2937',  // 영웅문 verbatim 검정 (gracefully gray-900)
  text: '',           // P0-7 fix-11 — 라벨 제거 (화살표만 visible)
  size: MARKER_SIZE,  // P0-20 Fix-65 — 0.3 본문 축소
};

/**
 * 'YYYY-MM-DD' string → BusinessDay {year, month, day} 변환.
 * candle series time schema (expanded-chart.js L129 normalizeData verbatim) 정합.
 *
 * @param {string} dateStr
 * @returns {{year, month, day}|null}
 */
function toBusinessDay(dateStr) {
  if (!dateStr || typeof dateStr !== 'string') return null;
  const parts = String(dateStr).slice(0, 10).split('-');
  if (parts.length !== 3) return null;
  const y = parseInt(parts[0], 10);
  const m = parseInt(parts[1], 10);
  const d = parseInt(parts[2], 10);
  if (!y || !m || !d) return null;
  return { year: y, month: m, day: d };
}

/**
 * 배당락 + RSI 과매도 marker 통합 attach.
 *
 * 본질: v5 marker primitive 단일 호출 (분리된 series.setMarkers() 2회 deprecated 패턴 회피).
 * 본 wrapper는 marker 배열 build + createSeriesMarkers 1회 호출.
 *
 * P0-4 영웅문 정합 정정 (2026-05-21 10:02 KST 대표 critical 정정):
 *   분홍 강세 본질 = vertical line primitive (별건 plugins/pink-signal.js 신축, markers.js scope 외)
 *
 * P0-4 영웅문 정합 fix #3 — RSI 과매도 marker 신축 (2026-05-21 10:01 KST 대표 정정):
 *   영웅문 verbatim "캔들 상단의 아래쪽 검은 화살표 = RSI 과매도 신호"
 *
 * @param {ISeriesApi} candleSeries — 캔들 series (markers attach 대상)
 * @param {Object} [options]
 * @param {Array<string>} [options.exDividendDates] — 배당락 일자 (Array<'YYYY-MM-DD'>)
 * @param {Array<string>} [options.rsiOversoldDates] — RSI 과매도 일자 (Array<'YYYY-MM-DD'>, RSI<30 시점)
 * @returns {ISeriesMarkers|null} — createSeriesMarkers return (제거 시 destroy() 또는 setMarkers([]) 호출)
 */
export function attachMarkers(candleSeries, options = {}) {
  if (!candleSeries) return null;

  const exdDates = Array.isArray(options.exDividendDates) ? options.exDividendDates : [];
  const rsiOversoldDates = Array.isArray(options.rsiOversoldDates) ? options.rsiOversoldDates : [];

  const markers = [];

  // #6 배당락 marker (SPEC v6 §3.4 verbatim — belowBar circle #6B7A99)
  exdDates.forEach((dateStr) => {
    const time = toBusinessDay(dateStr);
    if (!time) return;
    markers.push({
      time,
      ...EX_DIVIDEND_OPTIONS,
    });
  });

  // P0-4 RSI 과매도 marker (영웅문 verbatim — aboveBar arrowDown #1F2937)
  rsiOversoldDates.forEach((dateStr) => {
    const time = toBusinessDay(dateStr);
    if (!time) return;
    markers.push({
      time,
      ...RSI_OVERSOLD_OPTIONS,
    });
  });

  if (markers.length === 0) return null;

  // markers time 기준 정렬 (TradingView v5 marker primitive 요구 본질 — ascending time order)
  markers.sort((a, b) => {
    const ta = a.time.year * 10000 + a.time.month * 100 + a.time.day;
    const tb = b.time.year * 10000 + b.time.month * 100 + b.time.day;
    return ta - tb;
  });

  // v5 unified marker primitive 단일 호출 (SPEC §15 + §3.4 verbatim)
  try {
    return createSeriesMarkers(candleSeries, markers);
  } catch (err) {
    // silent fail — marker primitive 미지원 또는 series detach 등 edge case
    return null;
  }
}

/**
 * 기존 attach한 markers 갱신 (재계산 필요 시 setMarkers([...]) 후속 호출).
 *
 * @param {ISeriesMarkers} seriesMarkers — attachMarkers return
 * @param {Object} options — exDividendDates / rsiOversoldDates (attachMarkers와 동일 schema)
 */
export function updateMarkers(seriesMarkers, options = {}) {
  if (!seriesMarkers || typeof seriesMarkers.setMarkers !== 'function') return;

  const exdDates = Array.isArray(options.exDividendDates) ? options.exDividendDates : [];
  const rsiOversoldDates = Array.isArray(options.rsiOversoldDates) ? options.rsiOversoldDates : [];

  const markers = [];
  exdDates.forEach((dateStr) => {
    const time = toBusinessDay(dateStr);
    if (!time) return;
    markers.push({ time, ...EX_DIVIDEND_OPTIONS });
  });
  rsiOversoldDates.forEach((dateStr) => {
    const time = toBusinessDay(dateStr);
    if (!time) return;
    markers.push({ time, ...RSI_OVERSOLD_OPTIONS });
  });
  markers.sort((a, b) => {
    const ta = a.time.year * 10000 + a.time.month * 100 + a.time.day;
    const tb = b.time.year * 10000 + b.time.month * 100 + b.time.day;
    return ta - tb;
  });

  try {
    seriesMarkers.setMarkers(markers);
  } catch (err) {
    // noop
  }
}

/**
 * markers detach (chart remove 시 정리 본질).
 *
 * @param {ISeriesMarkers} seriesMarkers — attachMarkers return
 */
export function detachMarkers(seriesMarkers) {
  if (!seriesMarkers) return;
  try {
    // v5 marker primitive detach 패턴 — setMarkers([]) 빈 배열로 reset (destroy() 메서드 없음)
    if (typeof seriesMarkers.setMarkers === 'function') {
      seriesMarkers.setMarkers([]);
    }
  } catch (err) {
    // noop
  }
}

if (typeof window !== 'undefined') {
  window.ChartTVPluginMarkers = { attachMarkers, updateMarkers, detachMarkers };
}
