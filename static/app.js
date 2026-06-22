const $ = (sel) => document.querySelector(sel);

// Selected symbol: expands its table row and filters the signal feed
let selectedSymbol = null;
// Scan cadence (minutes), kept in sync by loadConfig — drives the
// "fresh signal" highlight window
let scanIntervalMin = 5;

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

// Finance convention: negatives shown in parentheses, e.g. (6.2%)
const paren = (text, isNeg) => isNeg ? `(${text})` : text;
const pct = (v) => v == null ? '—' : paren((Math.abs(v) * 100).toFixed(1) + '%', v < 0);
const num = (v) => v == null ? '—' : paren(Math.abs(Number(v)).toLocaleString(), v < 0);

function fmtTime(utc) {
  // SQLite timestamps lack timezone info (they're UTC); ISO ones already have it
  const hasTz = /Z$|[+-]\d\d:\d\d$/.test(utc);
  const d = new Date(hasTz ? utc : utc + 'Z');
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) +
         ' ' + d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

// ---- watchlist table ----

function detailCell(k, v) {
  return `<div class="row"><span class="k">${k}</span><span class="v">${v}</span></div>`;
}

function rankColor(r) {
  return r >= 60 ? 'var(--yellow)' : r <= 30 ? 'var(--green)' : 'var(--muted)';
}

function ivRankCell(m) {
  if (m.iv_rank == null) {
    return `<td class="num muted" title="${m.iv_sessions || 0} sessions — collecting">—</td>`;
  }
  const c = rankColor(m.iv_rank);
  return `<td><span class="ivrank">` +
    `<span class="ivrank-track"><span class="ivrank-fill" style="width:${m.iv_rank}%;background:${c}"></span></span>` +
    `<span class="ivrank-num" style="color:${c}">${Math.round(m.iv_rank)}</span></span></td>`;
}

// IV history per symbol changes slowly — cache so the sparkline doesn't refetch
// on every 15s table rebuild (only on first expand of a symbol).
const sparkCache = {};

async function drawSpark(sym) {
  let data = sparkCache[sym];
  if (!data) {
    try { data = await api('/api/iv_history/' + sym); sparkCache[sym] = data; }
    catch { return; }
  }
  const el = document.getElementById('iv-spark');
  if (!el) return;  // detail row may have been rebuilt/closed
  const pts = data.points || [];
  if (pts.length < 2 || data.min == null) {
    el.innerHTML = `<span class="muted" style="font-size:11.5px">IV history collecting (${pts.length} session${pts.length === 1 ? '' : 's'})…</span>`;
    return;
  }
  const W = 320, H = 64, pad = 8, span = data.max - data.min || 1;
  const x = (i) => (pts.length === 1 ? 0 : (i / (pts.length - 1)) * (W - 40));
  const y = (v) => pad + (1 - (v - data.min) / span) * (H - 2 * pad);
  const line = pts.map((p, i) => `${x(i).toFixed(1)},${y(p.iv).toFixed(1)}`).join(' ');
  const cur = pts[pts.length - 1].iv;
  const cy = y(cur).toFixed(1);
  const col = data.rank == null ? 'var(--accent)' : rankColor(data.rank);
  el.innerHTML =
    `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:64px;display:block">` +
    `<rect x="0" y="${pad}" width="${W - 40}" height="${H - 2 * pad}" fill="var(--border)" opacity="0.35"/>` +
    `<polyline points="${line}" fill="none" stroke="var(--accent)" stroke-width="1.5"/>` +
    `<line x1="0" y1="${cy}" x2="${W - 40}" y2="${cy}" stroke="${col}" stroke-width="1" stroke-dasharray="3 3"/>` +
    `<circle cx="${(W - 40).toFixed(1)}" cy="${cy}" r="3" fill="${col}"/>` +
    `<text x="${W - 36}" y="${pad + 4}" font-size="10" fill="var(--muted)">${(data.max * 100).toFixed(0)}%</text>` +
    `<text x="${W - 36}" y="${H - 2}" font-size="10" fill="var(--muted)">${(data.min * 100).toFixed(0)}%</text>` +
    `</svg>` +
    `<div class="spark-cap muted">ATM IV over ${data.sessions} session${data.sessions === 1 ? '' : 's'} · now ${(cur * 100).toFixed(0)}%` +
    `${data.rank != null ? ` (rank ${Math.round(data.rank)})` : ''}</div>`;
}

async function refreshTable() {
  const rows = await api('/api/metrics');
  const tbody = $('#watch-body');
  tbody.innerHTML = '';
  for (const m of rows) {
    const tr = document.createElement('tr');
    tr.className = 'watch-row' + (m.symbol === selectedSymbol ? ' active' : '');
    const ivhv = m.atm_iv && m.hv20 ? m.atm_iv / m.hv20 : null;
    let price = m.spot != null ? '$' + m.spot.toFixed(2) : '…';
    if (m.spot != null && m.prev_close) {
      const chg = (m.spot - m.prev_close) / m.prev_close;
      const dir = chg >= 0 ? 'up' : 'down';
      const arrow = chg >= 0 ? '▲' : '▼';
      price += ` <span class="chg ${dir}">${arrow}${Math.abs(chg * 100).toFixed(1)}%</span>`;
    }
    tr.innerHTML = `
      <td class="sym">${m.symbol}</td>
      <td class="num ${m.spot == null ? 'stale' : ''}">${price}</td>
      <td class="num"><span class="${ivhv > 1.25 ? 'hot' : ''}">${ivhv ? ivhv.toFixed(2) : '—'}</span>${m.atm_dte != null ? ` <span class="dte" title="Horizon of the IV reading: nearest expiry">${m.atm_dte}d</span>` : ''}</td>
      ${ivRankCell(m)}
      <td class="num">${m.pc_ratio ?? '—'}</td>
      <td class="num">${m.confluence_24h ? '<span class="flame" title="Confluence in the last 24h">🔥</span>' : ''}${m.signals_24h ? `<span class="badge">${m.signals_24h}</span>` : ''}</td>
      <td><button class="remove" title="Remove ${m.symbol}">×</button></td>`;
    tr.querySelector('.remove').onclick = async (e) => {
      e.stopPropagation();
      await api(`/api/watchlist/${m.symbol}`, { method: 'DELETE' });
      if (selectedSymbol === m.symbol) selectedSymbol = null;
      refreshAll();
    };
    tr.onclick = () => setFilter(selectedSymbol === m.symbol ? null : m.symbol);
    tbody.appendChild(tr);

    if (m.symbol === selectedSymbol) {
      const dr = document.createElement('tr');
      dr.className = 'detail-row';
      const gex = m.net_gex != null
        ? paren('$' + Math.abs(Math.round(m.net_gex)).toLocaleString(), m.net_gex < 0)
        : '—';
      let shortFloat = '—';
      if (m.short_pct_float != null) {
        shortFloat = (m.short_pct_float * 100).toFixed(1) + '%';
        if (m.days_to_cover != null) shortFloat += ` (${m.days_to_cover.toFixed(1)}d cover)`;
      }
      let earnings = '—';
      if (m.next_earnings) {
        const days = Math.round((new Date(m.next_earnings) - Date.now()) / 86400000);
        earnings = new Date(m.next_earnings).toLocaleDateString([], { month: 'short', day: 'numeric' }) +
                   (days >= 0 ? ` (${days}d)` : '');
      }
      const rankTxt = m.iv_rank == null ? '—'
        : `${Math.round(m.iv_rank)}${m.iv_pctile != null ? ` · ${Math.round(m.iv_pctile)} pctile` : ''}`;
      dr.innerHTML = `<td colspan="7"><div class="detail-grid">
        ${detailCell('ATM IV', pct(m.atm_iv) + (m.atm_dte != null ? ` · ${m.atm_dte}d` : ''))}
        ${detailCell('HV 20d', pct(m.hv20))}
        ${detailCell('IV rank', rankTxt)}
        ${detailCell('Call vol', num(m.call_volume))}
        ${detailCell('Put vol', num(m.put_volume))}
        ${detailCell('Peak γ strike', m.peak_gamma_strike ?? '—')}
        ${detailCell('Skew (p−c)', pct(m.skew))}
        ${detailCell('Net GEX /1%', gex)}
        ${detailCell('Earnings', earnings)}
        ${detailCell('Short float', shortFloat)}
        ${detailCell('Scanned', m.scanned_at ? fmtTime(m.scanned_at) : 'pending')}
      </div><div id="iv-spark" class="iv-spark"></div></td>`;
      tbody.appendChild(dr);
      drawSpark(m.symbol);
    }
  }
}

$('#add-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const input = $('#symbol-input');
  const sym = input.value.trim().toUpperCase();
  if (!sym) return;
  // The backend scans the new symbol right away, even after hours
  await api('/api/watchlist', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ symbol: sym }),
  });
  input.value = '';
  await refreshTable();
  // Give the single-symbol scan a moment, then pull in its metrics
  setTimeout(refreshAll, 6000);
});

function setFilter(sym) {
  selectedSymbol = sym;
  $('#signal-filter-label').textContent = sym ? `— ${sym}` : '';
  $('#clear-filter').classList.toggle('hidden', !sym);
  refreshAll();
}
$('#clear-filter').onclick = () => setFilter(null);

// ---- help modal ----

function openHelp(anchorId) {
  $('#help-overlay').classList.remove('hidden');
  if (anchorId) {
    const dt = document.getElementById(anchorId);
    if (dt) {
      dt.scrollIntoView({ block: 'start' });
      dt.classList.add('flash');
      setTimeout(() => dt.classList.remove('flash'), 1600);
    }
  }
}

function closeHelp() { $('#help-overlay').classList.add('hidden'); }

$('#help-btn').onclick = () => openHelp();
$('#help-close').onclick = closeHelp;
$('#help-overlay').addEventListener('click', (e) => {
  if (e.target === $('#help-overlay')) closeHelp();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') { closeHelp(); closeSettings(); }
});

// ---- signals ----

function isFresh(utc) {
  const hasTz = /Z$|[+-]\d\d:\d\d$/.test(utc);
  const ageMin = (Date.now() - new Date(hasTz ? utc : utc + 'Z')) / 60000;
  return ageMin < Math.max(scanIntervalMin * 2, 10);
}

function signalCard(s) {
  const fresh = isFresh(s.created_at);
  const isSetup = s.kind === 'setup_read';
  const div = document.createElement('div');
  div.className = `signal ${s.severity}${isSetup ? ' setup' : ''}${fresh ? ' fresh' : ''}`;
  div.innerHTML = `
    <div class="head">
      <span class="sym"></span>
      <span class="kind" title="What does this mean?">${s.kind.replaceAll('_', ' ')}</span>
      ${fresh ? '<span class="new-pill">new</span>' : ''}
      <span class="time">${fmtTime(s.created_at)}</span>
    </div>
    <div class="msg"></div>`;
  div.querySelector('.sym').textContent = s.symbol;
  div.querySelector('.sym').onclick = () => setFilter(s.symbol);
  div.querySelector('.kind').onclick = () => openHelp('help-' + s.kind);
  div.querySelector('.msg').textContent = s.message;
  return div;
}

function wrapCard(s) {
  let d = {};
  try { d = JSON.parse(s.details) || {}; } catch { /* fall through to plain card */ }
  const div = document.createElement('div');
  div.className = 'signal wrap' + (isFresh(s.created_at) ? ' fresh' : '');
  const fmtChg = (v) => v == null ? '—'
    : `<span class="${v >= 0 ? 'up' : 'down'} chg">${v >= 0 ? '▲' : '▼'}${Math.abs(v * 100).toFixed(1)}%</span>`;
  const fmtIv = (r) => {
    if (r.atm_iv == null) return '—';
    let out = (r.atm_iv * 100).toFixed(0) + '%';
    if (r.iv_chg_pts != null) {
      const cls = r.iv_chg_pts >= 0 ? 'iv-up' : 'iv-down';
      out += ` <span class="${cls}">${r.iv_chg_pts >= 0 ? '+' : ''}${r.iv_chg_pts.toFixed(1)}pt</span>`;
    }
    return out;
  };
  const rows = (d.rows || []).map((r) => `
    <span class="sym" data-sym="${r.symbol}">${r.symbol}</span>
    <span>${fmtChg(r.day_chg)}</span>
    <span>${fmtIv(r)}</span>
    <span class="headline">${r.confluence ? '🔥 ' : ''}${r.headline
      ? r.headline.text.replace(/</g, '&lt;') : '<span class="quiet-note">quiet — no signals</span>'}</span>`).join('');
  div.innerHTML = `
    <div class="head">
      <span class="wrap-title">Daily wrap</span>
      <span class="kind">${d.date || ''} · ${d.names ?? '?'} names · ${d.signals ?? '?'} signals · ${d.confluences ?? 0} confluence(s)</span>
      <span class="time">${fmtTime(s.created_at)}</span>
    </div>
    <div class="wrap-grid">
      <span class="wrap-h">sym</span><span class="wrap-h">day</span><span class="wrap-h">ATM IV</span><span class="wrap-h">headline</span>
      ${rows}
    </div>
    ${d.stuck && d.stuck.length ? `<div class="stuck">What stuck: ${d.stuck.join(' · ')}</div>`
      : '<div class="stuck">Nothing left elevated at the close.</div>'}`;
  div.querySelectorAll('.sym[data-sym]').forEach((el) => {
    el.onclick = () => setFilter(el.dataset.sym);
  });
  return div;
}

function isToday(utc) {
  const hasTz = /Z$|[+-]\d\d:\d\d$/.test(utc);
  const d = new Date(hasTz ? utc : utc + 'Z');
  return d.toDateString() === new Date().toDateString();
}

function feedSection(box, label) {
  const head = document.createElement('div');
  head.className = 'feed-section';
  head.textContent = label;
  box.appendChild(head);
}

async function refreshSignals() {
  const qs = selectedSymbol ? `&symbol=${selectedSymbol}` : '';
  const signals = await api(`/api/signals?limit=100${qs}`);
  const box = $('#signals');
  if (!signals.length && !selectedSymbol) return; // keep the explainer
  box.innerHTML = signals.length ? '' :
    `<p class="empty">No signals for ${selectedSymbol} yet.</p>`;
  if (!signals.length) return;

  const today = signals.filter(s => isToday(s.created_at));
  const older = signals.filter(s => !isToday(s.created_at));

  feedSection(box, 'Today');
  if (!today.length) {
    box.insertAdjacentHTML('beforeend',
      '<p class="feed-empty">No signals yet today.</p>');
  }
  for (const s of today) box.appendChild(s.kind === 'daily_wrap' ? wrapCard(s) : signalCard(s));

  if (older.length) {
    feedSection(box, 'Older');
    for (const s of older) box.appendChild(s.kind === 'daily_wrap' ? wrapCard(s) : signalCard(s));
  }
}

// ---- status / config ----

async function refreshStatus() {
  const st = await api('/api/status');
  const pill = $('#scan-status');
  pill.textContent = st.scanning ? 'scanning…'
    : st.market_open ? 'market open' : 'market closed';
  pill.classList.toggle('scanning', st.scanning);
  pill.classList.toggle('open', !st.scanning && st.market_open);
  $('#instance-warn').classList.toggle('hidden', st.is_scan_owner !== false);
  $('#last-scan').textContent = 'last scan: ' +
    (st.last_scan_at ? fmtTime(st.last_scan_at) : '—');
  $('#next-scan').textContent = 'next: ' +
    (st.next_scan_at ? new Date(st.next_scan_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '—');
}

$('#scan-now').onclick = async () => {
  await api('/api/scan', { method: 'POST' });
  refreshStatus();
};

async function loadConfig() {
  const cfg = await api('/api/config');
  scanIntervalMin = cfg.scan_interval_minutes || 5;
  $('#cfg-interval').value = cfg.scan_interval_minutes;
  $('#cfg-ivhv').value = cfg.thresholds.iv_hv_ratio;
  $('#cfg-ivspike').value = cfg.thresholds.iv_spike_pct;
  $('#cfg-voloi').value = cfg.thresholds.uoa_vol_oi_ratio;
}

$('#save-config').onclick = async () => {
  const cfg = await api('/api/config');
  cfg.scan_interval_minutes = parseInt($('#cfg-interval').value, 10) || 5;
  cfg.thresholds.iv_hv_ratio = parseFloat($('#cfg-ivhv').value) || 1.25;
  cfg.thresholds.iv_spike_pct = parseFloat($('#cfg-ivspike').value) || 0.10;
  cfg.thresholds.uoa_vol_oi_ratio = parseFloat($('#cfg-voloi').value) || 2.0;
  await api('/api/config', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(cfg),
  });
  $('#config-saved').textContent = 'saved ✓';
  setTimeout(() => { $('#config-saved').textContent = ''; }, 2000);
  refreshStatus();
};

// UI scale: zoom the whole app so small labels stay readable (persisted)
const FS_KEY = 'sensi-ui-scale';
let uiScale = parseFloat(localStorage.getItem(FS_KEY)) || 1;
function applyScale() { document.body.style.zoom = uiScale; }
function setScale(v) {
  uiScale = Math.min(1.6, Math.max(0.9, Math.round(v * 10) / 10));
  localStorage.setItem(FS_KEY, uiScale);
  applyScale();
}
$('#fs-inc').onclick = () => setScale(uiScale + 0.1);
$('#fs-dec').onclick = () => setScale(uiScale - 0.1);
applyScale();

// Settings overlay (header button — same pattern as help)
function closeSettings() { $('#settings-overlay').classList.add('hidden'); }
$('#settings-btn').onclick = () => $('#settings-overlay').classList.remove('hidden');
$('#settings-close').onclick = closeSettings;
$('#settings-overlay').addEventListener('click', (e) => {
  if (e.target === $('#settings-overlay')) closeSettings();
});

// ---- performance view ----

const pctSigned = (v, digits = 1) =>
  v == null ? '—' : `${v >= 0 ? '+' : '−'}${Math.abs(v * 100).toFixed(digits)}%`;

const MEASURES = { direction: '↑ dir', magnitude: '⇕ size', stillness: '≈ still' };

function edgeCell(v, good) {
  // green when the detector was right, red when wrong, muted when neutral/none
  if (v == null) return '<td class="num muted">—</td>';
  const cls = good == null ? 'muted' : good > 0.0005 ? 'pos' : good < -0.0005 ? 'neg' : 'muted';
  return `<td class="num ${cls}">${pctSigned(v)}</td>`;
}

function renderSignalTab(d) {
  const body = $('#perf-signal-body');
  body.innerHTML = '';
  for (const s of d.by_signal) {
    const tr = document.createElement('tr');
    const hit = s.hit == null ? '—' : `${Math.round(s.hit * 100)}%`;
    tr.innerHTML =
      `<td><span class="kind kind-link" data-help-kind="${s.kind}" title="What does this mean?">${s.kind.replaceAll('_', ' ')}</span></td>` +
      `<td class="measures">${MEASURES[s.type] || s.type}</td>` +
      `<td class="num">${s.n}</td>` +
      edgeCell(s.edge_1d, s.type === 'stillness' ? -s.edge_1d : s.edge_1d) +
      edgeCell(s.edge_5d, s.good_5d) +
      `<td class="num muted">${hit}</td>` +
      `<td><span class="verdict ${s.verdict}">${s.verdict}</span></td>`;
    body.appendChild(tr);
  }
}

function renderNameTab(d) {
  $('#th-name-move').textContent = `+${d.horizon}d move vs base`;
  const body = $('#perf-name-body');
  body.innerHTML = '';
  for (const n of d.by_name) {
    const move = n.mean_abs == null ? '—'
      : `${(n.mean_abs * 100).toFixed(1)}% <span class="muted">vs ${n.base_abs != null ? (n.base_abs * 100).toFixed(1) + '%' : '—'}</span>`;
    const good = (n.mean_abs != null && n.base_abs != null) ? n.mean_abs - n.base_abs : null;
    const moveCls = good == null ? 'muted' : good > 0 ? 'pos' : 'neg';
    const hit = n.hit == null ? '—' : `${Math.round(n.hit * 100)}%`;
    const tr = document.createElement('tr');
    tr.innerHTML =
      `<td class="kind">${n.symbol}</td>` +
      `<td class="num">${n.fires}</td>` +
      `<td class="num ${moveCls}">${move}</td>` +
      `<td class="num muted">${hit}</td>` +
      `<td>${n.top_signal ? `<span class="kind-link muted" data-help-kind="${n.top_signal}" title="What does this mean?">${n.top_signal.replaceAll('_', ' ')}</span>` : '<span class="muted">—</span>'}</td>` +
      `<td><span class="verdict ${n.verdict}">${n.verdict}</span></td>`;
    body.appendChild(tr);
  }
  renderHeatmap(d.heatmap);
}

function heatClass(cell) {
  if (!cell || cell.n < 3 || cell.good == null) return 'na';
  if (cell.good >= 0.02) return 'g3';
  if (cell.good >= 0.003) return 'g1';
  if (cell.good <= -0.003) return 'bad';
  return 'n0';
}

function renderHeatmap(heat) {
  const kinds = heat.kinds.slice(0, 7);
  const grid = $('#perf-heatmap');
  grid.className = 'heat-grid';
  grid.style.gridTemplateColumns = `72px repeat(${kinds.length}, 1fr)`;
  let html = '<span></span>' +
    kinds.map(k => `<span class="hh kind-link" data-help-kind="${k}" title="${k.replaceAll('_', ' ')} — what does this mean?">${k.split('_').map(w => w.slice(0, 4)).join('')}</span>`).join('');
  for (const row of heat.rows) {
    html += `<span class="hname">${row.symbol}</span>`;
    for (const k of kinds) {
      const c = row.cells[k];
      const cls = heatClass(c);
      const txt = cls === 'na' ? '·' : pctSigned(c.edge);
      html += `<span class="heat-cell ${cls}">${txt}</span>`;
    }
  }
  grid.innerHTML = html;
}

async function refreshPerformance() {
  try {
    const d = await api('/api/outcomes');
    renderSignalTab(d);
    renderNameTab(d);
    const ready = d.by_signal.some(s => s.verdict !== 'collecting');
    $('#perf-note').innerHTML =
      `measures: ↑ dir = moved the predicted way · ⇕ size = a move happened either way · ≈ still = stayed calmer than usual. ` +
      `edge = signal outcome minus the same name's baseline. Verdicts use +5d once ${d.min_samples}+ matured samples exist, falling back to +1d. ` +
      (ready ? '' : '+5d returns are still maturing — early verdicts lean on +1d.');
  } catch (e) {
    console.error(e);
  }
}

// view + tab switching
function showView(perf) {
  $('#dashboard-view').classList.toggle('hidden', perf);
  $('#performance-view').classList.toggle('hidden', !perf);
  $('#view-dashboard').classList.toggle('active', !perf);
  $('#view-performance').classList.toggle('active', perf);
  if (perf) refreshPerformance();
}
$('#view-dashboard').onclick = () => showView(false);
$('#view-performance').onclick = () => showView(true);

function showPerfTab(name) {
  $('#perf-signal').classList.toggle('hidden', name !== 'signal');
  $('#perf-name').classList.toggle('hidden', name !== 'name');
  $('#tab-signal').classList.toggle('active', name === 'signal');
  $('#tab-name').classList.toggle('active', name === 'name');
}
$('#tab-signal').onclick = () => showPerfTab('signal');
$('#tab-name').onclick = () => showPerfTab('name');

// Any signal name in the Performance view links to its help glossary entry
$('#performance-view').addEventListener('click', (e) => {
  const el = e.target.closest('[data-help-kind]');
  if (el) openHelp('help-' + el.dataset.helpKind);
});

// ---- main loop ----

async function refreshAll() {
  try {
    await Promise.all([refreshTable(), refreshSignals(), refreshStatus()]);
  } catch (e) {
    console.error(e);
  }
}

loadConfig();
refreshAll();
setInterval(refreshAll, 15000);
