// Galyari browser renderer (Phase 5B / 5A Tier 2). Pure view: consumes per-symbol frame
// messages from bridge.py and paints a Bookmap-style scrolling liquidity heatmap (blue =
// resting size), green/red trade bubbles + bid/ask lines, a volume profile, Δ + CVD strips,
// and a right-edge price ladder. One symbol → full view; several → a grid of live
// heatmaps (click a tile to expand, Esc to return). Liquidity is stored at ABSOLUTE price
// bins, so zoom is a lossless display crop — never a rebuild. No build step, no library.
"use strict";

const WS_PORT = 8765;                                // matches bridge --ws-port default
const C = { bg: "#070b12", panel: "#0d1420", grid: "#27384d", fg: "#dde6f0",
            muted: "#93a4bd", buy: "#36d68f", sell: "#ff6b66", mid: "#eaf1f8",
            vp: "#5a9fd4", poc: "#ffd23f" };
const BG_RGB = [7, 11, 18];
// blue ramp (navy → teal → cyan → near-white): resting size lifts through a gamma so thin
// books still read, like the reference heatmap.
const RAMP = [[10,16,28],[16,42,66],[20,74,108],[28,112,150],[60,160,196],
              [120,205,230],[205,240,252]];
function ramp(t) {
  t = Math.max(0, Math.min(1, t)) * (RAMP.length - 1);
  const i = Math.floor(t), f = t - i, a = RAMP[i], b = RAMP[Math.min(i + 1, RAMP.length - 1)];
  return [a[0] + (b[0]-a[0])*f, a[1] + (b[1]-a[1])*f, a[2] + (b[2]-a[2])*f];
}
const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, x));

let tick = 0.01, cols = 240, live = false, multi = false;
let order = [], expanded = null;
const views = new Map();
let dirty = true;

// --- per-symbol rolling state (absolute-bin storage) ------------------------
class View {
  constructor(sym) {
    this.sym = sym; this.tick = tick; this.cols = cols;
    this.cells = Array.from({ length: cols }, () => new Map());   // absRow -> volume
    this.mid = Array(cols).fill(NaN); this.bid = Array(cols).fill(NaN);
    this.ask = Array(cols).fill(NaN); this.cvd = Array(cols).fill(NaN);
    this.delta = Array(cols).fill(0);
    this.trades = []; this.sizeRef = 100; this.lastCvd = 0; this.lastMid = NaN;
    this.lastSpread = NaN; this.lastImb = 0; this.book = { bids: [], asks: [] };
    this.viewRows = 0; this.center = null;
    this.off = document.createElement("canvas"); this.offctx = this.off.getContext("2d");
  }
  absRow(p) { return Math.round(p / this.tick); }
  fitToBook(bids, asks) {
    // frame the actual book on first sight: span the levels (in rows) + margin, clamped.
    // Price-relative bands break on cheap stocks (a $1.44 book is sub-tick at 0.15%), so
    // fit to the real data instead, then let the user zoom freely.
    const ps = bids.concat(asks).map(l => l[0]);
    const span = ps.length ? (Math.max(...ps) - Math.min(...ps)) / this.tick : 80;
    this.viewRows = clamp(Math.round(span * 1.3), 40, 260);
  }
  updateCenter() {
    if (Number.isNaN(this.lastMid)) return;
    if (this.center === null) this.center = this.lastMid;
    if (Math.abs((this.lastMid - this.center) / this.tick) > this.viewRows * 0.3)
      this.center = this.lastMid;                    // recenter only on real drift (no jitter)
  }
  zoom(f) {                                          // scale rows directly: f<1 in, f>1 out
    if (this.viewRows) { this.viewRows = clamp(Math.round(this.viewRows * f), 16, 1500); dirty = true; }
  }
  ingest(p) {
    const col = new Map();
    for (const [pr, v] of (p.bids || []).concat(p.asks || [])) col.set(this.absRow(pr), v);
    this.cells.shift(); this.cells.push(col);
    this.mid.shift(); this.mid.push(p.mid == null ? NaN : p.mid);
    this.bid.shift(); this.bid.push(p.bids && p.bids.length ? p.bids[0][0] : NaN);
    this.ask.shift(); this.ask.push(p.asks && p.asks.length ? p.asks[0][0] : NaN);
    this.cvd.shift(); this.cvd.push(p.cvd);
    this.delta.shift(); this.delta.push(p.delta || 0);
    for (const t of this.trades) t.age += 1;
    this.trades = this.trades.filter(t => t.age < this.cols);
    for (const tr of (p.trades || [])) {
      this.trades.push({ age: 0, price: tr.p, size: tr.s, side: tr.side });
      this.sizeRef = Math.max(1, 0.94 * this.sizeRef + 0.06 * tr.s);
    }
    if (p.mid != null) {
      this.lastMid = p.mid;
      if (!this.viewRows) this.fitToBook(p.bids || [], p.asks || []);
    }
    this.updateCenter();
    this.lastCvd = p.cvd; this.lastSpread = p.spread == null ? NaN : p.spread;
    this.lastImb = p.imbalance || 0; this.book = { bids: p.bids || [], asks: p.asks || [] };
  }
  loAbs() { return this.absRow(this.center) - (this.viewRows >> 1); }
  vmax(lo) {
    const hi = lo + this.viewRows, nz = [];
    for (const col of this.cells) for (const [r, v] of col) if (r >= lo && r < hi && v > 0) nz.push(v);
    if (!nz.length) return 1;
    nz.sort((a, b) => a - b);
    const med = nz[nz.length >> 1], p97 = nz[Math.min(nz.length - 1, Math.floor(nz.length * 0.97))];
    return Math.max(1, Math.min(p97, med * 6));
  }
}
function getView(sym) {
  let v = views.get(sym);
  if (!v) { v = new View(sym); views.set(sym, v); }
  return v;
}

// --- canvas + geometry ------------------------------------------------------
const canvas = document.getElementById("c"), ctx = canvas.getContext("2d");
let W = 0, H = 0, DPR = 1;
const hitTiles = [];
let backHot = null;

function resize() {
  DPR = window.devicePixelRatio || 1;
  W = canvas.clientWidth; H = canvas.clientHeight;
  canvas.width = W * DPR; canvas.height = H * DPR;
  ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  dirty = true;
}
window.addEventListener("resize", resize);

// paint a view's absolute-bin matrix into (x,y,w,h), cropped to its current window
function blit(v, x, y, w, h) {
  if (!v.viewRows || Number.isNaN(v.lastMid)) return null;
  const rows = v.viewRows, lo = v.loAbs(), vm = v.vmax(lo);
  if (v.off.width !== v.cols || v.off.height !== rows) { v.off.width = v.cols; v.off.height = rows; }
  const img = v.offctx.createImageData(v.cols, rows), d = img.data;
  for (let i = 0; i < d.length; i += 4) { d[i] = BG_RGB[0]; d[i+1] = BG_RGB[1]; d[i+2] = BG_RGB[2]; d[i+3] = 255; }
  for (let c = 0; c < v.cols; c++) {
    for (const [absr, val] of v.cells[c]) {
      const r = absr - lo;
      if (r < 0 || r >= rows || val <= 0) continue;
      const px = (((rows - 1 - r) * v.cols) + c) * 4;
      const [rr, gg, bb] = ramp(Math.pow(Math.min(val / vm, 1), 0.5));
      d[px] = rr; d[px+1] = gg; d[px+2] = bb; d[px+3] = 255;
    }
  }
  v.offctx.putImageData(img, 0, 0);
  ctx.imageSmoothingEnabled = false; ctx.drawImage(v.off, x, y, w, h);
  return { lo, rows };
}
function mapper(v, x, y, w, h, lo, rows) {
  return {
    yOf: (p) => y + h - ((p / v.tick - lo) / (rows - 1)) * h,
    xOf: (c) => x + c / (v.cols - 1) * w,
  };
}
function polyline(v, hist, m, color, lw, alpha) {
  ctx.strokeStyle = color; ctx.lineWidth = lw; ctx.globalAlpha = alpha;
  ctx.beginPath(); let pen = false;
  for (let c = 0; c < v.cols; c++) {
    const p = hist[c];
    if (Number.isNaN(p)) { pen = false; continue; }
    const X = m.xOf(c), Y = m.yOf(p);
    pen ? ctx.lineTo(X, Y) : ctx.moveTo(X, Y); pen = true;
  }
  ctx.stroke(); ctx.globalAlpha = 1;
}
function drawTrades(v, m, scale) {
  for (const t of v.trades) {
    const rad = Math.max(2.6, Math.min(17, 2.6 + 5.5 * Math.sqrt(t.size / v.sizeRef))) * scale;
    const X = m.xOf(v.cols - 1 - t.age), Y = m.yOf(t.price);
    ctx.beginPath(); ctx.arc(X, Y, rad, 0, 6.2832);
    ctx.fillStyle = t.side > 0 ? C.buy : t.side < 0 ? C.sell : C.muted;
    ctx.globalAlpha = 0.95; ctx.fill();
    ctx.globalAlpha = 1; ctx.lineWidth = 1.4; ctx.strokeStyle = "#05080e"; ctx.stroke();
  }
}
function spreadBand(v, m) {
  ctx.fillStyle = C.mid; ctx.globalAlpha = 0.08; ctx.beginPath();
  let started = false;
  for (let c = 0; c < v.cols; c++) {
    if (Number.isNaN(v.bid[c]) || Number.isNaN(v.ask[c])) continue;
    const X = m.xOf(c);
    started ? ctx.lineTo(X, m.yOf(v.ask[c])) : ctx.moveTo(X, m.yOf(v.ask[c])); started = true;
  }
  for (let c = v.cols - 1; c >= 0; c--) {
    if (Number.isNaN(v.bid[c]) || Number.isNaN(v.ask[c])) continue;
    ctx.lineTo(m.xOf(c), m.yOf(v.bid[c]));
  }
  if (started) ctx.fill(); ctx.globalAlpha = 1;
}

// --- compact grid tile ------------------------------------------------------
function drawTile(v, R) {
  ctx.fillStyle = C.panel; ctx.fillRect(R.x, R.y, R.w, R.h);
  const hy = R.y + 20, hh = R.h - 24, hx = R.x + 2, hw = R.w - 4;
  const win = blit(v, hx, hy, hw, hh);
  if (win) {
    const m = mapper(v, hx, hy, hw, hh, win.lo, win.rows);
    spreadBand(v, m); polyline(v, v.mid, m, C.mid, 1.2, 0.9); drawTrades(v, m, 0.7);
  }
  ctx.strokeStyle = C.grid; ctx.lineWidth = 0.8; ctx.strokeRect(R.x + 0.5, R.y + 0.5, R.w - 1, R.h - 1);
  ctx.textAlign = "left"; ctx.textBaseline = "middle";
  ctx.fillStyle = "#fff"; ctx.font = "bold 14px monospace"; ctx.fillText(v.sym, R.x + 8, R.y + 12);
  ctx.font = "12px monospace"; ctx.fillStyle = C.fg;
  ctx.fillText(Number.isNaN(v.lastMid) ? "—" : v.lastMid.toFixed(2),
    R.x + 12 + ctx.measureText(v.sym).width, R.y + 12);
  ctx.textAlign = "right"; ctx.fillStyle = v.lastCvd >= 0 ? C.buy : C.sell;
  ctx.fillText(`CVD ${v.lastCvd >= 0 ? "+" : ""}${Math.round(v.lastCvd)}`, R.x + R.w - 8, R.y + 12);
  ctx.textAlign = "left";
}

// --- full single-symbol view ------------------------------------------------
const FM = { l: 58, t: 34, b: 12 };
const G = 12, VPW = 74, LADW = 100, AXISW = 50;      // VP + ladder + right-axis widths

function drawFull(v, R) {
  const rightW = VPW + LADW + AXISW + G * 3;
  const plotH = R.h - FM.t - FM.b, cvdH = plotH * 0.15, deltaH = plotH * 0.10;
  const heatH = plotH - cvdH - deltaH - G * 2;
  const hx = R.x + FM.l, hy = R.y + FM.t, hw = R.w - FM.l - rightW;
  const vpx = hx + hw + G, ladx = vpx + VPW + G, axisx = ladx + LADW + G;

  const win = blit(v, hx, hy, hw, heatH);
  if (win) {
    const m = mapper(v, hx, hy, hw, heatH, win.lo, win.rows);
    drawGrid(v, hx, hy, hw, heatH, axisx, win.lo, win.rows);    // price grid + dual axis + frame
    spreadBand(v, m);
    polyline(v, v.bid, m, C.buy, 1.3, 0.7); polyline(v, v.ask, m, C.sell, 1.3, 0.7);
    polyline(v, v.mid, m, "#05080e", 4.5, 0.65);               // dark casing for contrast
    polyline(v, v.mid, m, C.mid, 1.8, 1);                       // bright price line on top
    drawTrades(v, m, 1);
    drawVolumeProfile(v, m, vpx, hy, VPW, heatH, win.lo, win.rows);
    drawLadder(v, m, ladx, ladx + LADW, win.lo, win.rows);
    drawDelta(v, hx, hy + heatH + G, hw, deltaH);
    drawCvd(v, hx, hy + heatH + G + deltaH + G, hw, cvdH);
    timeGuides(v, hx, hy, hw, hy + heatH + G + deltaH + G + cvdH);  // vertical dashed, all panels
  }
  drawHeader(v, R);
}

// horizontal price grid (visible over the heatmap), dual price axis, and a crisp frame
function drawGrid(v, x, y, w, h, rightX, lo, rows) {
  ctx.font = "13px monospace"; ctx.textBaseline = "middle";
  for (let i = 0; i <= 8; i++) {
    const r = (i / 8) * (rows - 1), price = (lo + r) * v.tick;
    const Y = Math.round(y + h - (r / (rows - 1)) * h) + 0.5;
    ctx.strokeStyle = "#9fb4cf"; ctx.globalAlpha = 0.22; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, Y); ctx.lineTo(x + w, Y); ctx.stroke();
    ctx.globalAlpha = 1; ctx.fillStyle = "#eef4fb";
    ctx.textAlign = "right"; ctx.fillText(price.toFixed(2), x - 8, Y);     // left axis
    ctx.textAlign = "left"; ctx.fillText(price.toFixed(2), rightX, Y);     // right axis
  }
  ctx.strokeStyle = "#465d78"; ctx.lineWidth = 1;                          // frame edge
  ctx.strokeRect(x + 0.5, y + 0.5, w, h);
}

// vertical dashed time guides every 50 columns, spanning every stacked panel
function timeGuides(v, x, top, w, bottom) {
  ctx.strokeStyle = "#9fb4cf"; ctx.globalAlpha = 0.28; ctx.lineWidth = 1; ctx.setLineDash([3, 5]);
  for (let c = 50; c < v.cols; c += 50) {
    const X = Math.round(x + c / (v.cols - 1) * w) + 0.5;
    ctx.beginPath(); ctx.moveTo(X, top); ctx.lineTo(X, bottom); ctx.stroke();
  }
  ctx.setLineDash([]); ctx.globalAlpha = 1;
}

function drawVolumeProfile(v, m, x, y, w, h, lo, rows) {
  const vol = new Float32Array(rows);
  for (const t of v.trades) { const r = v.absRow(t.price) - lo; if (r >= 0 && r < rows) vol[r] += t.size; }
  let vmaxv = 1, poc = -1;
  for (let r = 0; r < rows; r++) if (vol[r] > vmaxv) { vmaxv = vol[r]; poc = r; }
  const rowH = Math.max(2, h / rows);
  ctx.fillStyle = C.vp; ctx.globalAlpha = 0.8;
  for (let r = 0; r < rows; r++) {
    if (!vol[r]) continue;
    ctx.fillRect(x, m.yOf((lo + r) * v.tick) - rowH / 2, (vol[r] / vmaxv) * (w - 2), rowH);
  }
  ctx.globalAlpha = 1;
  if (poc >= 0) {
    const Y = m.yOf((lo + poc) * v.tick);
    ctx.strokeStyle = C.poc; ctx.lineWidth = 1.6;
    ctx.beginPath(); ctx.moveTo(x, Y); ctx.lineTo(x + w, Y); ctx.stroke();
  }
  ctx.fillStyle = C.muted; ctx.font = "11px monospace"; ctx.textAlign = "left";
  ctx.textBaseline = "alphabetic"; ctx.fillText("vol@price", x, y - 8);
}

// right-edge DOM ladder: resting size at each visible price row, green (bid) / red (ask)
function drawLadder(v, m, x, xr, lo, rows) {
  const depth = new Map();                            // absRow -> [vol, side(+1 bid/-1 ask)]
  for (const [p, q] of v.book.bids) depth.set(v.absRow(p), [q, 1]);
  for (const [p, q] of v.book.asks) depth.set(v.absRow(p), [q, -1]);
  let mx = 1; for (const [, e] of depth) mx = Math.max(mx, e[0]);
  const barW = xr - x, step = Math.max(1, Math.round(rows / 26));
  ctx.font = "11px monospace"; ctx.textBaseline = "middle";
  for (let r = 0; r < rows; r += step) {
    const e = depth.get(lo + r); if (!e) continue;
    const Y = m.yOf((lo + r) * v.tick), col = e[1] > 0 ? C.buy : C.sell;
    ctx.fillStyle = col; ctx.globalAlpha = 0.45;
    ctx.fillRect(x, Y - Math.max(1.5, step * 0.4), (e[0] / mx) * barW, Math.max(3, step * 0.8));
    ctx.globalAlpha = 1; ctx.fillStyle = C.fg; ctx.textAlign = "right";
    ctx.fillText(String(e[0]), xr - 3, Y);
  }
}

function drawDelta(v, x, y, w, h) {
  const mid = y + h / 2; let dmax = 1;
  for (const d of v.delta) dmax = Math.max(dmax, Math.abs(d));
  ctx.fillStyle = C.grid; ctx.fillRect(x, mid, w, 0.5);
  const bw = w / v.cols;
  for (let c = 0; c < v.cols; c++) {
    const d = v.delta[c]; if (!d) continue;
    const bh = (d / dmax) * (h / 2 - 1);
    ctx.fillStyle = d > 0 ? C.buy : C.sell;
    ctx.fillRect(x + c / (v.cols - 1) * w - bw / 2, mid - Math.max(bh, 0), bw * 0.9, Math.abs(bh));
  }
  ctx.fillStyle = C.muted; ctx.font = "12px monospace";
  ctx.textAlign = "right"; ctx.textBaseline = "middle"; ctx.fillText("Δ", x - 8, mid);
  ctx.textAlign = "left";
}

function drawCvd(v, x, y, w, h) {
  let lo = Infinity, hi = -Infinity;
  for (const c of v.cvd) if (!Number.isNaN(c)) { lo = Math.min(lo, c); hi = Math.max(hi, c); }
  if (!isFinite(lo)) return;
  const span = (hi - lo) || 1, zeroY = y + h - ((0 - lo) / span) * h;
  ctx.strokeStyle = C.grid; ctx.lineWidth = 0.5;
  ctx.beginPath(); ctx.moveTo(x, zeroY); ctx.lineTo(x + w, zeroY); ctx.stroke();
  ctx.strokeStyle = v.lastCvd >= 0 ? C.buy : C.sell; ctx.lineWidth = 1.4;
  ctx.beginPath(); let pen = false;
  for (let c = 0; c < v.cols; c++) {
    const val = v.cvd[c]; if (Number.isNaN(val)) { pen = false; continue; }
    const X = x + c / (v.cols - 1) * w, Y = y + h - ((val - lo) / span) * h;
    pen ? ctx.lineTo(X, Y) : ctx.moveTo(X, Y); pen = true;
  }
  ctx.stroke();
  ctx.fillStyle = C.muted; ctx.font = "12px monospace";
  ctx.textAlign = "right"; ctx.textBaseline = "middle"; ctx.fillText("CVD", x - 8, y + h / 2);
  ctx.textAlign = "left";
}

function drawHeader(v, R) {
  const x0 = R.x + (multi ? 70 : FM.l);
  ctx.textAlign = "left"; ctx.textBaseline = "middle"; ctx.font = "bold 15px monospace";
  ctx.fillStyle = "#fff"; ctx.fillText(v.sym, x0, R.y + 16);
  ctx.font = "13px monospace"; ctx.fillStyle = C.fg;
  const sp = Number.isNaN(v.lastSpread) ? "—" : v.lastSpread.toFixed(2);
  ctx.fillText(`mid ${Number.isNaN(v.lastMid) ? "—" : v.lastMid.toFixed(2)}    spread ${sp}` +
    `    imb ${(v.lastImb * 100).toFixed(0)}%    CVD ${v.lastCvd >= 0 ? "+" : ""}${Math.round(v.lastCvd)}`,
    x0 + 58, R.y + 16);
  ctx.fillStyle = C.muted; ctx.textAlign = "right";
  ctx.fillText("scroll / ± to zoom", R.w - 12, R.y + 16); ctx.textAlign = "left";
  if (multi) {
    backHot = { x: R.x + 8, y: R.y + 5, w: 56, h: 24 };
    ctx.fillStyle = C.panel; ctx.fillRect(backHot.x, backHot.y, backHot.w, backHot.h);
    ctx.strokeStyle = C.grid; ctx.strokeRect(backHot.x + 0.5, backHot.y + 0.5, backHot.w - 1, backHot.h - 1);
    ctx.fillStyle = C.fg; ctx.font = "13px monospace"; ctx.textAlign = "center";
    ctx.fillText("‹ grid", backHot.x + 28, backHot.y + 13); ctx.textAlign = "left";
  } else backHot = null;
}

// --- top-level render -------------------------------------------------------
function render() {
  if (!W) resize();
  ctx.fillStyle = C.bg; ctx.fillRect(0, 0, W, H);
  hitTiles.length = 0;
  const syms = order.length ? order : [...views.keys()];
  if (multi && !expanded && syms.length > 1) {
    const n = syms.length, gc = Math.ceil(Math.sqrt(n)), gr = Math.ceil(n / gc), pad = 6;
    const cw = (W - pad * (gc + 1)) / gc, ch = (H - pad * (gr + 1)) / gr;
    syms.forEach((sym, i) => {
      const R = { x: pad + (i % gc) * (cw + pad), y: pad + Math.floor(i / gc) * (ch + pad), w: cw, h: ch };
      hitTiles.push({ ...R, symbol: sym }); drawTile(getView(sym), R);
    });
  } else {
    const sym = expanded || syms[0];
    if (sym) drawFull(getView(sym), { x: 0, y: 0, w: W, h: H });
  }
}
function loop() { requestAnimationFrame(loop); if (dirty) { render(); dirty = false; } }

// --- interaction ------------------------------------------------------------
function viewAt(mx, my) {
  if (expanded || !multi) {
    const s = expanded || order[0] || [...views.keys()][0];
    return s ? getView(s) : null;
  }
  for (const t of hitTiles)
    if (mx >= t.x && mx <= t.x + t.w && my >= t.y && my <= t.y + t.h) return getView(t.symbol);
  return null;
}
canvas.addEventListener("click", (e) => {
  if (backHot && e.offsetX >= backHot.x && e.offsetX <= backHot.x + backHot.w &&
      e.offsetY >= backHot.y && e.offsetY <= backHot.y + backHot.h) { expanded = null; dirty = true; return; }
  for (const t of hitTiles)
    if (e.offsetX >= t.x && e.offsetX <= t.x + t.w && e.offsetY >= t.y && e.offsetY <= t.y + t.h) {
      expanded = t.symbol; dirty = true; return;
    }
});
canvas.addEventListener("wheel", (e) => {
  const v = viewAt(e.offsetX, e.offsetY);
  if (v) { e.preventDefault(); v.zoom(e.deltaY < 0 ? 0.85 : 1.18); }
}, { passive: false });
window.addEventListener("keydown", (e) => {
  if (e.target.id === "go") return;
  if (e.key === "Escape") { expanded = null; dirty = true; }
  else if (e.key === "+" || e.key === "=" || e.key === "-") {
    const f = e.key === "-" ? 1.18 : 0.85;
    const targets = (expanded || !multi)
      ? [getView(expanded || order[0] || [...views.keys()][0])].filter(Boolean)
      : [...views.values()];
    targets.forEach(v => v.zoom(f));
  }
});

// --- websocket --------------------------------------------------------------
let ws;
function connect() {
  const status = document.getElementById("status");
  ws = new WebSocket(`ws://${location.hostname}:${WS_PORT}`);
  ws.onopen = () => { status.textContent = "connected"; status.className = "live"; };
  ws.onclose = () => { status.textContent = "disconnected — retrying"; status.className = "down"; setTimeout(connect, 1500); };
  ws.onmessage = (ev) => {
    const f = JSON.parse(ev.data);
    if (f.type === "hello") {
      live = f.live; multi = f.multi; tick = f.tick; cols = f.cols; order = f.order || [];
      views.clear(); expanded = null; dirty = true;
      document.getElementById("mode").textContent =
        live ? (multi ? "live · grid" : "live") : (multi ? "sim · grid" : "offline");
      document.getElementById("hint").textContent = multi
        ? "click a tile to expand · scroll to zoom"
        : (live ? "type a symbol to switch · scroll to zoom" : "scroll to zoom");
      document.getElementById("go").disabled = !live;
      updateTitle(); return;
    }
    if (f.type === "frame") {
      if (f.order && f.order.length) order = f.order;
      for (const sym in f.symbols) getView(sym).ingest(f.symbols[sym]);
      if (!order.length) order = Object.keys(f.symbols);
      updateTitle(); dirty = true;
    }
  };
}
function updateTitle() {
  const syms = order.length ? order : [...views.keys()];
  document.getElementById("symbol").textContent =
    (multi && !expanded) ? `grid · ${syms.length} symbols` : (expanded || syms[0] || "—");
}

document.getElementById("go").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  const sym = e.target.value.trim().toUpperCase();
  if (sym && ws && ws.readyState === 1) {
    if (views.has(sym)) { expanded = sym; dirty = true; }
    else ws.send(JSON.stringify({ type: "switch", symbol: sym }));
  }
  e.target.value = "";
});

resize();
connect();
loop();
