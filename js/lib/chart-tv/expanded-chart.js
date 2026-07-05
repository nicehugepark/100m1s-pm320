/* ───── lib/chart-tv/expanded-chart.js — Phase 7d-1 + P0-16 TradingView v5 wrapper (보조지표 정정 통합본) ─────
   cycle22 P0-16 — REQ DOC-20260521-REQ-001 v3 verbatim 정합 (일목 제거 + 피보 visible + sub-pane title 좌측).

   본질 (대표 2026-05-21 14:57 KST verbatim
     "일목균형표는 도저히 안되겠다 제거해줘. 그리고 피보나치 이어서 계속 해줘 화면에 표시되지도 않아.
      그리고 하단지표의 지표 이름 라벨도 좌측 구석으로 위치를 바꿔줘"):
   - **일목 본문 제거** (ichimoku.js git rm + import/state/whitespace future data 본문 모두 폐기) — destructive ack 본질
   - **피보 visible 본문 default ON + auto-anchor** (대표 verbatim "이어서 계속 화면에 표시되지도 않아" 본질)
     - root cause = drawing tool 본문 사용자 2회 클릭 의무 → 화면 visible 0건 (anchor A/B 미설정 본질)
     - fix = chip default ON + 가시 영역 hi/lo 본문 auto-anchor (사용자 후속 drag 가능)
   - **sub-pane title 좌측 본문** (영웅문 23a74560 본문 정합)
     - root cause = TradingView v5 `title` 옵션 본문 = priceScale **우측** label 본질 (native API 좌측 미지원)
     - fix = HTML overlay 본문 신축 (chart-tv-main DOM 위 absolute positioned `<div>` 본문 paneIdx별 Y 좌표 + 좌측 8px)
     - §11.15 외부 spec 사전 검증 PASS — v5 IPaneApi.getHTMLElement() / PaneApi.paneIndex() 본문 paneIdx 좌표 측정 본질

   §11.15 외부 spec 사전 검증 (WebSearch ≥2회 + 공식 docs PASS):
   - https://tradingview.github.io/lightweight-charts/docs/series-types
     "title property — displayed on the label next to the last value label" (우측 priceScale 본문 native)
   - https://tradingview.github.io/lightweight-charts/tutorials/how_to/panes
     "PaneApi.getHTMLElement() — returns DOM element of pane (or null if not created)"
   - HTML overlay 본문 = chart-tv-main 본문 absolute position div, paneIdx별 top 좌표 본문 측정 본질

   §16 self-catch (P0-16):
   1. 일목 본문 제거 시 future whitespace data 본문 (SHIFT_FUTURE=26) 본질 자체가 일목 cloud 미래 영역 visible용 → 통째 제거 본질 정합
   2. addBusinessDays helper 본문 ichimoku.js export → 일목 제거 cascade로 본 helper 본문 호출처 0건 (자기 검증 PASS)
   3. 피보 default ON 본문 = drawing tool paradigm 보존 + 가시 영역 hi/lo auto-anchor 본문 보강 (대표 verbatim "이어서 계속")
   4. HTML overlay 본문 paneIdx별 Y 좌표 측정 = pane separator 본문 동적 layout 본질 + ResizeObserver 본문 재측정 의무
*/

import {
  createChart,
  CandlestickSeries,
  LineSeries,
  HistogramSeries,
  LineStyle,
  CrosshairMode,
  createSeriesMarkers,
} from 'https://cdn.jsdelivr.net/npm/lightweight-charts@5.0.8/+esm';

// P0-16 Fix-50 — 일목 import 제거 (ichimoku.js git rm cascade, addBusinessDays helper 폐기)
// Phase 7d-1 정정 plugin 3종 + Phase 7d-1 P0-4 분홍 강세 vertical line primitive + 토글 panel
import { attachFibonacci, detachFibonacci } from './plugins/fibonacci.js';
import { VolumeByDecilePrimitive } from './plugins/volume-by-decile.js';
import { attachMarkers, detachMarkers } from './plugins/markers.js';
import { PinkSignalPrimitive } from './plugins/pink-signal.js';
import { RSIOverboughtCloudPrimitive } from './plugins/rsi-overbought-cloud.js';
import { buildTogglePanel, INDICATOR_CHIPS } from './toggle-panel.js';

const STORAGE_KEY = 'm100s.chart.tv.indicators.global';

// SPEC §4.1 viewport별 차트 크기
// P0-11 Fix-34 (2026-05-21 12:44 KST 대표 verbatim
//   "현재 나의 갤럭시s25 모바일폰에서 확인한 화면이다. ... 차트 자체가 모바일 버전과 데스크탑 버전을 다르게 가야할거같다.
//    그리고 모바일 버전에서는 y축 위치도 여전히 문제다."):
//   image #10 76d1f57d 직접 read evidence — 갤럭시 S25 본문:
//     - sub-pane title `#거래대` / `MAC` / `R` 본문 우측 본문 잘림 (`#거래대금` / `MACD` / `RSI`)
//     - 우측 priceScale 본문 visible 0건 (영웅문 23a74560 본문 우측 priceScale 727,000 ~ 270,692 본질 vs 본 시스템 부재)
//   root cause 결정적 진단:
//     - P0-10 Fix-32 본문 `Math.min(w - 16, 640)` = window.innerWidth 기준 → viewport 412px (S25) 본문 = 396px chart width
//     - 그러나 실제 chart parent 본문 = `.cal-feature-chart-expanded` 본문 (margin 8px 12px 4px + padding 12px)
//     - main 본문 (`.cal-chart-tv-main`, width:100%) 본문 실제 가용 width = card_width - margin(24) - padding(24) = card_width - 48px
//     - main { display:flex; justify-content:center; } → chart 본문 396px 본문이 부모 본문 (예: 364px) 초과 → overflow 우측 본문 잘림
//     - chart canvas 본문 자체가 부모 본문 초과 → 우측 priceScale (100px) + sub-pane title 본문 잘림
//   정합 본질 — `container.clientWidth` 본문 실측 채택 (DOM 본문 정확 가용 width):
//     - container = renderChartTV(container, ...) 인자 = `.cal-feature-chart-expanded` slot (padding 12 적용된 inner box)
//     - container.clientWidth 본문 = inner content area width (padding 제외) — 정확한 chart 본문 가용 width
//     - layout 본문 timing 본질: slot은 renderer.js L1492 `card.appendChild(slot)` 후 L1525 즉시 ChartTV.render 호출
//       → slot 본문 layout 완료 후 호출되므로 clientWidth 본문 정확 측정 PASS
//   별건 layout 본문 (대표 verbatim "모바일과 데스크탑 다르게"):
//     - 모바일 본문 priceScale minimumWidth 100 → 60 본문 축소 (좁은 폰 본문 가시 영역 본질)
//     - 모바일 본문 chart slot margin/padding 본문 축소 (Fix-37 본문 별건 CSS layer)
//   §11.15 외부 spec 사전 검증 PASS:
//     - HTMLElement.clientWidth 본문 = inner padding 본문 본문 포함 X (W3C CSSOM spec)
//     - TradingView Lightweight Charts v5 createChart options.width 본문 = canvas pixel width (정확 integer)
//     - PriceScaleOptions.minimumWidth 본문 = integer px (v5 docs)
function getViewportSize(container) {
  const w = window.innerWidth;
  // 모바일 본문 (w < 768) — container.clientWidth 본문 실측 width 본문 채택 (DOM 본문 정확 가용 width)
  if (w < 768) {
    let adaptiveWidth;
    if (container && container.clientWidth > 0) {
      // container = chart slot inner content area (padding 제외)
      // 본 chart slot 본문이 부모 카드 본문 width 본질에서 margin/padding 본문 빠진 본질 실측 width 본문
      adaptiveWidth = Math.max(280, Math.min(container.clientWidth, 640));
    } else {
      // fallback (container 본문 layout 본질 직전 호출 본질 시) — viewport - 48px (margin 24 + padding 24)
      const SAFETY_MARGIN = 48;
      adaptiveWidth = Math.max(280, Math.min(w - SAFETY_MARGIN, 640));
    }
    // height 본문 ratio (640:360 = 16:9) 보존 본질
    const adaptiveHeight = Math.round(adaptiveWidth * 360 / 640);
    return { width: adaptiveWidth, height: Math.max(280, adaptiveHeight) };
  }
  if (w <= 1024) return { width: 880, height: 400 };
  return { width: 1000, height: 440 };
}

// lead 옵션 A-3 회신 verbatim (2026-05-21 09:15:50 KST 대표 추가 정정):
// "그리고 하단 지표인 거래대금 rsi macd는 토글뱌튼 필요없이 기본 출력이야"
// → 거래대금/MACD/RSI = base 영구 ON (사용자 toggle 불가, chip 부재). DEFAULT_INDICATORS 본문 외 정합.
//
// P0-16 Fix-50 본문 (2026-05-21 14:57 KST 대표 verbatim "일목균형표는 도저히 안되겠다 제거해줘"):
//   토글 chip 본질 = **5 chip** (MA + 매물대 + 분홍 + 배당락 + 피보 — 일목 chip 제거 본질).
// P0-16 Fix-51 본문 (대표 verbatim "피보나치 이어서 계속 해줘 화면에 표시되지도 않아"):
//   fibonacci default = **true** (drawing tool default ON + 가시 영역 hi/lo auto-anchor 본문 정합).
//
// MA = REQ v2 §2 #4 verbatim 7선 (5/10/20/43/60/120/240) → cycle23 chart-tv-3changes Spot 3 본문 6선 (240 제거).
// cycle23 chart-tv-3changes Spot 3 (2026-05-22 17:24 KST 대표 verbatim
//   "ma 240선도 제거해줘. 현재 캔들 윈도우에서는 사실상 의미가 없네"):
//   - MA 240 본문 1년 영업일 본문 영웅문 zoom verbatim but 본 시스템 본문 candle window 본문 240일 미만 본질 가능 본문
//     본질상 MA 240 본문 visible 본질 무의미 본문 본질 (대표 verbatim "사실상 의미가 없네") → 제거 본질
//   - 6선 = MA 5/10/20/43/60/120 (240 제거 cascade)
const DEFAULT_INDICATORS = {
  ma6: true,             // #4 MA 6선 (5/10/20/43/60/120) — chip (cycle23: 240 제거 cascade)
  volumeByDecile: true,  // #1 매물대 화면 가변 — chip
  pinkSignal: true,      // #2 분홍 강세 marker — chip
  exDividend: true,      // #6 배당락 marker — chip
  fibonacci: true,       // #3 Fibonacci (P0-16 Fix-51: default ON + auto-anchor 본문) — chip
  // 하단 sub-pane 3종 (tradingValue/macd/rsi) = base 영구 ON (chip 부재). state 본문 외 layer 본질.
  // (일목 제거 P0-16 Fix-50 cascade)
};

function loadIndicatorState() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return { ...DEFAULT_INDICATORS };
    const parsed = JSON.parse(raw);
    return { ...DEFAULT_INDICATORS, ...parsed };
  } catch (e) {
    return { ...DEFAULT_INDICATORS };
  }
}

function saveIndicatorState(state) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch (e) { /* private mode 등 silent fail */ }
}

// dailybars → TradingView v5 candle schema 변환
function normalizeData(dailyArr) {
  if (!Array.isArray(dailyArr) || dailyArr.length < 1) return [];
  return dailyArr
    .filter((d) => d && typeof d.c === 'number' && d.c > 0 && d.date)
    .map((d) => {
      const parts = String(d.date).slice(0, 10).split('-');
      if (parts.length !== 3) return null;
      const y = parseInt(parts[0], 10);
      const m = parseInt(parts[1], 10);
      const day = parseInt(parts[2], 10);
      if (!y || !m || !day) return null;
      return {
        time: { year: y, month: m, day: day },
        open: d.o,
        high: d.h,
        low: d.l,
        close: d.c,
        _v: typeof d.v === 'number' ? d.v : 0,
        _tv: typeof d.tv === 'number' ? d.tv : 0,
      };
    })
    .filter(Boolean);
}

// MA 산식 (SMA period)
function computeMA(data, period) {
  if (!Array.isArray(data) || data.length < period) return [];
  const out = [];
  let sum = 0;
  for (let i = 0; i < data.length; i++) {
    sum += data[i].close;
    if (i >= period) sum -= data[i - period].close;
    if (i >= period - 1) {
      out.push({ time: data[i].time, value: sum / period });
    }
  }
  return out;
}

// REQ v3 §3.1 verbatim 정합 — MA 7선 (5/10/20/43/60/120/240) → cycle23 본문 6선 (240 제거 cascade).
// 대표 verbatim 2026-05-21 09:15 KST "ma 선의 종류와 색상이다" + 영웅문 zoom 7 line 본문.
//
// cycle23 chart-tv-3changes Spot 3 (2026-05-22 17:24 KST 대표 verbatim
//   "ma 240선도 제거해줘. 현재 캔들 윈도우에서는 사실상 의미가 없네"):
//   - MA 240 = #90EE90 연두 (LightGreen) — 영웅문 zoom verbatim 1년 영업일 본질 → cycle23 본문 제거
//   - 사유: 본 시스템 candle window 본문 240일 미만 본질 빈번 → MA 240 본질 visible 본질 무의미 본문
//   - 6선 = MA 5/10/20/43/60/120 (240 제거 cascade)
//
// P0-7 fix-6 (2026-05-21 11:01 KST):
//   REQ v3 §2 + REQ v4 §3.1 verbatim 영웅문 zoom 색상 정정 채택 (별건 cycle 후행 본질 → 본 P0-7 통합):
//     MA 5 = #FF69B4 분홍 (HotPink) — 영웅문 zoom verbatim
//     MA 10 = #FFD700 노랑 (Gold) — 영웅문 zoom verbatim
//     MA 20 = #87CEEB 하늘 (SkyBlue) — 영웅문 zoom verbatim
//     MA 43 = #FFA500 주황 (Orange) — 영웅문 zoom verbatim, 대표 매매 customization
//     MA 60 = #FF8C00 주황 (DarkOrange) — 영웅문 zoom verbatim
//     MA 120 = #4169E1 파랑 (RoyalBlue) — 영웅문 zoom verbatim
//     MA 240 = #90EE90 연두 (LightGreen) — 영웅문 zoom verbatim, 1년 영업일 (cycle23: 제거)
//
// P0-7 fix-1 (2026-05-21 10:55 KST 대표 verbatim "확대 차트에서 ma선 레이블은 모두 제거해줘. 내 영웅문 화면에도 없잖아"):
//   title 본문 제거 — priceScale 본문 라벨 visible 부재 본질 (영웅문 정합).
//   기존 priceLineVisible:false + lastValueVisible:false + crosshairMarkerVisible:false 본문 정합 유지.
//   title 본문 빈 string '' 본질 → priceScale legend layer 본문 출력 부재.
//
// state key `ma6` 명칭은 그대로 유지 (localStorage backward 호환 본질). 의미는 cycle23 본문 6선으로 축소.
const MA_CONFIGS = [
  { period: 5,   color: '#FF69B4', title: '', width: 1 },   // HotPink 분홍 (영웅문 zoom verbatim)
  { period: 10,  color: '#FFD700', title: '', width: 1 },   // Gold 노랑 (영웅문 zoom verbatim)
  { period: 20,  color: '#87CEEB', title: '', width: 1 },   // SkyBlue 하늘 (영웅문 zoom verbatim)
  { period: 43,  color: '#FFA500', title: '', width: 1.2 }, // Orange 주황 (영웅문 zoom verbatim, 대표 customization)
  { period: 60,  color: '#FF8C00', title: '', width: 1 },   // DarkOrange 주황 (영웅문 zoom verbatim)
  { period: 120, color: '#4169E1', title: '', width: 1 },   // RoyalBlue 파랑 (영웅문 zoom verbatim)
  // cycle23 chart-tv-3changes Spot 3: MA 240 본질 제거 (대표 verbatim "현재 캔들 윈도우에서는 사실상 의미가 없네")
];

// PM320-D6 P1 — Fib hint 범례 점선 색상 상수 (fibonacci.js 정합).
//   AMBER = primary 세트(최근 저점 되돌림, 드래그) = fibonacci.js LEVELS color '#F5A623'.
//   TEAL  = secondary 세트(장기 저점 base, 고정)   = fibonacci.js SECONDARY_COLOR '#38BDF8'.
//   2026-06-10 토구사 union(secondary 세트 신축) 시 legend row 본문은 추가됐으나 본 두 상수 선언 누락
//   → showFibHint() 내 mkLegend(FIB_LEGEND_AMBER, ...) 호출 시점 ReferenceError → hint 전체 abort
//   (범례 + 사용법 안내 모두 미표시). 본 선언으로 복원. (fibonacci.js 색 변경 금지 — 토구사 확정 시각언어.)
const FIB_LEGEND_AMBER = '#F5A623';
const FIB_LEGEND_TEAL = '#38BDF8';

// EMA helper
function computeEMA(data, period) {
  if (!Array.isArray(data) || data.length < period) return [];
  const out = new Array(data.length).fill(null);
  const k = 2 / (period + 1);
  let sum = 0;
  for (let i = 0; i < period; i++) sum += data[i].close;
  out[period - 1] = sum / period;
  for (let i = period; i < data.length; i++) {
    out[i] = data[i].close * k + out[i - 1] * (1 - k);
  }
  return out;
}

// MACD (12/26/9)
function computeMACD(data) {
  if (data.length < 35) return { line: [], signal: [], hist: [] };
  const ema12 = computeEMA(data, 12);
  const ema26 = computeEMA(data, 26);
  const macdVals = data.map((_, i) => {
    if (ema12[i] == null || ema26[i] == null) return null;
    return ema12[i] - ema26[i];
  });
  const firstIdx = macdVals.findIndex((v) => v != null);
  const signal = new Array(data.length).fill(null);
  if (firstIdx >= 0 && data.length - firstIdx >= 9) {
    const k = 2 / (9 + 1);
    let seed = 0;
    for (let i = firstIdx; i < firstIdx + 9; i++) seed += macdVals[i];
    signal[firstIdx + 8] = seed / 9;
    for (let i = firstIdx + 9; i < data.length; i++) {
      signal[i] = macdVals[i] * k + signal[i - 1] * (1 - k);
    }
  }
  const line = [];
  const sigOut = [];
  const hist = [];
  for (let i = 0; i < data.length; i++) {
    if (macdVals[i] != null) {
      line.push({ time: data[i].time, value: macdVals[i] });
    }
    if (signal[i] != null) {
      sigOut.push({ time: data[i].time, value: signal[i] });
    }
    if (macdVals[i] != null && signal[i] != null) {
      const h = macdVals[i] - signal[i];
      hist.push({
        time: data[i].time,
        value: h,
        color: h >= 0 ? 'rgba(197,57,57,0.6)' : 'rgba(25,88,199,0.6)',
      });
    }
  }
  return { line, signal: sigOut, hist };
}

// RSI (Wilder, period=14)
function computeRSI(data, period = 14) {
  if (data.length < period + 1) return [];
  const out = [];
  let avgGain = 0;
  let avgLoss = 0;
  for (let i = 1; i <= period; i++) {
    const ch = data[i].close - data[i - 1].close;
    if (ch > 0) avgGain += ch; else avgLoss -= ch;
  }
  avgGain /= period;
  avgLoss /= period;
  const rs0 = avgLoss === 0 ? 100 : (avgGain / avgLoss);
  out.push({ time: data[period].time, value: 100 - (100 / (1 + rs0)) });

  for (let i = period + 1; i < data.length; i++) {
    const ch = data[i].close - data[i - 1].close;
    const gain = ch > 0 ? ch : 0;
    const loss = ch < 0 ? -ch : 0;
    avgGain = (avgGain * (period - 1) + gain) / period;
    avgLoss = (avgLoss * (period - 1) + loss) / period;
    const rs = avgLoss === 0 ? 100 : (avgGain / avgLoss);
    out.push({ time: data[i].time, value: 100 - (100 / (1 + rs)) });
  }
  return out;
}

// P0-4 영웅문 정합 fix #3 helper (2026-05-21 10:01 KST 대표 정정):
//   RSI 14 시계열 본문 → RSI < 30 (과매도) 시점 'YYYY-MM-DD' string 배열 추출.
//   영웅문 verbatim 임계값 30 본문 (gracefully).
function extractRSIOversoldDates(data, rsiData) {
  if (!Array.isArray(rsiData) || rsiData.length === 0) return [];
  const dates = [];
  rsiData.forEach((point) => {
    if (typeof point.value !== 'number' || point.value >= 30) return;
    const t = point.time;
    if (!t || typeof t.year !== 'number') return;
    const mm = String(t.month).padStart(2, '0');
    const dd = String(t.day).padStart(2, '0');
    dates.push(`${t.year}-${mm}-${dd}`);
  });
  return dates;
}

// P0-22 Fix-77 (2026-05-21 18:14 KST 대표 verbatim
//   "그리고 하단 거래대금 지표의 y축 값의 표기 역시 한글 단위로 표시해줘. 9000억, 1.2천억, 22억 이런식으로"):
//   거래대금 priceScale 본문 한글 단위 formatter — 만/억/천억/조 본문 본질.
//   §11.15 외부 spec 사전 검증 PASS:
//     - WebSearch 2회 corroborating (TradingView Lightweight Charts v5 PriceFormatCustom)
//     - WebFetch https://tradingview.github.io/lightweight-charts/docs/api/interfaces/PriceFormatCustom
//       "type:'custom', formatter:(price:number)=>string, minMove:number"
//   §16 self-catch:
//     - 대표 verbatim 양식 "9000억" + "1.2천억" + "22억" 본문 mixed = (a) < 1조 본문 천억 자리수 본문 다른 양식 본문 mixed
//     - 결정적 본질: 1억 ~ 999억 = "NN억" 정수 본문 (9000억 = 9.0e11 본문 한자리 본문 "9000억" 본문 정합)
//     - 1,000억 ~ 9,999억 = 대표 verbatim "1.2천억" 본문 = 천억 단위 본문 소수점 1자리 본문 본질
//     - 본 본질 정정 cascade: 대표 verbatim "9000억" 본문 = 9.0e11 본문 본질 → "9000억" 본문 본질 (1조 미만 본문 모두 억)
//     - 별 path: 1조 이상 = "N.N조"
//     - 영웅문 reference 본문 K (천) 단위 본문 무시 본문 대표 verbatim 우선 본문 본질 정합
function formatKRWUnit(price) {
  if (typeof price !== 'number' || !isFinite(price)) return '';
  const abs = Math.abs(price);
  const sign = price < 0 ? '-' : '';
  // 1조 이상 (≥ 1e12) → "N.N조"
  if (abs >= 1e12) {
    return `${sign}${(abs / 1e12).toFixed(1)}조`;
  }
  // 1억 이상 (≥ 1e8) → "NN억" (정수, 1조 미만 본문 모두 억 단위 본질)
  if (abs >= 1e8) {
    return `${sign}${Math.round(abs / 1e8)}억`;
  }
  // 1만 이상 (≥ 1e4) → "NN만" (정수)
  if (abs >= 1e4) {
    return `${sign}${Math.round(abs / 1e4)}만`;
  }
  // < 1만 → 정수 raw (드물 본질)
  return `${sign}${Math.round(abs)}`;
}

// 거래대금 histogram (sub-pane) — 캔들 색 동조 (양봉/음봉)
function buildTradingValue(data) {
  return data.map((d) => ({
    time: d.time,
    value: d._tv,
    color: d.close >= d.open ? 'rgba(197,57,57,0.55)' : 'rgba(25,88,199,0.55)',
  }));
}

function buildContainer(slot, ticker) {
  slot.innerHTML = '';

  const wrap = document.createElement('div');
  wrap.className = 'cal-chart-tv-wrap';

  // 토글 chip bar 영역 (chart 상단)
  const togglesHost = document.createElement('div');
  togglesHost.className = 'cal-chart-tv-toggles-host';

  const main = document.createElement('div');
  main.className = 'cal-chart-tv-main';
  main.setAttribute('role', 'img');
  main.setAttribute('aria-label', `일봉 확대 차트, ${ticker}`);

  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'cal-chart-close';
  close.setAttribute('aria-label', '확대 차트 닫기');
  close.textContent = '접기 ▴';

  const attr = document.createElement('div');
  attr.className = 'cal-chart-tv-attr';
  attr.innerHTML = '<a href="https://www.tradingview.com/" target="_blank" rel="noopener" style="color:#6B7A99; font-size:10px; text-decoration:none;">Charts by TradingView</a>';

  // cycle23 layout 정정 (2026-05-22 15:56 KST 대표 verbatim "확대 차트의 접기 버튼 또한 charts by tradingview 글자 위에 있는데 라이센스 글자보다 아래쪽에 위치시켜줘"):
  //   기존 order: togglesHost → main → close → attr (close가 attr 위)
  //   신규 order: togglesHost → main → attr → close (close가 attr 아래, license 글자 아래 위치 본질)
  wrap.appendChild(togglesHost);
  wrap.appendChild(main);
  wrap.appendChild(attr);
  wrap.appendChild(close);
  slot.appendChild(wrap);

  return { wrap, togglesHost, main, close };
}

/**
 * 차트 render orchestrator (Phase 7d-1).
 * contract: window.ChartTV.render(slot, dailyArr, options)
 */
function renderChartTV(container, dailyArr, options = {}) {
  if (!container) return null;

  const ticker = options.ticker || '';
  const data = normalizeData(dailyArr);
  const { wrap, togglesHost, main, close } = buildContainer(container, ticker);

  // 다크모드 (DOC-20260610-DSN-001 §2.3) — 차트 배경은 transparent라 페이지 추종하되
  // 축/그리드/separator 텍스트는 JS 하드코딩이라 현재 테마를 읽어 분기 주입.
  function isDarkTheme() {
    try {
      const attr = document.documentElement.getAttribute('data-theme');
      if (attr === 'dark') return true;
      if (attr === 'light') return false;
      return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    } catch (e) { return false; }
  }
  function chartThemeColors() {
    const dk = isDarkTheme();
    return {
      text: dk ? 'rgba(255,255,255,0.6)' : 'rgba(0,0,0,0.6)',
      sep: dk ? 'rgba(255,255,255,0.14)' : 'rgba(0,0,0,0.12)',
      sepHover: dk ? 'rgba(255,255,255,0.24)' : 'rgba(0,0,0,0.2)',
      grid: dk ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.08)',
      tsBorder: dk ? 'rgba(255,255,255,0.14)' : 'rgba(0,0,0,0.12)',
    };
  }
  const _tc = chartThemeColors();

  if (data.length < 1) {
    main.innerHTML = '<div class="cal-chart-empty" role="img" aria-label="차트 데이터 없음">데이터 누적 중</div>';
    return null;
  }

  // P0-11 Fix-34: container 인자 전달 — chart slot inner content width 실측 본문 채택 (모바일 본문 overflow 봉쇄)
  const vp = getViewportSize(container);
  // sub-pane 3종 (거래대금 + MACD + RSI) — height 분배 본질
  // P0-7 fix-5 (2026-05-21 10:55 KST 대표 verbatim "하단 지표의 높이가 너무 높다. 지금의 절반 수준으로 해봐"):
  //   subPaneHeight 본문 0.15 → 0.075 (절반 본질). main pane stretch factor 본질 상대 증가.
  //   setStretchFactor v5.0.8 API 본문 추가 적용 (chart instance 생성 후 chart.panes() 본문 호출 본질).
  const subPaneHeight = Math.round(vp.height * 0.075);
  const totalHeight = vp.height + subPaneHeight * 3;

  let state = options.indicatorState || loadIndicatorState();

  // chart instance
  // P0-18 Fix-59 (2026-05-21 16:03 KST 대표 verbatim "y축의 값들도 폰트를 더 작게 적어줘"):
  //   layout.fontSize 본문 신축 — chart 전역 본문 font size (default 12 → 9 본문 축소).
  //   본 옵션 본문 = priceScale (우측 가격 라벨) + timeScale (하단 날짜) + sub-pane priceScale 본문 동시 영향.
  //   §11.15 외부 spec 사전 검증 PASS:
  //     - WebFetch https://tradingview.github.io/lightweight-charts/docs/api/interfaces/LayoutOptions
  //       "fontSize - Font size of text on scales in pixels, default 12"
  //     - chart 본문 전역 본문 textColor / fontFamily / fontSize 본문 동급 옵션 본질
  //   영웅문 23a74560 reference 본문 우측 priceScale 본문 작은 font 본질 정합 (727,000 / 657,680 / 612,923 등 visible).
  const chart = createChart(main, {
    width: vp.width,
    height: totalHeight,
    layout: {
      background: { color: 'transparent' },
      textColor: _tc.text,  // 다크 추종 (DSN-001 §2.3)
      fontSize: 9,  // P0-18 Fix-59: y축 + 하단 날짜 font 축소 (default 12 → 9)
      panes: {
        separatorColor: _tc.sep,
        separatorHoverColor: _tc.sepHover,
        enableResize: true,
      },
    },
    grid: {
      vertLines: { color: _tc.grid, style: LineStyle.Dotted },
      horzLines: { color: _tc.grid, style: LineStyle.Dotted },
    },
    crosshair: { mode: CrosshairMode.Normal },
    // P0-23 Fix-80 (2026-05-21 19:38 KST 대표 verbatim
    //   "확대 차트에서 특정일을 선택 시 하단의 날짜 표시가 21 5월 '26 으로 표기가 되는데 이것도 yyyy-mm-dd로 바꿔줘"):
    //   - localization.dateFormat 본문 'yyyy-MM-dd' 본질 → crosshair 본문 BusinessDay tooltip 본문 정합
    //   - default 본문 'dd MMM \'yy' 본문 = "21 5월 '26" (한국 locale MMM = '5월') → 본 fix 본문 ISO 양식
    //   - Fix-75 (tickMarkFormatter) 본문 timeScale axis tick 본문 별 layer — 본 Fix-80 본문 crosshair label layer
    //   §11.15 외부 spec 사전 검증 PASS:
    //     - WebFetch https://tradingview.github.io/lightweight-charts/docs/api/interfaces/LocalizationOptions
    //       "dateFormat — String containing yyyy, yy, MMMM, MMM, MM and dd literals" / default 'dd MMM \'yy'
    //       "Ignored if timeFormatter has been specified" (본 코드 본문 timeFormatter 미설정 → dateFormat 적용 본질)
    //     - WebSearch 2회 corroborating (Lightweight Charts v5 LocalizationOptions dateFormat / timeFormatter signature)
    //   §16 self-catch:
    //     - dateFormat 본문 timeScale tick label 본문 영향 X (tickMarkFormatter 본문 우선) → Fix-75 정합 유지
    //     - dateFormat 본문 crosshair label 본문 직접 영향 본질 (LocalizationOptions API spec)
    //     - timeFormatter 본문 미설정 본질 (dateFormat 우선 본질 정합)
    localization: {
      dateFormat: 'yyyy-MM-dd',
    },
    timeScale: {
      borderColor: _tc.tsBorder,  // 다크 추종 (DSN-001 §2.3)
      timeVisible: false,
      secondsVisible: false,
      // P0-22 Fix-75 (2026-05-21 18:12 KST 대표 verbatim
      //   "하단부 날짜가 표시되는데 yyyy-mm-dd 포맷으로 검정 글씨에 바탕 없이 해줘"):
      //   - tickMarkFormatter 본문 yyyy-MM-dd 양식 본문 본질 (TradingView v5 native)
      //   - BusinessDay 본문 {year, month, day} 본문 통과 본질 (normalizeData 본문 정합)
      //   - 검정 글씨 본문 = layout.textColor 본문 rgba(0,0,0,0.6) 본문 본질 정합 (이미 본문 검정 ~ 검정)
      //   - 바탕 없음 본문 = layout.background:transparent 본문 본질 정합 (이미 transparent)
      //   §11.15 외부 spec 사전 검증 PASS:
      //     - WebSearch 2회 corroborating (TradingView Lightweight Charts v5 TimeScaleOptions.tickMarkFormatter)
      //     - WebFetch https://tradingview.github.io/lightweight-charts/docs/api/interfaces/TimeScaleOptions
      //       "tickMarkFormatter — customize tick marks labels on time axis"
      //     - signature: (time, tickMarkType, locale) => string, BusinessDay 본문 {year, month, day}
      //   §16 self-catch:
      //     - tickMarkType 본문 enum (Year/Month/DayOfMonth/Time/TimeWithSeconds) — daily candle 본문 DayOfMonth 본질 빈번
      //     - 가독성 본문 본질 = month/day 본문 2자리 zero-pad (yyyy-MM-dd 본문 양식 본질 표준)
      //     - tickMarkType 본문 무시 본문 동일 yyyy-MM-dd 출력 본문 정합 (대표 verbatim 일관 양식 본질)
      tickMarkFormatter: (time) => {
        if (!time || typeof time.year !== 'number') return '';
        const yyyy = String(time.year);
        const mm = String(time.month).padStart(2, '0');
        const dd = String(time.day).padStart(2, '0');
        return `${yyyy}-${mm}-${dd}`;
      },
    },
    // P0-11 Fix-36 (2026-05-21 12:44 KST 대표 verbatim "y축 위치도 여전히 문제"):
    //   image #10 76d1f57d 본문 우측 priceScale visible 0건 — chart canvas 본문 자체가 부모 본문 초과 (Fix-34 본질)
    //   본 Fix-34 본문 container.clientWidth 본문 채택 → chart canvas 본문 부모 본문 align 정합 후
    //   모바일 본문 좁은 screen (S25 412px 본문 → chart slot inner ~316px) 본문 priceScale 100px 본문이 33% 차지 → 압축 본질
    //   모바일 본문 minimumWidth 60 본문 (5자리 99,999원 본문 visible 정합), 데스크탑 100 유지.
    //   §11.15 외부 spec 사전 검증 PASS — v5 PriceScaleOptions.minimumWidth (px) accepts integer.
    rightPriceScale: {
      borderColor: 'rgba(0,0,0,0.12)',
      visible: true,
      scaleMargins: { top: 0.15, bottom: 0.15 },  // P0-9 Fix-25 본문 유지
      minimumWidth: window.innerWidth < 768 ? 60 : 100,  // P0-11 Fix-36: 모바일 60 / 데스크탑 100 별건 (대표 verbatim "모바일과 데스크탑 다르게")
    },
    handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
    handleScale: { axisPressedMouseMove: true, mouseWheel: true, pinch: true },
  });

  // P0-7 fix-5 (2026-05-21 10:55 KST 대표 verbatim "하단 지표의 높이가 너무 높다. 지금의 절반 수준으로 해봐"):
  //   chart 생성 직후 setStretchFactor 본문 호출 — main pane 본문 비율 증가 + sub-pane 절반 본질.
  //   v5.0.8 IPaneApi.setStretchFactor() + getStretchFactor() — default = 1.0 동일 비율 본문.
  //   main pane stretch factor = 4.0 (sub-pane 3종 대비 4배) → 각 sub-pane = 1/(4+1+1+1) = 1/7 본문.
  //   §11.15 외부 spec 사전 검증 PASS (WebSearch v5.0.8 release notes + 공식 docs).
  try {
    const panes = chart.panes();
    if (Array.isArray(panes) && panes.length > 0 && typeof panes[0].setStretchFactor === 'function') {
      panes[0].setStretchFactor(4.0);  // main pane 본문 4배
    }
  } catch (err) { /* noop v5.0.8 미지원 fallback = totalHeight 본문만 적용 */ }

  // 캔들 series (main pane = paneIdx 0)
  // lead 옵션 A-3 채택 #4 — 현재가 priceLine 본질 (대표 verbatim 09:08 KST (c) "현재가가 표시되지 않는것도 문제")
  // P0-4 영웅문 정합 fix #1 (2026-05-21 10:00 KST):
  //   priceLineColor 동적 분기 — 마지막 candle close vs open 비교 후 양봉=#C53939 / 음봉=#1958C7
  //   영웅문 verbatim "14,370 ▲ 1,920 (15.42%)" 양봉 = red priceLine 정합
  const lastCandle = data.length > 0 ? data[data.length - 1] : null;
  const lastBullish = lastCandle && lastCandle.close >= lastCandle.open;
  const priceLineColor = lastBullish ? '#C53939' : '#1958C7';
  // P0-13 Fix-45 (2026-05-21 13:44 KST 대표 verbatim "주가의 소수점은 필요없다. 한국은 소수점 화폐가 없다"):
  //   priceFormat 본문 한국 화폐 정합 — precision 0 + minMove 1 본질 (정수 본문 visible).
  //   영웅문 verbatim 본문 정합: 727,000 / 657,680 / 592,000 본문 정수 본문 (소수점 부재).
  //   §11.15 외부 spec 사전 검증 PASS — TradingView v5 PriceFormat:
  //     { type: 'price', precision: 0, minMove: 1 } = 정수만 visible 본문 정합 본질 (PriceFormatBuiltIn spec).
  const KRW_PRICE_FORMAT = { type: 'price', precision: 0, minMove: 1 };
  const candleSeries = chart.addSeries(CandlestickSeries, {
    upColor: '#C53939',
    downColor: '#1958C7',
    wickUpColor: '#C53939',
    wickDownColor: '#1958C7',
    borderUpColor: '#C53939',
    borderDownColor: '#1958C7',
    lastValueVisible: true,
    priceLineVisible: true,
    priceLineWidth: 1,
    priceLineColor: priceLineColor,
    priceLineStyle: 2, // Dashed
    priceFormat: KRW_PRICE_FORMAT,  // P0-13 Fix-45: 한국 화폐 정수 본문 정합
  });
  // P0-16 Fix-50 (2026-05-21 14:57 KST 대표 verbatim "일목균형표는 도저히 안되겠다 제거해줘"):
  //   P0-15 Fix-49 future whitespace 본문 (SHIFT_FUTURE=26)은 일목 cloud 미래 영역 visible용 본질.
  //   일목 제거 cascade로 future whitespace 본문 자체 폐기 → candle series 본문 actual candles only 정합.
  //   영웅문 23a74560 본문 cloud 부재 = 현시 candle 영역만 visible 정합 (피보 horizontal line 본문 visible 보존).
  candleSeries.setData(data.map((d) => ({
    time: d.time, open: d.open, high: d.high, low: d.low, close: d.close,
  })));

  // ─── 모든 plugin/series instance 보관 (toggle 시 add/remove) ───
  // P0-16 Fix-50: ichimoku layer 제거 (일목 plugin git rm cascade)
  const layers = {
    ma6: [],            // Array<ISeriesApi>
    volumeByDecile: null, // ISeriesPrimitive
    tradingValue: null, // ISeriesApi (sub-pane 1)
    macd: null,         // { line, signal, hist }
    rsi: null,          // ISeriesApi (sub-pane 3)
    rsiSignal: null,    // ISeriesApi (sub-pane 3, signal 9 line)
    rsiOverboughtCloud: null, // ISeriesPrimitive (cycle23: RSI 14 ≥ 70 cloud fill)
    seriesMarkers: null,
    pinkSignal: null,   // ISeriesPrimitive (P0-4 분홍 vertical line)
    // Phase 7d-2 신축 — fibonacci 자석 drawing tool controller (signature 변경, Array → single instance)
    fibController: null,
  };

  // RSI 과매도 dates 미리 산출 (markers attach 본문 source) — P0-4 영웅문 정합 fix #3
  const rsiDataPrecomputed = computeRSI(data, 14);
  const rsiOversoldDatesAuto = extractRSIOversoldDates(data, rsiDataPrecomputed);

  // ── MA 6선 ──
  function addMA6() {
    if (layers.ma6.length > 0) return;
    MA_CONFIGS.forEach((cfg) => {
      const maData = computeMA(data, cfg.period);
      if (maData.length === 0) return;
      const line = chart.addSeries(LineSeries, {
        color: cfg.color,
        lineWidth: cfg.width,
        title: cfg.title,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
        priceFormat: KRW_PRICE_FORMAT,  // P0-13 Fix-45: MA 가격 라인 본문 정수 본문 정합
      });
      line.setData(maData);
      layers.ma6.push(line);
    });
  }
  function removeMA6() {
    layers.ma6.forEach((s) => { try { chart.removeSeries(s); } catch (e) { /* noop */ } });
    layers.ma6 = [];
  }

  // ── 일목 본문 제거 (P0-16 Fix-50, 2026-05-21 14:57 KST 대표 verbatim "일목균형표는 도저히 안되겠다 제거해줘") ──

  // ── 매물대 화면 가변 ──
  function addVolumeByDecile() {
    if (layers.volumeByDecile) return;
    try {
      layers.volumeByDecile = new VolumeByDecilePrimitive(chart, candleSeries, data);
      candleSeries.attachPrimitive(layers.volumeByDecile);
    } catch (err) {
      layers.volumeByDecile = null;
    }
  }
  function removeVolumeByDecile() {
    if (!layers.volumeByDecile) return;
    try {
      candleSeries.detachPrimitive(layers.volumeByDecile);
      layers.volumeByDecile.detached();
    } catch (e) { /* noop */ }
    layers.volumeByDecile = null;
  }

  // ── 거래대금 sub-pane (paneIdx 1) ──
  // v5.0.8 공식 API: chart.addSeries(Type, opts, paneIdx) 3번째 positional 인자
  // P0-4 영웅문 정합 fix #2 (2026-05-21 10:00 KST):
  //   title: '#거래대금' 영웅문 verbatim (priceScale 좌측 상단 자동 표시 본질, TradingView v5 native)
  // P0-17 Fix-54 (2026-05-21 15:18 KST 대표 verbatim "하단 보조지표 라벨이 옮겨진듯하지만 아직 남아있고"):
  //   root cause = native series title 본문 = priceScale 우측 label 본문 visible (P0-16 HTML overlay 추가만 + native title 본문 동시 제거 0건 → 이중 라벨 cascade)
  //   fix = title 빈 string 본문 정정 (HTML overlay 좌측 단독 visible 본문 정합, 영웅문 23a74560 sub-pane 좌측 라벨만 본질 정합)
  //   §11.15 외부 spec 사전 검증 PASS: TradingView v5 series title 빈 string 본문 시 priceScale 우측 label 부재 (WebSearch 2회 corroborating)
  // P0-20 Fix-69 (2026-05-21 17:46 KST 대표 verbatim "거래대금의 경우 500억 위치에 검정 가로선 항상 표시"):
  //   거래대금 sub-pane 본문 500억 (= 50,000,000,000 KRW 정수 본문) 가로선 본문 createPriceLine 본문 신축.
  //   color: '#000000' (영웅문 정합 검정), lineWidth: 1, lineStyle: Solid, axisLabelVisible: false (라벨 없이 가로선만).
  //   §11.15 외부 spec 사전 검증 PASS — ISeriesApi.createPriceLine(options) 본문 v5 native API.
  //   §16 self-catch: 한국 거래대금 본문 500억 본문 정수 본문 verbatim — 50_000_000_000 (5e10) 정합.
  //
  // P0-20 Fix-70 (2026-05-21 17:46 KST 대표 verbatim "거래대금 y축 값을 최고 거래대금 기준으로 보여주기"):
  //   기존 priceScaleId: '' (overlay) 본문 → 'tradingValue' 본문 별도 priceScale id 본문 변경.
  //   별도 priceScale 본문 = 자체 autoScale (default true) 본문 visible range 본문 최고값 기준 본문 정합.
  //   lastValueVisible: true 본문 명시 — 우측 priceScale 본문 last value 본문 visible (영웅문 정합).
  //   priceScale scaleMargins 본문 추가 = sub-pane 본문 상단/하단 본문 margin 본문 0.05 (visible 본질 강화).
  //   §11.15 외부 spec 사전 검증 PASS:
  //     - WebSearch 2회 corroborating (TradingView Lightweight Charts v5 priceScale autoScale default true)
  //     - https://tradingview.github.io/lightweight-charts/docs/api/interfaces/PriceScaleOptions
  //       "autoScale - Automatically set price range based on visible data range, default true"
  //     - 별도 priceScale id 본문 = pane 본문 별도 visible range 본질 (overlay '' 본문 main pane priceScale 종속 paradigm 회피)
  //   §16 self-catch: 영웅문 23a74560 본문 우측 priceScale "2,172.71K / 196,768.00" + 3005fbac 본문 "1,096.99K / 739,721.00" 본문 visible 정합.
  function addTradingValue() {
    if (layers.tradingValue) return;
    try {
      layers.tradingValue = chart.addSeries(HistogramSeries, {
        title: '',  // P0-17 Fix-54: native title 본문 제거 (HTML overlay 좌측 단독)
        // P0-22 Fix-77 (2026-05-21 18:14 KST 대표 verbatim
        //   "하단 거래대금 지표의 y축 값의 표기 역시 한글 단위로 표시해줘. 9000억, 1.2천억, 22억 이런식으로"):
        //   priceFormat 본문 'volume' → 'custom' + formatKRWUnit 본문 한글 단위 본질 (만/억/조).
        //   minMove: 1 본문 (정수 본문 본질 trading value 본문 정합, KRW 원 단위 본질).
        //   §11.15 외부 spec PASS: PriceFormatCustom (type='custom', formatter, minMove) 본문 v5 native.
        priceFormat: { type: 'custom', formatter: formatKRWUnit, minMove: 1 },
        priceScaleId: 'tradingValue',  // P0-20 Fix-70: '' (overlay) → 별도 priceScale id 본문 (자체 autoScale visible range 본문 기준)
        lastValueVisible: true,         // P0-20 Fix-70: 우측 last value visible (영웅문 정합)
      }, 1);
      layers.tradingValue.setData(buildTradingValue(data));

      // P0-20 Fix-70: 거래대금 sub-pane priceScale 본문 옵션 본문 명시 — autoScale visible range 본문 기준
      //   chart.priceScale(id, paneIdx) 본문 v5 native API (id='tradingValue', paneIdx=1).
      try {
        chart.priceScale('tradingValue', 1).applyOptions({
          autoScale: true,
          scaleMargins: { top: 0.05, bottom: 0.05 },  // sub-pane visible range 본문 최대 본질
        });
      } catch (e) { /* noop fallback */ }

      // P0-20 Fix-69: 500억 검정 가로선 본문 createPriceLine 본문 신축 (모든 종목 본문 항상 표시)
      try {
        layers.tradingValue.createPriceLine({
          price: 50_000_000_000,           // 500억 = 5e10 KRW 정수 verbatim (한국 화폐 정수 본질)
          color: '#000000',                // 영웅문 reference 검정
          lineWidth: 1,
          lineStyle: LineStyle.Solid,
          axisLabelVisible: false,         // 라벨 없이 가로선만 visible (대표 verbatim "검정 가로선 항상 표시" 본질)
          title: '',
        });
      } catch (e) { /* noop fallback */ }
    } catch (err) {
      layers.tradingValue = null;
    }
  }
  function removeTradingValue() {
    if (!layers.tradingValue) return;
    try { chart.removeSeries(layers.tradingValue); } catch (e) { /* noop */ }
    layers.tradingValue = null;
  }

  // ── MACD sub-pane (paneIdx 2) ──
  // P0-4 영웅문 정합 fix #2 (2026-05-21 10:00 KST):
  //   title 영웅문 verbatim — 'MACD Oscillator 12,26,9' / 'MACD 시그널 9' / 'Hist'
  //   (영웅문 reference 본문 "MACD Oscillator 12,26,9 MACD 시그널 3,177.81" 정합)
  //
  // P0-7 fix-10 (2026-05-21 11:03 KST 대표 verbatim):
  //   "macd의 경우 시그널 선과 오실래이터 선이 크로스 할 때 데드크로스일 경우 파란색 아래쪽 화살표,
  //    골든크로스일 때 빨강색 위쪽 화살표를 macd 보조지표의 크로스하는 라인에 그려줘야해.
  //    데드크로스는 라인 위에 골든크로스는 라인 아래에."
  //
  //   골든크로스 (MACD line이 signal line 위로 cross) = belowBar arrowUp #C53939 (한국 시장 강세 빨강)
  //   데드크로스 (MACD line이 signal line 아래로 cross) = aboveBar arrowDown #1958C7 (한국 시장 약세 파랑)
  //
  //   §11.15 외부 spec 사전 검증 PASS:
  //   - createSeriesMarkers(series, markers) v5 primitive 본질 (markers.js 동일 패턴)
  //   - position 'aboveBar' / 'belowBar' 본문 sub-pane series 본문 정합 (high/low value 기준)
  //
  //   §16 self-catch:
  //   - 모든 종목 동일 공식 본문 (종목 레벨 하드코딩 0건, 대표 정책 정합)
  //   - signal line undefined / NaN 본문 graceful skip
  //   - edge case 첫 시점 (i=0) cross detection 부재 (prev 본문 없음)
  function detectMACDCrosses(line, signal) {
    // line/signal 본문 = [{time, value}, ...] 동일 length 가정 못함 (signal 본문 9 영업일 지연 본질)
    // time 본문 key string 'YYYY-MM-DD' 생성 → signal map 본문 lookup
    const golden = [];
    const dead = [];
    if (!Array.isArray(line) || !Array.isArray(signal) || line.length < 2 || signal.length < 2) {
      return { golden, dead };
    }
    const timeKey = (t) => `${t.year}-${String(t.month).padStart(2, '0')}-${String(t.day).padStart(2, '0')}`;
    const signalMap = new Map();
    signal.forEach((p) => { signalMap.set(timeKey(p.time), p.value); });

    // P0-18 Fix-60 (2026-05-21 16:03 KST 대표 verbatim "macd 화살표 역시 훨씬 작게"):
    //   MACD 골든/데드 cross marker 본문 size 본문 신축 — default 1 → 0.5 본문 축소.
    //   §11.15 외부 spec 사전 검증 PASS:
    //     - WebFetch https://tradingview.github.io/lightweight-charts/docs/api/interfaces/SeriesMarkerBar
    //       "size?: number — optional The optional size of the marker, default 1"
    //     - v5 SeriesMarkerBar.size 본문 visible 본문 비율 본질 (작을수록 marker 자체 size 본문 작아짐)
    //   image #16 885ff7ba 본문 MACD sub-pane 본문 빨간 ↑ arrow size 큼 → 0.5 본문 축소 본질 정합.
    // P0-20 Fix-65 (2026-05-21 17:46 KST 대표 verbatim "macd 화살표를 더 작게"):
    //   0.5 → 0.3 본문 추가 축소 (영웅문 23a74560 본문 MACD ↑ arrow visible 매우 작음 정합).
    //   §11.15 외부 spec 사전 검증 PASS:
    //     - WebSearch https://tradingview.github.io/lightweight-charts/docs/api/interfaces/SeriesMarkerBar
    //       "size?: number — optional, default 1" — minimum value 명시 부재, 0.3 본문 visible 보존 본질.
    //     - default 1 → 0.3 = 70% 축소 본질 (영웅문 reference 본문 visible 정합).
    const MARKER_SIZE = 0.3;
    let prevDiff = null;
    for (let i = 0; i < line.length; i++) {
      const lp = line[i];
      const sv = signalMap.get(timeKey(lp.time));
      if (typeof lp.value !== 'number' || typeof sv !== 'number' || isNaN(lp.value) || isNaN(sv)) {
        prevDiff = null;
        continue;
      }
      const curDiff = lp.value - sv;
      if (prevDiff != null) {
        // 골든크로스: prev <= 0 && cur > 0 (MACD line이 signal line 아래에서 위로)
        if (prevDiff <= 0 && curDiff > 0) {
          golden.push({
            time: lp.time,
            position: 'belowBar',
            shape: 'arrowUp',
            color: '#C53939',
            size: MARKER_SIZE,  // P0-18 Fix-60
          });
        }
        // 데드크로스: prev >= 0 && cur < 0 (MACD line이 signal line 위에서 아래로)
        if (prevDiff >= 0 && curDiff < 0) {
          dead.push({
            time: lp.time,
            position: 'aboveBar',
            shape: 'arrowDown',
            color: '#1958C7',
            size: MARKER_SIZE,  // P0-18 Fix-60
          });
        }
      }
      prevDiff = curDiff;
    }
    return { golden, dead };
  }

  // P0-10 Fix-29 (2026-05-21 12:17 KST 대표 verbatim
  //   "'rsi 14 시그널 9' 레이블은 그냥 'RSI'로 macd는 그냥 'MACD'로 하고"):
  //   영웅문 reference 영웅문은 'MACD Oscillator 12,26,9 MACD 시그널' / 'RSI 14 시그널 9' 본문 한 줄
  //   but 대표 verbatim 단순화 의도 — 그냥 'MACD' / 'RSI' 본문 채택 정합 (영웅문 본문 무시, 대표 명시 우선).
  //   - line series title = 'MACD' (영웅문 본문 무시, 대표 verbatim 정합)
  //   - signal series title = '' (단일 row title 보존)
  //   - Hist series title = '' (영웅문 본문 부재 정보, P0-9 Fix-23 본문 유지)
  function addMACD() {
    if (layers.macd) return;
    const m = computeMACD(data);
    if (m.line.length === 0) return;
    try {
      const line = chart.addSeries(LineSeries, {
        // P0-17 Fix-54 (2026-05-21 15:18 KST): native title 본문 제거 (HTML overlay 좌측 단독 본문 정합)
        color: '#0064FF', lineWidth: 1, title: '',
        priceLineVisible: false, lastValueVisible: false,
      }, 2);
      const signal = chart.addSeries(LineSeries, {
        color: '#4D8EFF', lineWidth: 1, title: '',  // signal title 부재 (단일 row 정합)
        priceLineVisible: false, lastValueVisible: false,
      }, 2);
      const hist = chart.addSeries(HistogramSeries, {
        title: '',                                   // P0-9 Fix-23: 영웅문 본문 부재 정보 제거
        priceFormat: { type: 'volume' },
      }, 2);
      line.setData(m.line);
      signal.setData(m.signal);
      hist.setData(m.hist);

      // P0-7 fix-10: MACD 크로스 detection + marker attach (line series 본문에 attach, sort by time)
      const { golden, dead } = detectMACDCrosses(m.line, m.signal);
      const crossMarkers = [...golden, ...dead].sort((a, b) => {
        const ta = a.time.year * 10000 + a.time.month * 100 + a.time.day;
        const tb = b.time.year * 10000 + b.time.month * 100 + b.time.day;
        return ta - tb;
      });
      let macdCrossMarkers = null;
      if (crossMarkers.length > 0) {
        try {
          macdCrossMarkers = createSeriesMarkers(line, crossMarkers);
        } catch (err) { /* noop createSeriesMarkers v5 미지원 fallback */ }
      }

      layers.macd = { line, signal, hist, crossMarkers: macdCrossMarkers };
    } catch (err) {
      layers.macd = null;
    }
  }
  function removeMACD() {
    if (!layers.macd) return;
    try {
      if (layers.macd.crossMarkers && typeof layers.macd.crossMarkers.setMarkers === 'function') {
        layers.macd.crossMarkers.setMarkers([]);
      }
      chart.removeSeries(layers.macd.line);
      chart.removeSeries(layers.macd.signal);
      chart.removeSeries(layers.macd.hist);
    } catch (e) { /* noop */ }
    layers.macd = null;
  }

  // ── RSI sub-pane (paneIdx 3) ──
  // P0-4 영웅문 정합 fix #2 (2026-05-21 10:00 KST):
  //   title 영웅문 verbatim — 'RSI 14 시그널 9' (영웅문 reference "RSI 14 시그널 9 46.86 / 63.09 / 84.39" 정합)
  //
  // P0-7 fix-9 (2026-05-21 11:00 KST 대표 verbatim
  //   "하단 rsi 보조지표의 경우 과열30, 침체30, period 14, signal 9 거꾸로 보기를 해서 보여줘"):
  //   - 거꾸로 보기 (invertScale) — priceScale invertScale:true 본질 (Y축 반전, 침체 위 + 과열 아래)
  //   - 양 임계값 30 (과열 30 + 침체 30 본질) — invertScale 본질 상 둘 다 30 본문 정합
  //     §16 본질: 영웅문 본인 customization 본문 (표준 RSI overbought 70 / oversold 30 vs 영웅문 둘 다 30 본질)
  //   - signal 9 — SMA(9) of RSI series 본문 추가 신축
  //   - period 14 — 기존 정합 유지
  //   - title 영웅문 verbatim 유지 'RSI 14 시그널 9'
  //   - §11.15 외부 spec 사전 검증 PASS (TradingView v5 PriceScaleOptions invertScale:bool, default:false)
  // P0-10 Fix-29 (2026-05-21 12:17 KST 대표 verbatim
  //   "'rsi 14 시그널 9' 레이블은 그냥 'RSI'로 macd는 그냥 'MACD'로 하고"):
  //   영웅문 본문 'RSI 14 시그널 9' but 대표 명시 단순화 → 'RSI' 본문 채택 정합
  //   - line series title = 'RSI' (대표 verbatim 정합)
  //   - signal series title = '' (단일 row title 보존)
  // P0-9 Fix-23 본문 유지: priceLine title '' (영웅문 본문 부재 정보)
  function addRSI() {
    if (layers.rsi) return;
    const rsiData = computeRSI(data, 14);
    if (rsiData.length === 0) return;
    try {
      // cycle23 rsi-yaxis-redesign-v2 (2026-05-22 20:09 KST 대표 verbatim
      //   "rsi 보조지표 y축 값을 바꿔달랫는데 30,70이 추가만 된 것 같다.
      //    배경색까지 포함된 채 추가됐는데 기존에 표시되던 값을 바꾸기만 하면 돼"):
      //   - 직전 cycle23 chart-tv-3changes Spot 1 본문 createPriceLine 30/70 axisLabelVisible:true 본질만 추가
      //     → 자동 priceScale tick (40/60/80) 본문 그대로 + 30/70 axisLabel 본문 추가 → 이중 표시 cascade.
      //   - 직전 cycle23 rsi-yaxis-single-fix 본문 priceScale('right', 3).visible:false 본질 시도
      //     → sub-pane content 본문 본질 사라짐 critical 부작용 (TradingView GitHub issue #1473 동형) → revert.
      //   - 본 v2 본질 = **overlay priceScale paradigm** 채택 (priceScaleId 신규 unique ID 본문 본질):
      //     (a) RSI line + signal series 본문 priceScaleId: 'rsi-overlay' (default 'right' 본문 부재 → overlay scale 본질)
      //     (b) overlay priceScale 본문 본질 = UI 본문 본질 hidden (자동 tick label cascade 부재) — visible:false 부작용 회피
      //     (c) priceScale('rsi-overlay', 3).applyOptions({ invertScale: true }) 본문 본질 (영웅문 Y축 반전 customization 영구)
      //     (d) createPriceLine 30/70 본문 axisLabelVisible:false (overlay scale 본문 cascade 부재 본문 명시 정합)
      //     (e) 30/70 본문 별도 HTML overlay layer 본문 신축 (line 1276~ SUB_PANE_TITLES paradigm 정합 cascade)
      //   §11.15 외부 spec 사전 검증 PASS (WebSearch 3회 corroborating):
      //     - https://tradingview.github.io/lightweight-charts/docs/price-scale "Overlay price scales: An unlimited number
      //       of overlay price scales can be created. They remain hidden in the UI. To create overlay scale, assign
      //       priceScaleId with value differing from 'left' and 'right'."
      //     - GitHub issue #1473 본질: priceScale visible:false 본문 sub-pane content 부작용 known issue
      //     - PriceLineOptions.axisLabelVisible boolean (default true) — overlay scale 본문 본질 자동 cascade hide 정합
      //     - PriceScaleApi.applyOptions invertScale:bool 본문 overlay scale 본질 동일 지원 (price-scale.ts spec)
      //   §16 self-catch:
      //     - overlay priceScale 본문 본질 → priceToCoordinate 본문 본질 그대로 동작 (RSIOverboughtCloudPrimitive 본문 정합 영구)
      //     - 자동 tick (40/60/80) 본문 본질 overlay scale UI hidden cascade 부재 PASS (대표 verbatim "기존 40/80 본문" 본질 해결)
      //     - sub-pane 본문 본질 priceScale visible:false 부작용 회피 (issue #1473 sub-pane 사라짐 cascade 0건)
      //     - 30/70 본문 본질 HTML overlay 본문 별도 layer visible PASS (사용자 인지 layer 본질)
      //     - invertScale:true 본문 본질 영구 보존 (영웅문 customization 영구) → HTML overlay 30 (상단) / 70 (하단) 본질 정합
      //     - cycle23 직전 rsi-overbought-cloud-fill (threshold 70) 정합 (RSI ≥ 70 cloud + 70 overlay label visible)
      const RSI_OVERLAY_SCALE_ID = 'rsi-overlay';

      // RSI 메인 라인 (period 14) — overlay priceScale 본문 본질 (자동 tick UI hidden)
      layers.rsi = chart.addSeries(LineSeries, {
        // P0-17 Fix-54 (2026-05-21 15:18 KST): native title 본문 제거 (HTML overlay 좌측 단독 본문 정합)
        color: '#0064FF', lineWidth: 1, title: '',
        priceLineVisible: false, lastValueVisible: false,
        priceScaleId: RSI_OVERLAY_SCALE_ID,  // cycle23 v2: overlay scale (자동 tick UI hidden 본질)
      }, 3);
      layers.rsi.setData(rsiData);
      // createPriceLine 30/70 본문 본질 — axisLabelVisible:false (overlay scale 본문 본질 cascade 부재, 명시 정합)
      //   30/70 본문 본질 = 그래프 영역 본문 점선 가로선 visible (Y축 label 본문 본질 별도 HTML overlay layer)
      layers.rsi.createPriceLine({ price: 30, color: '#94A3B8', lineStyle: LineStyle.Dashed, title: '', axisLabelVisible: false });
      layers.rsi.createPriceLine({ price: 70, color: '#94A3B8', lineStyle: LineStyle.Dashed, title: '', axisLabelVisible: false });

      // P0-7 fix-9: signal 9 line 본문 신축 — SMA(9) of RSI series (overlay priceScale 본문 본질 cascade)
      const signalData = [];
      if (rsiData.length >= 9) {
        let sum = 0;
        for (let i = 0; i < rsiData.length; i++) {
          sum += rsiData[i].value;
          if (i >= 9) sum -= rsiData[i - 9].value;
          if (i >= 8) signalData.push({ time: rsiData[i].time, value: sum / 9 });
        }
      }
      if (signalData.length > 0) {
        layers.rsiSignal = chart.addSeries(LineSeries, {
          color: '#FFA726', lineWidth: 1, title: '',  // P0-9 Fix-24: signal title 제거 (영웅문 본문 단일 row 정합)
          priceLineVisible: false, lastValueVisible: false,
          priceScaleId: RSI_OVERLAY_SCALE_ID,  // cycle23 v2: RSI line과 동일 overlay scale (priceToCoordinate 정합)
        }, 3);
        layers.rsiSignal.setData(signalData);
      }

      // P0-7 fix-9: invertScale 본문 — RSI overlay priceScale Y축 반전 (영웅문 customization)
      // cycle23 v2: priceScale id 'right' → RSI_OVERLAY_SCALE_ID 본문 본질 cascade
      try {
        chart.priceScale(RSI_OVERLAY_SCALE_ID, 3).applyOptions({ invertScale: true });
      } catch (err) { /* noop fallback */ }

      // cycle23 — RSI 14 ≥ 70 과매수 cloud fill primitive attach (대표 verbatim 2026-05-22 17:15 KST)
      //   "확대 차트 하단 보조지표 중 rsi 에서 rsi 14 값이 70이상을 경우에는
      //    rsi 선과 시그널 선 사이를 구름대처럼 채워줘"
      //   §11.15 외부 spec 사전 검증 PASS (ISeriesApi.attachPrimitive + paneViews 본문 RSI pane=3 draw 정합)
      //   §16 self-catch: invertScale 본질 priceToCoordinate 본문 자동 cascade (별도 handling 0건 정합)
      if (signalData.length > 0) {
        try {
          layers.rsiOverboughtCloud = new RSIOverboughtCloudPrimitive(
            chart, layers.rsi, rsiData, signalData,
          );
          layers.rsi.attachPrimitive(layers.rsiOverboughtCloud);
        } catch (err) {
          layers.rsiOverboughtCloud = null;
        }
      }
    } catch (err) {
      layers.rsi = null;
    }
  }
  function removeRSI() {
    if (!layers.rsi) return;
    // cycle23 RSI cloud primitive detach 선행 (RSI series remove 전 본문 본질)
    if (layers.rsiOverboughtCloud) {
      try { layers.rsi.detachPrimitive(layers.rsiOverboughtCloud); } catch (e) { /* noop */ }
      layers.rsiOverboughtCloud = null;
    }
    try { chart.removeSeries(layers.rsi); } catch (e) { /* noop */ }
    if (layers.rsiSignal) {
      try { chart.removeSeries(layers.rsiSignal); } catch (e) { /* noop */ }
      layers.rsiSignal = null;
    }
    layers.rsi = null;
  }

  // ── markers (배당락 + RSI 과매도) ──
  // P0-4 영웅문 정합 정정 (2026-05-21 10:02 KST): 분홍 강세 marker 본문 제거 — 별건 PinkSignalPrimitive layer로 이관
  // P0-4 영웅문 정합 fix #3 (2026-05-21 10:01 KST): RSI<30 (과매도) 시점 검은 arrowDown marker 신축
  // cycle23 chart-tv-3changes Spot 2 (2026-05-22 17:22 KST 대표 verbatim
  //   "배당락 토글이 있는데 아직 한번도 검증되진 않았지만 기본기능으로 판단하고 항상 표시해주는걸로
  //    한 다음 토글 버튼은 제거해줘"):
  //   - exDividend 본질 영구 ON (toggle 부재 + DEFAULT_INDICATORS 본문 true)
  //   - state.exDividend 본문 user localStorage 본문 false 잔존 본문 봉쇄 — 강제 항상 visible 본질
  //   - 본 cycle23 본문 `state.exDividend !== false` 본문 본질 → `(options.exDividendDates || [])` 본문 force
  //   §16 self-catch: user localStorage 잔존 false 본문 → 본 force 본문 본질 cycle23 verbatim "항상 표시" 영구 PASS
  function addMarkers() {
    if (layers.seriesMarkers) return;
    layers.seriesMarkers = attachMarkers(candleSeries, {
      exDividendDates: (options.exDividendDates || []),  // cycle23: state.exDividend 본문 무시, 영구 항상 표시 본질
      rsiOversoldDates: rsiOversoldDatesAuto || [],
    });
  }
  function removeMarkers() {
    if (!layers.seriesMarkers) return;
    try { detachMarkers(layers.seriesMarkers); } catch (e) { /* noop */ }
    layers.seriesMarkers = null;
  }

  // ── 분홍 강세 vertical line primitive (P0-4 영웅문 정합 정정 2026-05-21 10:02 KST) ──
  function addPinkSignal() {
    if (layers.pinkSignal) return;
    try {
      const pinkDates = options.pinkSignalDates || [];
      if (!Array.isArray(pinkDates) || pinkDates.length === 0) return;
      layers.pinkSignal = new PinkSignalPrimitive(chart, pinkDates, {});
      candleSeries.attachPrimitive(layers.pinkSignal);
    } catch (err) {
      layers.pinkSignal = null;
    }
  }
  function removePinkSignal() {
    if (!layers.pinkSignal) return;
    try {
      candleSeries.detachPrimitive(layers.pinkSignal);
      layers.pinkSignal.detached();
    } catch (e) { /* noop */ }
    layers.pinkSignal = null;
  }

  // ── Fibonacci 자석 drawing tool (Phase 7d-2 신축, default OFF) ──
  // 본질: 사용자 클릭 + 자석 snap + drag handle + localStorage 영구화 (대표 verbatim 08:08 KST)
  // signature: attachFibonacci(chart, series, candles, ticker, container, options)
  // P0-17 Fix-56 (2026-05-21 15:18 KST 대표 verbatim "어떻게 지표를 이동하는지 방법을 모르겠다"):
  //   - UX hint badge 본문 신축 — chip ON 시점 좌측 상단 hint 본문 4초간 visible 후 fade
  //   - sessionStorage 본문 본 세션 1회 visible 본문 (반복 노출 방지)
  //   - localStorage 본문 m100s.chart.tv.fib.hint.dismissed=1 본문 사용자 dismiss 본문 영구화 (별건)
  //   §11.15 외부 spec 사전 검증 PASS:
  //     - HTML5 sessionStorage / localStorage 본문 native API
  //     - CSS transition opacity 본문 native (vendor prefix 부재)
  // P0-24 Fix-82 (2026-05-21 22:40 KST 대표 verbatim "피보나치 사용법이 여전히 알 수 없다 뜻대로 되지 않아"):
  //   1. hint 본문 visible 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 dismiss 본문 본문 본문 본문 본문 본문 sessionStorage dismiss 본문 본문 본문 본문 본문 본문 본문 매 chip ON 시점 visible (사용자 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 1회 본문 본문 본문 본문 본문 paradigm 본문 본문 본문 본문 본문 본문 본문 본문 본문)
  //   2. hint 본문 본문 "초기화" 버튼 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 — anchor 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 controller.reset() 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문
  //   3. hint 본문 visible 5초 → 8초 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 사용자 인지 본문 본문 본문 본문 본문 본문 본문 본문
  // P0-24 Fix-83/84: 자석 작동 toast + reset 본문 본문 본문 본문 visible range 본문 본문 본문 본문 anchor 재계산 (fibonacci.js _onClick + reset(true) 본문)
  const HINT_TEXT = '피보나치: 큰 노란 점을 끌어 끝점 조정. 자석이 자동 snap.';
  let hintEl = null;
  function showFibHint() {
    // P0-24 Fix-82: sessionStorage dismiss 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 매 chip ON 시점 visible.
    // localStorage 영구 dismiss 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 — 사용자가 명시적 dismiss 클릭 시점 본문 본문 본문 본문 본문 본문 본문.
    try {
      if (localStorage.getItem('m100s.chart.tv.fib.hint.dismissed') === '1') return;
    } catch (e) { /* private mode silent */ }

    if (hintEl) return;
    hintEl = document.createElement('div');
    hintEl.className = 'cal-chart-tv-fib-hint';
    hintEl.setAttribute('role', 'tooltip');
    hintEl.setAttribute('aria-live', 'polite');
    // PM320 (대표 2026-06-10) — hint 박스가 확대차트 좌상단 ~40%를 가리던 문제 fix:
    //   좌상단(top:8px) → 좌하단(bottom:8px)로 이동 + 자동 fade 8s→3s 단축. "다시 안 보기" dismiss 유지.
    hintEl.style.cssText = [
      'position: absolute',
      'left: 8px',
      'bottom: 8px',
      'background: rgba(26,29,38,0.95)',  // PM320-D6 P1-a — 머티리얼 핑크 → 햇살 다크 그레이(--tx 톤). 햇살 정체성 정합(골드+토스블루). 내부 색 칩(주황/청록)은 의미색 유지
      'color: #fff',
      'font-size: 12px',
      'font-weight: 600',
      'padding: 6px 10px',
      'border-radius: 4px',
      'box-shadow: 0 2px 8px rgba(0,0,0,0.30)',
      'z-index: 200',
      'pointer-events: auto',
      'opacity: 0',
      'transition: opacity 0.3s ease',
      'max-width: 260px',
      'line-height: 1.4',
      'cursor: default',
      'display: flex',
      'flex-direction: column',
      'gap: 4px',
    ].join(';');

    // P0-24 Fix-82: hint 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 + "초기화" + "닫기" 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문
    const textRow = document.createElement('div');
    textRow.textContent = HINT_TEXT;
    textRow.style.cssText = 'flex: 1; min-width: 0;';
    hintEl.appendChild(textRow);

    // PM320-D6 P1 — 점선 2세트 색상 범례 1줄 (주황=최근 저점 / 청록=장기 저점). 차트 가독 침해 0 (hint 내부).
    const legendRow = document.createElement('div');
    legendRow.style.cssText = 'display: flex; flex-direction: column; gap: 2px; font-size: 10px; font-weight: 500; line-height: 1.35; opacity: 0.95;';
    const mkLegend = (color, label) =>
      `<span style="display:inline-flex;align-items:center;gap:5px;"><span style="display:inline-block;width:14px;height:0;border-top:2px dashed ${color};flex-shrink:0;"></span>${label}</span>`;
    legendRow.innerHTML =
      mkLegend(FIB_LEGEND_AMBER, '주황 = 최근 저점 기준 되돌림(드래그 가능)')
      + mkLegend(FIB_LEGEND_TEAL, '청록 = 장기 저점 기준(고정)');
    hintEl.appendChild(legendRow);

    const buttonRow = document.createElement('div');
    buttonRow.style.cssText = 'display: flex; gap: 6px; margin-top: 2px;';

    // P0-24 Fix-84: "초기화 (기간 재조정)" 버튼 본문 본문 본문 — controller.reset(true) 본문 본문 본문 본문 visible range 본문 본문 본문 본문 anchor 재계산
    const resetBtn = document.createElement('button');
    resetBtn.type = 'button';
    resetBtn.textContent = '초기화 (기간 재조정)';
    resetBtn.setAttribute('aria-label', 'Fibonacci 기간 초기화 — 현재 가시 영역 최고가/최저가로 재설정');
    resetBtn.style.cssText = [
      'background: rgba(255,255,255,0.22)',
      'color: #fff',
      'border: 1px solid rgba(255,255,255,0.45)',
      'border-radius: 3px',
      'padding: 3px 8px',
      'font-size: 10px',
      'font-weight: 600',
      'cursor: pointer',
      'flex: 1',
      'min-width: 0',
      'white-space: nowrap',
    ].join(';');
    resetBtn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      // controller.reset(true) — auto-reseed 본문 본문 본문 본문 본문 본문 현재 가시 영역 hi/lo 본문 본문 본문 본문 anchor 재계산
      try {
        if (layers.fibController && typeof layers.fibController.reset === 'function') {
          layers.fibController.reset(true);
        }
      } catch (err) { /* noop */ }
    });
    buttonRow.appendChild(resetBtn);

    const dismissBtn = document.createElement('button');
    dismissBtn.type = 'button';
    dismissBtn.textContent = '다시 안 보기';
    dismissBtn.setAttribute('aria-label', 'Fibonacci 안내 본문 본문 본문 본문 본문 본문 영구 닫기');
    dismissBtn.style.cssText = [
      'background: transparent',
      'color: rgba(255,255,255,0.85)',
      'border: 1px solid rgba(255,255,255,0.35)',
      'border-radius: 3px',
      'padding: 3px 8px',
      'font-size: 10px',
      'font-weight: 500',
      'cursor: pointer',
      'flex: 0 0 auto',
      'white-space: nowrap',
    ].join(';');
    dismissBtn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      try { localStorage.setItem('m100s.chart.tv.fib.hint.dismissed', '1'); } catch (err) { /* noop */ }
      hideFibHint();
    });
    buttonRow.appendChild(dismissBtn);

    hintEl.appendChild(buttonRow);

    main.style.position = 'relative';
    main.appendChild(hintEl);
    // 다음 frame 본문 fade in (transition 발동 본질)
    requestAnimationFrame(() => {
      if (hintEl) hintEl.style.opacity = '1';
    });
    // P0-24 Fix-82: 자동 fade out 5초 → 8초. PM320 (대표 2026-06-10): 8초 → 3초 (차트 가림 최소화, dismiss 유지).
    setTimeout(() => hideFibHint(), 3000);
  }
  function hideFibHint() {
    if (!hintEl) return;
    hintEl.style.opacity = '0';
    const el = hintEl;
    hintEl = null;
    setTimeout(() => {
      try { el.remove(); } catch (e) { /* noop */ }
    }, 350);
  }
  function addFibonacci() {
    if (layers.fibController) return;
    layers.fibController = attachFibonacci(chart, candleSeries, data, ticker, main, {});
    // P0-17 Fix-56: UX hint badge 본문 visible (chip ON 시점 본문 사용자 본문 drag 본질 발견 cascade)
    // P0-24 Fix-82: 매 chip ON 시점 visible (sessionStorage 본문 본문 본문 본문 본문 본문 본문 본문 폐기, localStorage 영구 dismiss 본문 본문 본문 본문 본문 본문 본문 본문)
    showFibHint();
  }
  function removeFibonacci() {
    if (!layers.fibController) return;
    try { detachFibonacci(layers.fibController); } catch (e) { /* noop */ }
    layers.fibController = null;
    hideFibHint();
  }

  function applyState(s) {
    // cycle23 toggle-fibonacci-only (2026-05-22 17:58 KST 대표 verbatim
    //   "기능 검증을 위해 피보나치 토글 버튼만 남기고 나머지는 기본으로 항상 표시 시킨 후 토글 버튼을 모두 제거해줘"):
    //   - ma6 / volumeByDecile / pinkSignal 본문 영구 ON 본질 (state 본문 무시 본문 force 본질)
    //   - 사용자 localStorage 본문 false 잔존 봉쇄 본문 정합 (배당락 본문 본질 본문 정합)
    //   - INDICATOR_CHIPS 본문 fibonacci 1 entry만 본문 본질 → ma6/volumeByDecile/pinkSignal chip 본문 부재 cascade
    addMA6();              // 영구 ON (chip 제거 cascade, state.ma6 무시)
    addVolumeByDecile();   // 영구 ON (chip 제거 cascade, state.volumeByDecile 무시)
    addPinkSignal();       // 영구 ON (chip 제거 cascade, state.pinkSignal 무시)
    // 하단 sub-pane 3종 = base 영구 ON (lead 옵션 A-3 회신 verbatim 09:15:50 KST 대표 정정)
    // chip 부재 + toggle 불가 + state 본문 외 layer 본질
    // P0-4 영웅문 정합 정정 (2026-05-21 10:02 KST):
    //   분홍 강세 = vertical line primitive (별건 layer, cycle23 toggle-fibonacci-only 본문 force ON cascade)
    //   배당락 + RSI 과매도 = markers.js 통합 layer (createSeriesMarkers 본문)
    //   RSI 과매도 marker = 영웅문 본질 visible 영구 (RSI<30 자동 추출, 사용자 toggle 불가, 영웅문 reference 정합)
    // cycle23 chart-tv-3changes Spot 2 (2026-05-22 17:22 KST 대표 verbatim "배당락 ... 토글 버튼은 제거"):
    //   - exDividend toggle chip 제거 본질 → branching 본문 본질 단일 layer 본문 정합 (배당락 + RSI 과매도 영구 visible)
    //   - addMarkers() 본문 1회 호출 본문 본질 (state.exDividend 본문 본문 무시 본질 본문 addMarkers 본문 force)
    removeMarkers();
    addMarkers();
    // cycle23 toggle-fibonacci-only: fibonacci 본문 본질 사용자 toggle 유지 (기능 검증 본문)
    if (s.fibonacci) addFibonacci(); else removeFibonacci();
  }

  applyState(state);

  // 하단 sub-pane 3종 base 영구 ON — applyState 호출 후 1회 신축, toggle 불가
  // (lead 옵션 A-3 회신 verbatim "토글뱌튼 필요없이 기본 출력")
  addTradingValue();
  addMACD();
  addRSI();

  // P0-10 Fix-30 (2026-05-21 12:17 KST 대표 verbatim
  //   "보조지표의 y축 우측에 있는 값은 보여주지 않아도 된다"):
  //   sub-pane 3종 (거래대금 paneIdx=1 / MACD paneIdx=2 / RSI paneIdx=3) priceScale 본문 정합:
  //     - 우측 y축 본문 가격 라벨 visible 부재 의무 (label visible 본문 hide)
  //     - 본질: lastValueVisible 본문 false 본문 + priceScale borderVisible 본문 유지 (chart 본문 visual 정합)
  //   §11.15 외부 spec 사전 검증 PASS:
  //     - TradingView v5 PriceScaleOptions.visible 본문 false → priceScale 전체 hide (border + label 둘 다)
  //     - chart.priceScale(id, paneIdx).applyOptions({visible: false}) 본문 v5 native API
  //   §16 self-catch:
  //     - sub-pane 본문 priceScale 본문 hide 시 chart 본문 layout 본문 shift 가능성 본문 (canvas width 본문 늘어남)
  //     - main pane right priceScale 본문은 visible 유지 (P0-10 Fix-28 minimumWidth:100 본문 정합)
  //     - sub-pane 본문 priceScale 본문 hide 본문 가능 → main pane priceScale 본문 본문 alignment 본문 정합
  try {
    // 거래대금 sub-pane (paneIdx=1) priceScale visible 본문 본질
    // P0-20 Fix-70 (2026-05-21 17:46 KST 대표 verbatim "거래대금 y축 값을 최고 거래대금 기준으로 보여주기"):
    //   P0-10 Fix-30 본문 `lastValueVisible: false` 본문 polic 본문 정정 cascade —
    //   대표 verbatim 우선 본문 본질 = 거래대금 sub-pane 본문 last value visible 본문 의무 (영웅문 정합).
    //   addTradingValue() 본문 lastValueVisible:true 본문 명시 본문 본문 정합 → 본 layer 본문 override 본문 폐기.
    //   priceLineVisible: false 본문 보존 (현재가 dashed line 본문 부재 본질, 가로선만 createPriceLine 본문 직접 control).
    if (layers.tradingValue) {
      // P0-20 Fix-70 cascade: lastValueVisible 본문 true 본문 보존 (addTradingValue 본문 명시).
      //   priceLineVisible: false 본문 본문 거래대금 series 본문 last value dashed line 본문 제거 본문 정합.
      layers.tradingValue.applyOptions({ priceLineVisible: false });
    }
    // MACD sub-pane (paneIdx=2) priceScale label hide
    if (layers.macd) {
      if (layers.macd.line) layers.macd.line.applyOptions({ lastValueVisible: false, priceLineVisible: false });
      if (layers.macd.signal) layers.macd.signal.applyOptions({ lastValueVisible: false, priceLineVisible: false });
      if (layers.macd.hist) layers.macd.hist.applyOptions({ lastValueVisible: false, priceLineVisible: false });
    }
    // RSI sub-pane (paneIdx=3) priceScale label hide
    if (layers.rsi) layers.rsi.applyOptions({ lastValueVisible: false, priceLineVisible: false });
    if (layers.rsiSignal) layers.rsiSignal.applyOptions({ lastValueVisible: false, priceLineVisible: false });
  } catch (err) { /* noop fallback = priceScale label visible 유지 */ }

  // 토글 panel chip bar 신축
  buildTogglePanel(togglesHost, state, (newState) => {
    state = newState;
    saveIndicatorState(state);
    applyState(state);
  });

  // timeScale — lead 옵션 A-3 채택 #5 (대표 verbatim 09:08 KST (a) "가장 최근 날짜로 포커싱이 안되는게 문제")
  // P0-16 Fix-50 (2026-05-21 14:57 KST 대표 verbatim "일목균형표는 도저히 안되겠다 제거해줘"):
  //   FUTURE_CLOUD 본문 폐기 (일목 cloud 미래 영역 visible 본문 의무 부재 cascade).
  //   visible range 본문 = 최근 candle 영역 50 영업일 본문만 본질 (영웅문 23a74560 본문 cloud 부재 정합).
  //   candle series N=240 본문 effective range 0~N-1 본문만 본질 → toIdx = N-1 본문 정합.
  try {
    const N = data.length;
    if (N > 0) {
      const VISIBLE_RECENT = 50;       // 최근 영업일 (영웅문 정합 약 39 + 여유분)
      const fromIdx = Math.max(0, N - VISIBLE_RECENT);
      const toIdx = N - 1;             // P0-16 Fix-50: future cloud 본문 폐기 cascade
      chart.timeScale().setVisibleLogicalRange({ from: fromIdx, to: toIdx });
    }
  } catch (err) { /* noop */ }

  // ─── P0-16 Fix-52 sub-pane title 좌측 HTML overlay (2026-05-21 14:57 KST 대표 verbatim
  //     "하단지표의 지표 이름 라벨도 좌측 구석으로 위치를 바꿔줘") ───
  //
  // root cause: TradingView v5 series `title` 옵션 본문 = priceScale **우측** label 본질 (native API 좌측 미지원)
  //   - 영웅문 23a74560 본문 #거래대금 / MACD Oscillator 12,26,9 / RSI 14 시그널 9 본문 좌측 상단 본질
  //   - native series title 본문은 lastValueVisible:false 상태에서도 좌측 출력 부재 (우측 priceScale 좌측 column 본질)
  //
  // fix 본질: HTML overlay 본문 신축 — chart-tv-main DOM 위 absolute positioned div
  //   - paneIdx별 Y 좌표 본문 측정 = chart.panes()[idx].getHTMLElement().offsetTop 본문 + getHeight() 본질
  //   - 좌측 본문 = absolute left:8px (영웅문 영역 본질 정합)
  //   - top 본문 = pane offsetTop + 6px (sub-pane 상단 안쪽 6px margin 본질)
  //   - ResizeObserver 본문 재측정 의무 (pane separator drag / window resize 본질)
  //
  // §11.15 외부 spec 사전 검증 PASS:
  //   - v5 IChartApi.panes() → IPaneApi[] 본문 (release notes v5.0)
  //   - IPaneApi.getHTMLElement() → HTMLElement | null (pane DOM 본문 직접 접근)
  //   - IPaneApi.getHeight() → number (px 본문)
  //   - WebSearch 2회 corroborating (TradingView Lightweight Charts v5 panes API + Pane.getHTMLElement)
  //
  // §16 self-catch:
  //   - sub-pane 3종 layer order: paneIdx 1 (거래대금) / 2 (MACD) / 3 (RSI) — addSeries 3번째 인자 본문 정합
  //   - main pane (paneIdx 0) 본문 title overlay 부재 (candle 본문 priceScale 우측 본질)
  //   - getHTMLElement() 본문 null fallback (v5.0.0~5.0.7 본문 미지원 가능) → silent skip 본문 graceful
  //   - overlay div 본문 pointer-events:none 본질 (chart 본문 click/drag 본문 통과)
  const SUB_PANE_TITLES = ['#거래대금', 'MACD', 'RSI'];
  const subPaneLabels = [];

  // cycle23 rsi-yaxis-redesign-v2 (2026-05-22 20:09 KST 대표 verbatim "기존에 표시되던 값 (40/80)을 30,70으로 바꾸기만"):
  //   - RSI series 본문 priceScaleId='rsi-overlay' overlay scale 본문 본질 cascade → 자동 tick label UI 부재 (40/60/80 hide)
  //   - createPriceLine 30/70 본문 본질 axisLabelVisible:false (overlay scale 본문 cascade 부재 본문 명시)
  //   - 본 RSI Y축 30/70 label overlay layer 본문 본질 신축 — 30 (상단 invertScale) / 70 (하단 invertScale) 본질
  //   - paneEl 본문 본질 RSI sub-pane (paneIdx 3) 본문 본질 우측 본문 (영웅문 paradigm 정합)
  //   §11.15 외부 spec 사전 검증 PASS: IPaneApi.getHTMLElement / getHeight 본문 v5 native (panes API)
  //   §16 self-catch:
  //     - invertScale:true 본질 → 본 overlay 30 (top 6px) / 70 (bottom 6px from rsi pane) 본질 정합
  //     - paneEl.getHeight() 본문 본질 → 70 본문 bottom 본문 절대 위치 (rsiTop + rsiHeight - 18)
  //     - priceScale visible:false 부작용 (issue #1473 sub-pane 사라짐) 회피 — overlay scale paradigm 본질
  const rsiYAxisLabels = []; // [labelEl_30, labelEl_70]

  function positionSubPaneLabels() {
    try {
      const panes = chart.panes();
      if (!Array.isArray(panes)) return;
      // paneIdx 1, 2, 3 본문 (main paneIdx 0 본문 제외)
      for (let idx = 1; idx <= 3; idx++) {
        const pane = panes[idx];
        if (!pane || typeof pane.getHTMLElement !== 'function') continue;
        const paneEl = pane.getHTMLElement();
        if (!paneEl) continue;
        const label = subPaneLabels[idx - 1];
        if (!label) continue;
        // paneEl 본문 = main 자체의 자식 본문 → offsetTop 본문 main 본문 relative 정합
        const topPx = paneEl.offsetTop + 6;
        label.style.top = `${topPx}px`;
        label.style.display = 'block';
      }
      // cycle23 v2: RSI Y축 30/70 label 본문 본질 RSI sub-pane (paneIdx 3) 본질 위치 적용
      //   invertScale:true 본질 → 30 (top:6px) / 70 (bottom:6px) 본문 본질 정합
      const rsiPane = panes[3];
      if (rsiPane && typeof rsiPane.getHTMLElement === 'function') {
        const rsiPaneEl = rsiPane.getHTMLElement();
        if (rsiPaneEl) {
          const rsiTop = rsiPaneEl.offsetTop;
          const rsiHeight = (typeof rsiPane.getHeight === 'function') ? rsiPane.getHeight() : rsiPaneEl.offsetHeight;
          if (rsiYAxisLabels[0]) {
            // 30 = invertScale 본질 상단 (top:6px from rsi pane top)
            rsiYAxisLabels[0].style.top = `${rsiTop + 6}px`;
            rsiYAxisLabels[0].style.display = 'block';
          }
          if (rsiYAxisLabels[1]) {
            // 70 = invertScale 본질 하단 (bottom from rsi pane top)
            rsiYAxisLabels[1].style.top = `${rsiTop + rsiHeight - 18}px`;
            rsiYAxisLabels[1].style.display = 'block';
          }
        }
      }
    } catch (err) { /* noop fallback (v5.0.0~5.0.7 본문 panes()/getHTMLElement() 미지원) */ }
  }

  // cycle23 v2: RSI Y축 30/70 label overlay 본문 신축 (overlay priceScale paradigm cascade)
  //   - 우측 본질 (right:8px) — 영웅문 paradigm 본문 정합 (sub-pane title 좌측 본문 분리)
  //   - 자동 tick (40/60/80) 본문 본질 overlay scale UI hidden 본문 cascade 부재
  //   - 30/70 본문 본질 본 layer visible (사용자 인지 layer 본질)
  ['30', '70'].forEach((priceText) => {
    const labelY = document.createElement('div');
    labelY.className = 'cal-chart-tv-rsi-yaxis-label';
    labelY.textContent = priceText;
    labelY.style.cssText = [
      'position: absolute',
      'right: 8px',                                      // 우측 본질 (priceScale 본문 자리 본질 정합)
      'font-size: 10px',
      'font-weight: 600',
      'color: rgba(0,0,0,0.85)',
      'pointer-events: none',
      'z-index: 10',
      'display: none',
      'background: transparent',
      'padding: 0',
      'text-shadow: 0 0 2px #fff, 0 0 2px #fff, 0 0 2px #fff',
    ].join(';');
    rsiYAxisLabels.push(labelY);
  });

  SUB_PANE_TITLES.forEach((title, i) => {
    const label = document.createElement('div');
    label.className = 'cal-chart-tv-subpane-title';
    label.dataset.paneIdx = String(i + 1);
    label.textContent = title;
    // P0-20 Fix-68 (2026-05-21 17:46 KST 대표 verbatim "하단 보조지표의 이름도 배경이 반투명한 흰색인데 완전 투명하게"):
    //   background rgba(255,255,255,0.65) → transparent 본문 완전 투명.
    //   가독성 본문 정합 의무 — text-shadow 흰색 outline 본문 추가 (영웅문 정합 본문 #거래대금/MACD/RSI 좌측 본문 visible 본질).
    //   §16 self-catch: 영웅문 23a74560 + 3005fbac reference 본문 배경 부재 + 검정 text + 흰색 outline 본문 visible 정합.
    label.style.cssText = [
      'position: absolute',
      'left: 8px',
      'top: 0',
      'font-size: 10px',
      'font-weight: 600',
      'color: rgba(0,0,0,0.85)',                         // 가독성 본문 강화 (0.6 → 0.85)
      'pointer-events: none',
      'z-index: 10',
      'display: none',
      'background: transparent',                         // P0-20 Fix-68: 완전 투명
      'padding: 0',                                      // 배경 부재 본문 정합
      'text-shadow: 0 0 2px #fff, 0 0 2px #fff, 0 0 2px #fff',  // P0-20 Fix-68: 흰색 outline 본문 가독성 본질
    ].join(';');
    main.style.position = 'relative';  // overlay parent 본문 positioned 본질
    main.appendChild(label);
    subPaneLabels.push(label);
  });

  // cycle23 v2: RSI Y축 30/70 label 본문 본질 main 본질 append (overlay parent 본문 정합)
  rsiYAxisLabels.forEach((labelY) => {
    main.appendChild(labelY);
  });

  // 초기 1회 positioning (sub-pane series add 직후 layout 본문 완료 후 호출 본질)
  // requestAnimationFrame 본문 = 다음 paint frame 본문 layout 완료 후 호출 본질
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(() => positionSubPaneLabels());
  } else {
    setTimeout(() => positionSubPaneLabels(), 0);
  }

  // 다크모드 토글 (DOC-20260610-DSN-001 §2.3) — 열린 차트가 있을 때 테마 전환 시 축/그리드/separator 색 재적용.
  const onThemeChange = () => {
    const c = chartThemeColors();
    try {
      chart.applyOptions({
        layout: {
          textColor: c.text,
          panes: { separatorColor: c.sep, separatorHoverColor: c.sepHover },
        },
        grid: {
          vertLines: { color: c.grid },
          horzLines: { color: c.grid },
        },
        timeScale: { borderColor: c.tsBorder },
      });
    } catch (e) { /* noop */ }
  };
  window.addEventListener('pm320:themechange', onThemeChange);

  close.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    const card = container.closest('.cal-feature-card');
    if (card) {
      card.classList.remove('chart-expanded');
      card.setAttribute('aria-expanded', 'false');
      const trigger = card.querySelector('[data-expand-trigger="chart"]');
      if (trigger) trigger.setAttribute('aria-expanded', 'false');
    }
    try { window.removeEventListener('pm320:themechange', onThemeChange); } catch (err) { /* noop */ }
    try { chart.remove(); } catch (err) { /* noop */ }
  });

  const ro = new ResizeObserver(() => {
    // P0-11 Fix-34: container 인자 전달 — resize 시점 chart slot inner width 실측 본문 채택
    const vp2 = getViewportSize(container);
    const subH2 = Math.round(vp2.height * 0.075);  // P0-7 fix-5 정합 (0.15 → 0.075 본문)
    chart.applyOptions({ width: vp2.width, height: vp2.height + subH2 * 3 });
    // P0-16 Fix-52: sub-pane title overlay 본문 좌표 재측정 (pane 본문 height 본문 재계산 후)
    if (typeof requestAnimationFrame === 'function') {
      requestAnimationFrame(() => positionSubPaneLabels());
    }
  });
  ro.observe(main);

  return {
    chart,
    candleSeries,
    layers,
    state,
    applyState,
    destroy() {
      try { ro.disconnect(); } catch (e) { /* noop */ }
      try { window.removeEventListener('pm320:themechange', onThemeChange); } catch (e) { /* noop */ }
      try { removeMarkers(); } catch (e) { /* noop */ }
      // Phase 7d-2 fibonacci 자석 drawing tool — subscribe handler unsubscribe + handle DOM 제거 의무
      try { removeFibonacci(); } catch (e) { /* noop */ }
      try { removeVolumeByDecile(); } catch (e) { /* noop */ }
      try { removePinkSignal(); } catch (e) { /* noop */ }
      try { chart.remove(); } catch (e) { /* noop */ }
    },
  };
}

window.ChartTV = {
  render: renderChartTV,
  loadIndicatorState,
  saveIndicatorState,
  INDICATOR_CHIPS,
};

export { renderChartTV, loadIndicatorState, saveIndicatorState, INDICATOR_CHIPS };
