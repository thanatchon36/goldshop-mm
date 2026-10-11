"""Backtest report: summary.json, timeseries.csv, fills.csv and a self-contained report.html."""

from __future__ import annotations

import csv
import html
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from goldshop.backtest.replay import BacktestResult, Point


def summarize(r: BacktestResult) -> dict[str, Any]:
    last = r.series[-1] if r.series else Point(0, 0, 0, 0, 0, 0, 0, 0)
    first = r.series[0] if r.series else last
    n = len(r.fills)
    by_h: dict[int, list[float]] = {}
    for s in r.markouts:
        by_h.setdefault(s.horizon_ms, []).append(s.bps)
    peak, mdd = -math.inf, 0.0
    for p in r.series:
        peak = max(peak, p.net)
        mdd = max(mdd, peak - p.net)
    return {
        "symbol": r.symbol,
        "start": _iso(first.ts_ms),
        "end": _iso(last.ts_ms),
        "hours": round((last.ts_ms - first.ts_ms) / 3_600_000, 3),
        "net_pnl": last.net,
        "gross_pnl": last.realized + last.unrealized,
        "fees": last.fees,
        "funding": last.funding,
        "fills": n,
        "buys": sum(1 for f in r.fills if f.side.value == "BUY"),
        "sells": sum(1 for f in r.fills if f.side.value == "SELL"),
        "volume_usdt": sum(f.price * f.qty for f in r.fills),
        "avg_edge_bps": r.edge_bps_sum / n if n else None,
        "markout_avg_bps": {f"+{h // 1000}s": sum(v) / len(v) for h, v in sorted(by_h.items())},
        "final_inventory": last.inventory,
        "max_abs_inventory": max((abs(p.inventory) for p in r.series), default=0.0),
        "max_drawdown": mdd,
        "rejects_gtx": r.rejects,
        "stale_gaps": r.stale_gaps,
        "params": {
            "gamma": r.params.gamma, "kappa": r.params.kappa, "horizon_s": r.params.horizon_s,
            "maker_fee_bps": r.params.maker_fee_bps, "min_spread_bps": r.params.min_spread_bps,
            "order_qty": r.params.order_qty, "max_inventory_qty": r.params.max_inventory_qty,
            "latency_ms": r.latency_ms,
        },
    }


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def write_report(r: BacktestResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize(r)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    with open(out_dir / "timeseries.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ts_ms", "mid", "net", "realized", "unrealized", "fees", "funding", "inventory"])
        for p in r.series:
            w.writerow([p.ts_ms, p.mid, p.net, p.realized, p.unrealized, p.fees, p.funding, p.inventory])
    with open(out_dir / "fills.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ts_ms", "side", "price", "qty", "fee", "client_id"])
        for f in r.fills:
            w.writerow([f.ts_ms, f.side.value, f.price, f.qty, f.fee, f.client_id])
    path = out_dir / "report.html"
    path.write_text(_html(r, summary))
    return path


# --------------------------------------------------------------------------- html

def _downsample(points: list[Point], n: int = 1500) -> list[Point]:
    if len(points) <= n:
        return points
    step = len(points) / n
    out = [points[int(i * step)] for i in range(n)]
    out[-1] = points[-1]
    return out


def _histogram(values: list[float], bins: int = 30) -> list[tuple[float, float, int]]:
    if not values:
        return []
    lo, hi = min(values), max(values)
    if lo == hi:
        return [(lo - 0.5, hi + 0.5, len(values))]
    w = (hi - lo) / bins
    counts = [0] * bins
    for v in values:
        counts[min(int((v - lo) / w), bins - 1)] += 1
    return [(lo + i * w, lo + (i + 1) * w, c) for i, c in enumerate(counts)]


def _fmt(x: float | None, nd: int = 2, unit: str = "") -> str:
    return "n/a" if x is None else f"{x:+,.{nd}f}{unit}"


def _html(r: BacktestResult, s: dict[str, Any]) -> str:
    pts = _downsample(r.series)
    pnl = [[p.ts_ms, round(p.net, 6)] for p in pts]
    inv = [[p.ts_ms, p.inventory] for p in pts]
    hists = {}
    for h in (1000, 5000, 30000):
        vals = [m.bps for m in r.markouts if m.horizon_ms == h]
        hists[f"+{h // 1000}s"] = {"bins": [[a, b, c] for a, b, c in _histogram(vals)], "n": len(vals),
                                   "mean": (sum(vals) / len(vals)) if vals else None}
    data = json.dumps({"pnl": pnl, "inv": inv, "hists": hists})
    mo = s["markout_avg_bps"]
    tiles = [
        ("Net PnL", _fmt(s["net_pnl"], 4, " USDT")),
        ("Gross (before fees)", _fmt(s["gross_pnl"], 4, " USDT")),
        ("Fees", _fmt(-s["fees"], 4, " USDT")),
        ("Funding", _fmt(s["funding"], 4, " USDT")),
        ("Fills", f'{s["fills"]:,} ({s["buys"]} buy / {s["sells"]} sell)'),
        ("Volume", f'{s["volume_usdt"]:,.0f} USDT'),
        ("Avg edge vs mid", _fmt(s["avg_edge_bps"], 2, " bps")),
        ("Max drawdown", f'{s["max_drawdown"]:,.4f} USDT'),
    ]
    tiles_html = "".join(
        f'<div class="tile"><div class="tl">{html.escape(k)}</div><div class="tv">{html.escape(v)}</div></div>'
        for k, v in tiles
    )
    prm = s["params"]
    rows = [
        ("Period", f'{s["start"]} → {s["end"]} ({s["hours"]} h)'),
        ("Markout +1s / +5s / +30s",
         " / ".join(_fmt(mo.get(k), 2) for k in ("+1s", "+5s", "+30s")) + " bps"),
        ("Final / max |inventory|", f'{s["final_inventory"]:+g} / {s["max_abs_inventory"]:g}'),
        ("GTX rejects", str(s["rejects_gtx"])),
        ("Data gaps treated as stale", str(s["stale_gaps"])),
        ("Order / max inventory qty", f'{prm["order_qty"]:g} / {prm["max_inventory_qty"]:g}'),
        ("γ / κ / τ", f'{prm["gamma"]} / {prm["kappa"]} / {prm["horizon_s"]}s'),
        ("Maker fee / min spread", f'{prm["maker_fee_bps"]} bps / {prm["min_spread_bps"]} bps'),
        ("Simulated latency", f'{prm["latency_ms"]} ms'),
    ]
    rows_html = "".join(f"<tr><th>{html.escape(a)}</th><td>{html.escape(b)}</td></tr>" for a, b in rows)
    title = f"Backtest {r.symbol}"
    return _TEMPLATE.replace("__TITLE__", html.escape(title)).replace(
        "__TILES__", tiles_html).replace("__ROWS__", rows_html).replace("__DATA__", data)


_TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{color-scheme:light;--surface:#fcfcfb;--card:#ffffff;--text:#0b0b0b;--text2:#52514e;--muted:#8a897f;
--grid:#e7e6e1;--axis:#c9c8c0;--series:#2a78d6;--zero:#8a897f;--border:#e7e6e1}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--surface:#1a1a19;
--card:#222221;--text:#f0efec;--text2:#c3c2b7;--muted:#8f8e86;--grid:#2f2f2d;--axis:#4a4945;--series:#3987e5;
--zero:#8f8e86;--border:#2f2f2d}}
:root[data-theme="dark"]{color-scheme:dark;--surface:#1a1a19;--card:#222221;--text:#f0efec;--text2:#c3c2b7;
--muted:#8f8e86;--grid:#2f2f2d;--axis:#4a4945;--series:#3987e5;--zero:#8f8e86;--border:#2f2f2d}
*{box-sizing:border-box}body{margin:0;background:var(--surface);color:var(--text);
font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
main{max-width:1100px;margin:0 auto;padding:24px 16px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:0 0 8px}.sub{color:var(--text2);margin:0 0 20px}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:12px;margin-bottom:20px}
.tile,.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 14px}
.tl{color:var(--text2);font-size:12px}.tv{font-size:17px;font-weight:600;font-variant-numeric:tabular-nums}
.card{margin-bottom:16px;position:relative}.hint{color:var(--muted);font-size:12px;margin:-4px 0 8px}
svg{display:block;width:100%;height:auto;overflow:visible}
svg text{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
.grid line{stroke:var(--grid)}.zero{stroke:var(--zero);stroke-dasharray:3 3}
.line{fill:none;stroke:var(--series);stroke-width:2;stroke-linejoin:round}
.bar{fill:var(--series)}.bar:hover{opacity:.75}.cross{stroke:var(--axis)}
.dot{fill:var(--series);stroke:var(--card);stroke-width:2}
.tip{position:absolute;pointer-events:none;background:var(--card);border:1px solid var(--border);
border-radius:6px;padding:4px 8px;font-size:12px;color:var(--text);white-space:nowrap;display:none;
box-shadow:0 2px 8px rgba(0,0,0,.12)}
.hgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
table{border-collapse:collapse;width:100%}th,td{text-align:left;padding:6px 8px;border-top:1px solid var(--border);
font-variant-numeric:tabular-nums}th{color:var(--text2);font-weight:500;width:40%}
</style></head><body><main>
<h1>__TITLE__</h1>
<p class="sub">Replay of recorded tick data through the paper fill engine (fills only on trade-through, latency, GTX rejects, maker fees, funding).</p>
<div class="tiles">__TILES__</div>
<div class="card"><h2>Net PnL (USDT, after fees and funding)</h2><div id="pnl"></div><div class="tip"></div></div>
<div class="card"><h2>Inventory (base qty)</h2><div id="inv"></div><div class="tip"></div></div>
<div class="card"><h2>Markout distribution (bps; positive = price moved in our favour)</h2>
<p class="hint">Count of fills per markout bucket at each horizon. Dashed line = 0.</p>
<div class="hgrid" id="hists"></div><div class="tip"></div></div>
<div class="card"><h2>Details</h2><table>__ROWS__</table></div>
</main>
<script>
const D = __DATA__;
const NS = "http://www.w3.org/2000/svg";
const el = (n, a = {}, p) => { const e = document.createElementNS(NS, n);
  for (const k in a) e.setAttribute(k, a[k]); if (p) p.appendChild(e); return e; };
function ticks(lo, hi, n) { if (lo === hi) { lo -= 1; hi += 1; }
  const raw = (hi - lo) / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 5, 10].map(m => m * mag).find(s => s >= raw);
  const out = []; for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-12; v += step) out.push(+v.toPrecision(12));
  return out; }
const fmtT = ms => new Date(ms).toISOString().slice(5, 16).replace("T", " ");
const fmtN = v => Math.abs(v) >= 100 ? v.toFixed(0) : Math.abs(v) >= 1 ? v.toFixed(2) : v.toPrecision(2);
function lineChart(id, pts, unit) {
  const host = document.getElementById(id), tip = host.parentElement.querySelector(".tip");
  const W = 1000, H = 260, L = 60, R = 16, T = 10, B = 28;
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" }, host);
  if (!pts.length) { el("text", { x: W / 2, y: H / 2, "text-anchor": "middle" }, svg).textContent = "no data"; return; }
  const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
  const x0 = xs[0], x1 = Math.max(xs[xs.length - 1], x0 + 1);
  let y0 = Math.min(0, ...ys), y1 = Math.max(0, ...ys); if (y0 === y1) { y0 -= 1; y1 += 1; }
  const pad = (y1 - y0) * 0.06; y0 -= pad; y1 += pad;
  const X = v => L + (v - x0) / (x1 - x0) * (W - L - R), Y = v => T + (y1 - v) / (y1 - y0) * (H - T - B);
  const g = el("g", { class: "grid" }, svg);
  for (const t of ticks(y0, y1, 5)) { el("line", { x1: L, x2: W - R, y1: Y(t), y2: Y(t) }, g);
    const tx = el("text", { x: L - 6, y: Y(t) + 4, "text-anchor": "end" }, svg); tx.textContent = fmtN(t); }
  for (let i = 0; i <= 4; i++) { const v = x0 + (x1 - x0) * i / 4;
    const tx = el("text", { x: X(v), y: H - 8, "text-anchor": i === 0 ? "start" : i === 4 ? "end" : "middle" }, svg);
    tx.textContent = fmtT(v); }
  el("line", { class: "zero", x1: L, x2: W - R, y1: Y(0), y2: Y(0) }, svg);
  el("path", { class: "line", d: pts.map((p, i) => (i ? "L" : "M") + X(p[0]).toFixed(1) + "," + Y(p[1]).toFixed(1)).join("") }, svg);
  const cross = el("line", { class: "cross", y1: T, y2: H - B, visibility: "hidden" }, svg);
  const dot = el("circle", { class: "dot", r: 5, visibility: "hidden" }, svg);
  const hit = el("rect", { x: L, y: T, width: W - L - R, height: H - T - B, fill: "transparent" }, svg);
  hit.addEventListener("pointermove", e => {
    const b = svg.getBoundingClientRect(), vx = (e.clientX - b.left) / b.width * W;
    const t = x0 + (vx - L) / (W - L - R) * (x1 - x0);
    let lo = 0, hi = pts.length - 1; while (hi - lo > 1) { const m = (lo + hi) >> 1; (pts[m][0] < t ? lo = m : hi = m); }
    const p = Math.abs(pts[lo][0] - t) < Math.abs(pts[hi][0] - t) ? pts[lo] : pts[hi];
    cross.setAttribute("x1", X(p[0])); cross.setAttribute("x2", X(p[0])); cross.setAttribute("visibility", "visible");
    dot.setAttribute("cx", X(p[0])); dot.setAttribute("cy", Y(p[1])); dot.setAttribute("visibility", "visible");
    tip.style.display = "block"; tip.textContent = `${fmtT(p[0])} UTC · ${p[1].toPrecision(6)} ${unit}`;
    const hb = host.parentElement.getBoundingClientRect();
    tip.style.left = Math.min(e.clientX - hb.left + 12, hb.width - tip.offsetWidth - 8) + "px";
    tip.style.top = (e.clientY - hb.top - 34) + "px"; });
  hit.addEventListener("pointerleave", () => { tip.style.display = "none";
    cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); });
}
function hist(host, name, h, tip) {
  const card = document.createElement("div"); host.appendChild(card);
  const title = document.createElement("div"); title.className = "tl";
  title.textContent = `${name} · n=${h.n} · mean ${h.mean == null ? "n/a" : h.mean.toFixed(2) + " bps"}`;
  card.appendChild(title);
  const W = 320, H = 170, L = 34, R = 8, T = 8, B = 24;
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" }, card);
  if (!h.bins.length) { el("text", { x: W / 2, y: H / 2, "text-anchor": "middle" }, svg).textContent = "no fills"; return; }
  const x0 = Math.min(h.bins[0][0], 0), x1 = Math.max(h.bins[h.bins.length - 1][1], 0);
  const cmax = Math.max(...h.bins.map(b => b[2]));
  const X = v => L + (v - x0) / (x1 - x0) * (W - L - R), Y = c => T + (1 - c / cmax) * (H - T - B);
  const g = el("g", { class: "grid" }, svg);
  for (const t of ticks(0, cmax, 3)) { el("line", { x1: L, x2: W - R, y1: Y(t), y2: Y(t) }, g);
    el("text", { x: L - 4, y: Y(t) + 4, "text-anchor": "end" }, svg).textContent = t; }
  for (const t of ticks(x0, x1, 4)) el("text", { x: X(t), y: H - 6, "text-anchor": "middle" }, svg).textContent = fmtN(t);
  for (const [a, b, c] of h.bins) { if (!c) continue;
    const x = X(a) + 1, w = Math.max(X(b) - X(a) - 2, 1), y = Y(c), hgt = Y(0) - y;
    const r = Math.min(4, w / 2, hgt);
    const bar = el("path", { class: "bar", d: `M${x},${Y(0)}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${Y(0)}Z` }, svg);
    bar.addEventListener("pointermove", e => { tip.style.display = "block";
      tip.textContent = `${name}: ${a.toFixed(2)} … ${b.toFixed(2)} bps · ${c} fills`;
      const hb = host.parentElement.getBoundingClientRect();
      tip.style.left = Math.min(e.clientX - hb.left + 12, hb.width - tip.offsetWidth - 8) + "px";
      tip.style.top = (e.clientY - hb.top - 34) + "px"; });
    bar.addEventListener("pointerleave", () => tip.style.display = "none"); }
  el("line", { class: "zero", x1: X(0), x2: X(0), y1: T, y2: H - B }, svg);
}
lineChart("pnl", D.pnl, "USDT");
lineChart("inv", D.inv, "");
const hh = document.getElementById("hists"), htip = hh.parentElement.querySelector(".tip");
for (const k of ["+1s", "+5s", "+30s"]) hist(hh, k, D.hists[k], htip);
</script></body></html>
"""
