/* ───── lib/chart-tv/plugins/rsi-overbought-cloud.js — #7 RSI 70+ 과매수 cloud fill primitive (TradingView v5) ─────
   cycle23 — RSI 14 ≥ 70 (과매수 본질) 구간 본문 RSI line + signal line 양 line 사이 cloud fill.

   본질 (대표 2026-05-22 17:15 KST verbatim):
   - "확대 차트 하단 보조지표 중 rsi 에서 rsi 14 값이 70이상을 경우에는 rsi 선과 시그널 선 사이를
      구름대처럼 채워줘. 첨부한 이미지를 참고해줘."
   - 영웅문 본문 cloud paradigm 동형 (RSI 14 line + 시그널 9 line 양 line 사이 fill 본질)
   - 70 미만 구간 본질 fill 0건 (조건부 fill, RSI ≥ 70 trigger 본문 본질만)

   §11.15 외부 spec 사전 검증 (WebSearch ≥2회 + 공식 docs + repo grep 3종):
   - https://tradingview.github.io/lightweight-charts/docs/plugins/intro
     "ISeriesPrimitive — paneViews() returns IPrimitivePaneView[] for canvas draw"
   - https://tradingview.github.io/lightweight-charts/docs/api/interfaces/ISeriesApi
     "attachPrimitive(primitive) — Attach primitive to series. Primitive draws in series' pane."
     "priceToCoordinate(price) — Converts series price to y-coordinate (current pane)"
   - repo grep verbatim:
     · js/lib/chart-tv/plugins/pink-signal.js — ISeriesPrimitive paneViews + canvas draw 본질 동형
     · js/lib/chart-tv/plugins/volume-by-decile.js L129~131 — priceToCoordinate 사용 본질 정합
     · js/lib/chart-tv/plugins/fibonacci.js L612 — priceToCoordinate y 좌표 변환 본질 동형

   §16 self-catch:
   - 본 RSI sub-pane (paneIdx=3) 본문 invertScale: true 본문 본질 (expanded-chart.js L887)
     → priceToCoordinate 본질 자동 invert 본질 (TradingView v5 본문 본질 invertScale 본문 priceToCoordinate cascade)
     → 본 plugin은 invertScale 본문 별도 handling 0건 (priceToCoordinate 본질 자체가 invert 정합)
   - 본 primitive 본문 attach 대상 = RSI line series (paneIdx=3 본문 본질, attachPrimitive 본문 본 pane 본문 draw 정합)
   - cloud 본질 = 연속 RSI ≥ 70 구간 본문 polygon (RSI line + signal line 양 line 사이 본질 fill)
   - RSI 본질 < 70 시점 본문 polygon 종결 + 다음 trigger 시점 본문 새 polygon 본질 시작

   색상 결정 본질 (대표 자율 결정 위임):
   - 영웅문 본문 cloud fill 색상 본질 첨부 이미지 본문 catch 불가 (현 라이브 51.19 + 64.66 모두 70 미만 본질)
   - 한국 시장 강세 = 빨강 본문 본질 정합 (#C53939 candle 본문 본질 동형, expanded-chart.js L689)
   - 과매수 본질 = 강세 본질 정점 본문 본질 (overbought signal 본질) → 빨강 계열 본문 본질
   - 본 cloud color = '#EF4444' (red-500) + alpha 0.20 (cluster v21 §1.68 P0-22 Fix-74 alpha 0.25 paradigm 동형, sub-pane 본문 분명한 visible 의무 본질)
   - line stroke 본문 부재 (fill 본문 본질만, 양 line 본문 visible 본질 보존 정합)
*/

const DEFAULT_OPTIONS = {
  color: '#EF4444',       // red-500 (한국 시장 강세 본질 정합, 과매수 강조)
  alpha: 0.20,            // 반투명 (sub-pane 본문 RSI line + signal line visible 본질 보존)
  threshold: 70,          // RSI 14 ≥ 70 trigger 본문 본질 (대표 verbatim)
};

/**
 * 'YYYY-MM-DD' time string 본문 → key 본질 (Map lookup 본문 본질 정합).
 * markers.js / pink-signal.js toBusinessDay 본질 동형 (string key 본질만 사용).
 */
function timeKey(t) {
  if (typeof t === 'string') return t;
  if (t && typeof t === 'object' && t.year != null) {
    return `${t.year}-${String(t.month).padStart(2, '0')}-${String(t.day).padStart(2, '0')}`;
  }
  return String(t);
}

class RSIOverboughtCloudRenderer {
  constructor(primitive) {
    this._primitive = primitive;
  }

  draw(target) {
    const p = this._primitive;
    if (!p._rsiData || p._rsiData.length === 0) return;
    if (!p._signalMap || p._signalMap.size === 0) return;
    target.useBitmapCoordinateSpace((scope) => {
      this._drawImpl(scope);
    });
  }

  _drawImpl(scope) {
    const ctx = scope.context;
    const p = this._primitive;
    const opts = p._options;
    const chart = p._chart;
    const series = p._series;
    if (!chart || !series) return;

    const timeScale = chart.timeScale();
    const threshold = opts.threshold;

    // 연속 RSI ≥ threshold 구간 본문 polygon 본질 구축
    // 각 polygon = { points: [{x, yRsi, ySig}, ...] } 본문 본질
    // 본 plugin 본문 본질: RSI line + signal line 사이 fill (RSI 본문 위/아래 무관 본문 본질 fill)
    const polygons = [];
    let currentPoly = null;

    for (let i = 0; i < p._rsiData.length; i++) {
      const rsiPt = p._rsiData[i];
      if (rsiPt == null || rsiPt.value == null) {
        if (currentPoly && currentPoly.points.length > 0) {
          polygons.push(currentPoly);
          currentPoly = null;
        }
        continue;
      }

      const sigVal = p._signalMap.get(timeKey(rsiPt.time));
      if (sigVal == null) {
        if (currentPoly && currentPoly.points.length > 0) {
          polygons.push(currentPoly);
          currentPoly = null;
        }
        continue;
      }

      // trigger 본문 본질: RSI ≥ threshold (signal 본질 무관)
      if (rsiPt.value >= threshold) {
        const xLogical = timeScale.timeToCoordinate(rsiPt.time);
        if (xLogical == null) {
          if (currentPoly && currentPoly.points.length > 0) {
            polygons.push(currentPoly);
            currentPoly = null;
          }
          continue;
        }
        let yRsi = null;
        let ySig = null;
        try {
          yRsi = series.priceToCoordinate(rsiPt.value);
          ySig = series.priceToCoordinate(sigVal);
        } catch (e) { /* noop */ }
        if (yRsi == null || ySig == null) {
          if (currentPoly && currentPoly.points.length > 0) {
            polygons.push(currentPoly);
            currentPoly = null;
          }
          continue;
        }
        if (!currentPoly) currentPoly = { points: [] };
        currentPoly.points.push({
          x: xLogical * scope.horizontalPixelRatio,
          yRsi: yRsi * scope.verticalPixelRatio,
          ySig: ySig * scope.verticalPixelRatio,
        });
      } else {
        // < threshold 시점 본문 polygon 종결
        if (currentPoly && currentPoly.points.length > 0) {
          polygons.push(currentPoly);
          currentPoly = null;
        }
      }
    }
    if (currentPoly && currentPoly.points.length > 0) {
      polygons.push(currentPoly);
    }

    if (polygons.length === 0) return;

    // 본문 polygon 본질 fill 본문 본질 draw
    ctx.globalAlpha = opts.alpha;
    ctx.fillStyle = opts.color;

    polygons.forEach((poly) => {
      const pts = poly.points;
      if (pts.length < 1) return;
      ctx.beginPath();
      // forward path 본문: RSI line 본질 (top edge 본문 본질, invertScale 본문 본질 자동)
      ctx.moveTo(pts[0].x, pts[0].yRsi);
      for (let i = 1; i < pts.length; i++) {
        ctx.lineTo(pts[i].x, pts[i].yRsi);
      }
      // reverse path 본문: signal line 본질 (bottom edge 본문 본질)
      for (let i = pts.length - 1; i >= 0; i--) {
        ctx.lineTo(pts[i].x, pts[i].ySig);
      }
      ctx.closePath();
      ctx.fill();
    });

    ctx.globalAlpha = 1.0;
  }
}

class RSIOverboughtCloudPaneView {
  constructor(primitive) {
    this._primitive = primitive;
    this._renderer = new RSIOverboughtCloudRenderer(primitive);
  }

  zOrder() {
    // 'normal' = RSI line + signal line 본문 본질 동등 layer (alpha 반투명 본질 line visible 보존)
    return 'normal';
  }

  renderer() {
    return this._renderer;
  }

  update() { /* noop — dataset은 setData 시 재계산 */ }
}

/**
 * ISeriesPrimitive 구현 — RSI 14 ≥ 70 과매수 cloud fill.
 *
 * @param {IChartApi} chart — TradingView chart instance (timeScale + priceScale 본문 본질)
 * @param {ISeriesApi} series — RSI line series (paneIdx=3, priceToCoordinate 본문 본질)
 * @param {Array<{time, value}>} rsiData — RSI 14 시계열 (computeRSI 본문 본질 산출)
 * @param {Array<{time, value}>} signalData — Signal 9 시계열 (SMA(9) of RSI 본문 본질 산출)
 * @param {Object} [options] — { color, alpha, threshold } 부분 override 본문 본질
 */
export class RSIOverboughtCloudPrimitive {
  constructor(chart, series, rsiData, signalData, options = {}) {
    this._chart = chart;
    this._series = series;
    this._options = { ...DEFAULT_OPTIONS, ...options };
    this._rsiData = Array.isArray(rsiData) ? rsiData : [];
    this._signalMap = new Map();
    if (Array.isArray(signalData)) {
      signalData.forEach((pt) => {
        if (pt && pt.value != null) {
          this._signalMap.set(timeKey(pt.time), pt.value);
        }
      });
    }
    this._paneViews = [new RSIOverboughtCloudPaneView(this)];
  }

  updateAllViews() {
    this._paneViews.forEach((v) => v.update());
  }

  paneViews() {
    return this._paneViews;
  }

  /**
   * dataset 갱신 (RSI + signal 양 본문 본질).
   */
  setData(rsiData, signalData) {
    this._rsiData = Array.isArray(rsiData) ? rsiData : [];
    this._signalMap = new Map();
    if (Array.isArray(signalData)) {
      signalData.forEach((pt) => {
        if (pt && pt.value != null) {
          this._signalMap.set(timeKey(pt.time), pt.value);
        }
      });
    }
    this.updateAllViews();
  }

  // detach 시 cleanup
  detached() {
    this._chart = null;
    this._series = null;
    this._rsiData = null;
    this._signalMap = null;
  }
}

if (typeof window !== 'undefined') {
  window.ChartTVPluginRSIOverboughtCloud = { RSIOverboughtCloudPrimitive };
}
