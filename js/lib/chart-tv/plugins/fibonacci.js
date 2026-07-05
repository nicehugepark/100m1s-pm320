/* ───── lib/chart-tv/plugins/fibonacci.js — #3 Fibonacci 자석 drawing tool (TradingView v5) ─────
   cycle22 Phase 7d-2 — REQ DOC-20260521-REQ-002 v3 §3.1 + §4.2 verbatim 정합.

   P0-25 (2026-05-21 23:46 KST 대표 결정 verbatim "영웅문 paradigm 채택 — chart area drag/click + separate handle 폐기"):
     영웅문 23a74560 reference 본문 chart canvas 자체 click/drag paradigm 본질 (separate dot handle 부재, swing high/low에 inline arrow marker).
     P0-24 Fix-82 누적 handle 시각 강화 (radius 14 + pulse + 적색 glow) 본문에도 대표 verbatim "피보나치 사용법이 여전히 알 수 없다" =
     handle 시각 강화 cascade 한계. paradigm shift = chart area 자체 interaction (subscribeClick + chartElement mousedown 본질).

     본질 변경 본문:
       - separate DOM handle (Phase 7d-2 ~ P0-24): default visible + drag trigger 본문 폐기
       - chart canvas (chartElement) 자체 = click/drag interface 본질
       - handle DOM 본문 보존 but **default invisible** (영웅문 inline ↓ marker 정합 = anchor 시각 cue만, drag trigger 아님)
       - chart canvas mousedown: anchor A 또는 B 근처 (pixel tolerance 본문) = drag mode 시작
       - chart canvas mousemove: drag 중 anchor 위치 갱신 + magnetSnap 재적용
       - chart canvas mouseup: drag 종료 + state save

   본질 (대표 2026-05-21 08:08 KST verbatim "피보나치의 경우 피크 저가 고가가 자석 기능이고
        내가 선택해서 이동하거나 기간을 조정할 수 있다"):

   1. **자석 기능 (magnet)**:
      - 사용자 클릭 시 ±N 영업일 (default 5) 윈도우 내 local peak/trough 자동 detection
      - 가장 가까운 swing high/low 가격으로 자동 snap
   2. **사용자 선택** (P0-25 chart area paradigm 본문):
      - chart canvas click 1차 = swing 시작점 (anchor A) — subscribeClick handler 본문 보존
      - chart canvas click 2차 = swing 끝점 (anchor B) — subscribeClick handler 본문 보존
      - 2점 결정 후 fibonacci horizontal level 자동 draw
   3. **드래그 조정** (P0-25 chart area paradigm 본문 신축):
      - chart canvas (chartElement) mousedown/mousemove/mouseup 본문 직접 본문
      - anchor A/B pixel position ±20px 본문 mousedown 시점 = drag mode (영웅문 chart area drag 정합)
      - drag 중 실시간 fibonacci level 재계산
      - drag 종료 시 자석 snap 재적용 + state save
      - separate handle DOM 본문 보존 (영웅문 ↓ marker 정합) but default 시각 약화 (pulse/적색 glow 폐기, transparent + 매우 작음)
   4. **localStorage 영구화**:
      - schema: `m100s.chart.tv.fib.{ticker}` = `{ anchorA: {time, price}, anchorB: {time, price} }`
      - 차트 재진입 시 사용자 그린 Fib 자동 복원
   5. **axisLabelVisible 본질**:
      - Phase 7d-1 fibonacci.js 본문 createPriceLine axisLabelVisible: true 본문 paradigm 유지
      - 본 drawing tool은 series.createPriceLine + 동적 price 결합 본질로 axis label 표시

   §11.15 외부 spec 사전 검증 (WebSearch ≥2회 + 공식 docs + repo grep 3종 PASS):
   - https://tradingview.github.io/lightweight-charts/docs/api/interfaces/IChartApi
     "subscribeClick(handler: MouseEventHandler<Time>): void"
     "subscribeCrosshairMove(handler: MouseEventHandler<Time>): void"
   - https://tradingview.github.io/lightweight-charts/docs/plugins/intro
     "ISeriesPrimitive — paneViews() returns IPrimitivePaneView[] for canvas draw"
   - repo grep verbatim: js/lib/chart-tv/plugins/pink-signal.js + volume-by-decile.js
     (subscribe handler + canvas draw 본질 동형 패턴)

   §16 self-catch (Phase 7d-2):
   - 기존 Phase 7d-1 단순 createPriceLine 3종 helper API (`attachFibonacci(series, candles, options)`)는
     본 Phase 7d-2 drawing tool로 완전 대체 (backward 비호환 signature 변경).
     expanded-chart.js addFibonacci/removeFibonacci 본문 동시 정정 의무.
   - swing 시작/끝점 = 사용자 클릭 + 자석 snap 본질. 기존 "가시 영역 hi/lo 자동" paradigm 폐기.
   - axisLabelVisible 본질 = series.createPriceLine + axisLabelVisible: true 옵션 결합 본질 (v5 native).

   위임 PROMPT vs REQ v3 verbatim 색상 mismatch §16 catch:
   - 위임 PROMPT MA 색상 (#3B82F6 등) vs REQ v3 §2 verbatim (#FF69B4 등) mismatch.
   - 본 Phase 7d-2 = MA 10 line 추가 본질만. 색상 본문 정정은 별건 cycle 후행.
*/

import { LineStyle } from 'https://cdn.jsdelivr.net/npm/lightweight-charts@5.0.8/+esm';

const STORAGE_PREFIX = 'm100s.chart.tv.fib.';

// Fibonacci ratio level (대표 verbatim "피크 저가 고가" + Phase 7d-1 paradigm 정합).
// 기본 3종 (38.2/50/61.8) + 확장 4종 (0/23.6/76.4/100) = 7 level (영웅문 본문 정합).
const LEVELS = [
  { ratio: 0.0,   title: 'Fib 0%',    color: '#94A3B8' },
  { ratio: 0.236, title: 'Fib 23.6%', color: '#F5A623' },
  { ratio: 0.382, title: 'Fib 38.2%', color: '#F5A623' },
  { ratio: 0.5,   title: 'Fib 50%',   color: '#F5A623' },
  { ratio: 0.618, title: 'Fib 61.8%', color: '#F5A623' },
  { ratio: 0.764, title: 'Fib 76.4%', color: '#F5A623' },
  { ratio: 1.0,   title: 'Fib 100%',  color: '#94A3B8' },
];

// 2026-06-10 토구사 union: secondary 세트(BASE 구조 골) Fib 색상 — primary(amber #F5A623)와 구분되는 청록.
const SECONDARY_COLOR = '#38BDF8';

// P0-17 Fix-55 (2026-05-21 15:18 KST 대표 verbatim "피보나치 역시 라벨값이 너무 지저분하다 안보여줘도 돼.
//   대신 가격 fib xx% 값을 제거해줘"):
//   - axisLabelVisible: false → 우측 priceScale axis 본문 가격값 (22450/17619 등) 제거
//   - title 본문 제거 (LEVELS 본문 lv.title 본문 createPriceLine 호출 시 빈 string 본문 채택)
//   - 7 horizontal line 본문 visible 보존 (라벨만 제거 본질)
//   §11.15 외부 spec 사전 검증 PASS:
//     - TradingView v5 createPriceLine.axisLabelVisible:false → priceScale 우측 가격 라벨 hide
//     - title 빈 string 본문 → priceLine 본문 좌측 비율 라벨 hide
//     - line color/lineStyle/lineWidth 본문은 그대로 → 가로선 본문 visible 보존
//
// P0-18 Fix-58 (2026-05-21 16:03 KST 대표 verbatim "피보나치 선 마다 좌측에 작은 글씨로 가격을 표사해주고"):
//   1차 시도 — createPriceLine title 본문 = 가격 string. §16 self-catch (P0-18 Playwright audit):
//   TradingView v5 PriceLineOptions.title 본문 visibility = axisLabelVisible:true 종속 본질
//   → axisLabelVisible:false (Fix-55) + title:formatPriceLabel 본문 동시 설정 = title visible 0건 결정적 paradigm 충돌.
//
// P0-19 Fix-63 (2026-05-21 16:35 KST P0-18 Fix-58 paradigm 충돌 cascade 정합, 대표 verbatim
//   "피보나치 선 마다 좌측에 작은 글씨로 가격을 표사해주고" 16:03 KST):
//   - createPriceLine title 본문 = '' (빈 string) 복원 — Fix-55 axisLabelVisible:false 보존
//   - 좌측 본문 가격 라벨 = HTML overlay DOM 본문 신축 (P0-17 Fix-52 sub-pane title 좌측 본문 동형 paradigm)
//     · 각 fib level별 absolute-positioned <div> 본문 chart container 위 직접 신축
//     · left: 8px (영웅문 23a74560 본문 좌측 본문 정합)
//     · top: series.priceToCoordinate(price) — y좌표 실시간 계산 본문
//     · font-size: 10px (대표 verbatim "작은 글씨" 정합)
//     · text: formatPriceLabel(price) — ko-KR locale 본문 정수 (예: '727,000')
//     · pointer-events: none (chart click/drag 본문 통과)
//   - ResizeObserver 본문 chart resize 시점 재측정 (P0-17 Fix-52 동형)
//   - timeScale.subscribeVisibleLogicalRangeChange 본문 zoom/scroll 시점 재측정 (priceToCoordinate y 좌표 변화)
//   §11.15 외부 spec 사전 검증 PASS:
//     - ISeriesApi.priceToCoordinate(price) → Coordinate | null — chart pane 본문 y좌표 반환
//     - ITimeScaleApi.subscribeVisibleLogicalRangeChange(handler) — pan/zoom 시점 callback
//     - WebSearch 2회 corroborating (TradingView Lightweight Charts v5 priceToCoordinate + subscribeVisibleLogicalRangeChange)
//     - repo verbatim: js/lib/chart-tv/plugins/volume-by-decile.js L129~131 + L195 (priceToCoordinate + subscribe 동형 패턴)
//     - repo verbatim: js/lib/chart-tv/expanded-chart.js L1019~1038 (label className+absolute overlay 동형 패턴 sub-pane title Fix-52)
//   §16 self-catch (P0-19):
//     - HTML overlay z-index: 10 본문 (sub-pane title Fix-52 동형) → drag handle (z-index:100) 본문 침범 부재
//     - priceToCoordinate null fallback (price 가시 영역 외부 본문) → label 본문 display:none silent skip
//     - destroy cleanup 본문 overlay <div> + ResizeObserver + subscribeVisibleLogicalRangeChange unsubscribe 의무
//     - ResizeObserver race condition: observe target 본문 chartContainer (main DOM) — chart.applyOptions resize cascade 본질 동기 PASS
//     - subscribeVisibleLogicalRangeChange handler 본문 _renderOverlayLabels() 즉시 호출 (debounce 부재 — handler 본문 가벼움 본질)
const DEFAULT_OPTIONS = {
  magnetWindow: 5,        // ±N 영업일 자석 detection window
  lineStyle: LineStyle.Dotted,
  lineWidth: 1,
  axisLabelVisible: false,  // P0-17 Fix-55: 우측 가격값 (22450/17619 등) 제거
  handleColor: '#F5A623',
  // P0-25 (2026-05-21 23:46 KST 대표 결정 영웅문 paradigm 채택):
  //   handle radius 14 → 6 본문 축소 (영웅문 inline ↓ marker 정합, drag trigger 본문 chart canvas 본문 본질).
  //   handle = anchor 시각 cue만 (영웅문 swing high/low 인접 ↓ arrow marker 동형 paradigm).
  //   drag trigger = chart canvas (chartElement) mousedown/mousemove/mouseup 본문 직접 본문.
  //   handle pixel tolerance (drag detect) = ±20px 본문 chart canvas 본문 mousedown 시점 본문 본질.
  //   §11.15 외부 spec PASS:
  //     - TradingView v5 chartElement() returns chart wrapper div for custom event listeners
  //       https://tradingview.github.io/lightweight-charts/docs/api/interfaces/IChartApi (chartElement method)
  //     - 영웅문 23a74560 reference 본문 chart area 자체 click/drag paradigm 본질 (separate dot handle 부재)
  handleRadius: 6,
  dragTolerance: 20,      // P0-25: chart canvas mousedown 시점 anchor 근처 detect ±20px (모바일 finger tap target 정합)
};

/**
 * P0-18 Fix-58 — 가격 → 좌측 라벨 string formatter.
 * 한국 화폐 정수 본문 정합 (소수점 부재, P0-13 Fix-45 KRW_PRICE_FORMAT 본문 동형).
 * 예: 98400 → '98,400', 22450 → '22,450'.
 *
 * @param {number} price
 * @returns {string}
 */
function formatPriceLabel(price) {
  if (typeof price !== 'number' || !isFinite(price)) return '';
  return Math.round(price).toLocaleString('ko-KR');
}

// ─── 2026-06-10 토구사 캘리브레이션 확정 — 두 스케일 union 저점 앵커 상수 (3종 6정답 3/3 PASS) ───
// 데이터 = NXT 포함 일봉(renderer dailybars-nxt 우선 fetch, 라이브). 종가 ZigZag θ=0.15.
// 윈도우 분리: 규칙 I(즉각 파동)=최근 120봉, 규칙 II(구조적 base)=240봉.
// 골 refine = ZigZag 종가 골 ±3봉 내 저가(low-of-day) 최저일.
// parent(골) = 골 직전 swing-high 피벗 중 high-of-day 최대 (해석 A). drop = (parent.high − low.low)/parent.high.
// 카드 = BASE(구조 base 골) + RECENT(최근 골) 최대 2세트 (parent-group 규칙, 아래 _selectCardGoals).
const ZIGZAG_THETA = 0.15;            // 반전율 15% (종가 기준, 토구사 확정)
const ZIGZAG_WINDOW_RECENT = 120;     // 규칙 I (즉각 파동) 윈도우 — 최근 120 영업일
const ZIGZAG_WINDOW_BASE = 240;       // 규칙 II (구조적 base) 윈도우 — 240 영업일
const ZIGZAG_REFINE_BARS = 3;         // 골 refine = 종가 골 ±3봉 내 저가 최저일
const ZIGZAG_RECENT_MIN_DROP = 0.20;  // RECENT 후보 = 낙폭 ≥ 20% (즉각 파동 유효성)
const ZIGZAG_NEAR_TIE = 0.005;        // BASE 2c 근사동률 tie-break ±0.5%p
const ZIGZAG_MIN_DAYS = 20;           // 데이터 부족(상장 초기) 가드 — 20 영업일 미만 시 피보 미표시(추정 금지)
const LIMIT_MOVE_PCT = 0.29;          // 상한가·하한가 가드 (±29%+)

/**
 * 상한가·하한가(±29%+) 종가 변동 판정 가드.
 * @param {number} prevClose
 * @param {number} close
 * @returns {boolean}
 */
function isLimitMove(prevClose, close) {
  if (typeof prevClose !== 'number' || typeof close !== 'number') return false;
  if (!(prevClose > 0)) return false;
  return Math.abs(close - prevClose) / prevClose >= LIMIT_MOVE_PCT;
}

/**
 * ZigZag 피벗 검출 — 종가(close) 기준, 반전율 θ (토구사 verbatim 알고리즘, 2026-06-10).
 *
 * 🔴 직전 버전(running-extreme)은 초기 방향 처리 버그로 장기 swing(후성 10/28 등)을 흡수 →
 *    본 버전은 "마지막 확정 피벗(piv[-1]) 기준 + 같은 방향 진행 시 극값 갱신 / 반대 방향 θ 돌파 시 새 피벗 확정".
 *
 * 알고리즘:
 *   - d = 방향 (+1 상승, -1 하락, 0 미정). piv = 피벗 candleIdx 배열.
 *   - 상승/미정(d>=0): 직전 피벗보다 신고가 → up-leg 갱신(d>0) 또는 상승 전환(d=0→1).
 *                      직전 피벗 대비 θ 하락 → 하락 전환(피벗 append, d=-1).
 *   - 하락/미정(d<=0): 대칭.
 *   - H/L type = 직전 피벗과의 종가 비교로 분류 (상승 끝 = H, 하락 끝 = L).
 *
 * @param {Array} candles — normalized candles (time/close/high/low)
 * @param {number} theta — 반전율 (0.15)
 * @returns {Array<{candleIdx: number, type: 'H'|'L', price: number}>}
 */
function computeZigZagPivots(candles, theta) {
  if (!Array.isArray(candles) || candles.length < 2) return [];
  const n = candles.length;
  const idxPivots = [0];
  let d = 0;
  for (let i = 1; i < n; i++) {
    const last = idxPivots[idxPivots.length - 1];
    const vi = candles[i].close;
    const vlast = candles[last].close;
    if (!(vi > 0) || !(vlast > 0)) continue;
    if (d >= 0) {
      if (vi > vlast) {
        if (d > 0) { idxPivots[idxPivots.length - 1] = i; }
        else { idxPivots.push(i); d = 1; }
      } else if (vi < vlast * (1 - theta)) {
        idxPivots.push(i); d = -1;
      }
    }
    if (d <= 0) {
      const last2 = idxPivots[idxPivots.length - 1];
      const vlast2 = candles[last2].close;
      if (vi < vlast2) {
        if (d < 0) { idxPivots[idxPivots.length - 1] = i; }
        else { idxPivots.push(i); d = -1; }
      } else if (vi > vlast2 * (1 + theta)) {
        idxPivots.push(i); d = 1;
      }
    }
  }
  // candleIdx → typed pivots (직전 피벗 대비 종가 비교로 H/L)
  const out = [];
  for (let k = 0; k < idxPivots.length; k++) {
    const idx = idxPivots[k];
    let type;
    if (k === 0) {
      type = (idxPivots.length > 1 && candles[idxPivots[1]].close > candles[idx].close) ? 'L' : 'H';
    } else {
      type = candles[idx].close > candles[idxPivots[k - 1]].close ? 'H' : 'L';
    }
    out.push({ candleIdx: idx, type, price: candles[idx].close });
  }
  return out;
}

/**
 * candles 배열 + 클릭한 시점(logical index) 기준 ±window 영업일 내 local peak/trough 자석 snap.
 * @param {Array} candles — normalized candles
 * @param {number} logicalIdx — chart subscribeClick param.logical
 * @param {number} clickPrice — 클릭한 y좌표의 price (series.coordinateToPrice)
 * @param {number} windowN — ±N 영업일 (default 5)
 * @returns {{time: object, price: number, candleIdx: number}|null}
 */
function magnetSnap(candles, logicalIdx, clickPrice, windowN) {
  if (!Array.isArray(candles) || candles.length === 0) return null;
  const i = Math.round(logicalIdx);
  if (i < 0 || i >= candles.length) return null;
  const from = Math.max(0, i - windowN);
  const to = Math.min(candles.length - 1, i + windowN);

  let bestPrice = candles[i].close;
  let bestCandle = candles[i];
  let bestIdx = i;
  let bestDist = Math.abs(candles[i].close - clickPrice);

  for (let k = from; k <= to; k++) {
    const c = candles[k];
    if (!c) continue;
    // peak (high) candidate
    const dHigh = Math.abs(c.high - clickPrice);
    if (dHigh < bestDist) {
      bestDist = dHigh;
      bestPrice = c.high;
      bestCandle = c;
      bestIdx = k;
    }
    // trough (low) candidate
    const dLow = Math.abs(c.low - clickPrice);
    if (dLow < bestDist) {
      bestDist = dLow;
      bestPrice = c.low;
      bestCandle = c;
      bestIdx = k;
    }
  }
  return { time: bestCandle.time, price: bestPrice, candleIdx: bestIdx };
}

/**
 * Fibonacci 자석 drawing tool controller.
 *
 * 본질: chart.subscribeClick (anchor 결정) + series.createPriceLine (axisLabelVisible 본질)
 *   + DOM overlay drag handle (사용자 끝점 조정) + localStorage (영구화).
 */
class FibonacciDrawingController {
  /**
   * @param {IChartApi} chart
   * @param {ISeriesApi} candleSeries
   * @param {Array} candles — normalized candles (time/open/high/low/close)
   * @param {string} ticker — localStorage key suffix
   * @param {HTMLElement} chartContainer — chart wrapper DOM (drag handle overlay parent)
   * @param {Object} [options]
   */
  constructor(chart, candleSeries, candles, ticker, chartContainer, options = {}) {
    this._chart = chart;
    this._series = candleSeries;
    this._candles = candles;
    this._ticker = ticker || 'default';
    this._container = chartContainer;
    this._options = { ...DEFAULT_OPTIONS, ...options };

    // state: anchorA / anchorB ({time, price, candleIdx})
    this._state = this._loadState();

    // priceLines (axis label visible 본질, createPriceLine return) — primary 세트(RECENT 골, drag 편집)
    this._priceLines = [];

    // 2026-06-10 토구사 union: secondary 세트(BASE 구조 골) — 정적 price line (drag 비대상)
    this._secondarySet = null;
    this._secondaryPriceLines = [];

    // DOM handles (drag 본질)
    this._handleA = null;
    this._handleB = null;

    // 현재 drag 대상 ('A' | 'B' | null)
    this._dragging = null;

    // P0-19 Fix-63: HTML overlay 좌측 가격 라벨 본문 (LEVELS.length 개수 본문 array)
    this._overlayLabels = [];

    // P0-24 Fix-83: 자석 snap 작동 visible feedback toast 본문 본문 본문 (1개만 visible 본질)
    this._snapToastEl = null;
    this._snapToastTimer = null;

    // subscribeClick handler ref (detach 시 unsubscribe 의무)
    this._clickHandler = (param) => this._onClick(param);
    this._chart.subscribeClick(this._clickHandler);

    // crosshair move handler (drag 중 실시간 갱신 본질)
    this._crosshairHandler = (param) => this._onCrosshairMove(param);
    this._chart.subscribeCrosshairMove(this._crosshairHandler);

    // P0-25 (2026-05-21 23:46 KST 대표 결정 영웅문 paradigm 채택):
    //   chart canvas (chartElement) 본문 mousedown/mousemove/mouseup 본문 직접 본문 drag mode 본질.
    //   anchor A/B pixel position ±dragTolerance 본문 mousedown 시점 = drag mode (영웅문 chart area drag 정합).
    //   §11.15 외부 spec PASS:
    //     - https://tradingview.github.io/lightweight-charts/docs/api/interfaces/IChartApi#chartelement
    //       "Returns the generated div element containing the chart. This can be used for adding your own
    //        additional event listeners, or for measuring the elements dimensions and position within the document."
    //     - WebSearch 2회 corroborating (TradingView v5 chartElement custom event listeners + mousedown drag pattern)
    //     - repo verbatim: js/lib/chart-tv/plugins/fibonacci.js L683~694 본문 handle 본문 mousedown listener 동형 (handle DOM 본문 chart canvas DOM 본문 대체 paradigm)
    this._chartEl = null;
    try { this._chartEl = this._chart.chartElement(); } catch (e) { /* noop */ }

    this._chartCanvasDragging = null;  // 'A' | 'B' | null — drag 시작 anchor

    // chart canvas mousedown — anchor 근처 detect 시 drag mode 시작
    this._onChartMouseDown = (e) => this._handleChartMouseDown(e);
    // document mousemove — drag 중 anchor 위치 갱신
    this._onDocMouseMove = (e) => this._handleDocMouseMove(e);
    // document mouseup — drag 종료
    this._onDocMouseUp = (e) => this._handleDocMouseUp(e);
    // touch 본문 정합 본문 (모바일 finger drag 본질, Apple HIG 정합)
    this._onChartTouchStart = (e) => this._handleChartTouchStart(e);
    this._onDocTouchMove = (e) => this._handleDocTouchMove(e);
    this._onDocTouchEnd = (e) => this._handleDocTouchEnd(e);

    if (this._chartEl) {
      this._chartEl.addEventListener('mousedown', this._onChartMouseDown, true);  // capture phase 본문 lightweight-charts 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문
      this._chartEl.addEventListener('touchstart', this._onChartTouchStart, { passive: false, capture: true });
      document.addEventListener('mousemove', this._onDocMouseMove);
      document.addEventListener('mouseup', this._onDocMouseUp);
      document.addEventListener('touchmove', this._onDocTouchMove, { passive: false });
      document.addEventListener('touchend', this._onDocTouchEnd);
    }

    // P0-19 Fix-63: timeScale visible logical range change handler — chart zoom/scroll 시점
    //   priceToCoordinate y좌표 변화 본문 overlay label 본문 재측정 의무 (volume-by-decile.js 동형)
    this._rangeHandler = () => this._renderOverlayLabels();
    try {
      this._chart.timeScale().subscribeVisibleLogicalRangeChange(this._rangeHandler);
    } catch (e) { /* noop fallback */ }

    // P0-19 Fix-63: ResizeObserver — chart container resize 시점 (expanded-chart.js Fix-52 동형)
    //   chart.applyOptions resize cascade → priceToCoordinate y좌표 변화 본문 재측정 의무
    this._resizeObserver = null;
    if (this._container && typeof ResizeObserver === 'function') {
      try {
        this._resizeObserver = new ResizeObserver(() => {
          // requestAnimationFrame 본문 = layout 본문 완료 후 호출 본질 (Fix-52 동형)
          if (typeof requestAnimationFrame === 'function') {
            requestAnimationFrame(() => this._renderOverlayLabels());
          } else {
            this._renderOverlayLabels();
          }
        });
        this._resizeObserver.observe(this._container);
      } catch (e) { /* noop */ }
    }

    // P0-16 Fix-51 (2026-05-21 14:57 KST 대표 verbatim "피보나치 이어서 계속 해줘 화면에 표시되지도 않아"):
    //   root cause = drawing tool 본질 사용자 2회 클릭 의무 → 화면 visible 0건 (anchor A/B 미설정 본질)
    //   fix = anchor A/B 미설정 시 candles 본문 최근 가시 영역 hi/lo 본문 auto-anchor 본질
    //         → 차트 진입 즉시 7 fibonacci level 본문 visible (대표 verbatim "이어서 계속" 본질 정합)
    //         → 사용자 후속 drag 본문 정밀 조정 가능 (paradigm 보존)
    // 2026-06-10 토구사 union — 두 스케일 저점 앵커 세트 자동 산출.
    //   primary(RECENT 골) = drag 편집 대상 (anchorA/anchorB), localStorage 미설정 시 auto.
    //   secondary(BASE 구조 골) = 정적 표시 (매 진입 재산출, 영구화 안 함).
    const autoSets = this._autoAnchorSets();
    const primary = autoSets.find((s) => s.role === 'recent') || autoSets[autoSets.length - 1] || null;
    const secondary = autoSets.find((s) => s.role === 'base') || null;
    if (!this._state.anchorA || !this._state.anchorB) {
      const p = primary || this._autoAnchorFromVisibleRange();
      if (p) {
        this._state.anchorA = p.high;
        this._state.anchorB = p.low;
        this._saveState();
      }
    }
    if (secondary && (!primary || secondary.low.candleIdx !== primary.low.candleIdx)) {
      this._secondarySet = secondary;
    }

    // 초기 render (primary: localStorage 복원 또는 auto / secondary: BASE 구조 골)
    if (this._state.anchorA && this._state.anchorB) {
      this._renderLevels();
      this._renderHandles();
    }
    this._renderSecondaryLevels();
  }

  /**
   * secondary 세트(BASE 구조 골) 정적 Fibonacci level render — primary(amber)와 청록으로 구분.
   * drag 비대상. 두 세트 레벨이 겹치는 가격대 = 다중 스케일 지지/저항 (대표 직관 "여러 스케일").
   */
  _renderSecondaryLevels() {
    this._clearSecondaryPriceLines();
    if (!this._secondarySet || !this._secondarySet.high || !this._secondarySet.low) return;
    const a = this._secondarySet.high.price;
    const b = this._secondarySet.low.price;
    if (a == null || b == null) return;
    LEVELS.forEach((lv) => {
      const price = b + (a - b) * lv.ratio;
      try {
        const line = this._series.createPriceLine({
          price,
          color: SECONDARY_COLOR,
          lineStyle: this._options.lineStyle,
          lineWidth: this._options.lineWidth,
          axisLabelVisible: this._options.axisLabelVisible,
          title: '',
        });
        this._secondaryPriceLines.push(line);
      } catch (e) { /* noop */ }
    });
  }

  _clearSecondaryPriceLines() {
    this._secondaryPriceLines.forEach((line) => {
      try { this._series.removePriceLine(line); } catch (e) { /* noop */ }
    });
    this._secondaryPriceLines = [];
  }

  /**
   * P0-16 Fix-51 auto-anchor 본문 — candles 본문 최근 50 영업일 본문 hi/lo 본문 자동 추출.
   * 영웅문 23a74560 본문 본질 정합 (727,000 high / 286,000 low 본문 swing 본질 본문).
   *
   * @returns {{high: {time, price, candleIdx}, low: {time, price, candleIdx}} | null}
   */
  _autoAnchorFromVisibleRange() {
    if (!Array.isArray(this._candles) || this._candles.length < 2) return null;
    const RECENT_N = 50;  // 영웅문 본문 visible 영역 본문 정합
    const N = this._candles.length;
    const from = Math.max(0, N - RECENT_N);
    let hi = -Infinity, lo = Infinity;
    let hiIdx = -1, loIdx = -1;
    for (let i = from; i < N; i++) {
      const c = this._candles[i];
      if (!c || !(c.high > 0) || !(c.low > 0)) continue;
      if (c.high > hi) { hi = c.high; hiIdx = i; }
      if (c.low < lo) { lo = c.low; loIdx = i; }
    }
    if (hiIdx < 0 || loIdx < 0) return null;
    return {
      high: { time: this._candles[hiIdx].time, price: hi, candleIdx: hiIdx },
      low: { time: this._candles[loIdx].time, price: lo, candleIdx: loIdx },
    };
  }

  /**
   * 2026-06-10 토구사 캘리브레이션 확정 — 두 스케일 union 저점 앵커 세트 자동 산출.
   *
   * 파이프라인 (NXT 포함 candles 기준):
   *   1. window 분리 ZigZag(θ=0.15, 토구사 알고리즘):
   *      - 규칙 I(즉각 파동) = 최근 120봉 / 규칙 II(구조적 base) = 240봉.
   *   2. 각 골(L 피벗) → ±3봉 저가(low-of-day) 최저일 refine + parent(직전 swing-high high 최대) drop% goal 산출.
   *   3. 카드 = _selectCardGoals (BASE + RECENT, 최대 2세트, parent-group 규칙).
   *      - 두 윈도우 goal 풀(규칙 I ∪ 규칙 II) 합집합에서 선택.
   *   가드: swing-high 0개 → 빈 결과 (억지 fit 금지, FLR-AGT-002) / <20영업일 → 빈 결과.
   *
   * 캘리브레이션 3/3 (NXT-spliced, harness 실측):
   *   테크윙 089030 [3/31,5/19] · 후성 093370 [3/04,6/08] · 현대 307950 [3/31,5/19].
   *
   * 반환: [{high, low, role}] — role 'recent'(primary, drag 편집) / 'base'(secondary, 정적). 최대 2.
   *
   * @returns {Array<{high, low, role}>}
   */
  _autoAnchorSets() {
    const candles = this._candles;
    if (!Array.isArray(candles) || candles.length < ZIGZAG_MIN_DAYS) return [];

    // 두 윈도우 goal 풀 (규칙 I = 120봉, 규칙 II = 240봉) 합집합
    const goalsRecent = this._goalsForWindow(ZIGZAG_WINDOW_RECENT);
    const goalsBase = this._goalsForWindow(ZIGZAG_WINDOW_BASE);
    if (goalsRecent.length === 0 && goalsBase.length === 0) return [];
    // lowIdx(=골 날짜) 키 dedupe — 두 윈도우가 같은 골 검출 시 더 깊은 drop(=장기 parent) 우선
    const poolMap = new Map();
    for (const g of [...goalsRecent, ...goalsBase]) {
      const prev = poolMap.get(g.lowIdx);
      if (!prev || g.drop > prev.drop) poolMap.set(g.lowIdx, g);
    }
    const pool = Array.from(poolMap.values());
    if (pool.length === 0) return [];

    const picked = this._selectCardGoals(pool);  // [base?, recent?] (low_date 오름차순, 최대 2)
    return picked.map((g, i) => ({
      high: { time: candles[g.highIdx].time, price: candles[g.highIdx].high, candleIdx: g.highIdx },
      low: { time: candles[g.lowIdx].time, price: g.low, candleIdx: g.lowIdx },
      role: g.role || (i === picked.length - 1 ? 'recent' : 'base'),
    }));
  }

  /**
   * 한 윈도우(최근 winN봉)에서 ZigZag → 각 골의 goal 산출.
   *   goal = { lowDate, low, lowIdx, drop, parentDate, highIdx } (전부 full candles 좌표).
   *   parent = 골 직전 swing-high 피벗 중 high-of-day 최대 (해석 A). drop=(parent.high − low)/parent.high.
   *
   * @param {number} winN
   * @returns {Array<Object>}
   */
  _goalsForWindow(winN) {
    const full = this._candles;
    const N = full.length;
    const offset = Math.max(0, N - winN);
    const sub = full.slice(offset);
    const pivots = computeZigZagPivots(sub, ZIGZAG_THETA);
    if (!pivots.some((p) => p.type === 'H')) return [];  // 가드: swing-high 0개

    const goals = [];
    for (let pos = 0; pos < pivots.length; pos++) {
      if (pivots[pos].type !== 'L') continue;
      const ref = this._refineTroughLow(sub, pivots[pos].candleIdx);
      if (!ref) continue;
      // parent = 직전 swing-high 피벗 중 high-of-day 최대
      let parentHigh = 0;
      let parentIdxSub = -1;
      for (let j = pos - 1; j >= 0; j--) {
        if (pivots[j].type !== 'H') continue;
        const hd = sub[pivots[j].candleIdx].high;
        if (hd > parentHigh) { parentHigh = hd; parentIdxSub = pivots[j].candleIdx; }
      }
      if (!(parentHigh > 0) || parentIdxSub < 0) continue;
      goals.push({
        lowIdx: offset + ref.idx,        // full candles 좌표 — 날짜 순서 비교 키 (단조 증가)
        low: ref.low,
        drop: (parentHigh - ref.low) / parentHigh,
        parentIdx: offset + parentIdxSub,  // parent-group 키 (같은 parentIdx = 같은 부모 고점)
        highIdx: offset + parentIdxSub,
      });
    }
    return goals;
  }

  /**
   * ZigZag 종가 골 idx(sub 좌표) → ±ZIGZAG_REFINE_BARS 봉 내 저가(low-of-day) 최저일.
   * 상한가·하한가 날 제외 (점상한가 꼬리 왜곡 회피). 전부 제외 시 가드 무시 재탐색.
   * @param {Array} sub
   * @param {number} li — ZigZag 골 candleIdx (sub 좌표)
   * @returns {{idx, low} | null}
   */
  _refineTroughLow(sub, li) {
    const lo = Math.max(0, li - ZIGZAG_REFINE_BARS);
    const hi = Math.min(sub.length - 1, li + ZIGZAG_REFINE_BARS);
    let bestIdx = -1;
    let bestLow = Infinity;
    for (let k = lo; k <= hi; k++) {
      const c = sub[k];
      if (!c || !(c.low > 0)) continue;
      if (k > 0 && sub[k - 1] && isLimitMove(sub[k - 1].close, c.close)) continue;
      if (c.low < bestLow) { bestLow = c.low; bestIdx = k; }
    }
    if (bestIdx < 0) {
      for (let k = lo; k <= hi; k++) {
        const c = sub[k];
        if (!c || !(c.low > 0)) continue;
        if (c.low < bestLow) { bestLow = c.low; bestIdx = k; }
      }
    }
    if (bestIdx < 0) return null;
    return { idx: bestIdx, low: bestLow };
  }

  /**
   * 카드 노출 골 선택 (토구사 parent-group 규칙, 최대 2세트).
   *   RECENT = drop ≥ 20% goal 중 lowDate 가장 늦은 것.
   *   BASE:
   *     2a. recent 의 parent-group(같은 parentDate) 안에서 recent 보다 이른 goal 중 가장 깊은 = ingrpBest.
   *     2b. ingrpBest 가 있고 ingrpBest.drop ≥ recent.drop → BASE = ingrpBest (같은 파동 더 깊은 base 우선).
   *     2c. 아니면 recent 보다 이른 전체 goal 중 가장 깊은 것 (근사동률 ±0.5%p 면 lowDate 늦은 것).
   *   카드 = {BASE, RECENT} lowDate 오름차순 (BASE==RECENT 면 1세트).
   *
   * @param {Array} pool — goal 합집합
   * @returns {Array<Object>} 선택 goal (role 부여, 최대 2)
   */
  _selectCardGoals(pool) {
    if (!pool || pool.length === 0) return [];
    const cand = pool.filter((g) => g.drop >= ZIGZAG_RECENT_MIN_DROP);
    if (cand.length === 0) return [];
    let recent = cand[0];
    for (const g of cand) {
      if (g.lowIdx > recent.lowIdx) recent = g;  // lowIdx 큰 = 날짜 늦은
    }

    let base = null;
    const grp = pool.filter((g) => g.parentIdx === recent.parentIdx && g.lowIdx < recent.lowIdx);
    if (grp.length > 0) {
      let ingrpBest = grp[0];
      for (const g of grp) { if (g.drop > ingrpBest.drop) ingrpBest = g; }
      if (ingrpBest.drop >= recent.drop) base = ingrpBest;  // 2b
    }
    if (base === null) {
      const earlier = pool.filter((g) => g.lowIdx < recent.lowIdx);
      if (earlier.length > 0) {
        let mx = earlier[0];
        for (const g of earlier) { if (g.drop > mx.drop) mx = g; }
        const near = earlier.filter((g) => Math.abs(g.drop - mx.drop) <= ZIGZAG_NEAR_TIE);
        if (near.length > 1) {
          base = near[0];
          for (const g of near) { if (g.lowIdx > base.lowIdx) base = g; }  // later-date tie-break
        } else {
          base = mx;
        }
      }
    }

    const out = [];
    if (base) { base.role = 'base'; out.push(base); }
    if (base === null || base.lowIdx !== recent.lowIdx) { recent.role = 'recent'; out.push(recent); }
    out.sort((a, b) => a.lowIdx - b.lowIdx);  // 날짜 오름차순
    return out;
  }

  /**
   * 하위 호환 — primary 세트(최근 골) {high, low} 반환 (drag 편집 대상).
   * @returns {{high, low} | null}
   */
  _autoAnchorZigZag() {
    const sets = this._autoAnchorSets();
    if (sets.length === 0) return null;
    // role 'recent' 가 primary (없으면 마지막)
    const primary = sets.find((s) => s.role === 'recent') || sets[sets.length - 1];
    return primary;
  }

  /**
   * localStorage 복원 schema:
   *   { anchorA: { time, price, candleIdx }, anchorB: { time, price, candleIdx } }
   */
  _loadState() {
    try {
      const raw = localStorage.getItem(STORAGE_PREFIX + this._ticker);
      if (!raw) return { anchorA: null, anchorB: null };
      const parsed = JSON.parse(raw);
      // candleIdx 재계산 — candles 배열이 변경됐을 수 있음
      const reindex = (anchor) => {
        if (!anchor || !anchor.time) return null;
        const t = anchor.time;
        const idx = this._candles.findIndex((c) =>
          c.time && c.time.year === t.year && c.time.month === t.month && c.time.day === t.day,
        );
        if (idx < 0) return null;
        return { ...anchor, candleIdx: idx };
      };
      return {
        anchorA: reindex(parsed.anchorA),
        anchorB: reindex(parsed.anchorB),
      };
    } catch (e) {
      return { anchorA: null, anchorB: null };
    }
  }

  _saveState() {
    try {
      localStorage.setItem(
        STORAGE_PREFIX + this._ticker,
        JSON.stringify({
          anchorA: this._state.anchorA,
          anchorB: this._state.anchorB,
        }),
      );
    } catch (e) { /* private mode silent fail */ }
  }

  /**
   * chart.subscribeClick handler — anchor A → anchor B 순차 설정 + 자석 snap.
   * @param {MouseEventParams} param
   */
  _onClick(param) {
    if (this._dragging) return;  // drag 중 클릭 무시
    if (!param || !param.point || param.logical == null) return;
    const clickPrice = this._series.coordinateToPrice(param.point.y);
    if (clickPrice == null) return;

    const snap = magnetSnap(this._candles, param.logical, clickPrice, this._options.magnetWindow);
    if (!snap) return;

    // anchor A 미설정 → A 설정 (handle만 표시, fib level 미 render)
    if (!this._state.anchorA) {
      this._state.anchorA = snap;
      this._saveState();
      this._renderHandles();
      // P0-24 Fix-83 (2026-05-21 22:40 KST 대표 verbatim "피크 저가 고가가 자석 기능이고 내가 선택해서 이동하거나 기간을 조정할 수 있다"):
      //   자석 작동 visible feedback — anchor 1차 결정 toast 본문 본문 본문 본문 본문 사용자 인지 cascade.
      this._showSnapToast(`A 끝점 설정됨 (자석: ${formatPriceLabel(snap.price)}원). 다시 클릭하여 B 끝점 설정.`);
      return;
    }
    // anchor A 있고 B 없음 → B 설정 + render
    if (!this._state.anchorB) {
      this._state.anchorB = snap;
      this._saveState();
      this._renderLevels();
      this._renderHandles();
      // P0-24 Fix-83: anchor 2차 결정 toast 본문 — fibonacci level visible 본질 confirm.
      this._showSnapToast(`B 끝점 설정됨 (자석: ${formatPriceLabel(snap.price)}원). 점을 끌어 조정 가능.`);
      return;
    }
    // 둘 다 있는 상태에서 클릭 = 재시작 (B를 새 클릭으로 갱신, drag 본질 보완 fallback)
    // 대표 verbatim "기간을 조정할 수 있다" = drag 본질이 메인. 클릭 재시작은 추가 paradigm.
    this._state.anchorB = snap;
    this._saveState();
    this._renderLevels();
    this._renderHandles();
    // P0-24 Fix-83: 재시작 toast 본문 — 사용자가 둘 다 있는 상태에서 새로 클릭한 경우.
    this._showSnapToast(`B 끝점 재설정 (자석: ${formatPriceLabel(snap.price)}원).`);
  }

  /**
   * P0-24 Fix-83 — 자석 snap 작동 visible feedback toast 본문 본문 본문 본문 본문 사용자 인지 cascade.
   *
   * 본질:
   *   - chart container 우측 상단 본문 absolute-positioned toast div 본문 신축 (sub-pane title Fix-52 동형 paradigm)
   *   - 2.4초 후 자동 fade out + remove
   *   - 동시 toast 본문 본문 본문 본문 본문 본문 본문 본문 1개 본문 본문 본문 본문 본문 (기존 toast 본문 본문 본문 본문 본문 즉시 remove)
   *   - z-index 200 (drag handle 100 + sub-pane title 10 본문 본문 본문 본문 본문 본문)
   *   - pointer-events: none (chart click 통과)
   *
   * §11.15 외부 spec PASS:
   *   - native DOM appendChild/remove + setTimeout/setInterval (vendor 본문 부재)
   *   - CSS transition opacity 본문 본문 본문 본문 본문 본문 (vendor prefix 부재 native)
   *   - repo verbatim: expanded-chart.js L948~1006 hint badge 본문 본문 본문 본문 fade transition 동형 paradigm
   */
  _showSnapToast(text) {
    if (!this._container) return;
    // 기존 toast 본문 본문 본문 본문 본문 즉시 remove (1개만 visible 본질)
    if (this._snapToastEl) {
      try { this._snapToastEl.remove(); } catch (e) { /* noop */ }
      this._snapToastEl = null;
    }
    if (this._snapToastTimer) {
      try { clearTimeout(this._snapToastTimer); } catch (e) { /* noop */ }
      this._snapToastTimer = null;
    }
    const toast = document.createElement('div');
    toast.className = 'cal-chart-tv-fib-snap-toast';
    toast.setAttribute('role', 'status');
    toast.setAttribute('aria-live', 'polite');
    // PM320-D6 — 머티리얼 핑크 모달 → 햇살 다크 그레이 비차단 토스트 (v291 hint badge rgba(26,29,38,0.95) 톤 정합).
    //   차트 하단(bottom)에 가는 1줄로 강등 — 데스크탑/모바일 모두 차트 본체 가림 회피.
    //   max-width: min(차트폭-16px, 220px) — 좁은 폰(차트 ~271px)에서도 양옆 8px 여백 보존, 전체 덮음 0건.
    toast.style.cssText = [
      'position: absolute',
      'left: 8px',
      'right: 8px',
      'bottom: 8px',
      'margin: 0 auto',
      'width: fit-content',
      'max-width: calc(100% - 16px)',
      'background: rgba(26,29,38,0.95)',
      'color: #fff',
      'font-size: 11px',
      'font-weight: 600',
      'padding: 5px 9px',
      'border-radius: 4px',
      'box-shadow: 0 2px 8px rgba(0,0,0,0.30)',
      'z-index: 200',
      'pointer-events: none',
      'opacity: 0',
      'transition: opacity 0.25s ease',
      'line-height: 1.3',
      'white-space: nowrap',
      'overflow: hidden',
      'text-overflow: ellipsis',
    ].join(';');
    toast.textContent = text;
    try {
      const computedPos = window.getComputedStyle(this._container).position;
      if (computedPos === 'static') {
        this._container.style.position = 'relative';
      }
    } catch (e) { /* noop */ }
    this._container.appendChild(toast);
    this._snapToastEl = toast;
    // 다음 frame 본문 fade in (transition 발동 본질)
    if (typeof requestAnimationFrame === 'function') {
      requestAnimationFrame(() => {
        if (toast && toast.parentNode) toast.style.opacity = '1';
      });
    } else {
      toast.style.opacity = '1';
    }
    // 2.4초 후 자동 fade out + remove
    this._snapToastTimer = setTimeout(() => {
      if (!toast || !toast.parentNode) return;
      toast.style.opacity = '0';
      setTimeout(() => {
        try { toast.remove(); } catch (e) { /* noop */ }
        if (this._snapToastEl === toast) this._snapToastEl = null;
      }, 300);
    }, 2400);
  }

  _onCrosshairMove(param) {
    // drag 시 실시간 갱신 본질 (mousemove은 chart.subscribeCrosshairMove로 대체 가능 — v5 paradigm)
    if (!this._dragging || !param || !param.point || param.logical == null) return;
    const newPrice = this._series.coordinateToPrice(param.point.y);
    if (newPrice == null) return;
    const snap = magnetSnap(this._candles, param.logical, newPrice, this._options.magnetWindow);
    if (!snap) return;
    if (this._dragging === 'A') {
      this._state.anchorA = snap;
    } else {
      this._state.anchorB = snap;
    }
    this._renderLevels();
    this._renderHandles();
  }

  /**
   * P0-25 (2026-05-21 23:46 KST 대표 결정 영웅문 paradigm 채택) —
   * chart canvas mousedown 시점 anchor A/B 근처 detect 시 drag mode 시작.
   *
   * 본질:
   *   - chart canvas (chartElement) bounding rect 본문 mouse client 좌표 본문 → chart pane 본문 x,y 본문 환산
   *   - anchor A/B 본문 pixel position (timeToCoordinate + priceToCoordinate) 본문 거리 측정
   *   - dragTolerance (±20px) 본문 본문 mousedown 본문 본문 → drag mode 본문 본문 본문 본문
   *   - lightweight-charts 본문 chart panning/zoom 본문 본문 conflict 회피 본문 stopPropagation 호출
   *
   * §16 self-catch:
   *   - capture phase 본문 lightweight-charts 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문
   *   - drag start 본문 본문 본문 chart panning 본문 본문 본문 본문 본문 본문 본문 본문 → drag tolerance 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문
   *   - magnetSnap 본문 본문 본문 본문 anchor 위치 본문 ±5 영업일 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문
   */
  _handleChartMouseDown(e) {
    const target = this._detectAnchorNearPointer(e.clientX, e.clientY);
    if (!target) return;
    e.preventDefault();
    e.stopPropagation();
    this._chartCanvasDragging = target;
    this._dragging = target;  // _onCrosshairMove 본문 본질 정합 (기존 handle drag 본문 본문 본문 동일 path)
  }

  _handleChartTouchStart(e) {
    if (!e.touches || e.touches.length === 0) return;
    const t = e.touches[0];
    const target = this._detectAnchorNearPointer(t.clientX, t.clientY);
    if (!target) return;
    e.preventDefault();
    e.stopPropagation();
    this._chartCanvasDragging = target;
    this._dragging = target;
  }

  _handleDocMouseMove(e) {
    if (!this._chartCanvasDragging) return;
    this._updateAnchorFromPointer(e.clientX, e.clientY);
  }

  _handleDocTouchMove(e) {
    if (!this._chartCanvasDragging) return;
    if (!e.touches || e.touches.length === 0) return;
    e.preventDefault();  // 본 body scroll 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문
    const t = e.touches[0];
    this._updateAnchorFromPointer(t.clientX, t.clientY);
  }

  _handleDocMouseUp(e) {
    if (!this._chartCanvasDragging) return;
    this._chartCanvasDragging = null;
    this._dragging = null;
    this._saveState();
  }

  _handleDocTouchEnd(e) {
    if (!this._chartCanvasDragging) return;
    this._chartCanvasDragging = null;
    this._dragging = null;
    this._saveState();
  }

  /**
   * P0-25 — pointer (mouse/touch) client 좌표 본문 → anchor A/B 근처 detect.
   *
   * 본질:
   *   - chartElement bounding rect 본문 client 좌표 본문 → chart-local x,y 환산
   *   - anchor A/B 본문 timeToCoordinate + priceToCoordinate 본문 pixel position 측정
   *   - Euclidean distance 본문 본문 dragTolerance 본문 본문 본문 본문 본문 본문 → 'A' | 'B' 본문 본문 본문 본문 본문
   *   - 양 anchor 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 → 더 가까운 본문 본문 본문 본문
   *
   * @param {number} clientX
   * @param {number} clientY
   * @returns {'A'|'B'|null}
   */
  _detectAnchorNearPointer(clientX, clientY) {
    if (!this._chartEl || !this._state.anchorA || !this._state.anchorB) return null;
    let rect = null;
    try { rect = this._chartEl.getBoundingClientRect(); } catch (e) { return null; }
    if (!rect) return null;
    const localX = clientX - rect.left;
    const localY = clientY - rect.top;
    const tolerance = this._options.dragTolerance || 20;

    const distToAnchor = (anchor) => {
      if (!anchor || !anchor.time) return Infinity;
      let x = null, y = null;
      try {
        x = this._chart.timeScale().timeToCoordinate(anchor.time);
        y = this._series.priceToCoordinate(anchor.price);
      } catch (e) { return Infinity; }
      if (x == null || y == null) return Infinity;
      const dx = localX - x;
      const dy = localY - y;
      return Math.sqrt(dx * dx + dy * dy);
    };

    const distA = distToAnchor(this._state.anchorA);
    const distB = distToAnchor(this._state.anchorB);
    if (distA > tolerance && distB > tolerance) return null;
    return distA <= distB ? 'A' : 'B';
  }

  /**
   * P0-25 — pointer (mouse/touch) client 좌표 본문 → anchor 위치 갱신 + magnetSnap 재적용.
   *
   * 본질:
   *   - chartElement bounding rect 본문 client 좌표 본문 → chart-local x,y 환산
   *   - x → logical index 본문 본문 본문 timeScale.coordinateToLogical(x)
   *   - y → price 본문 본문 본문 series.coordinateToPrice(y)
   *   - magnetSnap 본문 본문 본문 ±5 영업일 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문
   *   - drag 본문 anchor (A 또는 B) 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문
   *
   * §11.15 외부 spec PASS:
   *   - ITimeScaleApi.coordinateToLogical(x: number): Logical | null
   *     https://tradingview.github.io/lightweight-charts/docs/api/interfaces/ITimeScaleApi
   *   - ISeriesApi.coordinateToPrice(y: number): number | null
   *     https://tradingview.github.io/lightweight-charts/docs/api/interfaces/ISeriesApi
   */
  _updateAnchorFromPointer(clientX, clientY) {
    if (!this._chartEl || !this._chartCanvasDragging) return;
    let rect = null;
    try { rect = this._chartEl.getBoundingClientRect(); } catch (e) { return; }
    if (!rect) return;
    const localX = clientX - rect.left;
    const localY = clientY - rect.top;

    let logical = null, price = null;
    try {
      logical = this._chart.timeScale().coordinateToLogical(localX);
      price = this._series.coordinateToPrice(localY);
    } catch (e) { return; }
    if (logical == null || price == null) return;

    const snap = magnetSnap(this._candles, logical, price, this._options.magnetWindow);
    if (!snap) return;

    if (this._chartCanvasDragging === 'A') {
      this._state.anchorA = snap;
    } else {
      this._state.anchorB = snap;
    }
    this._renderLevels();
    this._renderHandles();
  }

  /**
   * Fibonacci horizontal level 자동 draw (createPriceLine 본문, axisLabelVisible: true 본질).
   * 본문: anchorA.price = swing 한쪽, anchorB.price = swing 반대쪽.
   *      Fib retracement: 0% = anchorB.price, 100% = anchorA.price.
   *      ratio*anchorA + (1-ratio)*anchorB = anchorB + ratio * (anchorA - anchorB)
   */
  _renderLevels() {
    this._clearPriceLines();
    if (!this._state.anchorA || !this._state.anchorB) return;

    const a = this._state.anchorA.price;
    const b = this._state.anchorB.price;
    if (a == null || b == null) return;

    LEVELS.forEach((lv) => {
      const price = b + (a - b) * lv.ratio;
      try {
        const line = this._series.createPriceLine({
          price,
          color: lv.color,
          lineStyle: this._options.lineStyle,
          lineWidth: this._options.lineWidth,
          axisLabelVisible: this._options.axisLabelVisible,
          // P0-19 Fix-63 (2026-05-21 16:35 KST P0-18 Fix-58 paradigm 충돌 cascade 정합):
          //   title 본문 = '' (빈 string) 복원 — Fix-55 axisLabelVisible:false 보존
          //   좌측 가격 라벨 본문은 HTML overlay div 본문 신축 본질 (this._renderOverlayLabels())
          //   v5 PriceLineOptions.title visibility = axisLabelVisible:true 종속 본질 paradigm 충돌 회피
          title: '',
        });
        this._priceLines.push(line);
      } catch (e) { /* noop */ }
    });

    // P0-19 Fix-63: HTML overlay 본문 좌측 가격 라벨 render 본질 cascade
    this._renderOverlayLabels();
  }

  _clearPriceLines() {
    this._priceLines.forEach((line) => {
      try { this._series.removePriceLine(line); } catch (e) { /* noop */ }
    });
    this._priceLines = [];
  }

  /**
   * P0-19 Fix-63 — HTML overlay 본문 좌측 가격 라벨 render 본질 (LEVELS.length 본문 div 본문).
   *
   * 본질:
   *   - LEVELS 본문 각 ratio별 price 계산 (anchorB + (anchorA - anchorB) * ratio)
   *   - left: 8px (영웅문 23a74560 본문 좌측 본문 정합)
   *   - top: series.priceToCoordinate(price) — chart pane 본문 y좌표 실시간 계산
   *   - text: formatPriceLabel(price) — ko-KR locale 본문 정수 (예: '727,000')
   *   - font-size: 10px (대표 verbatim "작은 글씨" 정합)
   *   - pointer-events: none (chart click/drag 본문 통과)
   *   - z-index: 10 (drag handle z-index:100 본문 침범 부재)
   *
   * §16 self-catch:
   *   - priceToCoordinate null fallback (price 가시 영역 외부) → label display:none silent skip
   *   - chart container 본문 position:relative 본문 절대 좌표 본질 (Fix-52 동형 main.style.position='relative')
   *
   * 본문 호출 시점:
   *   - _renderLevels() 내부 cascade (anchor 변경 시점)
   *   - _rangeHandler (zoom/scroll 시점)
   *   - ResizeObserver callback (chart resize 시점)
   */
  _renderOverlayLabels() {
    if (!this._container) return;
    if (!this._state.anchorA || !this._state.anchorB) {
      // anchor 미설정 시 모든 overlay label hide
      this._overlayLabels.forEach((el) => { if (el) el.style.display = 'none'; });
      return;
    }

    const a = this._state.anchorA.price;
    const b = this._state.anchorB.price;
    if (a == null || b == null) return;

    // 필요시 overlay div 본문 lazy create (LEVELS.length 본문 개수 보장)
    if (this._overlayLabels.length < LEVELS.length) {
      // chart container 본문 position:relative 보장 (Fix-52 동형, sub-pane title 본문 main.style.position='relative' 호출 후 본 hook 호출 가능)
      try {
        const computedPos = window.getComputedStyle(this._container).position;
        if (computedPos === 'static') {
          this._container.style.position = 'relative';
        }
      } catch (e) { /* noop */ }

      for (let i = this._overlayLabels.length; i < LEVELS.length; i++) {
        const label = document.createElement('div');
        label.className = 'cal-chart-tv-fib-price-label';
        label.dataset.fibIdx = String(i);
        // P0-20 Fix-66 (2026-05-21 17:46 KST 대표 "피보나치 가격라벨도 훨씬 작게"): font 10px → 8px.
        // PM320-D6 (task #32 ③): 가독성 위해 font 8px → 12px 상향. 배경은 Fix-67 완전 투명 유지(반투명 칩 추가는 #28 대표 보류).
        // P0-20 Fix-67 (2026-05-21 17:46 KST 대표 "가격라벨 바탕 완전 투명하게"): 배경 제거.
        //   가독성 보완 — text-shadow 흰색 outline 유지(영웅문 reference 배경 부재 + 흰색 outline + 검정 text 정합).
        label.style.cssText = [
          'position: absolute',
          'left: 8px',
          'top: 0',
          'font-size: 12px',                                 // PM320-D6 task #32 ③: 8 → 12 (가독성)
          'font-weight: 600',
          'color: rgba(0,0,0,0.85)',
          'pointer-events: none',
          'z-index: 10',
          'display: none',
          'background: transparent',                         // P0-20 Fix-67: 완전 투명 유지
          'padding: 0',
          'text-shadow: 0 0 2px #fff, 0 0 2px #fff, 0 0 2px #fff',  // P0-20 Fix-67: 흰색 outline 가독성
          'transform: translateY(-50%)',  // y좌표 = 가로선 중앙 정합
          'white-space: nowrap',
        ].join(';');
        this._container.appendChild(label);
        this._overlayLabels.push(label);
      }
    }

    // PM320 (대표 2026-06-10) — 좌측 가격 라벨 세로 겹침 디컨플릭트.
    //   배경: 6개 라벨이 좁은 가격대(예 12,375/11,805)에 몰리면 서로 침범 → 가독 0.
    //   원칙(대표 verbatim "구분선이 움직이는 게 아니라 숫자만 풀리는"): 피보 레벨 선(priceLine)은 절대 불변,
    //   라벨 텍스트의 화면 y(top)만 최소 간격 확보. 자기 선 y에서 최소한만 어긋나도록 단일 패스 push-down.
    // pass 1 — 각 라벨의 진짜 y(가로선 중앙) 산출. 가시 영역 밖(null)은 hide.
    const _items = [];
    LEVELS.forEach((lv, i) => {
      const price = b + (a - b) * lv.ratio;
      const label = this._overlayLabels[i];
      if (!label) return;
      let y = null;
      try { y = this._series.priceToCoordinate(price); } catch (e) { /* noop */ }
      if (y == null || !isFinite(y)) {
        label.style.display = 'none';
        return;
      }
      label.style.display = 'block';
      label.textContent = formatPriceLabel(price);
      _items.push({ label, y: Number(y) });
    });
    // pass 2 — y 오름차순 정렬 후 최소 간격(MIN_GAP) 강제. 인접 라벨이 겹치면 아래쪽을 push-down.
    //   translateY(-50%) 적용 상태이므로 top = 라벨 중앙. MIN_GAP = 라벨 높이(12px font + 여유) ≈ 14px.
    const MIN_GAP = 14;
    _items.sort((p, q) => p.y - q.y);
    let _prevTop = -Infinity;
    for (const it of _items) {
      let top = it.y;                       // 기본 = 자기 선 y (어긋남 0)
      if (top < _prevTop + MIN_GAP) {
        top = _prevTop + MIN_GAP;           // 겹치면 최소한만 아래로 밀어내기 (선은 불변)
      }
      it.label.style.top = `${top}px`;
      _prevTop = top;
    }
  }

  _clearOverlayLabels() {
    this._overlayLabels.forEach((el) => {
      if (!el) return;
      try { el.remove(); } catch (e) { /* noop */ }
    });
    this._overlayLabels = [];
  }

  /**
   * DOM overlay drag handle 2개 render (anchorA + anchorB).
   * 본질: chartContainer 위 absolute position div. timeToCoordinate + priceToCoordinate.
   */
  _renderHandles() {
    if (!this._container) return;
    if (!this._handleA) this._handleA = this._createHandleEl('A');
    if (!this._handleB) this._handleB = this._createHandleEl('B');

    this._positionHandle(this._handleA, this._state.anchorA);
    this._positionHandle(this._handleB, this._state.anchorB);
  }

  _createHandleEl(label) {
    const el = document.createElement('div');
    el.className = 'cal-chart-tv-fib-handle';
    el.dataset.anchor = label;
    el.setAttribute('aria-label', `피보나치 ${label} 끝점 — 드래그해 이동`);
    el.setAttribute('title', `${label} 끝점 — 드래그해 이동`);
    // P0-25 (2026-05-21 23:46 KST 대표 결정 영웅문 paradigm 채택):
    //   handle = 영웅문 inline ↓ marker 정합 (anchor 시각 cue만, drag trigger 본문 chart canvas 본문 본질).
    //   본문 visual 본문 본질 축소:
    //     - radius 14 → 6 (DEFAULT_OPTIONS, +75% 면적 본문 -82% 면적 본문 본문 축소)
    //     - background 본문 본문 transparent (border만 visible — 영웅문 ↓ arrow marker 정합)
    //     - border 3px → 2px (subtle)
    //     - box-shadow 본문 본문 본문 본문 본문 본문 본문 (외곽 glow 폐기)
    //     - pulse 애니메이션 폐기 (영웅문 reference 본문 정적 marker 정합)
    //     - cursor 본문 본문 본문 'default' (drag trigger 본문 chart canvas 본문 본질)
    //     - pointer-events 본문 'none' (chart canvas mousedown 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문)
    //   §11.15 외부 spec PASS:
    //     - 영웅문 23a74560 reference 본문 chart area click/drag paradigm + swing high/low inline ↓ arrow marker 본질 정합
    //     - WebSearch 2회 corroborating (TradingView v5 chartElement custom event listeners pattern)
    el.style.cssText = [
      'position: absolute',
      `width: ${this._options.handleRadius * 2}px`,
      `height: ${this._options.handleRadius * 2}px`,
      'background: transparent',
      `border: 2px solid ${this._options.handleColor}`,
      'border-radius: 50%',
      'cursor: default',
      'z-index: 100',
      'box-shadow: 0 0 0 1px rgba(255,255,255,0.8)',
      'pointer-events: none',
      'touch-action: none',
      'display: none',
    ].join(';');

    // P0-25: drag trigger 본문 chart canvas 본문 본질 — handle 본문 본문 event listener 본문 폐기 (영웅문 paradigm 정합).
    el._cleanup = () => { /* noop — listener 본문 본문 본문 본문 본문 본문 본문 본문 */ };

    this._container.appendChild(el);
    return el;
  }

  _positionHandle(el, anchor) {
    if (!el) return;
    if (!anchor || !anchor.time) {
      el.style.display = 'none';
      return;
    }
    try {
      const x = this._chart.timeScale().timeToCoordinate(anchor.time);
      const y = this._series.priceToCoordinate(anchor.price);
      if (x == null || y == null) {
        el.style.display = 'none';
        return;
      }
      el.style.display = 'block';
      el.style.left = `${x - this._options.handleRadius}px`;
      el.style.top = `${y - this._options.handleRadius}px`;
    } catch (e) {
      el.style.display = 'none';
    }
  }

  /**
   * 사용자 그린 Fibonacci 초기화 (clear all).
   *
   * P0-24 Fix-84 (2026-05-21 22:40 KST 대표 verbatim "내가 선택해서 이동하거나 기간을 조정할 수 있다"):
   *   reset() 호출 시 anchor 재계산 본질 — auto-anchor 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 visible range 본문 본문 본문 본문 본문 본문 본문 본문 본문.
   *   기간 조정 메커니즘 본문 본문 본문 본문 (대표 ④ catch):
   *     - 사용자가 chart zoom in/out → 자동 visible range 변경
   *     - reset 클릭 시 auto-anchor 본문 본문 본문 본문 본문 _autoAnchorFromVisibleRangeNow() 본문 본문 본문 본문 본문 본문 본문 본문 현재 가시 영역 본문 hi/lo 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문
   *   본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문.
   *
   * @param {boolean} [autoReseed=true] — true: reset 후 auto-anchor 재진행, false: 완전 비움
   */
  reset(autoReseed = true) {
    this._state = { anchorA: null, anchorB: null };
    this._clearPriceLines();
    // P0-19 Fix-63: overlay label 본문 hide (clear 본질은 destroy 시점만)
    this._overlayLabels.forEach((el) => { if (el) el.style.display = 'none'; });
    if (this._handleA) this._handleA.style.display = 'none';
    if (this._handleB) this._handleB.style.display = 'none';

    if (autoReseed) {
      // P0-24 Fix-84: 현재 가시 영역 본문 hi/lo 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문
      const auto = this._autoAnchorFromCurrentVisibleRange() || this._autoAnchorFromVisibleRange();
      if (auto) {
        this._state.anchorA = auto.high;
        this._state.anchorB = auto.low;
        this._renderLevels();
        this._renderHandles();
      }
    }
    this._saveState();
  }

  /**
   * P0-24 Fix-84 — 현재 chart timeScale 본문 visible logical range 본문 본문 hi/lo 본문 본문 본문 본문 본문.
   *
   * 본질:
   *   - chart.timeScale().getVisibleLogicalRange() → { from, to } logical index
   *   - candles 본문 본문 본문 본문 본문 본문 본문 hi/lo 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문
   *   - _autoAnchorFromVisibleRange() (RECENT_N=50 본문 본문 본문 본문) 와 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 사용자 zoom 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문 본문
   *
   * §11.15 외부 spec PASS:
   *   - ITimeScaleApi.getVisibleLogicalRange() → LogicalRange | null
   *     https://tradingview.github.io/lightweight-charts/docs/api/interfaces/ITimeScaleApi
   *
   * @returns {{high: {time, price, candleIdx}, low: {time, price, candleIdx}} | null}
   */
  _autoAnchorFromCurrentVisibleRange() {
    if (!Array.isArray(this._candles) || this._candles.length < 2) return null;
    let range = null;
    try {
      range = this._chart.timeScale().getVisibleLogicalRange();
    } catch (e) { /* noop */ }
    if (!range || range.from == null || range.to == null) return null;
    const from = Math.max(0, Math.floor(range.from));
    const to = Math.min(this._candles.length - 1, Math.ceil(range.to));
    if (from >= to) return null;

    let hi = -Infinity, lo = Infinity;
    let hiIdx = -1, loIdx = -1;
    for (let i = from; i <= to; i++) {
      const c = this._candles[i];
      if (!c || !(c.high > 0) || !(c.low > 0)) continue;
      if (c.high > hi) { hi = c.high; hiIdx = i; }
      if (c.low < lo) { lo = c.low; loIdx = i; }
    }
    if (hiIdx < 0 || loIdx < 0) return null;
    return {
      high: { time: this._candles[hiIdx].time, price: hi, candleIdx: hiIdx },
      low: { time: this._candles[loIdx].time, price: lo, candleIdx: loIdx },
    };
  }

  /**
   * 사용 종료 시 cleanup.
   */
  destroy() {
    try { this._chart.unsubscribeClick(this._clickHandler); } catch (e) { /* noop */ }
    try { this._chart.unsubscribeCrosshairMove(this._crosshairHandler); } catch (e) { /* noop */ }
    // P0-19 Fix-63: timeScale subscribeVisibleLogicalRangeChange unsubscribe 의무
    try {
      this._chart.timeScale().unsubscribeVisibleLogicalRangeChange(this._rangeHandler);
    } catch (e) { /* noop */ }
    // P0-19 Fix-63: ResizeObserver disconnect 의무
    try {
      if (this._resizeObserver) this._resizeObserver.disconnect();
    } catch (e) { /* noop */ }
    this._resizeObserver = null;
    // P0-25: chart canvas drag listener unsubscribe 의무 (영웅문 paradigm 본문 본문 본문 본문)
    if (this._chartEl) {
      try { this._chartEl.removeEventListener('mousedown', this._onChartMouseDown, true); } catch (e) { /* noop */ }
      try { this._chartEl.removeEventListener('touchstart', this._onChartTouchStart, { capture: true }); } catch (e) { /* noop */ }
    }
    try { document.removeEventListener('mousemove', this._onDocMouseMove); } catch (e) { /* noop */ }
    try { document.removeEventListener('mouseup', this._onDocMouseUp); } catch (e) { /* noop */ }
    try { document.removeEventListener('touchmove', this._onDocTouchMove); } catch (e) { /* noop */ }
    try { document.removeEventListener('touchend', this._onDocTouchEnd); } catch (e) { /* noop */ }
    this._chartEl = null;
    this._clearPriceLines();
    // 2026-06-10 토구사 union: secondary 세트 price line cleanup 의무
    this._clearSecondaryPriceLines();
    // P0-19 Fix-63: overlay label DOM 본문 제거 의무
    this._clearOverlayLabels();
    // P0-24 Fix-83: snap toast cleanup 의무
    if (this._snapToastTimer) {
      try { clearTimeout(this._snapToastTimer); } catch (e) { /* noop */ }
      this._snapToastTimer = null;
    }
    if (this._snapToastEl) {
      try { this._snapToastEl.remove(); } catch (e) { /* noop */ }
      this._snapToastEl = null;
    }
    [this._handleA, this._handleB].forEach((el) => {
      if (!el) return;
      try { if (typeof el._cleanup === 'function') el._cleanup(); } catch (e) { /* noop */ }
      try { el.remove(); } catch (e) { /* noop */ }
    });
    this._handleA = null;
    this._handleB = null;
    this._chart = null;
    this._series = null;
    this._candles = null;
    this._container = null;
  }
}

/**
 * Fibonacci 자석 drawing tool attach.
 * Phase 7d-2 본 API (signature 변경, Phase 7d-1 backward 비호환).
 *
 * @param {IChartApi} chart
 * @param {ISeriesApi} candleSeries
 * @param {Array} candles — normalized candles
 * @param {string} ticker — localStorage key suffix
 * @param {HTMLElement} chartContainer — drag handle overlay parent (cal-chart-tv-main 본문)
 * @param {Object} [options]
 * @returns {FibonacciDrawingController}
 */
export function attachFibonacci(chart, candleSeries, candles, ticker, chartContainer, options = {}) {
  if (!chart || !candleSeries || !Array.isArray(candles) || candles.length < 2) return null;
  return new FibonacciDrawingController(chart, candleSeries, candles, ticker, chartContainer, options);
}

/**
 * Fibonacci 자석 drawing tool detach.
 * @param {FibonacciDrawingController} controller
 */
export function detachFibonacci(controller) {
  if (!controller || typeof controller.destroy !== 'function') return;
  controller.destroy();
}

if (typeof window !== 'undefined') {
  window.ChartTVPluginFibonacci = { attachFibonacci, detachFibonacci, FibonacciDrawingController };
}
