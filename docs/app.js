// Quanty Dashboard — app.js

const DATA_URL = 'data/dashboard_data.json';
const HISTORY_URL = 'data/equity_history.json';

const COLORS = [
  '#58a6ff', '#3fb950', '#f85149', '#d29922',
  '#bc8cff', '#f0883e', '#79c0ff', '#56d364',
];

// Format helpers
const fmtKRW = (v) => '₩' + Math.round(v).toLocaleString();
const fmtUSD = (v) => '$' + v.toLocaleString(undefined, {minimumFractionDigits: 0, maximumFractionDigits: 0});
const fmtPct = (v) => (v >= 0 ? '+' : '') + v.toFixed(1) + '%';
const fmtCurrency = (v, cur) => cur === 'KRW' ? fmtKRW(v) : fmtUSD(v);

function pctClass(v) { return v >= 0 ? 'positive' : 'negative'; }

function badge(text, type) {
  const cls = type === 'bull' ? 'badge-bull' :
              type === 'bear' ? 'badge-bear' :
              type === 'cash' ? 'badge-cash' : 'badge-neutral';
  return `<span class="badge ${cls}">${text}</span>`;
}

function inferBadge(status) {
  const s = (status || '').toUpperCase();
  if (s.includes('BULL')) return badge(status, 'bull');
  if (s.includes('BEAR')) return badge(status, 'bear');
  if (s.includes('CASH') || s.includes('HOLD')) return badge(status, 'cash');
  return badge(status, 'neutral');
}

// Load data and render
async function init() {
  try {
    const [dataResp, histResp] = await Promise.all([
      fetch(DATA_URL),
      fetch(HISTORY_URL).catch(() => null),
    ]);

    if (!dataResp.ok) {
      document.getElementById('last-updated').textContent = 'No data yet — waiting for first daily update from server';
      return;
    }

    const data = await dataResp.json();
    const history = histResp && histResp.ok ? await histResp.json() : null;

    renderOverview(data);
    renderStrategyTable(data);
    renderDetailCards(data);
    renderAllocationChart(data);
    renderMarketChart(data);

    if (history) {
      renderEquityChart(history);
      renderDrawdownChart(history);
    } else {
      document.getElementById('equity-chart').parentElement.innerHTML =
        '<p style="color:var(--text-dim);text-align:center;padding:40px">Equity history will appear after 2+ daily updates</p>';
      document.getElementById('drawdown-chart').parentElement.innerHTML =
        '<p style="color:var(--text-dim);text-align:center;padding:40px">Drawdown chart will appear after 2+ daily updates</p>';
    }

  } catch (err) {
    console.error('Failed to load dashboard data:', err);
    document.getElementById('last-updated').textContent = 'Error loading data: ' + err.message;
  }
}

function renderOverview(data) {
  const p = data.portfolio;
  document.getElementById('last-updated').textContent = 'Last updated: ' + data.updated_at;

  const totalKRW = p.total_value_krw;
  document.getElementById('total-value').textContent = fmtKRW(totalKRW);

  const profitEl = document.getElementById('total-profit');
  profitEl.textContent = fmtPct(p.total_profit_pct);
  profitEl.className = 'metric-value ' + pctClass(p.total_profit_pct);

  document.getElementById('cash-krw').textContent = fmtKRW(p.cash_krw);
  document.getElementById('cash-usd').textContent = fmtUSD(p.cash_usd);
}

function renderStrategyTable(data) {
  const tbody = document.querySelector('#strategy-table tbody');
  tbody.innerHTML = '';

  for (const s of data.strategies) {
    const cur = s.currency;
    const holdingsCount = (s.holdings || []).filter(h => h.quantity > 0).length;
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><strong>${s.name}</strong></td>
      <td>${s.market}</td>
      <td>${fmtCurrency(s.budget, cur)}</td>
      <td>${fmtCurrency(s.total_value, cur)}</td>
      <td class="${pctClass(s.profit_pct)}">${fmtPct(s.profit_pct)}</td>
      <td>${inferBadge(s.status)}</td>
      <td>${holdingsCount}</td>
    `;
    tbody.appendChild(tr);
  }
}

function renderDetailCards(data) {
  const container = document.getElementById('detail-cards');
  container.innerHTML = '';

  for (const s of data.strategies) {
    const cur = s.currency;
    const card = document.createElement('div');
    card.className = 'detail-card';

    let holdingsHTML = '';
    if (s.holdings && s.holdings.length > 0) {
      const filtered = s.holdings.filter(h => h.quantity > 0);
      if (filtered.length > 0) {
        holdingsHTML = '<div class="holdings-list">';
        for (const h of filtered) {
          const pnlClass = (h.profit_rate || 0) >= 0 ? 'positive' : 'negative';
          holdingsHTML += `<div class="holding">
            <span><strong>${h.ticker}</strong> ${h.quantity}x</span>
            <span class="${pnlClass}">${fmtPct(h.profit_rate || 0)}</span>
          </div>`;
        }
        holdingsHTML += '</div>';
      }
    }
    if (!holdingsHTML) {
      holdingsHTML = '<div class="holdings-list"><em style="color:var(--text-dim)">No positions</em></div>';
    }

    let paramsHTML = '';
    if (s.params) {
      paramsHTML = '<div class="params">';
      for (const [k, v] of Object.entries(s.params)) {
        paramsHTML += `<span class="param-tag">${k}: ${v}</span>`;
      }
      paramsHTML += '</div>';
    }

    let regimeHTML = '';
    if (s.regime && typeof s.regime === 'object') {
      regimeHTML = '<div class="params" style="margin-top:6px">';
      for (const [ticker, r] of Object.entries(s.regime)) {
        const type = r === 'BULL' ? 'bull' : r === 'BEAR' ? 'bear' : 'neutral';
        regimeHTML += badge(`${ticker}: ${r}`, type) + ' ';
      }
      regimeHTML += '</div>';
    }

    card.innerHTML = `
      <h3>${s.name} ${inferBadge(s.status)}</h3>
      <div class="metrics-grid" style="margin-bottom:8px">
        <div class="metric" style="padding:8px">
          <span class="metric-label">Value</span>
          <span class="metric-value" style="font-size:18px">${fmtCurrency(s.total_value, cur)}</span>
        </div>
        <div class="metric" style="padding:8px">
          <span class="metric-label">P/L</span>
          <span class="metric-value ${pctClass(s.profit_pct)}" style="font-size:18px">${fmtPct(s.profit_pct)}</span>
        </div>
        <div class="metric" style="padding:8px">
          <span class="metric-label">Cash</span>
          <span class="metric-value" style="font-size:18px">${fmtCurrency(s.cash, cur)}</span>
        </div>
      </div>
      ${regimeHTML}
      ${holdingsHTML}
      ${paramsHTML}
    `;
    container.appendChild(card);
  }
}

function renderEquityChart(history) {
  const ctx = document.getElementById('equity-chart').getContext('2d');
  const datasets = [];
  let i = 0;

  for (const [name, values] of Object.entries(history.strategies)) {
    datasets.push({
      label: name,
      data: values,
      borderColor: COLORS[i % COLORS.length],
      backgroundColor: 'transparent',
      borderWidth: 2,
      pointRadius: 0,
      tension: 0.1,
    });
    i++;
  }

  new Chart(ctx, {
    type: 'line',
    data: { labels: history.dates, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: {
          ticks: { color: '#8b949e', maxTicksLimit: 12 },
          grid: { color: '#21262d' },
        },
        y: {
          ticks: { color: '#8b949e' },
          grid: { color: '#21262d' },
        },
      },
      plugins: {
        legend: { labels: { color: '#e6edf3', usePointStyle: true } },
        tooltip: {
          callbacks: {
            label: (ctx) => `${ctx.dataset.label}: ${ctx.parsed.y.toFixed(1)}`,
          },
        },
      },
    },
  });
}

function renderDrawdownChart(history) {
  const ctx = document.getElementById('drawdown-chart').getContext('2d');
  const datasets = [];
  let i = 0;

  for (const [name, values] of Object.entries(history.strategies)) {
    // Calculate drawdown from equity curve
    const dd = [];
    let peak = 0;
    for (const v of values) {
      peak = Math.max(peak, v);
      dd.push(((v - peak) / peak) * 100);
    }
    datasets.push({
      label: name,
      data: dd,
      borderColor: COLORS[i % COLORS.length],
      backgroundColor: 'transparent',
      borderWidth: 1.5,
      pointRadius: 0,
      tension: 0.1,
      fill: true,
    });
    i++;
  }

  new Chart(ctx, {
    type: 'line',
    data: { labels: history.dates, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      scales: {
        x: {
          ticks: { color: '#8b949e', maxTicksLimit: 12 },
          grid: { color: '#21262d' },
        },
        y: {
          ticks: { color: '#8b949e', callback: (v) => v.toFixed(0) + '%' },
          grid: { color: '#21262d' },
          max: 0,
        },
      },
      plugins: {
        legend: { labels: { color: '#e6edf3', usePointStyle: true } },
      },
    },
  });
}

function renderAllocationChart(data) {
  const ctx = document.getElementById('allocation-chart').getContext('2d');
  const labels = data.strategies.map(s => s.name);
  const values = data.strategies.map(s => {
    // Normalize to KRW for comparison
    if (s.currency === 'USD') return s.budget * (data.exchange_rate || 1380);
    return s.budget;
  });

  new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels,
      datasets: [{
        data: values,
        backgroundColor: COLORS.slice(0, labels.length),
        borderColor: '#161b22',
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: 'right',
          labels: { color: '#e6edf3', font: { size: 11 } },
        },
      },
    },
  });
}

function renderMarketChart(data) {
  const ctx = document.getElementById('market-chart').getContext('2d');
  const rate = data.exchange_rate || 1380;

  let krTotal = 0, usTotal = 0;
  for (const s of data.strategies) {
    if (s.market === 'KR') krTotal += s.total_value;
    else usTotal += s.total_value * rate;
  }

  new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: ['Korea (KRW)', 'US (USD)'],
      datasets: [{
        data: [krTotal, usTotal],
        backgroundColor: ['#58a6ff', '#3fb950'],
        borderColor: '#161b22',
        borderWidth: 2,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: 'right',
          labels: { color: '#e6edf3' },
        },
        tooltip: {
          callbacks: {
            label: (ctx) => ctx.label + ': ' + fmtKRW(ctx.parsed),
          },
        },
      },
    },
  });
}

// Init on load
document.addEventListener('DOMContentLoaded', init);
