/* ───── utils.js — 도메인·상태 함수 (lib 분리 후, REQ-001 §3 Phase 1) ─────
   순수 포맷·이스케이프·sanitize·pad2·ymd → js/lib/format.js (DSN §3.1).
   formatYMD·getNextTradingDay·dsnV9FormatMD·computeTradingDayDiff·getShortTermDayOffset → js/lib/trading-day.js.
   본 파일은 KRX SSOT(stage flow / effect badge / credit reason v9.6 / DSN-v8 stage chip / auto effects 등) 잔류.
*/

/* ───── DSN-20260425-DSN-002 v8 — 시제 분리 분기 함수 5종 ─────
   §6.2 + §10 placeholder + 인계 prompt §3·§4·§5.1.
   SP_STAGES = ['단기과열'] (togusa B-5 critical_review). 명세 §10 잠정값(투자위험 등) 대비 prompt 우선.
   reason: 시장감시규정 §5의2 단기과열완화제도가 단일가매매 자동 적용 단계. 투자위험은 거래정지 1일+신용제한이며 단일가매매 자동 적용 아님.
*/
const DSN_V8_SP_STAGES = ['단기과열'];

function dsnV8StripStageLabel(label) {
  if (!label) return '';
  return String(label)
    .replace(/\s*예고\s*/g, '')
    .replace(/\s*예상\s*/g, '')
    .replace(/\s*근접\s*/g, '')
    .replace(/[\[\]\(\)]/g, ' ')
    .trim();
}

function dsnV8GetTenseChip(badge) {
  // 시제 칩 4종 분기 (B-12 — design v8.1 사후 갱신).
  //   predicted source            → [예측 진입]
  //   label에 "예고" 포함            → [지정 예고]   (KRX 공식 예고 단계)
  //   "예고" 미포함 + view_date<start → [지정 예정]   (공시 확정 + 시작일 미도래)
  //   그 외                       → [지정 중]
  const label = badge.label || '';
  const isPredicted = (badge.source === 'predicted')
    || label.includes('예상')
    || label.includes('근접');
  if (isPredicted) {
    return { text: '예측 진입', cls: 'dsn-v8-tense-chip--predicted' };
  }
  if (label.includes('예고')) {
    return { text: '지정 예고', cls: 'dsn-v8-tense-chip--disclosure' };
  }
  if (badge.view_date && badge.start && badge.view_date < badge.start) {
    return { text: '지정 예정', cls: 'dsn-v8-tense-chip--disclosure' };
  }
  return { text: '지정 중', cls: 'dsn-v8-tense-chip--disclosure' };
}

function dsnV8GetSinglePriceStatus(badge, currentDate) {
  // §4.3 + §5.1 — SP_STAGES = ['단기과열'] (togusa B-5 critical_review).
  // §5.1 줄 3 의무: 단일가 라인 항상 1줄 노출 (null 금지). predicted/sp=null/start=null 모두 명시 fallback.
  // B-11 시정: predicted에 대해 null 리턴 → 호출부 if 가드로 라인 누락. 4/13 217590 케이스 회귀.
  const label = badge.label || '';
  const isPredicted = (badge.source === 'predicted')
    || label.includes('예상') || label.includes('근접');
  // start/end 부재 → "해당 없음 (이 단계 미적용)" (B-4 가드, predicted/disclosure 공통)
  if (!badge.start || !badge.end) return '해당 없음 (이 단계 미적용)';
  const stripped = dsnV8StripStageLabel(label);
  const stageHasSP = DSN_V8_SP_STAGES.some(s => stripped.includes(s));
  if (!stageHasSP) return '해당 없음 (이 단계 미적용)';
  const today = currentDate || (badge.view_date || new Date().toISOString().slice(0, 10));
  // predicted: KRX 공식 지정 전 → "적용 중" 불가. start 미만이면 "적용 예정 (지정 시)", 그 외 "해당 없음".
  if (isPredicted) {
    if (today < badge.start) return '적용 예정 (지정 시)';
    return '해당 없음';
  }
  // disclosure
  if (badge.single_price === true && today >= badge.start && today <= badge.end) {
    return '적용 중';
  }
  if (today < badge.start) return '적용 예정 (지정 시)';
  return '해당 없음';
}

function dsnV8GetScheduleLines(badge) {
  // §4.2 source별 일정 라벨. start/end null 시 "미정 (조건 충족 시 발효)" fallback
  const isPredicted = (badge.source === 'predicted')
    || (badge.label || '').includes('예상')
    || (badge.label || '').includes('근접');
  const isNotice = (badge.label || '').includes('예고');
  let startLabel, endLabel;
  if (isPredicted) {
    startLabel = '예측 발효일'; endLabel = '예측 종료일';
  } else if (isNotice) {
    startLabel = '예고일'; endLabel = '지정 예정';
  } else {
    startLabel = '지정일'; endLabel = '해제 예정';
  }
  const start = badge.start;
  const end = badge.end;
  // 인계 prompt §3 — predicted start/end null fallback
  const startValue = start || '미정 (조건 충족 시 발효)';
  const endValue = end || '미정 (조건 충족 시 발효)';
  return {
    start: { label: startLabel, value: startValue },
    end: { label: endLabel, value: endValue },
  };
}

function dsnV8GetConfidenceLine(badge) {
  // predicted only. confidence null/undefined → "신뢰도: 미상 (추정)"
  const isPredicted = (badge.source === 'predicted')
    || (badge.label || '').includes('예상')
    || (badge.label || '').includes('근접');
  if (!isPredicted) return null;
  const conf = badge.confidence;
  if (conf == null || conf === '') return '미상 (추정)';
  return String(conf);
}

function dsnV8FormatThresholds(thresholds, label, badgeContext) {
  // §6.2 formatThresholds + 0/0 fallback (§3 D3 표제 의무)
  // v9 §B: badgeContext.thresholds 전달 시 base_price=null(지수 ratio) 항목은 raw 분자/분모 표기로 치환.
  const arr = Array.isArray(thresholds) ? thresholds : [];
  const total = arr.length;
  const triggered = arr.filter(t => t && t.triggered).length;
  const stripped = dsnV8StripStageLabel(label || '');
  const titleStage = stripped || (label || '진입');
  if (total === 0) {
    return `<div class="dsn-v8-thresholds">
      <div class="dsn-v8-thresholds__title">🎯 ${escapeHtml(titleStage)}에 진입하는 조건 (0/0 충족)</div>
      <div class="dsn-v8-thresholds__empty">(자동 평가 없음) — 지정 사유는 사유 박스 참조</div>
    </div>`;
  }
  const _detectUnit = (t) => {
    const d = (t && t.desc) || '';
    if (/배\s*(이상|이하|↑|↓)?/.test(d) || /비율|ratio/i.test(d)) return '배';
    if (t && t.base_price == null) return '';
    return '원';
  };
  const items = arr.map(t => {
    const cls = t.triggered ? 'dsn-v8-thresholds__item dsn-v8-thresholds__item--triggered'
      : 'dsn-v8-thresholds__item dsn-v8-thresholds__item--unmet';
    // v9 §B: 지수 ratio raw 표기 우선
    const v9Raw = (typeof getRawExplanation === 'function') ? getRawExplanation(t, badgeContext) : '';
    if (v9Raw) {
      return `<li class="${cls}">${escapeHtml(v9Raw)}</li>`;
    }
    const desc = t.desc || '';
    const unit = _detectUnit(t);
    const fmt = (v) => unit === '배'
      ? Number(v).toFixed(2) + '배'
      : (unit ? Number(v).toLocaleString() + unit : Number(v).toLocaleString());
    const cur = (t.current != null) ? fmt(t.current) : '';
    const thr = (t.threshold != null) ? fmt(t.threshold) : '';
    const bodyParts = [desc];
    if (cur && thr) bodyParts.push(`${cur} ${t.triggered ? '≥' : '<'} ${thr}`);
    else if (thr) bodyParts.push(`임계 ${thr}`);
    return `<li class="${cls}">${escapeHtml(bodyParts.join(' — '))}</li>`;
  }).join('');
  return `<div class="dsn-v8-thresholds">
    <div class="dsn-v8-thresholds__title">🎯 ${escapeHtml(titleStage)}에 진입하는 조건 (${triggered}/${total} 충족)</div>
    <ul class="dsn-v8-thresholds__list">${items}</ul>
  </div>`;
}

function dsnV8RenderBlock(badge, ctx) {
  // REQ-020c — 대표 직접 지시: [지정 중] dsn-v8-block--disclosure + [내일 가능] dsn-v8-block--predicted
  // 상세 박스 둘 다 제거. 헤더 효과 배지(v95) + 그래프 박스 + entry-window가 의미 흡수.
  // 본 함수 본체는 dead code 잔존 (후속 사이클 정리). 호출부 renderer.js:576 → 빈 문자열만 반환.
  return '';
  // ↓↓↓ dead code (REQ-020c 사이클 후속 정리) ↓↓↓
  // §3·§5.1 — 단일 배지 1블록. 5줄 요약 + 🎯 thresholds + 통합 펼침
  // v9.1 §B: predicted 칩은 imminent/predicted 분기 (strict 3 AND 조건). ctx.allBadges 인접 검증 필수.
  // REQ-018 §2 영역 3 (휴지 사이클 0 C=a) — strict 미충족 predicted 박스 차단. 그래프 unvisited와 정합.
  // 단일 진실 소스: getPredictedBadgeVisibility (REQ-015 §II 함수, REQ-016 §III 그래프 unvisited 판정 동일).
  // 사용처 5위치: getStageFlow(349)·countStrictUnmetPredicted(872)·renderPredictedDetailOnly(1043, dead)·renderTriggerPin(1073)·여기(영역 3).
  // eslint-disable-next-line no-unreachable
  const _isPredictedEarly = (badge.source === 'predicted')
    || (badge.label || '').includes('근접')
    || (badge.label || '').includes('예상');
  if (_isPredictedEarly) {
    const _vis = (typeof getPredictedBadgeVisibility === 'function')
      ? getPredictedBadgeVisibility(badge, (ctx && ctx.currentDate) || badge.view_date || '', (ctx && ctx.allBadges) || null)
      : 'header';
    if (_vis === 'detail-only') {
      return '';   // strict 미충족 predicted — 그래프 unvisited와 정합 (REQ-016 §III)
    }
  }
  const viewDateForChip = (ctx && ctx.currentDate) || badge.view_date || '';
  const allBadgesForChip = (ctx && ctx.allBadges) || null;
  const tenseChipHtml = (typeof renderTenseChip === 'function')
    ? renderTenseChip(badge, viewDateForChip, allBadgesForChip)
    : null;
  const tense = dsnV8GetTenseChip(badge);
  const isPredicted = (badge.source === 'predicted')
    || (badge.label || '').includes('예상')
    || (badge.label || '').includes('근접');
  const blockCls = isPredicted ? 'dsn-v8-block dsn-v8-block--predicted' : 'dsn-v8-block dsn-v8-block--disclosure';
  const sched = dsnV8GetScheduleLines(badge);
  const spStatus = dsnV8GetSinglePriceStatus(badge, ctx && ctx.currentDate);
  const label = badge.label || '';
  const sourceNote = isPredicted ? '<span class="dsn-v8-block__source-note">(KRX 미공식 · 자체 추정)</span>' : '';

  // 5줄 요약
  const summaryRows = [];
  summaryRows.push(`<div class="dsn-v8-summary__label">● ${escapeHtml(sched.start.label)}</div><div class="dsn-v8-summary__value">${escapeHtml(sched.start.value)}</div>`);
  summaryRows.push(`<div class="dsn-v8-summary__label">● ${escapeHtml(sched.end.label)}</div><div class="dsn-v8-summary__value">${escapeHtml(sched.end.value)}</div>`);
  if (spStatus) {
    summaryRows.push(`<div class="dsn-v8-summary__label">● 단일가 매매</div><div class="dsn-v8-summary__value">${escapeHtml(spStatus)}</div>`);
  }
  // 4번째 줄: 공시=사유 / 예측=신뢰도
  if (isPredicted) {
    const conf = dsnV8GetConfidenceLine(badge);
    if (conf) summaryRows.push(`<div class="dsn-v8-summary__label">● 신뢰도</div><div class="dsn-v8-summary__value">${escapeHtml(conf)}</div>`);
  } else {
    const reasonRaw = badge.reason_text;
    const placeholders = ['공시 원문 참조', '-', '–', '—', 'null', 'N/A', 'n/a', '없음', ''];
    const reasonStr = reasonRaw ? String(reasonRaw).trim() : '';
    if (reasonStr && !placeholders.includes(reasonStr)) {
      // 80자 이내 1줄 클램프 (5줄 요약 §5.1 정책)
      const oneLine = reasonStr.replace(/\r?\n/g, ' ').replace(/\s+/g, ' ');
      summaryRows.push(`<div class="dsn-v8-summary__reason">📋 사유 — ${escapeHtml(oneLine)}</div>`);
    }
  }

  // 🎯 thresholds (v9 §B: badge context 전달로 지수 ratio raw 표기 분기)
  const thresholdsHtml = dsnV8FormatThresholds(badge.thresholds || [], label, badge);

  // 추정 경고 배너 (predicted + (pending|low|미상))
  let warningBannerHtml = '';
  const regConf = badge.regulation_source_confidence || '';
  const showBanner = isPredicted && (regConf === 'pending' || regConf === 'low' || !regConf);
  if (showBanner) {
    warningBannerHtml = `<div class="dsn-v8-warning-banner">⚠ 추정 라벨 — KRX 공식 지정이 아닙니다. 규정 검증 진행 중.</div>`;
  }

  // 통합 펼침 (definition / regulation / source)
  const stripped = dsnV8StripStageLabel(label);
  const summaryToggleText = isPredicted
    ? `${stripped}란 / 적용 예정 제한 ▾`
    : `${stripped}란 / 규정 상세 / 공시 원문 ▾`;
  const ctxDart = (ctx && ctx.dartUrl) || '';
  const sourceBlockHtml = isPredicted
    ? ''
    : (ctxDart ? `<div class="dsn-v8-extra__source"><a href="${escapeHtml(ctxDart)}" target="_blank" rel="noopener noreferrer">공시 원문 보기 (DART) →</a></div>` : '');
  const definitionText = (ctx && ctx.stageDefinition) || '';
  const regulationText = (ctx && ctx.regulationDetail) || '';
  const definitionHtml = definitionText
    ? `<div class="dsn-v8-extra__definition"><h5>${escapeHtml(stripped)}이란</h5><p>${escapeHtml(definitionText)}</p></div>`
    : '';
  const regulationHtml = regulationText
    ? `<div class="dsn-v8-extra__regulation"><h5>${isPredicted ? '적용 예정' : '지정 시'} 적용되는 제한</h5><p>${escapeHtml(regulationText)}</p></div>`
    : '';
  const extraHtml = (definitionHtml || regulationHtml || sourceBlockHtml)
    ? `<details class="dsn-v8-extra"><summary>${escapeHtml(summaryToggleText)}</summary>${definitionHtml}${regulationHtml}${sourceBlockHtml}</details>`
    : '';

  // v9.1: tenseChipHtml 우선 사용 — predicted 케이스 imminent 분기 포함
  const chipHtml = tenseChipHtml
    || `<span class="dsn-v8-tense-chip ${tense.cls}">[${escapeHtml(tense.text)}]</span>`;
  return `<div class="${blockCls}">
    <div class="dsn-v8-block__header">
      ${chipHtml}
      <span class="dsn-v8-block__label">${escapeHtml(label)}</span>
      ${sourceNote}
    </div>
    <div class="dsn-v8-summary">${summaryRows.join('')}</div>
    ${thresholdsHtml}
    ${warningBannerHtml}
    ${extraHtml}
  </div>`;
}

// 복수 배지 시제 순서 정렬 (현재 → 예측). source !== 'predicted'를 앞으로.
function dsnV8SortBadges(badges) {
  return [...badges].sort((a, b) => {
    const ap = (a.source === 'predicted') ? 1 : 0;
    const bp = (b.source === 'predicted') ? 1 : 0;
    return ap - bp;
  });
}

/* ───── DSN-20260425-DSN-003 v9 — 단계 플로우 그래프 + raw 신뢰 표기 + 현재 상태 1줄 + 인과 라인 ─────
   §A·§B·§C·§D + §6.1 BEM + §6.2 함수 시그니처 5종.
   togusa C-1 매트릭스(rules/krx-stage-flow.json) 기반 placeholder. 사후 외부화 가능.
*/
const KRX_MAIN_TRACK = ['투자주의', '투자경고 예고', '투자경고', '투자위험 예고', '투자위험', '매매거래정지'];
const KRX_SHORT_TERM_TRACK = ['단기과열 예고', '단기과열'];

function dsnV9MatchStageIndex(track, badgeLabel) {
  // 배지 라벨을 트랙 노드와 매칭. 정확 일치 우선, 그 다음 prefix 매칭.
  // "투자위험 근접" predicted_shadow → "투자위험 예고" 노드에 매핑 (krx-stage-flow.json predicted_track 정의).
  if (!badgeLabel) return -1;
  // 정확 일치
  let idx = track.findIndex(label => label === badgeLabel);
  if (idx !== -1) return idx;
  // predicted_shadow 매핑: "X 근접" → KRX 공식 "X 예고" 노드
  if (badgeLabel.endsWith('근접')) {
    const stripped = badgeLabel.replace(/\s*근접\s*$/, '').trim();
    // "투자위험 근접" → "투자위험 예고"
    idx = track.findIndex(label => label === `${stripped} 예고`);
    if (idx !== -1) return idx;
    // "투자주의 근접" → "투자주의" (1단계는 예고 부재)
    idx = track.findIndex(label => label === stripped);
    if (idx !== -1) return idx;
  }
  // prefix
  return track.findIndex(label => badgeLabel.startsWith(label));
}

function getStageFlow(badges, viewDate) {
  // v9.3.1 §I — 노드 상태 매트릭스 산출. {trackMain, trackShortTerm} 각 NodeState[].
  // NodeState = {label, state: 'unvisited'|'current'|'upcoming'|'predicted-imminent'}
  //   - 'current': source='disclosure' AND start<=viewDate<=end (또는 "X 예고" 라벨이 D-1 인접 시 idx=current)
  //   - 'upcoming': source='disclosure' AND viewDate<start AND getNextTradingDay(viewDate)===start (D-1 인접 단계만, 또는 "X 예고" D-1 시 idx+1=upcoming 분리 부착)
  //   - 'predicted-imminent': isPredicted AND getPredictedBadgeVisibility==='header' (strict 3 AND 충족)
  //   - 'unvisited': 기본 (도약 케이스 disclosure 포함, predicted detail-only 포함 — v9.3.1 휴지 C=a)
  const trackMain = KRX_MAIN_TRACK.map(label => ({ label, state: 'unvisited' }));
  const trackShortTerm = KRX_SHORT_TERM_TRACK.map(label => ({ label, state: 'unvisited' }));
  if (!Array.isArray(badges) || badges.length === 0) {
    return { trackMain, trackShortTerm };
  }

  // v9.3 §I.2 — D-1 인접 정의 자체가 도약 차단 (getNextTradingDay(viewDate)===start만 upcoming).
  //   currentIdx 기반 인접 체크 불필요. D-3+ 케이스는 nextTd !== start로 자동 unvisited 분류.

  for (const badge of badges) {
    const label = badge.label || '';
    const isShortTerm = label.includes('단기과열');
    const target = isShortTerm ? trackShortTerm : trackMain;
    const trackArr = isShortTerm ? KRX_SHORT_TERM_TRACK : KRX_MAIN_TRACK;
    const idx = dsnV9MatchStageIndex(trackArr, label);
    if (idx === -1) continue;
    const isPredicted = (badge.source === 'predicted')
      || label.includes('근접')
      || label.includes('예상');

    if (isPredicted) {
      // v9.3.1 §I — 휴지 사이클 0 결정 C=a: vis==='detail-only' 시 state 부착 폐기 (unvisited 유지).
      // strict 3 AND 충족(vis==='header')만 'predicted-imminent' 부착.
      const vis = (typeof getPredictedBadgeVisibility === 'function')
        ? getPredictedBadgeVisibility(badge, viewDate, badges)
        : 'header';
      if (vis === 'header' && target[idx].state === 'unvisited') {
        target[idx].state = 'predicted-imminent';
      }
      // vis==='detail-only' → state 부착 폐기 (unvisited 유지)
    } else {
      // disclosure source — current vs upcoming 분기 (§I.2 + v9.3.1 §I 휴지 A=a)
      const start = badge.start || '';
      const end = badge.end || '';
      const today = viewDate || '';
      if (start && today && today < start) {
        // v9.3 §I.2 — 발효일 미도래 → D-1 인접만 upcoming (대표 본질 가치 "당장 오늘 혹은 다음 영업일에 필연").
        // getNextTradingDay(today)===start AND today<start 동시 충족 시 D-1 정확 인접.
        const nextTd = (typeof getNextTradingDay === 'function') ? getNextTradingDay(today) : '';
        if (nextTd && nextTd === start) {
          // v9.3.1 §I 휴지 A=a — "X 예고" 라벨이면 idx=current(예고 진행 중) + idx+1=upcoming(다음 단계 발효 예정) 분리 부착.
          // guard: idx+1 < trackArr.length (마지막 단계 예고는 분리 부착 폐기, 자체만 upcoming).
          if (label.endsWith('예고') && idx + 1 < trackArr.length) {
            target[idx].state = 'current';
            target[idx + 1].state = 'upcoming';
          } else {
            target[idx].state = 'upcoming';
          }
          continue;
        } else {
          // D-2+ — unvisited 유지 (시간 여유 인지)
          continue;
        }
      } else if (start && end && today >= start && today <= end) {
        target[idx].state = 'current';
      } else {
        target[idx].state = 'current'; // 보수적 fallback (start 없거나 정보 부족)
      }
    }
  }
  return { trackMain, trackShortTerm };
}

// dsnV9FormatMD → js/lib/trading-day.js (REQ-001 §3 Phase 1 분리)

function getCurrentStateSummary(badges, viewDate) {
  // §C 카드 펼침 영역 1줄 헤더. 시제 3택 (미지정/기간/예고 중).
  // v9.1 §C.3: "지정 중" → "기간". label 정제 X (예: "투자경고 예고 기간" 그대로).
  if (!Array.isArray(badges) || badges.length === 0) return '';
  // 공시 우선
  const disclosure = badges.find(b =>
    b.source !== 'predicted'
    && !(b.label || '').includes('근접')
    && !(b.label || '').includes('예상')
  );
  const predicted = badges.find(b =>
    (b.source === 'predicted')
    || (b.label || '').includes('근접')
    || (b.label || '').includes('예상')
  );
  const primary = disclosure || predicted;
  if (!primary) return '';

  const today = viewDate || primary.view_date || new Date().toISOString().slice(0, 10);
  const start = primary.start || '';
  const end = primary.end || '';
  const label = primary.label || '';

  const isPredictedPrimary = primary === predicted && !disclosure;
  if (isPredictedPrimary) {
    return `📍 현재 = ${escapeHtml(label)} 진입 예측 (자체 추정 · KRX 미공식)`;
  }
  // disclosure
  if (start && today < start) {
    return `📍 현재 = ${escapeHtml(label)} (공시 발효 ${escapeHtml(dsnV9FormatMD(start))}, 미지정 상태)`;
  }
  if (start && end && today >= start && today <= end) {
    // v9.1: label 정제 X — "투자경고 예고 기간"으로 그대로 노출 (예고 단계 명시 보존)
    return `📍 현재 = ${escapeHtml(label)} 기간 (${escapeHtml(dsnV9FormatMD(start))}~${escapeHtml(dsnV9FormatMD(end))})`;
  }
  return `📍 현재 = ${escapeHtml(label)}`;
}

/* ───── DSN-20260425-DSN-004 v9.1 — 시제 칩 5번째 [내일 가능] + 법무 푸터 ─────
   §B 시제 칩 분기 (predicted source 시간차 기반 imminent 분기).
   §E "내일" 산출 — build_daily.py status_badges.next_trading_day_for_predicted 신뢰. renderer 재산출 X.
   §G CSS BEM dsn-v9-tense-chip--imminent.
   §D 법무 푸터 1줄 (펼침 영역 최하단).
*/

// formatYMD, getNextTradingDay → js/lib/trading-day.js (REQ-001 §3 Phase 1 분리, window 전역 호출 호환)

// v9.1 strict 룰 — KRX_MAIN_TRACK 인접 단계 검증용.
// 인용: rules/krx-stage-flow.json#flow.stages[].predicted_shadow.flow_node + $027360_4_24_mapping.
// 4/24 027360 케이스: disclosure="투자경고 예고"(stages[1]) + predicted="투자위험 근접"(stages[3] predicted_shadow) → 단계 도약(차이 2) → [예측 진입] 폴백.
const KRX_MAIN_TRACK_LABELS_FOR_STRICT = ['투자주의', '투자경고 예고', '투자경고', '투자위험 예고', '투자위험', '매매거래정지'];

function matchMainTrackStep(label) {
  // togusa C-1 매트릭스 — KRX_MAIN_TRACK 인덱스 산출. predicted "X 근접"은 KRX 공식 "X 예고"(또는 1단계 자체)로 매핑.
  // 인용: rules/krx-stage-flow.json#flow.stages[].predicted_shadow.flow_node ("stages[N] (label) 노드의 'predicted_shadow'").
  if (!label) return -1;
  let idx = KRX_MAIN_TRACK_LABELS_FOR_STRICT.findIndex(l => l === label);
  if (idx !== -1) return idx;
  if (label.endsWith('근접')) {
    const stripped = label.replace(/\s*근접\s*$/, '').trim();
    idx = KRX_MAIN_TRACK_LABELS_FOR_STRICT.findIndex(l => l === `${stripped} 예고`);
    if (idx !== -1) return idx;
    idx = KRX_MAIN_TRACK_LABELS_FOR_STRICT.findIndex(l => l === stripped);
    if (idx !== -1) return idx;
  }
  return -1;
}

function getCurrentStageIndex(badges) {
  // current = disclosure source 중 KRX_MAIN_TRACK 최대 인덱스 (predicted 제외).
  if (!Array.isArray(badges) || badges.length === 0) return -1;
  let maxIdx = -1;
  for (const b of badges) {
    if (!b) continue;
    const isPredicted = (b.source === 'predicted')
      || (b.label || '').includes('근접')
      || (b.label || '').includes('예상');
    if (isPredicted) continue;
    const idx = matchMainTrackStep(b.label || '');
    if (idx > maxIdx) maxIdx = idx;
  }
  return maxIdx;
}

function getPredictedTenseVariant(badge, viewDate, allBadges) {
  // §B.2 predicted 배지 시제 칩 분기 — 'imminent' (D+1 거래일 특정) vs 'predicted' (일자 미특정).
  // togusa strict 3 AND 조건 모두 충족 시에만 'imminent':
  //   1) badge.source === 'predicted'
  //   2) predicted_shadow.flow_node === current_stage_index + 1 (KRX_MAIN_TRACK 인접)
  //   3) badge.next_trading_day_for_predicted == view_date+1 거래일
  // 인용: rules/krx-stage-flow.json $027360_4_24_mapping ("stages[1] disclosure + stages[3] predicted_shadow = 단계 도약 → [예측 진입] 폴백").
  if (!badge || badge.source !== 'predicted') return null;
  const ntd = badge.next_trading_day_for_predicted;
  if (!ntd) return 'predicted';
  if (!viewDate) return 'predicted';
  // 조건 3: D+1 거래일 일치
  if (ntd !== getNextTradingDay(viewDate)) return 'predicted';
  // 조건 2: 인접 검증 (current+1만 허용). allBadges 미전달 시 보수적으로 'predicted' 폴백.
  if (Array.isArray(allBadges) && allBadges.length > 0) {
    const currentIdx = getCurrentStageIndex(allBadges);
    const predictedIdx = matchMainTrackStep(badge.label || '');
    if (currentIdx === -1 || predictedIdx === -1) return 'predicted';
    if (predictedIdx !== currentIdx + 1) return 'predicted';  // 단계 도약 차단 (4/24 027360 케이스)
  } else {
    // allBadges 부재 시 인접 검증 불가 → 안전 폴백
    return 'predicted';
  }
  return 'imminent';
}

function renderTenseChip(badge, viewDate, allBadges) {
  // §B.2 시제 칩 분기 진입점. v8 §4.4 칩 4종 + v9.1 §B 5번째 [내일 가능].
  // allBadges: 같은 카드의 status_badges 전체 (strict 인접 검증용).
  // backend schema (commit e9e384d): badge.next_trading_day_source ∈ {'verified','estimated','fallback_homepage','fallback_legacy','unknown'}.
  // estimated/fallback grade는 칩에 data-source-grade 속성 + ⚠️ prefix + tooltip 노출 (DSN-004 §IX 함정 #2).
  if (!badge) return '';
  const isPredicted = (badge.source === 'predicted')
    || (badge.label || '').includes('예상')
    || (badge.label || '').includes('근접');
  if (isPredicted) {
    const variant = getPredictedTenseVariant(badge, viewDate, allBadges);
    if (variant === 'imminent') {
      const grade = badge.next_trading_day_source || '';
      const isEstimated = (grade === 'estimated' || grade === 'fallback_homepage' || grade === 'fallback_legacy');
      const gradeAttr = grade ? ` data-source-grade="${escapeHtml(grade)}"` : '';
      const tooltip = isEstimated
        ? ' title="추정 휴장 캘린더 — KRX 공시 미발표"'
        : '';
      const warnPrefix = isEstimated
        ? '<span class="dsn-v9-tense-chip__grade-warn" aria-label="추정 휴장 캘린더">⚠️</span>'
        : '';
      // 휴지 메트릭 의무 — predicted_shadow_v9_1_fired_count (사이클 4 폐기 트리거 측정).
      // 옵션 1: window 전역 카운터 + console.info(ticker, count). 옵션 2: DOM data-v91-fired="true" (qa grep).
      if (typeof window !== 'undefined') {
        window.__v91_fired_count = (window.__v91_fired_count || 0) + 1;
        const ticker = (badge && (badge.ticker || badge.code || badge.stock_code)) || '';
        if (typeof console !== 'undefined' && console.info) {
          console.info(`[v9.1] [내일 가능] fired: ticker=${ticker} grade=${grade || 'unset'} (count=${window.__v91_fired_count})`);
        }
      }
      return `<span class="dsn-v8-tense-chip dsn-v8-tense-chip--predicted dsn-v9-tense-chip--imminent" data-v91-fired="true"${gradeAttr}${tooltip}>${warnPrefix}[내일 가능]</span>`;
    }
    return `<span class="dsn-v8-tense-chip dsn-v8-tense-chip--predicted">[예측 진입]</span>`;
  }
  // disclosure: v8 dsnV8GetTenseChip 재사용
  const tense = dsnV8GetTenseChip(badge);
  return `<span class="dsn-v8-tense-chip ${tense.cls}">[${escapeHtml(tense.text)}]</span>`;
}

function renderDisclaimerFooter() {
  // §D.2 법무 푸터 1줄 (legal P0 확정 텍스트).
  // v9.8 DSN-010 §V — 카드별 footer 폐기. 페이지 푸터(news.html)가 면책 1회 표시.
  // 본 함수는 legacy 호출 가드용 잔존 (호출 차단 — 빈 문자열 반환).
  return '';
}

function renderMicroDisclaimerIfShared() {
  // v9.8 DSN-010 §V.4 — 단일 카드 공유 (#stock-*) 진입 시만 micro 면책 활성.
  // 일반 목록 진입 시 페이지 푸터(news.html)가 1회 흡수 — 카드 noise 0.
  if (typeof window === 'undefined' || !window.location) return '';
  const hash = String(window.location.hash || '');
  if (!hash.startsWith('#stock-')) return '';
  return `<div class="dsn-v98-micro-disclaimer">`
    + `<span class="dsn-v98-micro-disclaimer__icon">ⓘ</span>`
    + `<span class="dsn-v98-micro-disclaimer__text">투자판단 권고 아님</span>`
    + `</div>`;
}

function getCausalLine(badges) {
  // §D multi-badge 인과 라인. 2개+이고 disclosure+predicted 동시일 때만 노출.
  if (!Array.isArray(badges) || badges.length < 2) return '';
  const disclosure = badges.find(b =>
    b.source !== 'predicted'
    && !(b.label || '').includes('근접')
    && !(b.label || '').includes('예상')
  );
  const predicted = badges.find(b =>
    (b.source === 'predicted')
    || (b.label || '').includes('근접')
    || (b.label || '').includes('예상')
  );
  if (!disclosure || !predicted) return '';
  const dLabel = disclosure.label || '';
  const pLabel = predicted.label || '';
  const dTag = dLabel.includes('예고') ? '[지정 예고]' : '[지정 중]';
  return `${dTag} ${escapeHtml(dLabel)} → [예측 진입] ${escapeHtml(pLabel)} (다음 단계 가능)`;
}

function getRawExplanation(threshold, badgeContext) {
  // §B raw 표기. base_price=null 케이스(지수 ratio)는 분자/분모 raw 산출 식 노출.
  // 실제 데이터에 stock_change_pct/index_change_pct 필드 부재 — badgeContext.price_chg + threshold.current(ratio)로 역산.
  if (!threshold) return '';
  const desc = threshold.desc || '';
  const isIndexRatio = (threshold.base_price == null) && /지수|ratio|배\s*이상/i.test(desc);
  if (isIndexRatio && badgeContext) {
    const ratio = threshold.current;
    const thrVal = threshold.threshold;
    const stockPct = (badgeContext.price_chg != null) ? badgeContext.price_chg * 100 : null;
    let stockBase = null, stockNow = null;
    // 같은 배지 thresholds에서 base_price≠null 항목 중 가장 빠른(price 비교) entry로 종목 base/현재가 추정
    if (Array.isArray(badgeContext.thresholds)) {
      const priceEntries = badgeContext.thresholds.filter(t =>
        t && t.base_price != null && t.current != null && /3일|5일|15일|일\s*전|기준가|최고가/.test(t.desc || '')
      );
      // 우선순위: "3일 전" > "5일 전" > "15일 최고가" 등 base_price 가장 작은 것(가장 큰 상승률)
      if (priceEntries.length > 0) {
        // 가장 큰 상승률 = (current - base_price) 가 가장 큰 entry
        priceEntries.sort((a, b) => {
          const ra = (a.current - a.base_price) / a.base_price;
          const rb = (b.current - b.base_price) / b.base_price;
          return rb - ra;
        });
        stockBase = priceEntries[0].base_price;
        stockNow = priceEntries[0].current;
      }
    }
    if (stockPct != null && stockBase != null && stockNow != null && ratio != null && thrVal != null) {
      const indexPct = stockPct / ratio;
      const fmtN = (n) => Number(n).toLocaleString('ko-KR');
      const fmtPct = (n) => (n >= 0 ? '+' : '') + Number(n).toFixed(2) + '%';
      return `종목 ${fmtPct(stockPct)} (${fmtN(stockBase)}원→${fmtN(stockNow)}원) ÷ 종합지수 ${fmtPct(indexPct)} = ${Number(ratio).toFixed(2)}배 (${Number(thrVal).toFixed(2)}배 이상 충족)`;
    }
    // fallback: 부분 raw
    if (ratio != null && thrVal != null) {
      return `종합지수 대비 ${Number(ratio).toFixed(2)}배 ÷ 임계 ${Number(thrVal).toFixed(2)}배 (${threshold.triggered ? '충족' : '미충족'})`;
    }
  }
  // base_price=not null 케이스 — v8 기존 표기 유지(호출자에서 분기). 빈 문자열 반환 시 v8 fallback.
  return '';
}

/* ───── DSN-20260425-DSN-005 v9.2 — 그래프 박스 자동 효과 + predicted 위계 분리 ─────
   §I 박스 데이터: 각 노드 박스 하단에 "X/X · {auto_effects_short}" 1줄 노출 (휴지 약한 명사형).
   §II predicted 위계: strict 3 AND 미충족 → 헤더 비노출, 펼침 detail-only.
   §III predicted-only 카드: 옵션 1-a 트리거 핀 "↗ 추정 N건".
   §I.3 매트릭스: rules/krx-stage-flow.json#flow.stages[].auto_effects_short (verified 9건, commit f3c0da7).
   homepage 인라인 상수 — 메인 레포 동기화 시 교체 가능. (사이클 0 strict 룰처럼)
*/
const AUTO_EFFECTS_SHORT = {
  '투자주의': '',
  '투자경고 예고': '',
  '투자경고': '신용금지',
  '투자위험 예고': '거래정지',
  '투자위험': '거래정지',
  '매매거래정지': '거래 중지',
  '해제': '정상 복귀',
  '단기과열 예고': '',
  '단기과열': '단일가매매',           // legacy fallback (dayOffset 미지정)
  '단기과열 D+0': '(D+2부터)',        // v9.3 사이클 2.5 — 휴지 G=a (togusa P0 부정합 보정)
  '단기과열 D+1': '(D+2부터)',        // v9.3 사이클 2.5 — 휴지 G=a
  '단기과열 D+2': '거래정지 1일',     // v9.3 §III.3
  '단기과열 D+3-5': '단일가매매'      // v9.3 §III.3
};

function getAutoEffectsShort(stageLabel, dayOffset) {
  // §I.3 stage 라벨 → 자동 효과 1줄. 미정의/(없음) → '' 반환.
  // v9.3 §III.3 + 사이클 2.5 (휴지 G=a) — togusa P0 부정합 보정:
  //   KRX SSOT(krx-stage-conditions.json:726·772~775·781) D+0·D+1=효과 부재 / D+2=거래정지 1일 / D+3~D+5=단일가매매
  //   D+0/D+1 박스 효과 텍스트 = '(D+2부터)' 약한 명사형 (휴지 동사 회피 룰 정합. 5자 모바일 안전).
  if (!stageLabel) return '';
  if (stageLabel === '단기과열' && dayOffset) {
    if (dayOffset === 'd+0') return AUTO_EFFECTS_SHORT['단기과열 D+0'];
    if (dayOffset === 'd+1') return AUTO_EFFECTS_SHORT['단기과열 D+1'];
    if (dayOffset === 'd+2') return AUTO_EFFECTS_SHORT['단기과열 D+2'];
    if (dayOffset === 'd+3-5') return AUTO_EFFECTS_SHORT['단기과열 D+3-5'];
    if (dayOffset === 'd+6+') return ''; // 자동 해제 후
  }
  if (Object.prototype.hasOwnProperty.call(AUTO_EFFECTS_SHORT, stageLabel)) {
    return AUTO_EFFECTS_SHORT[stageLabel] || '';
  }
  return '';
}

/* ───── DSN-20260426-DSN-001 v9.3 §II·§III — 헤더 뱃지 통합 + 단기과열 D+N 분기 ─────
   §II: 시장경보·거래정지·단일가 통합 라벨. 원 단계 라벨은 data-krx-stage·title·aria-label 보존.
   §III: 단기과열 D+1·D+2='거래정지' / D+3~D+5='단일가' 분기. computeTradingDayDiff 영업일 차이.
*/

// computeTradingDayDiff, getShortTermDayOffset → js/lib/trading-day.js (REQ-001 §3 Phase 1 분리)

function getShortTermBadgeKind(badge, viewDate) {
  // v9.3 §III.2 + 사이클 2.5 — 단기과열 헤더 뱃지 종류.
  //   'short-term-self'=원라벨 '단기과열' (D+0/D+1, 효과 부재 — togusa P0 부정합 보정 / 휴지 G=a)
  //   'time-stop'=거래정지 (D+2, 매매거래정지 1일)
  //   'single-price'=단일가 (D+3~D+5, 30분 단위 단일가매매)
  //   'market-warn'=시장경보(예고/근접 등)
  // D+0/D+1: 효과 부재이므로 원라벨 유지. D+2부터 변경 정책 적용 — togusa P0 부정합 보정 / 휴지 G=a
  // ("단기과열→단일가" 정책(휴지 결정 C=b)과 D+0/D+1='단기과열' 예외 분기 충돌 — 본 주석 명시 의무)
  if (!badge || !(badge.label || '').includes('단기과열')) return 'market-warn';
  // 예고·근접은 시장경보로 통합
  if ((badge.label || '').includes('예고') || (badge.label || '').includes('근접')) return 'market-warn';
  const offset = getShortTermDayOffset(badge, viewDate);
  if (offset === 'd+0' || offset === 'd+1') return 'short-term-self';
  if (offset === 'd+2') return 'time-stop';
  if (offset === 'd+3-5') return 'single-price';
  return 'market-warn';
}

function getHeaderBadgeLabel(badge, viewDate) {
  // v9.3 §II.1 + 사이클 2.5 — 헤더 뱃지 통합 라벨 매핑.
  // 매매거래정지 → '거래정지' (E=a 4자 가독성)
  // 단기과열 D+0/D+1 → '단기과열' 원라벨 (효과 부재 예외 — 휴지 G=a)
  // 단기과열 D+2 → '거래정지' / D+3-5 → '단일가' (C=b 분기)
  // 그 외 (투자주의/경고/위험/예고/근접) → '시장경보' (B=a 통합)
  if (!badge || !badge.label) return '시장경보';
  const label = badge.label;
  if (label === '매매거래정지') return '거래정지';
  if (label.includes('단기과열')) {
    const kind = getShortTermBadgeKind(badge, viewDate);
    if (kind === 'short-term-self') return '단기과열';  // 사이클 2.5 — 원라벨 예외 유지
    if (kind === 'time-stop') return '거래정지';
    if (kind === 'single-price') return '단일가';
    return '시장경보';
  }
  return '시장경보';
}

function getHeaderBadgeTitle(badge, viewDate) {
  // v9.3 §II.1 + 사이클 2.5 — 헤더 뱃지 hover/aria-label 텍스트. 원 단계 라벨 + (시장경보 N단계 D-N) 형식.
  // D+0/D+1은 "단기과열 (D+2 거래정지 예정)" 보강으로 사용자 정보 보강 (자동 효과 부재 + 향후 효과 안내).
  if (!badge || !badge.label) return '';
  const label = badge.label;
  // 매매거래정지
  if (label === '매매거래정지') return '매매거래정지 (정식명)';
  // 단기과열 분기
  if (label.includes('단기과열')) {
    if (label.includes('예고')) return '단기과열 예고';
    if (label.includes('근접')) return '단기과열 근접 (자체 추정 · KRX 미공식)';
    const kind = getShortTermBadgeKind(badge, viewDate);
    if (kind === 'short-term-self') {
      const offset = getShortTermDayOffset(badge, viewDate);
      if (offset === 'd+0') return '단기과열 D+0 (D+2 거래정지 예정)';
      if (offset === 'd+1') return '단기과열 D+1 (D+2 거래정지 예정)';
      return '단기과열';
    }
    if (kind === 'time-stop') return '단기과열 D+2 매매거래정지 1일';
    if (kind === 'single-price') return '단기과열 D+3~D+5 30분 단일가매매';
    return '단기과열';
  }
  // 시장경보 단계 매핑
  const stageMap = {
    '투자주의': '시장경보 1단계',
    '투자경고 예고': '시장경보 2단계 D-1',
    '투자경고': '시장경보 2단계',
    '투자위험 예고': '시장경보 3단계 D-1',
    '투자위험': '시장경보 3단계',
    '투자주의 근접': '시장경보 1단계 추정',
    '투자경고 근접': '시장경보 2단계 추정',
    '투자위험 근접': '시장경보 3단계 추정 · KRX 미공식'
  };
  const tag = stageMap[label] || '시장경보';
  return `${label} (${tag})`;
}

function getKrxStageDataset(badge) {
  // v9.3 §II.1 — data-krx-stage 속성값. 원 단계 라벨 그대로 보존 (FLR-010 방어).
  if (!badge || !badge.label) return '';
  return badge.label;
}

function getPredictedBadgeVisibility(badge, viewDate, allBadges) {
  // §II.2 predicted 배지 헤더 노출 분기 — strict 3 AND 충족 → 'header', 미충족 → 'detail-only'.
  // disclosure source는 항상 'header' (분기 무해당).
  if (!badge || badge.source !== 'predicted') return 'header';
  const variant = getPredictedTenseVariant(badge, viewDate, allBadges);
  return variant === 'imminent' ? 'header' : 'detail-only';
}

function countStrictUnmetPredicted(badges, viewDate) {
  // §III.4 트리거 핀 노출 조건 — disclosure 0 + predicted strict 미충족 ≥1 케이스 카운트.
  if (!Array.isArray(badges) || badges.length === 0) return 0;
  let count = 0;
  for (const b of badges) {
    if (!b) continue;
    const isPredicted = (b.source === 'predicted')
      || (b.label || '').includes('근접')
      || (b.label || '').includes('예상');
    if (!isPredicted) continue;
    const vis = getPredictedBadgeVisibility(b, viewDate, badges);
    if (vis === 'detail-only') count += 1;
  }
  return count;
}

// ============================================================================
// REQ-020 v9.5 — 효과 배지 (effect+when) 헬퍼
// 명세: DOC-20260427-DSN-005 §I.5 / §II.2 / §II.4 / §II.5.
// SSOT: build_daily.py _compute_effect_badges가 status_badges[].effect_badges[] 산출.
// utils.js 책임: 카드 단위 머지(A1) + 우선순위 정렬(A4) + 중복 제거 + 라벨 포맷.
// ============================================================================

const _DSN_V95_EFFECT_LABEL = {
  'credit-block': '신용불가',
  'trade-halt': '거래정지',
  'single-price': '단일가',
  'limit-up': '상한가',
};
// REQ-023 v9.8 §I — 'when' enum 단순화 (DSN-010).
// 휴지 결정 B: today / tomorrow / today_and_tomorrow 만 (확정만).
// 폐기 (DSN-009 v9.7 → DSN-010 v9.8 / 대표 본질 비판 #1·#2):
//   - 거래정지 N일 후·단일가 3~5일 enum 2종 — 거짓 정밀성/확정 표현 부적절 (FLR-AGT-002)
//   - tomorrow_maybe — predicted_shadow 부활 폐기 (togusa SSOT 4/27 21:12 — 인프라 산출 0건)
// 단기과열 예고는 §III 헤더 0건 + 사유 박스 1줄로 위계 분리.
// fallback 가드: dsnV95FormatEffectBadge가 미정의 시 토큰 raw 노출 (회귀 발견 가능).
const _DSN_V95_WHEN_LABEL = {
  'today': '오늘',
  'tomorrow': '내일',
  'today_and_tomorrow': '오늘+내일',
};
// A4 우선순위: 거래정지 > 상한가 > 신용불가 > 단일가 / today > today_and_tomorrow > tomorrow.
// REQ-082 Phase2 — 상한가는 매매 직결성 최상위 (위험·기회 둘 다 trade-halt 다음).
const _DSN_V95_EFFECT_ORDER = { 'trade-halt': 0, 'limit-up': 1, 'credit-block': 2, 'single-price': 3 };
const _DSN_V95_WHEN_ORDER = {
  'today': 0,
  'today_and_tomorrow': 1,
  'tomorrow': 2,
};

function dsnV95FormatEffectBadge(eb) {
  // §II.2 — "신용불가(내일)" / "거래정지(오늘+내일)" / "단일가(내일 가능)" 등.
  if (!eb || !eb.effect) return '';
  // REQ-082 Phase2 — 상한가는 when 표기 생략 + cc>=2 시 +N 부착 ("상한가" / "상한가+2")
  if (eb.effect === 'limit-up') {
    const cc = Number(eb.consecutive_count);
    return cc >= 2 ? `상한가+${cc}` : '상한가';
  }
  const ef = _DSN_V95_EFFECT_LABEL[eb.effect] || eb.effect;
  // 대표 발화 2026-04-29 16:55 — credit-block 라벨 정책:
  //   today → '신용불가' (괄호 생략, default = 오늘)
  //   tomorrow → '신용불가(내일)'
  //   today_and_tomorrow → '신용불가(+내일)' (괄호 안 "+내일"만)
  if (eb.effect === 'credit-block') {
    if (eb.when === 'today' || !eb.when) return '신용불가';
    if (eb.when === 'tomorrow') return '신용불가(내일)';
    if (eb.when === 'today_and_tomorrow') return '신용불가(+내일)';
  }
  const wh = _DSN_V95_WHEN_LABEL[eb.when] || eb.when || '';
  return wh ? `${ef}(${wh})` : ef;
}

function dsnV95EffectBadgeTitle(eb) {
  // §II.3 hover/aria-label — 원 단계 라벨 + 효과 매핑 + 시점 매핑.
  // 예: "투자경고 예고 → 신용불가 (내일 발효)" / "투자위험 근접 → 거래정지 (내일 발효 가능)".
  if (!eb || !eb.effect) return '';
  // REQ-082 Phase2 — 상한가는 KRX 단계 효과 아님 (가격제한 도달). 별도 툴팁.
  if (eb.effect === 'limit-up') {
    const cc = Number(eb.consecutive_count);
    return cc >= 2 ? `${cc}일 연속 상한가 (오늘 +30% 도달)` : '오늘 상한가 (+30% 도달)';
  }
  const ef = _DSN_V95_EFFECT_LABEL[eb.effect] || eb.effect;
  const src = eb.source_label || '';
  const whenText = {
    'today': '오늘 발효 중',
    'tomorrow': '내일 발효',
    'today_and_tomorrow': '오늘 발효 중 + 내일도 잔존',
  }[eb.when] || (_DSN_V95_WHEN_LABEL[eb.when] || '');
  if (src) return `${src} → ${ef} (${whenText})`;
  return `${ef} (${whenText})`;
}

function mergeEffectBadges(effects) {
  // §I.5 A1 — 동일 effect의 today + tomorrow 머지 → today_and_tomorrow.
  // v9.8: in_*_days/tomorrow_maybe 폐기로 머지 룰 단순화.
  if (!Array.isArray(effects) || effects.length === 0) return [];
  const byEffect = {};
  for (const eb of effects) {
    if (!eb || !eb.effect) continue;
    const key = eb.effect;
    if (!byEffect[key]) byEffect[key] = [];
    byEffect[key].push(eb);
  }
  const merged = [];
  for (const key in byEffect) {
    const items = byEffect[key];
    const hasToday = items.some(i => i.when === 'today');
    const hasTomorrow = items.some(i => i.when === 'tomorrow');
    if (hasToday && hasTomorrow) {
      // 머지: today + tomorrow → today_and_tomorrow.
      // severity는 first item 기준 (동일 effect 동일 severity 가정).
      const todayItem = items.find(i => i.when === 'today');
      const tomorrowItem = items.find(i => i.when === 'tomorrow');
      const sourceLabels = [
        todayItem && todayItem.source_label,
        tomorrowItem && tomorrowItem.source_label,
      ].filter(Boolean).join(' + ');
      merged.push({
        effect: key,
        when: 'today_and_tomorrow',
        severity: todayItem.severity || items[0].severity,
        source_label: sourceLabels || (items[0].source_label || ''),
        source_kind: items[0].source_kind || 'disclosure',
      });
      // v9.8: in_*_days/tomorrow_maybe 폐기로 외 항목 잔존 케이스 거의 0
      for (const i of items) {
        if (i.when !== 'today' && i.when !== 'tomorrow') merged.push(i);
      }
    } else {
      merged.push(...items);
    }
  }
  return merged;
}

function dedupEffectBadges(effects) {
  // P1 함정 #3 — 다중 출처(같은 effect+when) 1건만 잔존. source_label은 첫 출처 유지.
  if (!Array.isArray(effects)) return [];
  const seen = new Set();
  const out = [];
  for (const eb of effects) {
    if (!eb || !eb.effect || !eb.when) continue;
    const key = `${eb.effect}|${eb.when}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(eb);
  }
  return out;
}

function sortEffectBadges(effects) {
  // §II.4 A4 — 거래정지 > 신용불가 > 단일가 / today > today_and_tomorrow > tomorrow.
  if (!Array.isArray(effects)) return [];
  return effects.slice().sort((a, b) => {
    const ea = _DSN_V95_EFFECT_ORDER[a.effect];
    const eb = _DSN_V95_EFFECT_ORDER[b.effect];
    const da = (ea === undefined ? 99 : ea) - (eb === undefined ? 99 : eb);
    if (da !== 0) return da;
    const wa = _DSN_V95_WHEN_ORDER[a.when];
    const wb = _DSN_V95_WHEN_ORDER[b.when];
    return (wa === undefined ? 99 : wa) - (wb === undefined ? 99 : wb);
  });
}

function collectEffectBadges(allBadges, viewDate, creditRiskInfo) {
  // §II.5 — 카드 단위 통합. status_badges N건의 effect_badges[]를 합쳐 머지·dedup·정렬.
  // viewDate 인자는 향후 확장용 (현재 구현은 build_daily.py 산출 신뢰).
  // REQ-020c — creditRiskInfo (KRX 무관 신용 사유: 회사한도초과·ETF·SPAC·우선주 등) 합성 effect_badge 부착.
  //   {credit_risk: bool, credit_reason: string} 입력. 라벨 형식 통일 (cal-credit-badge → v95).
  //   KRX disclosure가 이미 credit-block 출력 시 dedup으로 자연 차단.
  if (!Array.isArray(allBadges)) return [];
  const allEffects = [];
  for (const b of allBadges) {
    if (!b) continue;
    const ebs = Array.isArray(b.effect_badges) ? b.effect_badges : [];
    for (const eb of ebs) {
      if (!eb) continue;
      // source_label 보강 (build_daily.py에서 부착됐지만 안전망)
      const enriched = { ...eb };
      if (!enriched.source_label && b.label) enriched.source_label = b.label;
      allEffects.push(enriched);
    }
  }
  // REQ-020c — KRX 무관 신용 사유 합성 effect_badge (today). dedup으로 KRX disclosure와 중복 자연 차단.
  if (creditRiskInfo && creditRiskInfo.credit_risk) {
    allEffects.push({
      effect: 'credit-block',
      when: 'today',
      severity: 'warning',
      source_label: creditRiskInfo.credit_reason || '신용 제한',
      source_kind: 'credit_risk',
    });
  }
  // 1. 머지 (A1)
  const merged = mergeEffectBadges(allEffects);
  // 2. dedup (P1 함정 #3) — credit_risk 합성도 KRX disclosure와 충돌 시 자연 1건 잔존
  const dedup = dedupEffectBadges(merged);
  // 3. 정렬 (A4)
  return sortEffectBadges(dedup);
}

function getNodeBoxText(node, badges, viewDate) {
  // v9.3 §I.3 그래프 노드 박스 하단 텍스트 산출. 매트릭스 4종 + state 4축 정합:
  //   미경험: '' (빈 칸)
  //   현재 (disclosure 진행중, state='current'): "X/X~X/XX · {효과}" 또는 효과 0건이면 "X/X~X/XX"
  //   필연 (disclosure 발효 D-1, state='upcoming'): "X/X · {효과}" (variant='upcoming')
  //   추정 임박 (predicted strict 충족, state='predicted-imminent'): "X/X · {효과}" (variant='predicted')
  //   추정 비임박 (predicted strict 미충족, state='predicted'): '' (시각만 점선 유지)
  //   기타: ''
  // 휴지 약한 명사형 정합 — 동사 0건. 효과 0건이면 effectText 빈 문자열.
  // v9.3 §IX 함정 #1 P0: 분기 4종 확장 의무 — predicted-imminent / upcoming 누락 시 박스 효과 빈 칸.
  if (!node) return { dateText: '', effectText: '', variant: 'empty' };
  const validStates = ['current', 'upcoming', 'predicted-imminent', 'predicted'];
  if (!validStates.includes(node.state)) {
    return { dateText: '', effectText: '', variant: 'empty' };
  }
  if (!Array.isArray(badges) || badges.length === 0) {
    return { dateText: '', effectText: '', variant: 'empty' };
  }
  // 노드 라벨에 매칭되는 배지 탐색 (current/upcoming=disclosure 우선, predicted=predicted)
  const label = node.label || '';
  const matchBadge = (b) => {
    const bl = b.label || '';
    if (bl === label) return true;
    if (bl.endsWith('근접')) {
      const stripped = bl.replace(/\s*근접\s*$/, '').trim();
      if (`${stripped} 예고` === label) return true;
      if (stripped === label) return true;
    }
    return bl.startsWith(label);
  };
  let badge = null;
  const isDisclosureNode = (node.state === 'current' || node.state === 'upcoming');
  const isPredictedNode = (node.state === 'predicted-imminent' || node.state === 'predicted');

  if (isDisclosureNode) {
    badge = badges.find(b => {
      const isPred = (b.source === 'predicted') || (b.label || '').includes('근접') || (b.label || '').includes('예상');
      return !isPred && matchBadge(b);
    });
  } else if (isPredictedNode) {
    badge = badges.find(b => {
      const isPred = (b.source === 'predicted') || (b.label || '').includes('근접') || (b.label || '').includes('예상');
      return isPred && matchBadge(b);
    });
  }
  if (!badge) return { dateText: '', effectText: '', variant: 'empty' };

  const today = viewDate || badge.view_date || '';
  const start = badge.start || '';
  const end = badge.end || '';

  // v9.3 §III.3: 단기과열 dayOffset 분기 — getAutoEffectsShort에 dayOffset 전달
  const isShortTerm = (badge.label || '').includes('단기과열') && !(badge.label || '').includes('예고') && !(badge.label || '').includes('근접');
  const dayOffset = isShortTerm ? getShortTermDayOffset(badge, today) : null;
  const effectText = getAutoEffectsShort(label, dayOffset);

  if (isDisclosureNode) {
    if (node.state === 'upcoming') {
      // v9.3 §I.3 — 필연 (D-1): "X/X · {효과}", variant='upcoming'
      if (start) return { dateText: dsnV9FormatMD(start), effectText, variant: 'upcoming' };
      return { dateText: '', effectText: '', variant: 'empty' };
    }
    // current
    // v9.3.2 §I (REQ-017 사이클 2 휴지 A=a): "예고" 단계 진행 중 (today<start)인 경우
    // 박스 날짜 = today (= viewDate, 예고 단계의 발효일). badge.start는 다음 단계(본 지정) 진입일이라 부정합.
    // viewDate fallback (P0 함정 차단): viewDate||badge.view_date||'' — 둘 다 부재 시 빈 박스 회귀.
    if (label.endsWith('예고') && start && today && today < start) {
      return { dateText: dsnV9FormatMD(today), effectText, variant: 'current' };
    }
    if (start && end && today >= start && today <= end) {
      return {
        dateText: `${dsnV9FormatMD(start)}~${dsnV9FormatMD(end)}`,
        effectText,
        variant: 'current'
      };
    }
    if (start) {
      return { dateText: dsnV9FormatMD(start), effectText, variant: 'current' };
    }
    return { dateText: '', effectText: '', variant: 'empty' };
  }

  // predicted 노드 (state=predicted-imminent or predicted)
  if (node.state === 'predicted') {
    // strict 미충족 — 점선 시각만, 박스 효과 빈 칸 (DSN-005 §I.5)
    return { dateText: '', effectText: '', variant: 'empty' };
  }
  // state='predicted-imminent' — strict 충족, 박스 효과 노출
  const ntd = badge.next_trading_day_for_predicted || '';
  if (!ntd) return { dateText: '', effectText: '', variant: 'empty' };
  return { dateText: dsnV9FormatMD(ntd), effectText, variant: 'predicted' };
}

function renderNodeBoxEffect(node, badges, viewDate) {
  // §I HTML 산출 — 박스 하단 영역. 빈 칸이면 영역 자체 비표시 (CSS .dsn-v92-stage-flow__node-effect--empty display:none).
  const info = getNodeBoxText(node, badges, viewDate);
  if (info.variant === 'empty' || (!info.dateText && !info.effectText)) {
    return '';
  }
  const variantCls = info.variant === 'current' ? ' dsn-v92-stage-flow__node-effect--current'
    : info.variant === 'upcoming' ? ' dsn-v92-stage-flow__node-effect--upcoming'
    : info.variant === 'predicted' ? ' dsn-v92-stage-flow__node-effect--predicted'
    : '';
  // 휴지 약한 명사형 — 가운뎃점 분리. 효과 비어있으면 날짜만.
  const inner = info.effectText
    ? `${escapeHtml(info.dateText)} · ${escapeHtml(info.effectText)}`
    : `${escapeHtml(info.dateText)}`;
  return `<div class="dsn-v92-stage-flow__node-effect${variantCls}">${inner}</div>`;
}

// REQ-019 §III (DSN-004 v9.4): 투자경고 예고 upcoming 노드용 진입 임계가 영역.
// 토구사 SSOT path 0~3 라벨 매핑. 신규 path 추가 시 이 매핑 동기화 책임 = design-lead.
const _DSN_V94_PATH_LABEL = {
  warn_surge_3d_100pct: '3일 전 종가 × 2.0',
  warn_surge_5d_60pct: '5일 전 종가 × 1.6',
  warn_surge_15d_100pct: '15일 전 종가 × 2.0',
  warn_repeated_caution: '15일 전 종가 × 1.75 (투자주의 반복)',
};
function dsnV94ShortPathLabel(via) {
  return _DSN_V94_PATH_LABEL[via] || (via ? `${via} (조건 미상)` : '');
}

function renderEntryWindow(badge) {
  // REQ-019 §III.3 — upcoming disclosure 5줄 임계가 박스.
  // 가드: 투자경고 예고 + source='disclosure' + start 존재 + entry_window_end 존재.
  // 가드 미통과 시 빈 문자열 (단기과열·current·predicted·predicted_shadow 영향 0건).
  if (!badge) return '';
  if (badge.label !== '투자경고 예고') return '';
  if (badge.source !== 'disclosure') return '';
  if (!badge.start || !badge.entry_window_end) return '';

  const startMD = dsnV9FormatMD(badge.start);
  const endMD = dsnV9FormatMD(badge.entry_window_end);
  const assumption = badge.entry_threshold_assumption || 'KOSPI 횡보 가정';

  // 줄 2 — 임계가 (path 0~3 산출 불가 시 fallback 텍스트)
  const hasPrice =
    typeof badge.entry_threshold_price === 'number' && badge.entry_threshold_price > 0;
  const thresholdLine = hasPrice
    ? `임계가: ${badge.entry_threshold_price.toLocaleString('ko-KR')}원 이상 (${escapeHtml(dsnV94ShortPathLabel(badge.entry_threshold_via))})`
    : '단기 임계가 산출 불가';

  // 줄 3 — 비가격 AND 조건 (산출 불가 시 생략)
  const andLine =
    hasPrice && badge.entry_threshold_and_condition
      ? `<div class="dsn-v94-stage-flow__entry-window-line--and">+ ${escapeHtml(badge.entry_threshold_and_condition)}</div>`
      : '';

  // 줄 4 — 고정 (path 4 KRX 비공개 잠재 경로 — 거짓 충실성 차단 의무)
  const path4Line = '+ 별도 비공개 조건(계좌관여율) 충족 시 진입 가능';

  return (
    `<div class="dsn-v94-stage-flow__entry-window">` +
    `<div class="dsn-v94-stage-flow__entry-window-line--primary">${escapeHtml(startMD)} ~ ${escapeHtml(endMD)} (10거래일)</div>` +
    `<div class="dsn-v94-stage-flow__entry-window-line--threshold">${thresholdLine}</div>` +
    andLine +
    `<div class="dsn-v94-stage-flow__entry-window-line--and">${path4Line}</div>` +
    `<div class="dsn-v94-stage-flow__entry-window-line--assumption">※ ${escapeHtml(assumption)}</div>` +
    `</div>`
  );
}

function _findUpcomingDisclosureBadge(node, badges) {
  // §III.3: upcoming 노드와 매칭되는 disclosure 카드 1건 찾기.
  // renderNodeBoxEffect의 매칭 로직과 동일 — '투자경고 예고' label + source='disclosure'.
  if (!Array.isArray(badges) || node.state !== 'upcoming') return null;
  const nodeLabel = String(node.label || '');
  return (
    badges.find((b) => {
      if (!b) return false;
      if (b.source !== 'disclosure') return false;
      const bl = String(b.label || '');
      // 노드 라벨 = "투자경고", badge.label = "투자경고 예고" → startsWith 매칭
      return bl.startsWith(nodeLabel) && bl.endsWith('예고');
    }) || null
  );
}

function renderStageFlowV9(badges, ctx) {
  // REQ-021 v9.6 §I.1 — 그래프 박스 통째 제거. 대표 19:28 KST 발화 ("시장경보 단계 흐름 제거").
  // 함수 본체·BEM CSS·entry-window(v9.4)·노드 박스 효과(v9.2)·단기과열 트랙·causal·current-state-summary = dead code 잔존.
  // 호출부(renderer.js:1122)에도 명시 빈 문자열 — 이중 가드 (회귀 안전).
  // dead code 정리는 후속 사이클 별도 REQ.
  return '';
  // ↓↓↓ 이하 dead code (사이클 후속 정리)
  // eslint-disable-next-line no-unreachable
  if (!Array.isArray(badges) || badges.length === 0) return '';
  const viewDate = (ctx && ctx.currentDate) || '';
  const flow = getStageFlow(badges, viewDate);
  const currentLine = getCurrentStateSummary(badges, viewDate);
  const causalLine = getCausalLine(badges);

  const renderNode = (node) => {
    // v9.3 §I — 노드 4축 위계 (current/upcoming/predicted-imminent/unvisited).
    // §IV: data-causal-from 부착 로직 제거 (CSS ::after ↘ 화살표 제거 정합).
    let cls = 'dsn-v9-stage-flow__node';
    if (node.state === 'current') cls += ' dsn-v9-stage-flow__node--current';
    else if (node.state === 'upcoming') cls += ' dsn-v9-stage-flow__node--upcoming';
    else if (node.state === 'predicted-imminent') cls += ' dsn-v9-stage-flow__node--predicted-imminent';
    else if (node.state === 'predicted') cls += ' dsn-v9-stage-flow__node--predicted';
    // v9.2 §I: 박스 하단 자동 효과 1줄 (current/upcoming/predicted-imminent 노드)
    const effectHtml = renderNodeBoxEffect(node, badges, viewDate);
    // REQ-019 §III: upcoming '투자경고' 노드에 entry_threshold 5줄 영역 부착
    const upcomingBadge = _findUpcomingDisclosureBadge(node, badges);
    const entryWindowHtml = upcomingBadge ? renderEntryWindow(upcomingBadge) : '';
    return `<span class="${cls}"><span class="dsn-v9-stage-flow__node-label">${escapeHtml(node.label)}</span>${effectHtml}${entryWindowHtml}</span>`;
  };
  const renderTrack = (nodes, modCls) => {
    const parts = [];
    nodes.forEach((n, i) => {
      parts.push(renderNode(n));
      if (i < nodes.length - 1) {
        parts.push('<span class="dsn-v9-stage-flow__arrow" aria-hidden="true">→</span>');
      }
    });
    return `<div class="dsn-v9-stage-flow__track ${modCls}">${parts.join('')}</div>`;
  };

  // REQ-018 §2 영역 1 (휴지 사이클 0 A=a) — 그래프 박스와 중복으로 둘 다 출력 차단.
  // currentLine/causalLine 변수 자체는 잔존 (회귀 시 1~2 라인 복원으로 즉시 부활).
  const currentLineHtml = '';
  const causalLineHtml = '';

  return `<section class="dsn-v9-stage-flow">
    ${currentLineHtml}
    ${causalLineHtml}
    <h5 class="dsn-v9-stage-flow__title">KRX 시장경보 단계 흐름</h5>
    ${renderTrack(flow.trackMain, 'dsn-v9-stage-flow__track--main')}
    <h5 class="dsn-v9-stage-flow__title dsn-v9-stage-flow__title--sub">단기과열 (별도 트랙)</h5>
    ${renderTrack(flow.trackShortTerm, 'dsn-v9-stage-flow__track--short-term')}
  </section>`;
}

function renderPredictedDetailOnly(badges, viewDate) {
  // REQ-018 §2 영역 2 (휴지 사이클 0 B=a) — 통째 제거.
  // 그래프 predicted 노드와 중복 + strict 미충족은 그래프 unvisited로 정합 처리.
  // 호출 측(renderer.js:1120) 미수정 — 빈 문자열 반환으로 자연 차단. dead code 잔존(향후 재활용 1줄 제거로 부활).
  return '';
  // ↓↓↓ 이하 dead code 잔존 (REQ-018 사이클 2)
  // eslint-disable-next-line no-unreachable
  if (!Array.isArray(badges) || badges.length === 0) return '';
  const detailOnlyBadges = badges.filter(b => {
    if (!b) return false;
    const isPred = (b.source === 'predicted')
      || (b.label || '').includes('근접')
      || (b.label || '').includes('예상');
    if (!isPred) return false;
    return getPredictedBadgeVisibility(b, viewDate, badges) === 'detail-only';
  });
  if (detailOnlyBadges.length === 0) return '';
  const items = detailOnlyBadges.map(b => {
    const chip = `<span class="dsn-v8-tense-chip dsn-v8-tense-chip--predicted">[예측 진입]</span>`;
    return `<li class="dsn-v92-predicted-detail-only__item">${chip} ${escapeHtml(b.label || '')}</li>`;
  }).join('');
  return `<div class="dsn-v92-predicted-detail-only">`
    + `<div class="dsn-v92-predicted-detail-only__title">추정 시그널 (KRX 미공식 · 자체 추정)</div>`
    + `<ul class="dsn-v92-predicted-detail-only__list">${items}</ul>`
    + `</div>`;
}

function renderTriggerPin(_badges, _viewDate) {
  // REQ-064 (2026-04-28): "추정 N건" 트리거 핀 제거 — 라벨 모호성 (대표 라이브 발화).
  // 함수 시그니처는 유지하여 호출자 안전성 보존. predicted 데이터 자체는 보존 (본문 펼침 영역).
  // 이전 구현: DSN-005 v9.2 §III 옵션 1-a (commit f3c0da7).
  return '';
}

function miniCandle(open, high, low, close, changePct, scaleLo, scaleHi) {
  // Q-20260515-CANDLE-ALG-UNIFY (lead-direct): scaleLo/scaleHi 제공 시 20일 normalize 사용 (buildCandles20 정합).
  // 미제공 시 self-zoom fallback (기존 동작 유지, 후방 호환).
  if (!close) return '';
  var W = 12, H = 24;
  // Q-20260519-CYCLE12-JOLSS (2026-05-19): open=null fallback = close (LU 점상 대응).
  //   build_daily.py Fix C/D stale 오판 회피 후에도 cron 미반영 데이터 cross-source
  //   유실 가능 → 방어 코드 (open NULL이지만 high/low/close 정합 케이스 처리).
  //   본질: hasOHLC 판정에서 open이 NULL이어도 high/low/close 정합 시 OHLC 분기 활성.
  //   대표 본질 룰 (2026-05-19 17:30): "특정 종목 hard-code 금지, 전체 로직 반영".
  if (open == null && high && low && close) {
    open = close;
  }
  var hasOHLC = open && high && low;
  // OHLC 없으면 pct 기반 단순 바 (심지 없음, 높이=pct 비례)
  if (!hasOHLC) {
    if (changePct == null) return '';
    var isUp = changePct >= 0;
    var color = isUp ? '#E03131' : '#1971C2';
    // 등락률 절대값에 비례: 1%=2px, 30%=24px (최대), 최소 3px
    var bodyH = Math.max(3, Math.min(H, Math.abs(changePct) * 0.8));
    var bodyTop = isUp ? (H - bodyH) : 0;
    return '<svg width="'+W+'" height="'+H+'" style="vertical-align:middle">' +
      '<rect x="2" y="'+bodyTop+'" width="8" height="'+bodyH+'" fill="'+color+'" rx="1"/></svg>';
  }
  // Q-20260515-CANDLE-COLOR-UNIFY: 색상/판정 로직을 lib/mini-candle.js buildCandles20과 통일 (대표 catch 23:34)
  // 이전: isUp = (close >= open) + #E03131/#1971C2 — close=open 시 양봉 처리
  // 통일: isUp = (close > open) + isFlat (close === open) + #C53939/#1958C7/#94A3B8 (sparkline 정합)
  var isUp = (close > open);
  var isFlat = (close === open);
  // Q-20260519-CYCLE12-JOLSS (2026-05-19): LU 점상 (changePct>=29.0) 시 빨강 (#C53939)
  //   강제. 졸스 등 LU 점상 종목 isFlat 회색 처리 cycle3 본질 catch 동형 (대표 발화
  //   "OHLC 모두 같은 값이라 '-' 모양이 되어야 하는데 '|' 모양 표시").
  //   LU/LD 점상은 회색 (#94A3B8) 정합 X — 등락 방향 명시 (빨강/파랑) 정합.
  if (isFlat && changePct != null) {
    if (changePct >= 29.0) {
      isUp = true; isFlat = false;
    } else if (changePct <= -29.0) {
      isUp = false; isFlat = false;
    }
  }
  var color = isFlat ? '#94A3B8' : (isUp ? '#C53939' : '#1958C7');
  // scaleLo/scaleHi 유효 (양수 + lo<hi) 시 20일 normalize, 아니면 self-zoom
  var useScale = (typeof scaleLo === 'number' && typeof scaleHi === 'number' && scaleLo > 0 && scaleHi > scaleLo);
  var lo = useScale ? scaleLo : low;
  var hi = useScale ? scaleHi : high;
  var range = hi - lo;
  // Q-20260519-CYCLE12-JOLSS (2026-05-19): range=0 시 가로선 '-' (수평선) 출력.
  //   대표 catch (17:25 KST): "OHLC가 모두 같은 값이라 '-' 모양이 되어야 하는데
  //   '|' 모양으로 표시되고 있다" — 본질 결함 (이전 코드: 세로선 stroke (6,0)→(6,H)).
  //   가로선 = (0, H/2) → (W, H/2) 수평선. 색상은 위 isUp/isFlat 분기 정합.
  if (range === 0) {
    var midY = H / 2;
    return '<svg width="'+W+'" height="'+H+'" style="vertical-align:middle">' +
      '<line x1="0" y1="'+midY+'" x2="'+W+'" y2="'+midY+'" stroke="'+color+'" stroke-width="2"/></svg>';
  }
  var scale = H / range;
  var yWickTop = (hi - high) * scale;
  var yWickBot = (hi - low) * scale;
  var bodyTop = (hi - Math.max(open, close)) * scale;
  var bodyBot = (hi - Math.min(open, close)) * scale;
  var bodyH = Math.max(bodyBot - bodyTop, 1);
  return '<svg width="'+W+'" height="'+H+'" style="vertical-align:middle">' +
    '<line x1="6" y1="'+yWickTop.toFixed(1)+'" x2="6" y2="'+yWickBot.toFixed(1)+'" stroke="'+color+'" stroke-width="1"/>' +
    '<rect x="2" y="'+bodyTop.toFixed(1)+'" width="8" height="'+bodyH.toFixed(1)+'" fill="'+color+'" rx="1"/>' +
    '</svg>';
}

/* ───── REQ-021 v9.6 — 신용불가 사유 박스 (DSN-007 §II + §IV) ─────
   대표 19:28 KST 발화: "지정조건상세 대신 신용불가 사유를 간단·확실하게."
   §II.2 텍스트 매트릭스 18 (KRX 단계 + 증권사 사유) + 영업일 SSOT (getNextTradingDay·getShortTermDayOffset).
   FLR-AGT-002 정합 — predicted strict 충족 사유 박스 미노출 (헤더 "(내일 가능)" 배지로만 표현).
   P0 차단 — 영업일 헬퍼 SSOT (4/30·5/4·5/8 등 휴장일 인접 케이스 정합).
*/

// §II.5 sev 색 토큰 매핑 (KRX 단계 → sev)
function _v96MapKrxSev(label) {
  if (!label) return 'caution';
  // 매매거래정지·투자위험 → danger
  if (label === '매매거래정지') return 'danger';
  if (label.startsWith('투자위험')) return 'danger';  // 투자위험·투자위험 예고
  // 단기과열 (D+0~D+5) → hot. 예고는 caution (§II.2 #10)
  if (label.includes('단기과열')) {
    if (label.includes('예고')) return 'caution';
    return 'hot';
  }
  // 투자경고 예고·투자주의 → caution. 투자경고 → warning
  if (label.startsWith('투자경고') && !label.includes('예고')) return 'warning';
  if (label.startsWith('투자경고') && label.includes('예고')) return 'caution';
  if (label.startsWith('투자주의') && !label.includes('예고')) return 'caution';
  if (label.startsWith('투자주의')) return 'caution';
  return 'caution';
}

// §II.2 단기과열 D 매트릭스 → 효과 텍스트 (#6~#9)
function _v96ShortTermEffectByOffset(offset) {
  // §II.2 #6 D+0: "D+2 거래정지·D+3~D+5 단일가"
  // §II.2 #7 D+1: "D+2 거래정지 예정"
  // §II.2 #8 D+2: "D+3~D+5 단일가매매"
  // §II.2 #9 D+3-5: "~D+5"
  if (offset === 'd+0') return 'D+2 거래정지·D+3~D+5 단일가';
  if (offset === 'd+1') return 'D+2 거래정지 예정';
  if (offset === 'd+2') return 'D+3~D+5 단일가매매';
  if (offset === 'd+3-5') return '~D+5';
  return '';
}

// §II.3 산출 로직. P0 차단: getNextTradingDay SSOT 활용 (영업일 휴장일 인접 정합).
function computeCreditBlockReason(badges, viewDate, creditRiskInfo) {
  const rows = [];

  // 1. KRX disclosure 행 (predicted 제외)
  const krxBadges = (badges || []).filter(b => {
    if (!b) return false;
    if (b.source !== 'disclosure') return false;
    return true;
  });

  for (const b of krxBadges) {
    const label = String(b.label || '');
    if (!label) continue;
    const start = b.start || '';
    const end = b.end || '';

    const isPrenotice = label.includes('예고');
    const sev = _v96MapKrxSev(label);

    // §II.2 #6~#9 단기과열 케이스 (예고 제외) — dayOffset 표기
    const isShortTerm = label.includes('단기과열') && !isPrenotice;

    let stageText = '';
    let extra = '';
    let period = '';

    if (isShortTerm) {
      // §II.2 #6~#9 — D+N 표기 + 효과 (P0: getShortTermDayOffset SSOT 영업일 계산)
      const offset = (typeof getShortTermDayOffset === 'function')
        ? getShortTermDayOffset(b, viewDate)
        : 'unknown';
      const effectText = _v96ShortTermEffectByOffset(offset);
      if (offset === 'd+0') {
        stageText = `${label} 지정`;
        if (start) period = `${dsnV9FormatMD(start)} D+0`;
      } else if (offset === 'd+1') {
        stageText = `${label} 지정`;
        period = 'D+1';
      } else if (offset === 'd+2') {
        stageText = `${label} 지정`;
        period = 'D+2 거래정지';
      } else if (offset === 'd+3-5') {
        stageText = `${label} 단일가매매`;
        period = 'D+3 ~ D+5';
      } else {
        // d+6+ 또는 unknown — 효과 부재. 박스 노출 가치 낮음. 기본 표기로 폴백.
        stageText = `${label} 지정`;
        if (start && end && start !== end) period = `${dsnV9FormatMD(start)} ~ ${dsnV9FormatMD(end)}`;
        else if (start) period = `${dsnV9FormatMD(start)} 발효`;
      }
      if (effectText) extra = ` — ${effectText}`;
    } else if (isPrenotice) {
      // §II.2 #2·#4·#10 — 예고. start = 발효 예정일.
      // P0 차단: viewDate 인접일 검증 (getNextTradingDay) — start === getNextTradingDay(viewDate)면 D-1 단정.
      stageText = label;
      if (start) period = `${dsnV9FormatMD(start)} 발효 예정`;
      // 투자위험 예고 효과 명시 (#4)
      if (label.startsWith('투자위험')) extra = ' — 매매거래정지 동반';
      // REQ-022 v9.7 §V — 단기과열 예고 (#10): build_daily.py 사전 산출 reason_text 우선 사용.
      // SSOT는 build_daily.py _add_trading_days (영업일 헬퍼). utils.js는 표시만 (P1 함정 #4 (b)).
      // REQ-067 — reason_text는 라벨 칩이 아닌 period(시점 텍스트)로 분리. 칩은 짧은 카테고리 유지.
      // reason_text가 "라벨 — 시점" 형태인 경우 라벨 prefix 제거하여 칩과 중복 제거.
      if (label.includes('단기과열')) {
        if (b.short_overheat_reason_text) {
          let txt = b.short_overheat_reason_text;
          // "단기과열 예고 — 4/28 지정 가능" → "4/28 지정 가능"
          const prefix = label + ' — ';
          if (txt.startsWith(prefix)) txt = txt.slice(prefix.length);
          period = txt;
          extra = '';
        } else {
          // fallback — 사전 산출 없으면 v9.6 base 텍스트 잔존
          period = '10거래일 이내 조건 충족 시 지정';
        }
      }
    } else {
      // §II.2 #1·#3·#5·#18 — 현재 발효 중 (지정 중)
      // §II.2 #5 매매거래정지: end가 비어있어도 단일일 표기
      stageText = `${label} 지정 중`;
      if (start && end && start !== end) period = `${dsnV9FormatMD(start)} ~ ${dsnV9FormatMD(end)}`;
      else if (start && end && start === end) period = `${dsnV9FormatMD(start)}, 1거래일`;
      else if (start) period = `${dsnV9FormatMD(start)}`;
      // §II.2 #3 투자위험 효과
      if (label.startsWith('투자위험')) extra = ' — 신용금지·매매거래정지';
    }

    // REQ-027 §C — 단기과열 예고 등 KRX 공시 링크 (SPEC-001 §V.3).
    // build_daily.py에서 status_badge.disclosure_url 또는 dart_url 부착 시 사유 박스에 공시 링크 노출.
    // REQ-063 §A — disclosure_title(raw_title) 부착 시 링크 텍스트로 풀 제목 사용 (cal-disc-item 정합).
    rows.push({
      label: stageText + extra,
      period,
      sev,
      source: 'krx_disclosure',
      url: b.disclosure_url || b.dart_url || '',
      title: b.disclosure_title || '',
    });
  }

  // §II.4 정렬 — severity desc (danger > warning > caution > hot)
  const _SEV_ORDER = { danger: 0, warning: 1, caution: 2, hot: 3, credit: 4 };
  rows.sort((a, b) => (_SEV_ORDER[a.sev] ?? 9) - (_SEV_ORDER[b.sev] ?? 9));

  // 2. 증권사 사유 행 (creditRiskInfo)
  // REQ-032 §A1 — creditReasonGloss 풀이 매핑 (대표 본질 비판: 컨텍스트 부재).
  // REQ-027 §B에서 폐기한 "익일 재평가 후 회복 가능" 등 추정·예측 단어는 0건 유지.
  // 본 매핑은 추정 단어 0건 + 현재 사실 풀이만 (FLR-AGT-002 정합).
  if (creditRiskInfo && creditRiskInfo.credit_risk && creditRiskInfo.credit_reason) {
    const reason = (typeof sanitize === 'function') ? sanitize(creditRiskInfo.credit_reason) : String(creditRiskInfo.credit_reason);
    // REQ-067 — 칩(label)은 짧은 reason만, 풀이 텍스트는 title(본문 summary)로 분리.
    // _glossCreditReason 결과 "투자경고 단계 도달 — 신용 불가"가 칩에 들어가는 회귀 차단.
    const gloss = _glossCreditReason(reason);
    let chipLabel = reason;
    let summaryText = '';
    if (gloss && gloss !== reason) {
      // "투자경고 단계 도달 — 신용 불가" → summary = "투자경고 단계 도달 — 신용 불가"
      // 칩은 원본 reason 유지 (짧은 카테고리)
      summaryText = gloss;
    }
    rows.push({
      label: chipLabel,
      title: summaryText,
      period: '',
      sev: 'credit',
      source: 'broker',
    });
  }

  // 3. predicted strict 충족 행 = 사유 박스 미노출 (§II.2 #17, FLR-AGT-002 정합)
  // 헤더 "(내일 가능)" 배지로만 표현. 의도적 미산출.

  // §II.4 최대 노출 N=3 (실측 frequency 낮음 — 후속 사이클 cropping 검토)
  const MAX_ROWS = 3;
  return rows.slice(0, MAX_ROWS);
}

// REQ-032 §A1 — credit_reason 라벨 풀이 매핑 (SPEC-001 §IV.1).
// 추정·예측 단어 0건 + 현재 사실 풀이만 (FLR-AGT-002 정합).
// 토구사 추가 검증 후 확장 가능.
const _CREDIT_REASON_GLOSS = {
  '투자경고': '투자경고 단계 도달 — 신용 불가',
  '회사한도초과': '회사한도초과 — 신용 한도 소진',
  'ETF': 'ETF — 신용거래 대상 외',
  'ETN': 'ETN — 신용거래 대상 외',
  'ETF/ETN': 'ETF/ETN — 신용거래 대상 외',
  '스팩(SPAC)': 'SPAC — 합병 전 신용거래 제한',
  'SPAC': 'SPAC — 합병 전 신용거래 제한',
  '우선주': '우선주 — 신용거래 제한 (일부 증권사 가능)',
  '신용거래 제한 종목': '신용거래 제한 — 증권사 자체 기준',
  '신용융자 불가': '신용융자 불가 — kt20017 단건 조회',
  // REQ-046 — SK증권 케이스 (대표 발화 2026-04-28 02:38 KST). FLR-AGT-002 정합 (현재 사실 풀이만).
  '매매거래정지': '매매거래정지 — KRX 거래 중지로 신용 불가',
  '거래정지': '거래정지 — KRX 거래 중지로 신용 불가',
};

function _glossCreditReason(reason) {
  if (!reason) return '';
  return _CREDIT_REASON_GLOSS[reason] || reason;  // 미매핑 시 원본 (안전 fallback)
}

// REQ-034 §C — 사유 박스 행 1건 렌더 (박스 컨테이너 폐기, inline 행으로 재설계).
// REQ-067 §A — KRX 시장경보 박스를 CB 박스(cal-disc-item)와 동일 패턴으로 통일:
//   단일 a 태그가 row 전체 wrap (라벨 칩 + 요약 + 우측 ↗). 분리 행/중복 제거.
//   url 부재 시 평문 div 유지 (predicted source 등 a 태그 미생성 가드 — REQ-066 정합).
// BEM: cal-credit-row + __label / __period / __link 유지 (CSS sev-* 색상·dot 칩으로 재해석).
function _renderCreditBlockRow(r) {
  const labelHtml = `<span class="cal-credit-row__label sev-${escapeHtml(r.sev)}">${escapeHtml(r.label)}</span>`;
  const periodHtml = r.period
    ? `<span class="cal-credit-row__period">${escapeHtml(r.period)}</span>`
    : '';
  // REQ-067 — 요약 본문 1회만 노출. r.title(공시 풀 제목)을 본문으로 사용. 부재 시 r.label로 폴백.
  const summaryText = r.title ? r.title : '';
  const extSvg = `<svg class="cal-credit-row__ext" width="10" height="10" viewBox="0 0 10 10" aria-hidden="true"><path d="M3 1h6v6M9 1L4 6" stroke="currentColor" stroke-width="1.2" fill="none"/></svg>`;
  const summaryHtml = summaryText
    ? `<span class="cal-credit-row__summary">${escapeHtml(summaryText)}</span>`
    : '';
  if (r.url) {
    // 글박스 전체 a 태그 wrap. 라벨 칩 + (요약 본문) + 시점 + 우측 ↗.
    return `<a class="cal-credit-row cal-credit-row--link" href="${escapeHtml(r.url)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(r.title || r.label)}">${labelHtml}${summaryHtml}${periodHtml}${extSvg}</a>`;
  }
  // url 부재 — 평문 div (predicted source · broker 사유 · disclosure_url 누락 fallback).
  // broker 사유 등은 summary(풀이 텍스트)도 함께 노출.
  return `<div class="cal-credit-row">${labelHtml}${summaryHtml}${periodHtml}</div>`;
}

// §IV.1 박스 N건 출력. §III BEM 재활용 + cal-status-detail--reason modifier 1개 신규.
// REQ-027 §C — KRX 공시 링크 칩 추가 (SPEC-001 §V.3).
// REQ-032 §A — 출처 그룹 분리 (KRX 공시 vs 증권사 사유) + 그룹 헤더 노출.
function renderCreditBlockReasonBox(badges, viewDate, creditRiskInfo) {
  const rows = computeCreditBlockReason(badges, viewDate, creditRiskInfo);
  if (rows.length === 0) return '';

  const krxRows = rows.filter(r => r.source === 'krx_disclosure');
  const brokerRows = rows.filter(r => r.source === 'broker');

  let html = '';
  if (krxRows.length > 0) {
    html += `<div class="cal-credit-group cal-credit-group--krx">`
      + `<div class="cal-credit-group__heading">KRX 시장경보 <span class="cal-credit-group__source">(한국거래소 공시)</span></div>`
      + krxRows.map(_renderCreditBlockRow).join('')
      + `</div>`;
  }
  if (brokerRows.length > 0) {
    html += `<div class="cal-credit-group cal-credit-group--brokerage">`
      + `<div class="cal-credit-group__heading">신용거래 제한 사유 <span class="cal-credit-group__source">(증권사 통지)</span></div>`
      + brokerRows.map(_renderCreditBlockRow).join('')
      + `</div>`;
  }
  return html;
}
