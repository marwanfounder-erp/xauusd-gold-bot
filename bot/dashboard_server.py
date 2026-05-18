import json
import logging
import threading
from datetime import datetime
from typing import Optional

from http.server import BaseHTTPRequestHandler, HTTPServer

logger = logging.getLogger(__name__)

GOLD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Multi-Pair London Breakout Bot</title>
<style>
  :root {
    --gold: #FFD700; --gold-dark: #B8860B;
    --blue: #4FC3F7; --red-pair: #EF5350;
    --bg: #0a0a0a; --card: #141414; --border: #2a2a2a;
    --text: #e0e0e0; --muted: #888;
    --green: #00c853; --red: #ff1744;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', sans-serif; }
  header {
    background: linear-gradient(135deg, #0d1117, #1a1200);
    border-bottom: 2px solid var(--gold);
    padding: 14px 24px;
    display: flex; align-items: center; gap: 12px;
  }
  header h1 { color: var(--gold); font-size: 1.3rem; letter-spacing: 1px; }
  header .subtitle { color: var(--muted); font-size: 0.82rem; }
  .badge {
    background: var(--gold); color: #000;
    font-size: 0.7rem; font-weight: 700;
    padding: 2px 8px; border-radius: 12px;
    text-transform: uppercase; letter-spacing: 1px;
  }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 14px; padding: 18px; }
  .card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 8px; padding: 14px;
  }
  .card.gold-border  { border-color: var(--gold-dark); }
  .card.blue-border  { border-color: var(--blue); }
  .card.red-border   { border-color: var(--red-pair); }
  .card label { color: var(--muted); font-size: 0.72rem; text-transform: uppercase; letter-spacing: 1px; }
  .card .value { color: var(--gold); font-size: 1.5rem; font-weight: 700; margin-top: 4px; }
  .card .value.blue  { color: var(--blue); }
  .card .value.redv  { color: var(--red-pair); }
  .card .sub  { color: var(--muted); font-size: 0.78rem; margin-top: 2px; }
  .section { padding: 0 18px 18px; }
  .section h2 { color: var(--gold); font-size: 0.85rem; text-transform: uppercase; letter-spacing: 2px; margin-bottom: 10px; }
  table { width: 100%; border-collapse: collapse; background: var(--card); border-radius: 8px; overflow: hidden; }
  th { background: #1a1600; color: var(--gold); font-size: 0.72rem; text-transform: uppercase;
       letter-spacing: 1px; padding: 9px 10px; text-align: left; }
  td { padding: 9px 10px; border-top: 1px solid var(--border); font-size: 0.82rem; }
  tr:hover td { background: #1a1a1a; }
  .buy  { color: var(--green);    font-weight: 700; }
  .sell { color: var(--red);      font-weight: 700; }
  .pos  { color: var(--green); }
  .neg  { color: var(--red); }
  .status-ok   { color: var(--green); }
  .status-warn { color: var(--gold); }
  .sym-xauusd { color: var(--gold); font-weight: 700; }
  .sym-gbpusd { color: var(--blue); font-weight: 700; }
  .sym-usdjpy { color: var(--red-pair); font-weight: 700; }
  .pair-row { display: flex; gap: 10px; align-items: center; padding: 6px 0;
              border-bottom: 1px solid var(--border); }
  .pair-row:last-child { border-bottom: none; }
  .pair-label { width: 80px; font-size: 0.8rem; font-weight: 700; }
  .pair-info  { flex: 1; font-size: 0.8rem; color: var(--muted); }
  .pair-pos   { font-size: 0.8rem; color: var(--green); }
  .pair-none  { font-size: 0.8rem; color: #444; }
  .logs { background: #0a0a0a; border: 1px solid var(--border); border-radius: 8px;
          padding: 10px; font-family: monospace; font-size: 0.73rem;
          max-height: 260px; overflow-y: auto; }
  .log-line { padding: 1px 0; border-bottom: 1px solid #111; color: #aaa; }
  .log-line.warn  { color: var(--gold); }
  .log-line.error { color: var(--red); }
  footer { text-align: center; color: var(--muted); font-size: 0.72rem; padding: 14px; }
</style>
</head>
<body>
<header>
  <div>
    <h1>&#127861; Multi-Pair London Breakout Bot</h1>
    <div class="subtitle">XAUUSD &nbsp;·&nbsp; GBPUSD &nbsp;·&nbsp; USDJPY &nbsp;|&nbsp; Auto-refresh: 10s</div>
  </div>
  <div class="badge" id="mode-badge">PAPER</div>
</header>

<!-- Account cards -->
<div class="grid">
  <div class="card">
    <label>Balance</label>
    <div class="value" id="balance">—</div>
    <div class="sub" id="equity">Equity: —</div>
  </div>
  <div class="card">
    <label>Daily P&amp;L</label>
    <div class="value" id="daily-pnl">—</div>
    <div class="sub" id="daily-pnl-pct">—</div>
  </div>
  <div class="card">
    <label>Drawdown</label>
    <div class="value" id="drawdown">—</div>
    <div class="sub">Max allowed: 7%</div>
  </div>
  <div class="card">
    <label>Session</label>
    <div class="value" id="session">—</div>
    <div class="sub" id="utc-time">—</div>
  </div>
  <div class="card">
    <label>Open Positions</label>
    <div class="value" id="open-positions">—</div>
    <div class="sub" id="max-positions">Max: 2</div>
  </div>
</div>

<!-- Live prices -->
<div class="grid">
  <div class="card gold-border">
    <label>&#127861; XAUUSD (Gold)</label>
    <div class="value" id="xauusd-ask">—</div>
    <div class="sub" id="xauusd-bid">Bid: —</div>
  </div>
  <div class="card blue-border">
    <label>&#127881; GBPUSD</label>
    <div class="value blue" id="gbpusd-ask">—</div>
    <div class="sub" id="gbpusd-bid">Bid: —</div>
  </div>
  <div class="card red-border">
    <label>&#127988; USDJPY</label>
    <div class="value redv" id="usdjpy-ask">—</div>
    <div class="sub" id="usdjpy-bid">Bid: —</div>
  </div>
</div>

<!-- Per-pair stats -->
<div class="section">
  <h2>Per-Pair Statistics</h2>
  <table>
    <thead><tr>
      <th>Pair</th><th>Trades</th><th>Win Rate</th><th>Total P&amp;L</th><th>Current Position</th>
    </tr></thead>
    <tbody id="pair-stats-body">
      <tr><td colspan="5" style="text-align:center;color:#555">Loading...</td></tr>
    </tbody>
  </table>
</div>

<!-- Open positions -->
<div class="section">
  <h2>Open Positions</h2>
  <table>
    <thead><tr>
      <th>Ticket</th><th>Symbol</th><th>Direction</th><th>Lots</th>
      <th>Entry</th><th>SL</th><th>TP</th><th>P&amp;L</th><th>Session</th>
    </tr></thead>
    <tbody id="positions-body">
      <tr><td colspan="9" style="text-align:center;color:#555">No open positions</td></tr>
    </tbody>
  </table>
</div>

<!-- Recent trades -->
<div class="section">
  <h2>Recent Trades (All Pairs)</h2>
  <table>
    <thead><tr>
      <th>Time</th><th>Symbol</th><th>Direction</th><th>Entry</th>
      <th>Close</th><th>P&amp;L</th><th>Session</th><th>Reason</th>
    </tr></thead>
    <tbody id="trades-body">
      <tr><td colspan="8" style="text-align:center;color:#555">No trades yet</td></tr>
    </tbody>
  </table>
</div>

<!-- Risk status -->
<div class="section">
  <h2>Risk Status</h2>
  <table>
    <thead><tr><th>Metric</th><th>Value</th><th>Limit</th><th>Status</th></tr></thead>
    <tbody id="risk-body"></tbody>
  </table>
</div>

<!-- News -->
<div class="section">
  <h2>Upcoming News</h2>
  <table>
    <thead><tr><th>Time UTC</th><th>Currency</th><th>Impact</th><th>Event</th></tr></thead>
    <tbody id="news-body">
      <tr><td colspan="4" style="text-align:center;color:#555">No upcoming events</td></tr>
    </tbody>
  </table>
</div>

<!-- Logs -->
<div class="section">
  <h2>Bot Logs</h2>
  <div class="logs" id="logs-container"></div>
</div>

<footer>Multi-Pair London Breakout Bot &mdash; XAUUSD · GBPUSD · USDJPY &mdash; Magic ID: 303030</footer>

<script>
async function fetchJSON(url) {
  try { const r = await fetch(url); return r.ok ? r.json() : null; } catch { return null; }
}
function fmt(v, dec=2) { return v != null ? Number(v).toFixed(dec) : '—'; }
function pct(v) { return v != null ? (Number(v)*100).toFixed(1)+'%' : '—'; }
function pnlClass(v) { return Number(v) >= 0 ? 'pos' : 'neg'; }
function symClass(s) {
  if (!s) return '';
  if (s.includes('XAU')) return 'sym-xauusd';
  if (s.includes('GBP')) return 'sym-gbpusd';
  if (s.includes('JPY')) return 'sym-usdjpy';
  return '';
}
function symDec(s) {
  if (!s) return 2;
  if (s.includes('XAU')) return 2;
  if (s.includes('JPY')) return 3;
  return 5;
}

async function update() {
  const [status, prices, positions, trades, risk, news, logs, pairStats] = await Promise.all([
    fetchJSON('/api/status'),
    fetchJSON('/api/prices'),
    fetchJSON('/api/positions'),
    fetchJSON('/api/trades'),
    fetchJSON('/api/risk'),
    fetchJSON('/api/news'),
    fetchJSON('/api/logs'),
    fetchJSON('/api/pair-stats'),
  ]);

  if (status) {
    document.getElementById('balance').textContent = '$' + fmt(status.balance);
    document.getElementById('equity').textContent = 'Equity: $' + fmt(status.equity);
    document.getElementById('daily-pnl').textContent = '$' + fmt(status.daily_pnl);
    document.getElementById('open-positions').textContent = status.open_positions ?? '—';
    document.getElementById('session').textContent = status.session ?? '—';
    document.getElementById('utc-time').textContent = status.utc_time ?? '';
    document.getElementById('mode-badge').textContent = (status.mode || 'paper').toUpperCase();
  }

  if (prices) {
    const p = prices;
    if (p.XAUUSD) {
      document.getElementById('xauusd-ask').textContent = '$' + fmt(p.XAUUSD.ask, 2);
      document.getElementById('xauusd-bid').textContent = 'Bid: $' + fmt(p.XAUUSD.bid, 2);
    }
    if (p.GBPUSD) {
      document.getElementById('gbpusd-ask').textContent = fmt(p.GBPUSD.ask, 5);
      document.getElementById('gbpusd-bid').textContent = 'Bid: ' + fmt(p.GBPUSD.bid, 5);
    }
    if (p.USDJPY) {
      document.getElementById('usdjpy-ask').textContent = fmt(p.USDJPY.ask, 3);
      document.getElementById('usdjpy-bid').textContent = 'Bid: ' + fmt(p.USDJPY.bid, 3);
    }
  }

  if (risk) {
    document.getElementById('drawdown').textContent = pct(risk.drawdown_pct);
    const dd_pct = Number(risk.daily_loss_pct || 0) * 100;
    document.getElementById('daily-pnl-pct').textContent = dd_pct.toFixed(1) + '% daily loss used';
    document.getElementById('max-positions').textContent = 'Max: ' + (risk.max_positions || 2);
    const rows = [
      ['Daily Loss',       pct(risk.daily_loss_pct),  '4%',  Number(risk.daily_loss_pct||0)<0.04],
      ['Total Drawdown',   pct(risk.drawdown_pct),    '7%',  Number(risk.drawdown_pct||0)<0.07],
      ['Open Positions',   risk.open_positions,        risk.max_positions || 2, Number(risk.open_positions||0)<(risk.max_positions||2)],
    ];
    document.getElementById('risk-body').innerHTML = rows.map(([m,v,l,ok]) =>
      `<tr><td>${m}</td><td>${v}</td><td>${l}</td>
       <td class="${ok?'status-ok':'status-warn'}">${ok?'OK':'CAUTION'}</td></tr>`
    ).join('');
  }

  if (pairStats) {
    const symbols = ['XAUUSD', 'GBPUSD', 'USDJPY'];
    const posMap = {};
    if (positions) positions.forEach(p => {
      if (!posMap[p.symbol]) posMap[p.symbol] = [];
      posMap[p.symbol].push(p);
    });
    let total = {trades:0, wins:0, pnl:0};
    let rows = symbols.map(sym => {
      const s = pairStats[sym] || {trades:0, wins:0, pnl:0, win_rate:0};
      total.trades += s.trades || 0;
      total.wins   += s.wins || 0;
      total.pnl    += s.pnl || 0;
      const openPos = posMap[sym] || [];
      const posStr = openPos.length > 0
        ? openPos.map(p => `<span class="${p.type?.toLowerCase()}">${p.type} @ ${fmt(p.price_open, symDec(sym))}</span>`).join('<br>')
        : '<span class="pair-none">No position</span>';
      return `<tr>
        <td class="${symClass(sym)}">${sym}</td>
        <td>${s.trades}</td>
        <td>${s.win_rate}%</td>
        <td class="${pnlClass(s.pnl)}">$${fmt(s.pnl)}</td>
        <td>${posStr}</td>
      </tr>`;
    });
    const totalWR = total.trades > 0 ? (total.wins / total.trades * 100).toFixed(1) : '0.0';
    rows.push(`<tr style="font-weight:700;background:#1a1a1a">
      <td>TOTAL</td>
      <td>${total.trades}</td>
      <td>${totalWR}%</td>
      <td class="${pnlClass(total.pnl)}">$${fmt(total.pnl)}</td>
      <td></td>
    </tr>`);
    document.getElementById('pair-stats-body').innerHTML = rows.join('');
  }

  if (positions && positions.length > 0) {
    document.getElementById('positions-body').innerHTML = positions.map(p =>
      `<tr>
        <td>${p.ticket}</td>
        <td class="${symClass(p.symbol)}">${p.symbol}</td>
        <td class="${p.type?.toLowerCase()}">${p.type}</td>
        <td>${p.volume}</td>
        <td>${fmt(p.price_open, symDec(p.symbol))}</td>
        <td>${fmt(p.sl, symDec(p.symbol))}</td>
        <td>${fmt(p.tp, symDec(p.symbol))}</td>
        <td class="${pnlClass(p.unrealized_profit||p.profit)}">$${fmt(p.unrealized_profit ?? p.profit)}</td>
        <td>${p.session||'—'}</td>
      </tr>`
    ).join('');
  } else if (positions && positions.length === 0) {
    document.getElementById('positions-body').innerHTML =
      '<tr><td colspan="9" style="text-align:center;color:#555">No open positions</td></tr>';
  }

  if (trades && trades.length > 0) {
    document.getElementById('trades-body').innerHTML = trades.slice(0,25).map(t =>
      `<tr>
        <td>${t.open_time ? new Date(t.open_time).toLocaleString() : '—'}</td>
        <td class="${symClass(t.symbol)}">${t.symbol||'—'}</td>
        <td class="${t.direction?.toLowerCase()}">${t.direction}</td>
        <td>${fmt(t.entry_price, symDec(t.symbol))}</td>
        <td>${fmt(t.close_price, symDec(t.symbol))}</td>
        <td class="${pnlClass(t.pnl)}">$${fmt(t.pnl)}</td>
        <td>${t.session||'—'}</td>
        <td>${t.close_reason||'—'}</td>
      </tr>`
    ).join('');
  }

  if (news && news.length > 0) {
    document.getElementById('news-body').innerHTML = news.map(n =>
      `<tr>
        <td>${n.event_time ? new Date(n.event_time).toUTCString().slice(17,22)+' UTC' : n.time}</td>
        <td>${n.currency}</td>
        <td style="color:${n.impact==='high'?'#ff1744':n.impact==='medium'?'#FFD700':'#aaa'}">${n.impact?.toUpperCase()}</td>
        <td>${n.title}</td>
      </tr>`
    ).join('');
  }

  if (logs && logs.lines) {
    const container = document.getElementById('logs-container');
    container.innerHTML = logs.lines.slice(-50).map(l => {
      const cls = l.includes('ERROR') ? 'error' : l.includes('WARN') ? 'warn' : '';
      return `<div class="log-line ${cls}">${l}</div>`;
    }).join('');
    container.scrollTop = container.scrollHeight;
  }
}

update();
setInterval(update, 10000);
</script>
</body>
</html>
"""


class DashboardHandler(BaseHTTPRequestHandler):
    bot_state = {}

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._serve_html()
        elif path == "/api/health":
            self._json({"status": "ok", "pairs": ["XAUUSD", "GBPUSD", "USDJPY"]})
        elif path == "/api/status":
            self._json(self._get_status())
        elif path == "/api/price":
            # legacy — returns XAUUSD tick
            prices = self.bot_state.get("prices", {})
            self._json(prices.get("XAUUSD", self.bot_state.get("price", {})))
        elif path == "/api/prices":
            self._json(self.bot_state.get("prices", {}))
        elif path == "/api/positions":
            self._json(self.bot_state.get("positions", []))
        elif path == "/api/trades":
            self._json(self.bot_state.get("trades", []))
        elif path == "/api/risk":
            self._json(self.bot_state.get("risk", {}))
        elif path == "/api/news":
            self._json(self.bot_state.get("news", []))
        elif path == "/api/equity":
            self._json(self.bot_state.get("equity_curve", []))
        elif path == "/api/logs":
            self._json({"lines": self.bot_state.get("logs", [])})
        elif path == "/api/pair-stats":
            self._json(self.bot_state.get("pair_stats", {}))
        elif path == "/api/pair-positions":
            self._json(self.bot_state.get("pair_positions", {}))
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_html(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(GOLD_HTML.encode())

    def _json(self, data):
        body = json.dumps(data, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _get_status(self) -> dict:
        state = self.bot_state
        now   = datetime.utcnow()
        hour  = now.hour
        if 7 <= hour < 10:
            session = "LONDON"
        elif 13 <= hour < 16:
            session = "NEW YORK"
        elif hour < 7:
            session = "ASIAN"
        else:
            session = "OFF-HOURS"
        return {
            "mode":           state.get("mode", "paper"),
            "balance":        state.get("balance", 0.0),
            "equity":         state.get("equity", 0.0),
            "daily_pnl":      state.get("daily_pnl", 0.0),
            "open_positions": state.get("open_positions", 0),
            "session":        session,
            "utc_time":       now.strftime("%H:%M UTC"),
            "running":        state.get("running", True),
            "symbols":        state.get("symbols", ["XAUUSD", "GBPUSD", "USDJPY"]),
        }


class DashboardServer:
    def __init__(self, config):
        self.config = config
        self.port   = config.dashboard_port
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def update(self, **kwargs):
        DashboardHandler.bot_state.update(kwargs)

    def start(self):
        DashboardHandler.bot_state = {"mode": "paper", "running": True}
        self._server = HTTPServer(("0.0.0.0", self.port), DashboardHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        logger.info(f"Dashboard running on http://0.0.0.0:{self.port}")

    def stop(self):
        if self._server:
            self._server.shutdown()
