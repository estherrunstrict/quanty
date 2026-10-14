/* Peer strategy rows for display only; never merge these into account data. */
function hfaiStrategyRows(strategies) {
  const rows = [...(strategies || [])];
  if (!rows.some(s => s.id === 'toss_hfai')) {
    const manual = rows.findIndex(s => s.id === 'manual');
    rows.splice(manual < 0 ? rows.length : manual, 0, {id: 'toss_hfai', name: 'Toss 13F · AI'});
  }
  return rows;
}
function hfaiState(data, now = Date.now()) {
  if (!data || data.schema_version !== 1 || !['paper', 'shadow'].includes(data.mode)) return 'UNAVAILABLE';
  const age = now - Date.parse(data.updated_at);
  if (!Number.isFinite(age) || age < -60000 || age > 20 * 60 * 1000) return 'STALE';
  if (data.status === 'DATA_BLOCKED' && data.data_status === 'INPUT_MISSING') return 'INPUT_MISSING';
  return data.status || 'UNAVAILABLE';
}
function renderHfai(data, parent) {
  const labels = {
    UNAVAILABLE: '상태 확인 불가', STALE: '상태 갱신 지연', AUTH_REQUIRED: '모델 인증 대기',
    AUTH_UNVERIFIED: '인증 확인 대기', AUTH_FAILED: '인증 실패', CONFIG_INVALID: '인증 설정 확인 필요',
    CONNECTION_FAILED: '모델 연결 실패', RESPONSE_INVALID: '모델 응답 검증 실패', CONNECTED: '모델 응답 확인',
    REAUTH_REQUIRED: 'ChatGPT 재로그인 필요', PLAN_PERMISSION_REQUIRED: '구독 사용 권한 승인 필요',
    PLAN_LIMITED: 'ChatGPT 구독 사용 한도 도달', PLAN_UNAVAILABLE: 'ChatGPT 구독 사용 불가',
    MODEL_SELECTION_REQUIRED: '구독 모델 조회 대기',
    QUOTA_REQUIRED: 'API 크레딧·사용 한도 확인 필요', API_LIMITED: 'API 호출 제한', MODEL_BLOCKED: '모델 호출 대기',
    INPUT_MISSING: '13F·시세 입력 대기', DATA_BLOCKED: '입력 검증 미통과', READY: '게시 시점 입력 검증 통과',
    WAITING_SCHEDULE: '다음 분석 일정 대기', EVALUATING: 'AI 분석 중', EXIT_PENDING: '모의 청산 자료 대기',
    HALTED_FLAT: '손실 기준 운용 중단', EVALUATION_BLOCKED: '판단 검증 미통과',
    SIMULATED: '모의 반영 완료', SHADOW: '관찰 판단 기록', RESEARCH_ONLY: '장외 연구 판단 기록',
    INTERRUPTED_OR_RUNNING: '분석 완료 미확인'
  };
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const label = v => labels[v] || '미확인';
  const state = hfaiState(data);
  const d = state === 'UNAVAILABLE' ? {} : data;
  const money = v => typeof v === 'number' && Number.isFinite(v)
    ? (VIEW.amounts ? Math.round(v).toLocaleString('ko-KR') + '원'
      : d.paper_capital_krw > 0 ? (v / d.paper_capital_krw * 100).toFixed(1) + '%' : '미확인') : '미확인';
  const at = v => Number.isFinite(Date.parse(v))
    ? new Date(v).toLocaleString('ko-KR', {timeZone:'Asia/Seoul', hour12:false}) + ' KST' : '없음';
  const node = document.createElement('article');
  node.className = 'strat hfai'; node.id = 'hfai';
  node.setAttribute('aria-labelledby', 'hfai-name');
  const modeTag = ['paper', 'shadow'].includes(d.mode) ? 'paper' : 'stale';
  const metric = (title, value) => `<div><div class="sg-lbl">${title}</div><div class="sg-val">${value}</div></div>`;
  node.innerHTML = `<div class="strat-top"><span class="strat-name" id="hfai-name"><span class="bot-dot"></span>Toss 13F · AI</span>
    <span class="tag ${modeTag}">${esc(d.mode?.toUpperCase() || '상태 미확인')}</span></div>
    <div class="strat-desc"><span class="strat-tag">13F · AI 제안 / 검토</span>기관 공시와 기업 자료를 분석하고 AI 제안·반론을 검토하는 전략입니다. 현재 모의 운용이며 실제 주문은 연결되지 않았습니다.</div>
    <div class="strat-grid">
      ${metric('모의 시작자금', money(d.paper_capital_krw))}
      ${metric('손실 중단 기준', money(d.loss_stop_krw))}
      ${metric('모의 누적 손익', money(d.paper_pnl_krw))}
      ${metric('모의 보유 종목', Number.isInteger(d.paper_position_count) && d.paper_position_count >= 0 ? d.paper_position_count + '개' : '미확인')}
      ${metric('실주문', d.real_orders_sent === 0 ? '0건' : '미확인')}
      ${metric('분석 후보', Number.isInteger(d.candidate_count) && d.candidate_count >= 0 ? d.candidate_count + '개' : '미확인')}
    </div>
    <div class="sig ${['UNAVAILABLE','STALE','DATA_BLOCKED','MODEL_BLOCKED','HALTED_FLAT','EXIT_PENDING'].includes(state) ? 'bear' : 'cash'} hfai-state">${esc(label(state))}</div>
    <div class="hfai-readiness"><p><b>모델</b> ${esc(d.model?.provider || '제공자 미확인')} · ${esc(d.model?.name || '미설정')} · ${esc(label(d.model?.status))}
    <small>인증 점검 시각: ${esc(at(d.model?.checked_at))}</small></p>
    <p><b>데이터</b> ${esc(label(d.data_status))}
    <small>시세 기준: ${esc(at(d.snapshot_at))}</small></p>
    <p><b>AI 제안·검토 완료</b> ${esc(at(d.model_completed_at))}
    <small>최근 평가: ${esc(d.last_evaluation ? label(d.last_evaluation.status) : '실행 기록 없음')}</small></p></div>
    <p class="guide-footnote">상태 기준 ${esc(at(d.updated_at))} · ${VIEW.amounts ? '금액 단위: 원' : '비율 기준: 이 봇의 모의 시작자금'}<br>모의자금·손익은 실제 계좌 합계에서 제외됩니다. 5분 간격 게시 · 20분 이상 미갱신 시 지연 표시.<br>연 100%는 검증된 수익률이 아닌 실험 목표입니다. 손실 중단 기준은 손실 한도 보장이 아닙니다.</p>`;
  parent.appendChild(node);
  return node;
}
function renderHfaiOperation(data) {
  const parent = document.querySelector('#operations .guide-operation');
  if (!parent) return;
  const node = document.createElement('a');
  node.href = '#hfai';
  node.className = 'hfai-operation';
  node.addEventListener('click', () => document.querySelector('.mob-tab[data-tab="strats"]')?.click());
  const state = hfaiState(data);
  const mode = state === 'UNAVAILABLE' ? '상태 미확인' : data.mode === 'shadow' ? '관찰' : '페이퍼';
  node.textContent = `toss_hfai · ${mode}${state === 'STALE' ? ' · 갱신 지연' : ''}`;
  parent.appendChild(node);
}
