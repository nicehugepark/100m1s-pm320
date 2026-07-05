/* ───── components/index-card.js — 야간 미국증시 지수 카드 (Q-20260605-103 Phase 3) ─────
   DSN: DOC-20260430-DSN-001-arch-frontend §3.2 (components 매트릭스) + §3.6.9 (신설).
   IIFE + window 전역 등록 (SW cache 호환, lib/* 동일 패턴).

   renderIndexCard(idx) — 미국 지수 1종(나스닥/S&P 500/다우존스)을 종목카드 동일형 카드 1장으로 렌더.
   - 대표 2026-06-05 20:11 결정(B안) + 20:26 catch 정정: 국내 종목카드 헤더 DOM 1:1 복제.
     섹션 = 타이틀 → 미국발 뉴스 → 지수 카드 세로 나열 (국내장 뉴스요약 → 종목카드 순서 동일).
     카드 헤더 head-left 4-child (renderer.js L1419-1435 verbatim):
       .cal-trade-rank(지수=빈 슬롯) → .cal-trade-candle(당일캔들 miniCandle) → .cal-feature-sparkline → .cal-feature-candles20.
     head-right: .cal-feature-name + .cal-feature-meta(등락률 | 포인트). 하단 = 240일 레인지 바.
   - global miniCandle(당일캔들) + lib/sparkline.js buildSparkline + lib/mini-candle.js buildCandles20 재사용 (중복 구현 0건).
   - 레인지 바는 종목카드 .stock-range.v2 클래스/시각 재사용 (기존 renderCalExpandContent 무수정 — 변형 builder, ui-preservation §1).
   - 색: 한국 증시 관습 상승 #C53939 / 하락 #1958C7 / 보합 #94A3B8 (mini-candle.js 정합). role="img" + aria-label.

   idx schema (us-indices/{kstDate}.json indices[] 1개):
     { name, point, change_pct, spark[], candle:{o,h,l,c}, daily20?[],
       range_240d?:{ low, high, current?, low_date, high_date,
                     low_change_pct|low_pct, high_change_pct|high_pct } }
   - spark[]: 당일 분봉 가격 배열 (sparkline). base = candle.o (당일 시가 기준선).
   - daily20[]: 선택 — 미니 일봉 20봉 [{date,o,h,l,c}]. 부재 시 미니 일봉 영역 미렌더.
   - range_240d: 선택 — 부재/불완전 시 레인지 바만 생략, 카드 헤더는 정상 렌더 (FLR-AGT-002).
*/
(function (root) {
  'use strict';

  var UP = '#C53939', DOWN = '#1958C7', FLAT = '#94A3B8';

  function esc(s) {
    return (typeof root.escapeHtml === 'function')
      ? root.escapeHtml(s)
      : String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
      });
  }

  // 포인트 포맷: 소수 2자리 + 천단위 콤마 (지수 관습).
  function fmtPoint(v) {
    if (typeof v !== 'number' || !isFinite(v)) return '—';
    return v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function fmtPct(v) {
    if (typeof v !== 'number' || !isFinite(v)) return '';
    return (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
  }

  function fmtPct1(v) {
    if (typeof v !== 'number' || !isFinite(v)) return '';
    return (v >= 0 ? '+' : '') + v.toFixed(1) + '%';
  }

  // 240일 레인지 바 — 종목카드 .stock-range.v2 시각 재사용 (renderCalExpandContent L1346-1390 패턴).
  //   지수: '원' 대신 포인트. 중앙 라벨 (Option A, 대표 지시 2026-06-08):
  //     range-pcts 행 중앙 = '현재' 라벨 (전 카드 공통 — 종목/정규장/선물).
  //     range-dates 행 중앙 = 정규장 → 거래일 날짜(tradeDate) 유지(Q-135), 선물 → 비움(실시간이라 거래일 무의미, Q-142).
  //       정규장 개장 중(liveOpen=true, Q-20260608 정규장 장중) → 비움(선물 동형). 중앙 마커가 당일
  //       장중 현재가인데 마감 거래일 날짜(6/5) 출력 시 "장중"과 시제 모순(거짓 충실성, FLR-AGT-002).
  //       윗줄 range-pcts "현재" 라벨이 이미 시제 명시 → range-dates 빈칸이면 충분(중복 회피). 폐장/주말이면 날짜 복귀.
  //     양 끝(low_date/high_date)은 실제 일자 유지. 중앙 가격 숫자(현재가)는 유지.
  //   range_240d 부재/불완전 시 '' (카드는 헤더만 렌더).
  function buildRangeBar(r, tradeDate, isFutures, liveOpen) {
    if (!r || typeof r !== 'object') return '';
    var low = r.low, high = r.high;
    var current = (typeof r.current === 'number') ? r.current : undefined;
    if (typeof low !== 'number' || typeof high !== 'number' || low <= 0 || high <= 0 || high < low) return '';
    if (typeof current !== 'number') return '';  // 현재 위치 마커 필수
    var span = high - low;
    var markerLeft = span > 0 ? Math.max(0, Math.min(100, ((current - low) / span) * 100)) : 50;
    // 양 끝 대비 등락률 — spec(low_change_pct/high_change_pct) ∪ 기존 종목카드(low_pct/high_pct).
    var lowPct = (typeof r.low_change_pct === 'number') ? r.low_change_pct
      : (typeof r.low_pct === 'number') ? r.low_pct : null;
    var highPct = (typeof r.high_change_pct === 'number') ? r.high_change_pct
      : (typeof r.high_pct === 'number') ? r.high_pct : null;
    var isNewLow = low === current, isNewHigh = high === current;
    var lowText = isNewLow ? '신저가' : fmtPct1(lowPct);
    var highText = isNewHigh ? '신고가' : fmtPct1(highPct);
    var lowCls = isNewLow ? 'down' : ((lowPct == null ? 0 : lowPct) >= 0 ? 'up' : 'down');
    var highCls = isNewHigh ? 'up' : ((highPct == null ? 0 : highPct) <= 0 ? 'down' : 'up');
    // 종목카드 .stock-range.v2 클래스 그대로 (대표 20:12 — 폰트·시각 픽셀 동일). 지수 분기 = 텍스트만.
    return '<div class="stock-range v2">'
      + '<div class="range-bar">'
      + '<div class="range-fill" style="--low-pct:0%;--high-pct:' + markerLeft + '%"></div>'
      + '<div class="range-marker" style="left:' + markerLeft + '%"></div>'
      + '</div>'
      + '<div class="range-row range-prices">'
      + '<span class="r-low">' + esc(fmtPoint(low)) + '</span>'
      + '<span class="r-now">' + esc(fmtPoint(current)) + '</span>'
      + '<span class="r-high">' + esc(fmtPoint(high)) + '</span>'
      + '</div>'
      + '<div class="range-row range-pcts">'
      + '<span class="r-low ' + lowCls + '">' + esc(lowText) + '</span>'
      + '<span class="r-now r-now-label">현재</span>'
      + '<span class="r-high ' + highCls + '">' + esc(highText) + '</span>'
      + '</div>'
      + '<div class="range-row range-dates">'
      + '<span class="r-low">' + esc(r.low_date || '') + '</span>'
      + '<span class="r-now">' + esc((isFutures || liveOpen) ? '' : (tradeDate || '')) + '</span>'
      + '<span class="r-high">' + esc(r.high_date || '') + '</span>'
      + '</div>'
      + '</div>';
  }

  // Q-20260608-140 (A안 페어 카드) → Q-20260608-141 (선물 풀차트·뉴스공유·배지제거) — 선물 별개 카드 builder.
  //   삽입형(.idx-futures-row 오버레이) 폐기 → 정규장 카드와 동일 골격의 별개 카드 1장 (renderIndexCard 재사용).
  //   §Q-141: B안 수집기로 선물 JSON에 candle·range_240d·daily_expanded 합류됨 → 정규장과 동일 템플릿으로
  //     캔들+스파크+일봉20+240레인지+(확대 클릭) 풀 렌더 (시각 일관, 이질감 0). 데이터 부재(stale) 시 candle/range
  //     셀은 정규장과 동일하게 graceful 미렌더(빈 박스 0). 배지 [선물] 제거(제목 "NASDAQ 선물"과 중복).
  //     뉴스(§3): 선물 전용 뉴스 부재 → 호출측이 대응 정규장 지수의 news 를 sharedNews 로 주입(공유 렌더).
  //   fut = { name, point, change_pct, spark[], candle?, range_240d?, daily_expanded? }.
  //   ageMin(신선도, renderer 게이트 통과분) → "N분 전 기준". sessionOpen = 거래중/마감 도트.
  //   displayName = 페어 식별 짧은 제목(예 'NASDAQ 선물'). sharedNews = 대응 정규장 지수 news(공유).
  //   isStale(Q-20260608-145) = 30분 초과 → "지연" 배지(카드 숨김 ❌, 정직 명시). renderer 게이트 폐기 후 상시 노출.
  function renderIndexFuturesCard(fut, ageMin, tradeDate, sessionOpen, displayName, sharedNews, isStale, isPastDate) {
    if (!fut || typeof fut !== 'object') return '';
    if (typeof fut.point !== 'number' || typeof fut.change_pct !== 'number') return '';
    // 선물 데이터를 정규장 idx schema 로 사용 — name 은 displayName(제목) override 로 처리.
    //   news 는 선물 전용 부재 → 대응 정규장 지수 news 공유(§3). 색 SSOT(candle 시가대비/스파크 캔들추종/등락률 전일대비)
    //   는 renderIndexCard 내부 로직 그대로 (정규장과 100% 동일).
    var idxLike = {
      name: (typeof fut.name === 'string' && fut.name) ? fut.name : '선물',
      point: fut.point,
      change_pct: fut.change_pct,
      spark: fut.spark,
      candle: fut.candle,
      range_240d: fut.range_240d,
      daily_expanded: fut.daily_expanded,
      daily20: fut.daily20,
      news: (sharedNews && typeof sharedNews === 'object') ? sharedNews : undefined
    };
    return renderIndexCard(idxLike, null, tradeDate, {
      futVariant: true,
      ageMin: ageMin,
      sessionOpen: sessionOpen,
      isStale: !!isStale,  // Q-20260608-145 — 30분 초과 지연 배지 분기
      isPastDate: !!isPastDate,  // Q-20260610 (2회차) — 지난 날짜 카드 시 현재형 라벨·라이브 도트 강등
      displayName: (typeof displayName === 'string' && displayName) ? displayName : idxLike.name,
      // Q-20260608-143 — 선물 카드 뉴스는 *실시간* (한국 장중=미 야간 선물 거래시간대). 시제 라벨 분리.
      newsTense: 'realtime'
    });
  }

  // idx 1종 + (선택)futureInfo → 카드 HTML 문자열. 입력 부적합 시 '' (호출측에서 섹션 미렌더 판단).
  //   opts (Q-20260608-141, 선물 풀차트) = { futVariant, ageMin, sessionOpen, displayName }.
  //     futVariant=true 시 선물 카드 변종 — 정규장과 동일 골격(candle·spark·일봉20·240레인지·뉴스) +
  //     선물 식별(좌측 보더 클래스) + 거래상태 도트 + "N분 전 기준" 갱신시각 + 제목 override(displayName).
  //     candle/range_240d 부재 시 해당 셀은 정규장과 동일하게 graceful 미렌더(빈 박스 0).
  function renderIndexCard(idx, futureInfo, tradeDate, opts) {
    if (!idx || typeof idx !== 'object') return '';
    if (typeof idx.name !== 'string' || !idx.name) return '';
    var futVariant = !!(opts && opts.futVariant);
    var displayName = (opts && typeof opts.displayName === 'string' && opts.displayName) ? opts.displayName : idx.name;
    // Q-20260610 (대표 catch 6/10, 2회차) — session_open 은 *수집 시점* 스냅샷 값. 지난 날짜 카드(viewDate < 오늘)를
    //   열람하면 그날 마감 시점의 session_open=true 가 frozen 된 채 노출 → "장중"(현재형) 라벨이 "지금 장중"으로
    //   오독(6/9 카드인데 "장중", FLR-AGT-002 허위 실시간). isPastDate(호출측 renderer 가 viewDate vs KST 오늘 비교)
    //   true 면 현재형 'live'/'realtime' 라벨·라이브 거래중 도트·range-dates 빈칸을 모두 마감(close) 시제로 강등.
    //   오늘 날짜 카드(isPastDate=false)는 현행 유지(장중엔 "장중" 정당).
    var isPastDate = !!(opts && opts.isPastDate);

    var pct = (typeof idx.change_pct === 'number' && isFinite(idx.change_pct)) ? idx.change_pct : null;
    var dir = pct == null ? 'flat' : (pct > 0 ? 'up' : (pct < 0 ? 'down' : 'flat'));
    var color = dir === 'up' ? UP : (dir === 'down' ? DOWN : FLAT);
    var arrow = dir === 'up' ? '▲' : (dir === 'down' ? '▼' : '·');

    var candle = idx.candle && typeof idx.candle === 'object' ? idx.candle : null;

    // 색 정책 확정 (대표 2026-06-05 21:32 verbatim, 국내·미장 공통): "하루치 캔들은 양봉/음봉 기준이 있다.
    //   스파크라인도 캔들의 확장이다 → 스파크 색 = 캔들 방향(시가 대비). 등락률은 전일보다 올랐으면 양봉색/
    //   내렸으면 음봉색." → candleDir = (close >= open) 시가 대비 = 캔들·스파크 공통 색. 등락률(dir/color/arrow)
    //   = change_pct 전일 대비 = 텍스트 색만(현행 유지). NASDAQ(o<c 양봉 red 캔들·스파크 / change_pct<0 ▼파랑 텍스트) 정합.
    var candleDir = (candle && typeof candle.o === 'number' && typeof candle.c === 'number')
      ? (candle.c >= candle.o ? 'up' : 'down')
      : dir;

    // 당일 캔들 (.cal-trade-candle) — global miniCandle(o,h,l,c,pct). OHLC 존재 시 색 = (close > open) 시가 대비.
    var todayCandleHtml = '';
    if (candle && typeof candle.o === 'number' && typeof candle.h === 'number'
      && typeof candle.l === 'number' && typeof candle.c === 'number'
      && typeof root.miniCandle === 'function') {
      todayCandleHtml = root.miniCandle(candle.o, candle.h, candle.l, candle.c, pct == null ? 0 : pct);
    }

    var sparkHtml = '';
    if (Array.isArray(idx.spark) && idx.spark.length >= 2 && candle && typeof candle.o === 'number'
      && typeof root.buildSparkline === 'function') {
      // 스파크 = 캔들의 확장 → candleDir(시가 대비) 추종 (대표 21:32). base = candle.o (당일 시가 기준선).
      sparkHtml = root.buildSparkline(idx.spark, candle.o, candleDir);
    }
    // 미니 일봉캔들 + 확대 차트 trigger (대표 20:47 — 국내 일봉캔들 클릭 → 확장 차트 인터랙션 1:1).
    //   국내: candles20Html = <div .cal-feature-candles20 data-expand-trigger="chart" data-daily20=... role=button
    //         tabindex=0 aria-expanded=false aria-controls="chart-{code}">. 지수도 동일 emit → 기존 delegated
    //         핸들러(_openChartExpand, [data-expand-trigger="chart"] 위임)가 그대로 발화 (핸들러 0줄 수정).
    //   확대용 series: idx.daily_expanded(백엔드 1y 일봉, range_240d 산출 source) 우선, 부재 시 daily20(20봉)
    //     graceful fallback (국내 Phase 2 prototype 동작 동형). data-stock-code = 합성 code(지수=idxCode) →
    //     slot id "chart-{idxCode}" + aria-controls anchor 정합. _fetchDailybars(idxCode) 는 /data/dailybars 404
    //     → prototype(data-daily20) 유지 (graceful, 거짓 데이터 0).
    var idxCode = 'idx-' + String(displayName).replace(/[^A-Za-z0-9가-힣]/g, '').slice(0, 20);
    var hasExpanded = Array.isArray(idx.daily_expanded) && idx.daily_expanded.length >= 1;
    var expandSeries = hasExpanded ? idx.daily_expanded : (Array.isArray(idx.daily20) ? idx.daily20 : []);
    // 미니 일봉(20봉) — daily20 우선, 부재/빈배열 시 daily_expanded 마지막 20봉으로 derive.
    //   디자인 워크스루 P1 (대표 20:47): 라이브 daily20=[] → 110px 회색 빈 박스 상시 노출 = "데이터 있는데 빈 박스".
    //   root fix: daily_expanded(1y) 존재 시 그 tail 20봉으로 미니캔들 정상 렌더 (FLR-AGT-002 — 빈 박스 대신 실데이터).
    //   derive 불가(둘 다 0)일 때만 셀 미렌더 + 확대 trigger 비활성.
    var miniSeries = (Array.isArray(idx.daily20) && idx.daily20.length >= 1)
      ? idx.daily20
      : (hasExpanded ? idx.daily_expanded.slice(-20) : []);
    var miniHtml = '';
    if (miniSeries.length >= 1 && typeof root.buildCandles20 === 'function') {
      miniHtml = root.buildCandles20(miniSeries);
    }
    var candles20Cell;
    if (miniHtml && expandSeries.length >= 1) {
      // data-daily20 = 확대 차트 1차 render payload (JSON). attribute 안전 위해 " escape.
      var d20Json = esc(JSON.stringify(expandSeries));
      candles20Cell = '<div class="cal-feature-candles20" data-expand-trigger="chart" data-daily20="' + d20Json + '"'
        + ' role="button" tabindex="0" aria-label="' + esc(idx.name) + ' 일봉, 클릭 시 확대 차트"'
        + ' aria-expanded="false" aria-controls="chart-' + esc(idxCode) + '">' + miniHtml + '</div>';
    } else if (miniHtml) {
      candles20Cell = '<div class="cal-feature-candles20">' + miniHtml + '</div>';
    } else {
      candles20Cell = '<div class="cal-feature-candles20 cal-candles20-empty"></div>';
    }
    // QA P1 (대표 20:50) — range_240d.current 백엔드 미포함 → buildRangeBar L59 '' (레인지 바 전부 미렌더).
    //   fix: current 부재 시 idx.point(현재 지수 포인트 = SSOT) 주입. 원본 무변이 위해 shallow copy.
    //   백엔드 schema 무변경(프론트 단독). low/high 가 있는데 current 만 없는 정상 케이스 해결.
    var r240in = idx.range_240d;
    if (r240in && typeof r240in === 'object' && typeof r240in.current !== 'number' && typeof idx.point === 'number') {
      r240in = Object.assign({}, r240in, { current: idx.point });
    }
    // Q-20260608 정규장 장중 — 정규장(비선물) 카드가 개장 중(idx.session_open===true)이면
    //   범위바 range-dates 중앙 빈칸(마감 거래일 6/5 날짜 숨김, 시제 모순 차단). 선물은 futVariant
    //   분기로 이미 빈칸 — liveOpen 은 정규장 전용 한정(선물 Q-142 회귀 0).
    var liveOpen = !futVariant && !isPastDate && !!(idx && idx.session_open === true);
    var rangeHtml = buildRangeBar(r240in, tradeDate, futVariant, liveOpen);
    // Q-20260608-143 — 뉴스 시제 라벨. 선물 변종 = '실시간', 정규장 = '미장 마감'.
    // Q-20260608 정규장 장중 — 정규장 개장 중(idx.session_open === true)이면 'live'(장중)로 분기.
    //   백엔드 build_us_digest refresh_intraday 가 indices[].session_open 부착(개장 판정).
    //   개장 시 point/change_pct/candle/daily_expanded 가 당일 장중값으로 갱신됨 → 라벨도 정합.
    //   폐장/주말이면 session_open=false → 'close'(미장 마감) 복귀(회귀 0). 시제 혼동 차단.
    var newsTense;
    if (isPastDate) {
      // 지난 날짜 카드 — 현재형 'realtime'/'live' 모두 'close'(미장 마감)로 강등. 그날의 마감 뉴스라는 시제가 정직.
      newsTense = 'close';
    } else if (opts && opts.newsTense === 'realtime') {
      newsTense = 'realtime';
    } else if (futVariant) {
      newsTense = 'realtime';
    } else if (idx && idx.session_open === true) {
      newsTense = 'live';  // 정규장 개장 중 = 장중
    } else {
      newsTense = 'close';
    }
    var newsBodyHtml = buildCardNews(idx.news, newsTense);

    var label = displayName + ' ' + (pct == null ? '' : (dir === 'up' ? '상승' : dir === 'down' ? '하락' : '보합'))
      + ' ' + fmtPoint(idx.point) + ' (' + (pct == null ? '등락률 없음' : fmtPct(pct)) + ')';

    // 선물 변종(§Q-20260608-141) — 거래상태 도트 + "N분 전 기준" 갱신시각. 제목줄에 도트, namecell 아래 갱신시각.
    var futStatusHtml = '';
    var futAgeHtml = '';
    if (futVariant) {
      var so = opts.sessionOpen;
      // Q-20260610 (대표 catch 6/10) — session_open 은 *수집 시점* 스냅샷 값. 데이터가 stale(>30분)이면
      //   "지금 거래중"을 단정할 수 없다(us-intraday cron 은 KST 08:50~15:30 만 갱신 → 15:30 후 session_open=true
      //   가 frozen 된 채 노출 → "🟢거래중 + 지연 440분 전" 모순, FLR-AGT-002 허위 실시간). fix: stale 일 때는
      //   초록 라이브 도트("거래중")를 단정하지 않고 회색 "마지막 거래중"(스냅샷 시점 상태)으로 강등.
      //   "지연 N분 전" 칩(futAgeHtml)이 신선도 진실을 별도로 명시. 신선(≤30분) 시에만 라이브 거래중/마감 단정.
      // Q-20260610 (2회차) — 지난 날짜 카드(isPastDate)도 stale 동형: 그날 수집 시점의 session_open=true 는
      //   현재 거래 상태가 아니므로 라이브 "거래중" 도트 단정 금지 → "마지막 거래중"(스냅샷 시점)으로 강등.
      if (opts.isStale || isPastDate) {
        if (so === true) {
          futStatusHtml = '<span class="idx-fut-status stale" title="수집 시점 기준 거래중 — 현재 상태는 갱신 지연으로 미확인"><span class="idx-fut-dot" aria-hidden="true"></span>마지막 거래중</span>';
        }
        // so===false stale 는 "마감" 단정도 보류(도트 미렌더) — 신선도 칩만 노출.
      } else if (so === true) {
        futStatusHtml = '<span class="idx-fut-status open" title="거래중"><span class="idx-fut-dot" aria-hidden="true"></span>거래중</span>';
      } else if (so === false) {
        futStatusHtml = '<span class="idx-fut-status closed" title="거래 마감"><span class="idx-fut-dot" aria-hidden="true"></span>거래 마감</span>';
      }
      var am = (typeof opts.ageMin === 'number') ? opts.ageMin : null;
      if (am != null) {
        var ageText = am <= 0 ? '방금 전 기준' : (am + '분 전 기준');
        // Q-20260608-145 — 30분 초과(opts.isStale) 시 "지연" 칩으로 stale 정직 명시(숨김 ❌, FLR-AGT-002).
        var staleChip = opts.isStale ? '<span class="idx-fut-delay" title="갱신 지연(데이터가 최신이 아님)">지연</span>' : '';
        futAgeHtml = '<div class="idx-fut-age' + (opts.isStale ? ' idx-fut-age--stale' : '') + '">' + staleChip + esc(ageText) + '</div>';
      }
    }
    // feat/market-context ① (2026-06-12) — 국내(코스피·코스닥) 변종 modifier. 추가 클래스만 부여
    //   (빈 미니일봉 셀 숨김 등 CSS 스코프) — 기존 미장 카드 출력 0 변경 (additive).
    var krVariant = !!(opts && opts.krVariant);
    var cardCls = 'cal-feature-card v2 cal-feature-card--idx' + (futVariant ? ' cal-feature-card--fut' : '')
      + (krVariant ? ' cal-feature-card--idx-kr' : '');
    // feat/market-context ① — range_240d 실가용 일수 정직 표기 ("61일 레인지", 240 미만 시 의무).
    //   opts.rangeDaysNote(string) 존재 + 레인지 바 렌더 시에만 바 직하 1줄. 미전달(기존 호출) 출력 0.
    var rangeDaysNote = (opts && typeof opts.rangeDaysNote === 'string' && opts.rangeDaysNote) ? opts.rangeDaysNote : '';

    // 대표 20:26 catch 정정 — 국내 종목카드 헤더 DOM 1:1 복제 (renderer.js L1419-1435 verbatim 구조).
    //   head-left 4-child 순서 동일: .cal-trade-rank → .cal-trade-candle(당일캔들) → .cal-feature-sparkline
    //   → .cal-feature-candles20(확대 trigger). rank 슬롯 = 지수 순위 부재 → 빈 placeholder(정렬 column 유지).
    //   head-right: .cal-feature-namecell(.cal-feature-name) + .cal-feature-meta(.cal-feature-pct | .cal-trade-amount).
    //   data-stock-code = idxCode (확대 slot id/aria anchor). 국내 미사용: 공유버튼/badges/상세 body (지수 무관).
    // role="img" 제거 (대표 20:47 확대 trigger 추가 → 카드 내 interactive button 존재. img role 은
    //   interactive 자식 비호환). 국내 종목카드도 카드 자체 role 없음(1:1). aria-label 은 card에 두되 group 의미.
    return '<div class="' + cardCls + '" aria-label="' + esc(label) + '"'
      + ' data-stock-code="' + esc(idxCode) + '" data-stock-name="' + esc(displayName) + '">'
      + '<div class="cal-feature-head v2">'
      + '<div class="cal-feature-head-left">'
      + '<div class="cal-trade-rank"></div>'
      + '<div class="cal-trade-candle">' + todayCandleHtml + '</div>'
      + (sparkHtml ? '<div class="cal-feature-sparkline">' + sparkHtml + '</div>' : '<div class="cal-feature-sparkline cal-spark-empty"></div>')
      + candles20Cell
      + '</div>'
      + '<div class="cal-feature-head-right">'
      + '<div class="cal-feature-namecell">'
      + '<span class="cal-feature-name">' + esc(displayName) + '</span>'
      + futStatusHtml
      + '</div>'
      + '<div class="cal-feature-meta">'
      + '<span class="cal-feature-pct ' + dir + '"><span class="idx-card-arrow" aria-hidden="true">' + arrow + '</span>' + esc(fmtPct(pct)) + '</span>'
      + '<span class="cal-meta-sep">|</span>'
      + '<span class="cal-trade-amount">' + esc(fmtPoint(idx.point)) + ' p</span>'
      + '</div>'
      + futAgeHtml
      + '</div>'
      + '</div>'
      + rangeHtml
      + (rangeHtml && rangeDaysNote ? '<div class="idx-range-days-note">' + esc(rangeDaysNote) + '</div>' : '')
      + newsBodyHtml
      + '</div>';
  }

  // 지수별 주요 기사 — 국내 종목카드 뉴스 영역 (.cal-feature-body > .cal-feature-summary >
  //   .cal-causal 요약 + .cal-feature-links > .cal-feature-link) 템플릿 1:1 (대표 20:31).
  //   데이터: idx.news = {summary, sources:[{name,url}]}. 부재/요약·소스 모두 0 시 '' (블록 전체 생략, placeholder 0).
  function buildCardNews(news, newsTense) {
    if (!news || typeof news !== 'object') return '';
    var summary = (typeof news.summary === 'string') ? news.summary.trim() : '';
    var sources = Array.isArray(news.sources) ? news.sources : [];
    var linksHtml = sources.map(function (s) {
      if (!s || typeof s !== 'object') return '';
      var url = (typeof s.url === 'string' && /^https?:\/\//i.test(s.url)) ? s.url : '';
      var name = (typeof s.name === 'string') ? s.name : '';
      if (!url || !name) return '';  // 출처명·유효 URL 둘 다 필수 (법무 + placeholder 금지)
      return '<a class="cal-feature-link" href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' + esc(name) + '</a>';
    }).filter(Boolean).join('');
    if (!summary && !linksHtml) return '';  // 둘 다 없으면 블록 생략
    // Q-20260608-143 — 시제 라벨(선물 '실시간' / 정규장 '미장 마감'). 같은 뉴스 영역이라도 데이터
    //   시제가 다름을 명시(전일 마감 뉴스를 실시간으로 오인 차단). 라벨은 summary 가 있을 때만.
    var tenseHtml = '';
    // Q-20260608-143 후속(대표 2026-06-08) — '실시간' 배지 제거(실시간은 당연, 국내장 무표시 정합).
    //   '미장 마감'만 유지(전일 미장 마감 뉴스를 당일로 오인 차단, 비자명 정보).
    if (summary && newsTense === 'close') {
      tenseHtml = '<span class="cal-news-tense cal-news-tense--close">' + esc('미장 마감') + '</span>';
    } else if (summary && newsTense === 'live') {
      // Q-20260608 정규장 장중 — 개장 중 배지("장중", 대표 결정: '실시간' 단어 회피).
      //   "미장 마감"(폐장)과 시각적 구분 = 정규장 진행 중을 정직 표시(FLR-AGT-002 정합).
      tenseHtml = '<span class="cal-news-tense cal-news-tense--live">' + esc('장중') + '</span>';
    }
    var summaryHtml = summary ? '<div class="cal-causal">' + tenseHtml + esc(summary) + '</div>' : '';
    var linksBlock = linksHtml ? '<div class="cal-feature-links">' + linksHtml + '</div>' : '';
    return '<div class="cal-feature-body">'
      + '<div class="cal-feature-summary">' + summaryHtml + linksBlock + '</div>'
      + '</div>';
  }

  root.renderIndexCard = renderIndexCard;
  root.renderIndexFuturesCard = renderIndexFuturesCard;  // Q-20260608-140 A안 페어 카드
})(typeof window !== 'undefined' ? window : this);
