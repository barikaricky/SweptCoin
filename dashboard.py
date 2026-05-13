"""
dashboard.py — SweptCoin Web Dashboard.

Run with:
    python dashboard.py

Then open http://localhost:5000 in your browser.
Auto-refreshes every 15 seconds. Shows live paper balance, watchlist coins
with TradingView 4H charts, BUY/SELL/HOLD signals, open positions, and
a full closed-trade ledger with per-trade PnL.
"""

from flask import Flask, render_template_string, jsonify
from sqlalchemy import create_engine, text
from datetime import datetime, timezone
import requests as _req
import config

app = Flask(__name__)

# ─── HTML Template ────────────────────────────────────────────────────────────
TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>SweptCoin Dashboard</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    :root {
      --bg:#0d1117; --surface:#161b22; --border:#30363d; --text:#e6edf3;
      --muted:#8b949e; --green:#3fb950; --red:#f85149; --blue:#58a6ff; --yellow:#d29922;
    }
    body { font-family:'Segoe UI',Arial,sans-serif; background:var(--bg); color:var(--text); padding:20px 28px; }
    .topbar { display:flex; justify-content:space-between; align-items:center; margin-bottom:6px; }
    h1 { font-size:1.55rem; font-weight:700; color:var(--blue); }
    .badge { padding:3px 10px; border-radius:12px; font-size:.75rem; font-weight:600; margin-left:8px; }
    .paper { background:#1f3a5f; color:var(--blue); } .live { background:#3d1a1a; color:var(--red); }
    .subtitle { color:var(--muted); font-size:.82rem; margin-bottom:24px; }
    .cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(165px,1fr)); gap:14px; margin-bottom:30px; }
    .card { background:var(--surface); border:1px solid var(--border); border-radius:10px; padding:16px 18px; }
    .card .lbl { font-size:.72rem; color:var(--muted); text-transform:uppercase; letter-spacing:.05em; }
    .card .val { font-size:1.4rem; font-weight:700; margin-top:5px; }
    .card .sub { font-size:.76rem; color:var(--muted); margin-top:3px; }
    .green{color:var(--green);} .red{color:var(--red);} .blue{color:var(--blue);} .yellow{color:var(--yellow);}
    h2 { font-size:.95rem; font-weight:600; color:#c9d1d9; border-bottom:1px solid var(--border); padding-bottom:7px; margin-bottom:14px; }
    .section { margin-bottom:38px; }
    /* Watchlist grid */
    .wl-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(520px,1fr)); gap:18px; }
    .coin-card { background:var(--surface); border:1px solid var(--border); border-radius:10px; overflow:hidden; }
    .coin-card.active-trade { border-color:var(--yellow); box-shadow:0 0 0 1px var(--yellow); }
    .coin-card.buy-signal  { border-color:var(--green);  box-shadow:0 0 0 1px var(--green); }
    .coin-header { display:flex; justify-content:space-between; align-items:flex-start; padding:10px 14px; background:#1c2128; border-bottom:1px solid var(--border); }
    .coin-sym { font-weight:700; font-size:1rem; }
    .coin-meta { font-size:.75rem; color:var(--muted); margin-top:2px; }
    .badges { display:flex; gap:6px; flex-wrap:wrap; margin-top:4px; align-items:center; }
    .sent { font-size:.76rem; padding:2px 9px; border-radius:10px; font-weight:600; }
    .BULLISH{background:#1a3a1f;color:var(--green);} .BEARISH{background:#3d1a1a;color:var(--red);} .NEUTRAL{background:#1e2530;color:var(--muted);}
    .sig-buy  { background:#1a3a1f; color:var(--green); font-size:.76rem; padding:2px 9px; border-radius:10px; font-weight:700; }
    .sig-hold { background:#1e2530; color:var(--muted); font-size:.76rem; padding:2px 9px; border-radius:10px; font-weight:600; }
    .sig-sell { background:#3d1a1a; color:var(--red); font-size:.76rem; padding:2px 9px; border-radius:10px; font-weight:700; }
    .open-pill { font-size:.72rem; padding:2px 8px; border-radius:8px; background:#3a2e00; color:var(--yellow); font-weight:700; }
    .coin-stats { display:flex; gap:16px; padding:7px 14px; font-size:.76rem; color:var(--muted); border-bottom:1px solid var(--border); flex-wrap:wrap; }
    .coin-stats .hi { color:var(--text); font-weight:600; }
    .tv-wrap { width:100%; height:320px; }
    .tv-wrap iframe { width:100%; height:100%; border:none; display:block; }
    /* Tables */
    table { width:100%; border-collapse:collapse; font-size:.83rem; }
    th { text-align:left; padding:7px 11px; background:var(--surface); color:var(--muted); font-size:.72rem; text-transform:uppercase; border-bottom:1px solid var(--border); }
    td { padding:8px 11px; border-bottom:1px solid #21262d; }
    tr:hover td { background:#1c2128; }
    .win{color:var(--green);font-weight:600;} .loss{color:var(--red);font-weight:600;}
    .empty { color:var(--muted); font-style:italic; padding:16px 12px; }
    .footer { color:var(--muted); font-size:.74rem; margin-top:30px; text-align:right; }
  </style>
</head>
<body>
<div class="topbar">
  <h1>&#9889; SweptCoin Dashboard
    <span class="badge {{ 'paper' if mode == 'PAPER' else 'live' }}">{{ mode }} &middot; {{ 'Testnet' if testnet else 'MAINNET' }}</span>
  </h1>
  <span style="color:var(--muted);font-size:.8rem">&#128336; <span id="tick">{{ now }}</span></span>
</div>
<p class="subtitle">Starting balance: ${{ "%.2f"|format(balance_start) }} &nbsp;&middot;&nbsp; Prices update every 5 s &nbsp;&middot;&nbsp; Page reloads every 60 s</p>

<!-- ── Stat cards ── -->
<div class="cards">
  <div class="card">
    <div class="lbl">Live Balance</div>
    <div class="val {{ 'green' if balance_delta >= 0 else 'red' }}" id="live-balance">${{ "%.2f"|format(balance_now) }}</div>
    <div class="sub {{ 'green' if balance_delta >= 0 else 'red' }}" id="live-delta">{{ "&#9650;" if balance_delta >= 0 else "&#9660;" }} ${{ "%+.4f"|format(balance_delta) }}</div>
  </div>
  <div class="card">
    <div class="lbl">Unrealized PnL</div>
    <div class="val" id="live-unrealized">$+0.0000</div>
    <div class="sub" id="live-unrealized-sub">open positions</div>
  </div>
  <div class="card">
    <div class="lbl">Realised PnL</div>
    <div class="val {{ 'green' if total_pnl >= 0 else 'red' }}">${{ "%+.4f"|format(total_pnl) }}</div>
    <div class="sub">{{ closed_count }} closed trades</div>
  </div>
  <div class="card">
    <div class="lbl">Win Rate</div>
    <div class="val {{ 'green' if win_rate >= 50 else 'red' }}">{{ "%.1f"|format(win_rate) }}%</div>
    <div class="sub">&#9989; {{ wins }} &nbsp; &#10060; {{ losses }}</div>
  </div>
  <div class="card">
    <div class="lbl">Open Positions</div>
    <div class="val yellow">{{ open_count }}</div>
    <div class="sub">of {{ max_positions }} max</div>
  </div>
  <div class="card">
    <div class="lbl">Watchlist</div>
    <div class="val blue">{{ watchlist|length }}</div>
    <div class="sub">coins screened</div>
  </div>
  <div class="card">
    <div class="lbl">Best / Worst</div>
    <div class="val green" style="font-size:1.05rem">${{ "%+.4f"|format(best) }}</div>
    <div class="sub red">${{ "%+.4f"|format(worst) }}</div>
  </div>
</div>

<!-- ── Watchlist with TradingView charts ── -->
<div class="section">
  <h2>&#128301; Watchlist &mdash; {{ watchlist|length }} Coins &nbsp;(Live TradingView 4H Charts)</h2>
  {% if watchlist %}
  <div class="wl-grid">
    {% for c in watchlist %}
    {% set is_open = c.symbol in open_symbols %}
    {% set is_buy  = c.last_signal == 'BUY' %}
    {% set is_sell = c.last_signal == 'SELL' %}
    <div class="coin-card {{ 'buy-signal' if is_buy else ('active-trade' if is_open else '') }}">
      <div class="coin-header">
        <div>
          <span class="coin-sym">{{ c.symbol }}</span>
          <div class="badges">
            {% if is_buy %}<span class="sig-buy">&#11014; BUY</span>
            {% elif is_sell %}<span class="sig-sell">&#11015; SELL</span>
            {% else %}<span class="sig-hold">&#9646; HOLD</span>{% endif %}
            <span class="sent {{ c.sentiment_dir }}">{{ c.sentiment_dir }} ({{ "%+.2f"|format(c.sentiment_score) }})</span>
            {% if is_open %}<span class="open-pill">&#9679; OPEN TRADE</span>{% endif %}
          </div>
          <div class="coin-meta">
            ${{ "%.1f"|format(c.market_cap_usd / 1e6) }}M mcap &nbsp;&middot;&nbsp;
            {{ "%.1f"|format(c.age_days / 365) }}y old &nbsp;&middot;&nbsp;
            P-Score: {{ c.partnership_score }}
          </div>
        </div>
      </div>
      <div class="coin-stats">
        {% if c.signal_reason %}<span>Signal: <span class="hi">{{ c.signal_reason[:90] }}</span></span>{% endif %}
        <span>Updated: <span class="hi">{{ c.last_screened }}</span></span>
      </div>
      <div class="tv-wrap">
        <iframe
          src="https://www.tradingview.com/widgetsnippet/?locale=en&symbol=BYBIT:{{ c.symbol }}&interval=240&theme=dark&style=1&hide_top_toolbar=0&hide_legend=0&save_image=0&calendar=0&hide_volume=0&support_host=https%3A%2F%2Fwww.tradingview.com"
          allowtransparency="true" frameborder="0" scrolling="no">
        </iframe>
      </div>
    </div>
    {% endfor %}
  </div>
  {% else %}
    <p class="empty">Screener hasn&#8217;t finished yet &mdash; coins appear ~6 min after bot starts.</p>
  {% endif %}
</div>

<!-- ── Open Positions ── -->
<div class="section">
  <h2>&#128993; Open Positions ({{ open_count }})</h2>
  {% if open_trades %}
  <table>
    <thead><tr>
      <th>#</th><th>Symbol</th><th>Entry</th><th>Current Price</th><th>Unrealized PnL</th>
      <th>Take Profit</th><th>Stop Loss</th><th>Trailing SL</th><th>Size USDT</th><th>Trigger</th><th>Opened</th>
    </tr></thead>
    <tbody>
    {% for t in open_trades %}
      <tr>
        <td>{{ t.id }}</td>
        <td><strong>{{ t.symbol }}</strong></td>
        <td>${{ "%.6g"|format(t.entry_price) }}</td>
        <td id="cprice-{{ t.id }}" style="color:var(--blue)">&#8230;</td>
        <td id="upnl-{{ t.id }}" class="yellow">&#8230;</td>
        <td class="win">${{ "%.6g"|format(t.take_profit) }}</td>
        <td class="loss">${{ "%.6g"|format(t.stop_loss) }}</td>
        <td>{{ "$%.6g"|format(t.trailing_stop) if t.trailing_stop else "&#8212;" }}</td>
        <td>${{ "%.2f"|format(t.quantity_usdt) }}</td>
        <td>{{ t.notes.split('|')[0].strip() if t.notes else "&#8212;" }}</td>
        <td>{{ t.entry_time }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
    <p class="empty">No open positions right now.</p>
  {% endif %}
</div>

<!-- ── Trade History ── -->
<div class="section">
  <h2>&#128203; Trade History ({{ closed_count }} closed)</h2>
  {% if closed_trades %}
  <table>
    <thead><tr>
      <th>#</th><th>Symbol</th><th>Result</th><th>Entry</th><th>Exit</th>
      <th>PnL USDT</th><th>PnL %</th><th>Size</th><th>Trigger</th><th>Opened</th><th>Closed</th>
    </tr></thead>
    <tbody>
    {% for t in closed_trades %}
      {% set pct = (t.pnl_usdt / t.quantity_usdt * 100) if t.quantity_usdt else 0 %}
      <tr>
        <td>{{ t.id }}</td>
        <td><strong>{{ t.symbol }}</strong></td>
        <td class="{{ 'win' if t.status=='WIN' else 'loss' }}">{{ "&#9989; WIN" if t.status=="WIN" else "&#10060; LOSS" }}</td>
        <td>${{ "%.6g"|format(t.entry_price) }}</td>
        <td>${{ "%.6g"|format(t.exit_price) if t.exit_price else "&#8212;" }}</td>
        <td class="{{ 'win' if (t.pnl_usdt or 0)>=0 else 'loss' }}">${{ "%+.4f"|format(t.pnl_usdt) if t.pnl_usdt is not none else "&#8212;" }}</td>
        <td class="{{ 'win' if pct>=0 else 'loss' }}">{{ "%+.1f"|format(pct) }}%</td>
        <td>${{ "%.2f"|format(t.quantity_usdt) }}</td>
        <td>{{ t.notes.split('|')[0].strip() if t.notes else "&#8212;" }}</td>
        <td>{{ t.entry_time }}</td>
        <td>{{ t.exit_time if t.exit_time else "&#8212;" }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table>
  {% else %}
    <p class="empty">No closed trades yet.</p>
  {% endif %}
</div>

<p class="footer">SweptCoin AI &nbsp;&middot;&nbsp; {{ now }}</p>
<script>
  // ── Live price polling every 5 seconds ──────────────────────────────────
  async function updateLive() {
    try {
      const d = await fetch('/api/live').then(r => r.json());
      const fmt = (n, dec=4) => (n >= 0 ? '+' : '') + n.toFixed(dec);

      // Balance card
      const bal = document.getElementById('live-balance');
      const dlt = document.getElementById('live-delta');
      if (bal) { bal.textContent = '$' + d.live_balance.toFixed(2); bal.className = 'val ' + (d.balance_delta >= 0 ? 'green' : 'red'); }
      if (dlt) { dlt.textContent = (d.balance_delta >= 0 ? '\u25b2' : '\u25bc') + ' $' + fmt(d.balance_delta); dlt.className = 'sub ' + (d.balance_delta >= 0 ? 'green' : 'red'); }

      // Unrealized PnL card
      const unr = document.getElementById('live-unrealized');
      const unrsub = document.getElementById('live-unrealized-sub');
      if (unr) { unr.textContent = '$' + fmt(d.total_unrealized); unr.className = 'val ' + (d.total_unrealized >= 0 ? 'green' : 'red'); }
      if (unrsub) { unrsub.textContent = d.positions.length + ' open position' + (d.positions.length !== 1 ? 's' : ''); }

      // Per-position current price + unrealized PnL
      d.positions.forEach(p => {
        const priceEl = document.getElementById('cprice-' + p.id);
        const pnlEl   = document.getElementById('upnl-'   + p.id);
        if (priceEl) priceEl.textContent = '$' + p.current_price;
        if (pnlEl) {
          pnlEl.textContent = '$' + fmt(p.unrealized_pnl) + ' (' + fmt(p.pnl_pct, 2) + '%)';
          pnlEl.className = p.unrealized_pnl >= 0 ? 'win' : 'loss';
        }
      });
    } catch(e) { console.warn('Live update failed:', e); }
  }
  setInterval(updateLive, 5000);
  updateLive();

  // ── Page reload countdown (every 60 s) ───────────────────────────────────
  let s = 60;
  const el = document.getElementById('tick');
  setInterval(() => {
    s--;
    if (s <= 0) { location.reload(); s = 60; }
    el.textContent = 'reload in ' + s + 's';
  }, 1000);
</script>
</body>
</html>
"""


# ─── Data helpers ─────────────────────────────────────────────────────────────

def _get_data():
    engine = create_engine(config.DATABASE_URL, connect_args={"check_same_thread": False})

    # ── Trades ──────────────────────────────────────────────────────────────
    with engine.connect() as conn:
        trade_rows = conn.execute(text(
            "SELECT id, symbol, entry_price, exit_price, take_profit, stop_loss, "
            "trailing_stop, quantity_usdt, is_paper, status, entry_time, exit_time, "
            "pnl_usdt, notes FROM trades ORDER BY id ASC"
        )).fetchall()

    class TradeRow:
        def __init__(self, r):
            self.id            = r[0]
            self.symbol        = r[1]
            self.entry_price   = r[2] or 0
            self.exit_price    = r[3]
            self.take_profit   = r[4] or 0
            self.stop_loss     = r[5] or 0
            self.trailing_stop = r[6]
            self.quantity_usdt = r[7] or 0
            self.is_paper      = bool(r[8])
            self.status        = r[9] or "OPEN"
            self.entry_time    = r[10]
            self.exit_time     = r[11]
            self.pnl_usdt      = r[12]
            self.notes         = r[13] or ""

    all_trades    = [TradeRow(r) for r in trade_rows]
    open_trades   = [t for t in all_trades if t.status == "OPEN"]
    closed_trades = [t for t in all_trades if t.status in ("WIN", "LOSS")]
    closed_trades.reverse()  # newest first
    wins   = [t for t in closed_trades if t.status == "WIN"]
    losses = [t for t in closed_trades if t.status == "LOSS"]

    total_pnl = sum(t.pnl_usdt or 0 for t in closed_trades)
    win_rate  = (len(wins) / len(closed_trades) * 100) if closed_trades else 0.0
    best      = max((t.pnl_usdt or 0 for t in closed_trades), default=0.0)
    worst     = min((t.pnl_usdt or 0 for t in closed_trades), default=0.0)

    open_symbols = {t.symbol for t in open_trades}

    # ── Watchlist (screened_coins) ───────────────────────────────────────────
    class CoinRow:
        pass

    watchlist = []
    try:
        with engine.connect() as conn:
            sc_rows = conn.execute(text(
                "SELECT symbol, market_cap_usd, age_days, partnership_score, "
                "sentiment_score, last_screened, last_signal, signal_reason "
                "FROM screened_coins WHERE is_active = 1 "
                "ORDER BY id DESC"
            )).fetchall()
        # De-duplicate: keep only the latest row per symbol
        seen = set()
        for r in sc_rows:
            sym = r[0]
            if sym in seen:
                continue
            seen.add(sym)
            c = CoinRow()
            c.symbol          = sym
            c.market_cap_usd  = r[1] or 0
            c.age_days        = r[2] or 0
            c.partnership_score = r[3] or 0
            c.sentiment_score = r[4] or 0.0
            c.last_screened   = r[5] or ""
            c.last_signal     = r[6] or "HOLD"
            c.signal_reason   = r[7] or ""
            c.sentiment_dir   = (
                "BULLISH" if c.sentiment_score > 0.05
                else "BEARISH" if c.sentiment_score < -0.05
                else "NEUTRAL"
            )
            watchlist.append(c)
    except Exception:
        pass  # screened_coins table may not exist yet

    return {
        "open_trades":    open_trades,
        "closed_trades":  closed_trades,
        "open_count":     len(open_trades),
        "closed_count":   len(closed_trades),
        "wins":           len(wins),
        "losses":         len(losses),
        "total_pnl":      total_pnl,
        "win_rate":       win_rate,
        "best":           best,
        "worst":          worst,
        "balance_start":  config.PAPER_STARTING_BALANCE,
        "balance_now":    config.PAPER_STARTING_BALANCE + total_pnl,
        "balance_delta":  total_pnl,
        "max_positions":  config.MAX_OPEN_POSITIONS,
        "mode":           "PAPER" if config.PAPER_TRADING else "LIVE",
        "testnet":        config.BYBIT_TESTNET,
        "watchlist":      watchlist,
        "open_symbols":   open_symbols,
    }


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/api/live")
def live_data():
    """Return current prices + unrealized PnL for open positions (polled by JS every 5 s).
    Always uses the real Bybit API for prices — paper trading simulates against real market data."""
    # Use real API for prices regardless of BYBIT_TESTNET.
    # Testnet prices are simulated/fake; paper trading should track actual market prices.
    base_url = "https://api.bybit.com"
    engine = create_engine(config.DATABASE_URL, connect_args={"check_same_thread": False})
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, symbol, entry_price, quantity_usdt FROM trades WHERE status='OPEN'"
        )).fetchall()
        realized = conn.execute(text(
            "SELECT COALESCE(SUM(pnl_usdt), 0.0) FROM trades WHERE status IN ('WIN','LOSS')"
        )).scalar() or 0.0

    positions = []
    total_unrealized = 0.0
    for r in rows:
        tid, symbol, entry_price, qty_usdt = r[0], r[1], float(r[2] or 0), float(r[3] or 0)
        current_price = entry_price  # fallback
        try:
            resp = _req.get(
                f"{base_url}/v5/market/tickers",
                params={"category": "spot", "symbol": symbol},
                timeout=3,
            ).json()
            current_price = float(resp["result"]["list"][0]["lastPrice"])
        except Exception:
            pass
        unrealized = round((current_price / entry_price - 1) * qty_usdt, 4) if entry_price else 0.0
        pnl_pct    = round((current_price / entry_price - 1) * 100, 2) if entry_price else 0.0
        total_unrealized += unrealized
        positions.append({
            "id": tid,
            "symbol": symbol,
            "current_price": current_price,
            "unrealized_pnl": unrealized,
            "pnl_pct": pnl_pct,
        })

    live_balance = round(config.PAPER_STARTING_BALANCE + float(realized) + total_unrealized, 2)
    return jsonify({
        "positions": positions,
        "total_unrealized": round(total_unrealized, 4),
        "realized_pnl": round(float(realized), 4),
        "live_balance": live_balance,
        "balance_delta": round(live_balance - config.PAPER_STARTING_BALANCE, 4),
    })


@app.route("/")
def index():
    try:
        data = _get_data()
    except Exception as e:
        return f"<pre style='color:red;padding:20px'>Error reading database:\n{e}</pre>", 500
    data["now"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return render_template_string(TEMPLATE, **data)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 50)
    print("  SweptCoin Dashboard")
    print("  Open: http://localhost:5000")
    print("  Stop: Ctrl+C")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=False)

