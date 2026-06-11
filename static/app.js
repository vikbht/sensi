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
      <td class="num"><span class="${ivhv > 1.25 ? 'hot' : ''}">${ivhv ? ivhv.toFixed(2) : '—'}</span></td>
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
      dr.innerHTML = `<td colspan="6"><div class="detail-grid">
        ${detailCell('ATM IV', pct(m.atm_iv))}
        ${detailCell('HV 20d', pct(m.hv20))}
        ${detailCell('Call vol', num(m.call_volume))}
        ${detailCell('Put vol', num(m.put_volume))}
        ${detailCell('Peak γ strike', m.peak_gamma_strike ?? '—')}
        ${detailCell('Skew (p−c)', pct(m.skew))}
        ${detailCell('Net GEX /1%', gex)}
        ${detailCell('Earnings', earnings)}
        ${detailCell('Short float', shortFloat)}
        ${detailCell('Scanned', m.scanned_at ? fmtTime(m.scanned_at) : 'pending')}
      </div></td>`;
      tbody.appendChild(dr);
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
  if (e.key === 'Escape') closeHelp();
});

// ---- signals ----

function isFresh(utc) {
  const hasTz = /Z$|[+-]\d\d:\d\d$/.test(utc);
  const ageMin = (Date.now() - new Date(hasTz ? utc : utc + 'Z')) / 60000;
  return ageMin < Math.max(scanIntervalMin * 2, 10);
}

function signalCard(s) {
  const fresh = isFresh(s.created_at);
  const div = document.createElement('div');
  div.className = `signal ${s.severity}${fresh ? ' fresh' : ''}`;
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

// Settings panel: collapsed by default, remember the user's choice
const settingsBox = $('#settings-box');
settingsBox.open = localStorage.getItem('sensi-settings-open') === '1';
settingsBox.addEventListener('toggle', () => {
  localStorage.setItem('sensi-settings-open', settingsBox.open ? '1' : '0');
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
