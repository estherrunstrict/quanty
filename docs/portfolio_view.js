/* Guidance is presentation-only: this page has no order submission path. */
function guideText(value) {
  return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function guideSource(url) {
  try { const u = new URL(url); return u.protocol === 'https:' ? u.href : ''; } catch { return ''; }
}
function renderInvestmentPanels(data) {
  const guide = data.investment_guidance;
  if (!guide) return;
  const app = document.getElementById('app');
  const section = (id, title, note) => {
    const node = document.createElement('section'); node.className = 'guide-section'; node.id = id;
    node.innerHTML = `<div class="guide-heading"><h2>${guideText(title)}</h2><span>${guideText(note)}</span></div>`;
    return node;
  };
  const fmt = v => typeof v === 'number' && Number.isFinite(v) ? v.toFixed(1) + '%' : '미확인';
  const actions = section('actions', '이번 주 운용 메모', '장기 복리 성장 · 큰 손실 제한');
  actions.innerHTML += '<div class="guide-actions">' + (guide.next_actions || []).map((s,i) =>
    `<div class="guide-action"><div class="guide-step">0${i+1} / NEXT ACTION</div><p>${guideText(s)}</p></div>`).join('') + '</div>';
  app.appendChild(actions);
  const watch = section('watch', '회복을 관찰하는 자산', '자동청산 없음 · 다음 결정까지 보유');
  watch.innerHTML += `<div class="guide-watch"><p class="watch-policy">${guideText(guide.residual_decision)}. 회복 수치는 계좌의 현지 통화 평균 원가 기준이며 세금·환율·비용은 별도입니다.</p>` +
    `<div class="guide-table-wrap"><table class="guide-table"><thead><tr><th>자산 / 기존 봇</th><th>실보유</th><th>평가액</th><th>원가 회복까지</th><th>운용 결정</th></tr></thead><tbody>` +
    (guide.watch || []).map(r => `<tr><td>${guideText(r.ticker)}<small>${guideText(r.owner)}</small></td><td>${guideText(r.quantity)}주</td><td>${won(r.value_krw)}</td><td>${fmt(r.recovery_needed_pct)}<small>${r.recovery_needed_pct === 0 ? '현지 통화 원가 도달' : '상승 필요율'}</small></td><td>${guideText(r.decision)}</td></tr>`).join('') +
    `</tbody></table></div>${guide.watch_quality !== 'verified' ? '<p class="guide-footnote">계좌 조회가 미확인입니다. 보유가 없다는 뜻이 아닙니다.</p>' : ''}</div>`;
  app.appendChild(watch);
  const manual = section('manual-guide', '수동 투자 자산의 다음 판단', '현재 비중에서 출발한 제안 · 직접 검토 후 실행');
  manual.innerHTML += '<div class="guide-manual">' + (guide.manual || []).map(r =>
    `<article class="guide-card"><div class="guide-card-header"><h3>${guideText(r.ticker)}<small>${guideText(r.name)}</small></h3><div class="guide-weight">${fmt(r.weight_pct)}<small>전체 자산 비중</small></div></div>` +
    `<span class="guide-badge">${guideText(r.direction)}</span><p class="guide-proposal">${guideText(r.proposal)}</p><p class="guide-timing">${guideText(r.timing)}</p>` +
    `<p class="guide-basis">${guideText(r.basis)}${r.source_review_overdue ? ' · 근거 갱신 필요' : ''}</p>` +
    `<div class="guide-source"><a href="${guideText(guideSource(r.source_url))}" target="_blank" rel="noopener noreferrer">공식 자료 ↗</a><span>자료 검토 ${guideText(r.sources_reviewed_at)} · 재검토 ${guideText(r.review_by)}</span></div></article>`).join('') + '</div>' +
    '<p class="guide-footnote">제안은 자동 주문과 연결되지 않습니다. 날짜는 검토 시점이며 매수·매도 가격을 예측하지 않습니다. 공시 전 미래 실적은 미확인입니다.</p>';
  app.appendChild(manual);
  const ops = section('operations', '운영 중인 실행 경로', '실거래 · 관찰 · 페이퍼의 상태를 구분');
  ops.innerHTML += '<div class="guide-operation">' + (guide.operations || []).map(r =>
    `<span data-mode="${guideText(r.mode)}" title="${guideText(r.reason)}">${guideText(r.bot)} · ${r.mode === 'live' ? '조건 확인 후 실행' : r.mode === 'observe' ? '보유·관찰' : '페이퍼'}</span>`).join('') +
    `</div><p class="guide-footnote">자산배분: ${guide.allocation?.status === 'withheld' ? '보류' : '검증된 변경만 적용'} · ${guideText(guide.allocation?.reason)}</p>`;
  app.appendChild(ops);
}
