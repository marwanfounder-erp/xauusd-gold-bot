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
<meta http-equiv="refresh" content="10">
<title>XAUUSD Gold Bot</title>
<style>
  :root {
    --gold: #FFD700;
    --gold-dark: #B8860B;
    --bg: #0a0a0a;
    --card: #141414;
    --border: #2a2a2a;
    --text: #e0e0e0;
    --muted: #888;
    --green: #00c853;
    --red: #ff1744;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', sans-serif; }
  header {
    background: linear-gradient(135deg, #1a1200, #2a1f00);
    border-bottom: 2px solid var(--gold);
    padding: 16px 24px;
    display: flex; align-items: center; gap: 12px;
  }
  header h1 { color: var(--gold); font-size: 1.4rem; letter-spacing: 1px; }
  header .subtitle { color: var(--muted); font-size: 0.85rem; }
  .badge {
    background: var(--gold); color: #000;
    font-size: 0.7rem; font-weight: 700;
    padding: 2px 8px; border-radius: 12px;
    text-transform: uppercase; letter-spacing: 1px;
  }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; padding: 20px; }
  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 8px; padding: 16px;
  }
  .card.gold-border { border-color: var(--gold-dark); }
  .card label { color: var(--muted); font-size: 0.75rem; text-transform: uppercase; letter-spacing: 1px; }
  .card .value { color: var(--gold); font-size: 1.6rem; font-weight: 700; margin-top: 4px; }
  .card .sub { color: var(--muted); font-size: 0.8rem; margin-top: 2px; }
  .section { padding: 0 20px 20px; }
  .section h2 { color: var(--gold); font-size: 0.9rem; text-transform: uppercase; letter-spacing: 2px; margin-bottom: 12px; }
  table { width: 100%; border-collapse: collapse; background: var(--card); border-radius: 8px; overflow: hidden; }
  th { background: #1a1600; color: var(--gold); font-size: 0.75rem; text-transform: uppercase;
       letter-spacing: 1px; padding: 10px 12px; text-align: left; }
  td { padding: 10px 12px; border-top: 1px solid var(--border); font-size: 0.85rem; }
  tr:hover td { background: #1a1a1a; }
  .buy { color: var(--green); font-weight: 700; }
  .sell { color: var(--red); font-weight: 700; }
  .pos { color: var(--green); }
  .neg { color: var(--red); }
  .status-ok { color: var(--green); }
  .status-warn { color: var(--gold); }
  .logs { background: #0a0a0a; border: 1px solid var(--border); border-radius: 8px;
          padding: 12px; font-family: monospace; font-size: 0.75rem;
          max-height: 280px; overflow-y: auto; }
  .log-line { padding: 2px 0; border-bottom: 1px solid #111; color: #aaa; }
  .log-line.warn { color: var(--gold); }
  .log-line.error { color: var(--red); }
  footer { text-align: center; color: var(--muted); font-size: 0.75rem; padding: 16px; }
</style>
</head>
<body>
<header>
  <div>
    <h1>&#127861; XAUUSD Gold Bot</h1>
    <div class="subtitle">London + NY Breakout Strategy &nbsp;|&nbsp; Auto-refresh: 10s</div>
  </div>
  <div class="badge" id="mode-badge">PAPER</div>
</header>

<div class="grid">
  <div class="card gold-border">
    <label>Gold Price (Ask)</label>
    <div class="value" id="gold-price">—</div>
    <div class="sub" id="gold-bid">Bid: —</div>
  </div>
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
    <div class="sub">Max: 1</div>
  </div>
</div>

<div class="section">
  <h2>Open Positions</h2>
  <table>
    <thead><tr>
      <th>Ticket</th><th>Direction</th><th>Lots</th>
      <th>Entry</th><th>SL</th><th>TP</th><th>P&amp;L</th><th>Session</th>
    </tr></thead>
    <tbody id="positions-body"><tr><td colspan="8" style="text-align:center;color:#555">No open positions</td></tr></tbody>
  </table>
</div>

<div class="section">
  <h2>Recent Trades</h2>
  <table>
    <thead><tr>
      <th>Time</th><th>Direction</th><th>Entry</th>
      <th>Close</th><th>P&amp;L</th><th>Session</th><th>Reason</th>
    </tr></thead>
    <tbody id="trades-body"><tr><td colspan="7" style="text-align:center;color:#555">No trades yet</td></tr></tbody>
  </table>
</div>

<div class="section">
  <h2>Risk Status</h2>
  <table>
    <thead><tr><th>Metric</th><th>Value</th><th>Limit</th><th>Status</th></tr></thead>
    <tbody id="risk-body"></tbody>
  </table>
</div>

<div class="section">
  <h2>Upcoming News</h2>
  <table>
    <thead><tr><th>Time UTC</th><th>Currency</th><th>Impact</th><th>Event</th></tr></thead>
    <tbody id="news-body"><tr><td colspan="4" style="text-align:center;color:#555">No upcoming events</td></tr></tbody>
  </table>
</div>

<div class="section">
  <h2>Bot Logs</h2>
  <div class="logs" id="logs-container"></div>
</div>

<footer>XAUUSD Gold Bot &mdash; London + NY Breakout &mdash; Magic ID: 303030</footer>

<script>
async function fetchJSON(url) {
  try { const r = await fetch(url); return r.ok ? r.json() : null; } catch { return null; }
}
function fmt(v, dec=2) { return v != null ? Number(v).toFixed(dec) : '—'; }
function pct(v) { return v != null ? (Number(v)*100).toFixed(1)+'%' : '—'; }
function pnlClass(v) { return Number(v) >= 0 ? 'pos' : 'neg'; }

async function update() {
  const [price, status, positions, trades, risk, news, logs] = await Promise.all([
    fetchJSON('/api/price'),
    fetchJSON('/api/status'),
    fetchJSON('/api/positions'),
    fetchJSON('/api/trades'),
    fetchJSON('/api/risk'),
    fetchJSON('/api/news'),
    fetchJSON('/api/logs'),
  ]);

  if (price) {
    document.getElementById('gold-price').textContent = '$' + fmt(price.ask);
    document.getElementById('gold-bid').textContent = 'Bid: $' + fmt(price.bid);
  }
  if (status) {
    document.getElementById('balance').textContent = '$' + fmt(status.balance);
    document.getElementById('equity').textContent = 'Equity: $' + fmt(status.equity);
    document.getElementById('daily-pnl').textContent = '$' + fmt(status.daily_pnl);
    document.getElementById('open-positions').textContent = status.open_positions ?? '—';
    document.getElementById('session').textContent = status.session ?? '—';
    document.getElementById('utc-time').textContent = status.utc_time ?? '';
    document.getElementById('mode-badge').textContent = (status.mode || 'paper').toUpperCase();
  }
  if (risk) {
    document.getElementById('drawdown').textContent = pct(risk.drawdown_pct);
    const dd_pct = Number(risk.daily_loss_pct || 0) * 100;
    document.getElementById('daily-pnl-pct').textContent = dd_pct.toFixed(1) + '% daily loss used';
    const rows = [
      ['Daily Loss', pct(risk.daily_loss_pct), '4%', Number(risk.daily_loss_pct||0)<0.04],
      ['Total Drawdown', pct(risk.drawdown_pct), '7%', Number(risk.drawdown_pct||0)<0.07],
      ['Open Positions', risk.open_positions, '1', Number(risk.open_positions||0)<1],
    ];
    document.getElementById('risk-body').innerHTML = rows.map(([m,v,l,ok]) =>
      `<tr><td>${m}</td><td>${v}</td><td>${l}</td>
       <td class="${ok?'status-ok':'status-warn'}">${ok?'OK':'CAUTION'}</td></tr>`
    ).join('');
  }
  if (positions && positions.length > 0) {
    document.getElementById('positions-body').innerHTML = positions.map(p =>
      `<tr>
        <td>${p.ticket}</td>
        <td class="${p.type?.toLowerCase()}">${p.type}</td>
        <td>${p.volume}</td>
        <td>$${fmt(p.price_open)}</td>
        <td>$${fmt(p.sl)}</td>
        <td>$${fmt(p.tp)}</td>
        <td class="${pnlClass(p.profit)}">$${fmt(p.profit)}</td>
        <td>${p.session||'—'}</td>
      </tr>`
    ).join('');
  }
  if (trades && trades.length > 0) {
    document.getElementById('trades-body').innerHTML = trades.slice(0,20).map(t =>
      `<tr>
        <td>${t.open_time ? new Date(t.open_time).toLocaleString() : '—'}</td>
        <td class="${t.direction?.toLowerCase()}">${t.direction}</td>
        <td>$${fmt(t.entry_price)}</td>
        <td>$${fmt(t.close_price)}</td>
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
        pass  # suppress access logs

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/" or path == "/index.html":
            self._serve_html()
        elif path == "/api/health":
            self._json({"status": "ok", "symbol": "XAUUSD"})
        elif path == "/api/status":
            self._json(self._get_status())
        elif path == "/api/price":
            self._json(self._get_price())
        elif path == "/api/positions":
            self._json(self._get_positions())
        elif path == "/api/trades":
            self._json(self._get_trades())
        elif path == "/api/risk":
            self._json(self._get_risk())
        elif path == "/api/news":
            self._json(self._get_news())
        elif path == "/api/equity":
            self._json(self._get_equity())
        elif path == "/api/logs":
            self._json(self._get_logs())
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
        now = datetime.utcnow()
        hour = now.hour
        if 7 <= hour < 10:
            session = "LONDON"
        elif 13 <= hour < 16:
            session = "NEW YORK"
        elif hour < 7:
            session = "ASIAN"
        else:
            session = "OFF-HOURS"
        return {
            "symbol": "XAUUSD",
            "mode": state.get("mode", "paper"),
            "balance": state.get("balance", 0.0),
            "equity": state.get("equity", 0.0),
            "daily_pnl": state.get("daily_pnl", 0.0),
            "open_positions": state.get("open_positions", 0),
            "session": session,
            "utc_time": now.strftime("%H:%M UTC"),
            "running": state.get("running", True),
        }

    def _get_price(self) -> dict:
        return self.bot_state.get("price", {"bid": None, "ask": None})

    def _get_positions(self) -> list:
        return self.bot_state.get("positions", [])

    def _get_trades(self) -> list:
        return self.bot_state.get("trades", [])

    def _get_risk(self) -> dict:
        return self.bot_state.get("risk", {})

    def _get_news(self) -> list:
        return self.bot_state.get("news", [])

    def _get_equity(self) -> list:
        return self.bot_state.get("equity_curve", [])

    def _get_logs(self) -> dict:
        return {"lines": self.bot_state.get("logs", [])}


class DashboardServer:
    def __init__(self, config):
        self.config = config
        self.port = config.dashboard_port
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
