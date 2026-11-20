"""Build reports/index.html: charts of the shipped datasets and of the newest sample run, as inline SVG (standard library only).

    python3 reports/make_reports.py                      # datasets/ + the newest folder in samples/
    python3 reports/make_reports.py --sample samples/2026-11-20

Each chart is a <figure> with an id, so it can be captured on its own (reports/png/ holds captures of each one).
"""
import argparse, csv, glob, html, json, math, os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
COL = {"BTC": "#e8891d", "LTC": "#5b7290", "PART": "#3a9d6a", "BCH": "#8a5cc2", "refund": "#d23f3f", "ink": "#1d2433",
       "muted": "#6b7385", "grid": "#e4e7ee", "accent": "#2d6cdf"}
W = 960


def rd(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def esc(s):
    return html.escape(str(s), quote=True)


def fmt_int(n):
    return f"{int(round(n)):,}"


class Svg:
    def __init__(self, w, h):
        self.w, self.h, self.parts = w, h, []

    def add(self, s):
        self.parts.append(s)

    def text(self, x, y, s, size=12, anchor="start", color=None, weight=400):
        self.add(f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" text-anchor="{anchor}" fill="{color or COL["muted"]}" font-weight="{weight}">{esc(s)}</text>')

    def line(self, x1, y1, x2, y2, color=None, width=1, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{color or COL["grid"]}" stroke-width="{width}"{d}/>')

    def rect(self, x, y, w, h, color, title=None, opacity=1):
        t = f"<title>{esc(title)}</title>" if title else ""
        self.add(f'<rect x="{x:.2f}" y="{y:.2f}" width="{max(w, 0):.2f}" height="{max(h, 0):.2f}" fill="{color}" opacity="{opacity}">{t}</rect>')

    def circle(self, x, y, r, color, title=None, opacity=0.85):
        t = f"<title>{esc(title)}</title>" if title else ""
        self.add(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{color}" opacity="{opacity}">{t}</circle>')

    def path(self, d, color, width=2, fill="none"):
        self.add(f'<path d="{d}" stroke="{color}" stroke-width="{width}" fill="{fill}" stroke-linejoin="round"/>')

    def out(self):
        return f'<svg viewBox="0 0 {self.w} {self.h}" width="100%" role="img" xmlns="http://www.w3.org/2000/svg" font-family="Inter, Segoe UI, Helvetica, Arial, sans-serif">{"".join(self.parts)}</svg>'


def nice_max(v):
    if v <= 0:
        return 1
    e = 10 ** math.floor(math.log10(v))
    for m in (1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10):
        if v <= m * e:
            return m * e
    return 10 * e


def nticks(vmax):
    for n in (4, 5, 3, 6):
        step = vmax / n
        if abs(step - round(step)) < 1e-9 or step < 1 and abs(step * 10 - round(step * 10)) < 1e-9:
            return n
    return 4


def legend(svg, x, y, items):
    for name, color in items:
        svg.rect(x, y - 9, 11, 11, color)
        svg.text(x + 16, y, name, 12, color=COL["ink"])
        x += 24 + 7.2 * len(name)


# ------------------------------------------------------------------ charts of the shipped datasets

def chart_daily(daily):
    by = defaultdict(dict)
    for r in daily:
        by[r["day"]][r["chain"]] = int(r["completed"] or 0)
    days = sorted(d for d in by if d >= "2026-05-14")
    chains = ["BTC", "LTC", "PART"]
    tot = [sum(by[d].get(c, 0) for c in chains) for d in days]
    ymax = nice_max(max(tot))
    svg = Svg(W, 380)
    L, R, T, B = 48, 16, 34, 330
    pw = (W - L - R) / len(days)
    n = nticks(ymax)
    for k in range(n + 1):
        y = B - (B - T) * k / n
        svg.line(L, y, W - R, y)
        svg.text(L - 8, y + 4, fmt_int(ymax * k / n), 11, "end")
    for i, d in enumerate(days):
        y = B
        for c in chains:
            n = by[d].get(c, 0)
            h = (B - T) * n / ymax
            svg.rect(L + i * pw + 0.4, y - h, pw - 0.8, h, COL[c], f"{d} {c}: {n}")
            y -= h
        if d.endswith("-01"):
            svg.line(L + i * pw, B, L + i * pw, B + 5, COL["muted"])
            svg.text(L + i * pw, B + 18, date.fromisoformat(d).strftime("%b"), 11)
    # 7-day average of all chains
    pts = []
    for i in range(6, len(days)):
        avg = sum(tot[i - 6:i + 1]) / 7
        pts.append(f"{'M' if not pts else 'L'}{L + (i + 0.5) * pw:.1f},{B - (B - T) * avg / ymax:.1f}")
    svg.path("".join(pts), COL["ink"], 1.6)
    # annotations
    def note(day, text, dy=0):
        if day in days:
            i = days.index(day)
            x = L + (i + 0.5) * pw
            y = B - (B - T) * tot[i] / ymax
            svg.line(x, y - 4, x, y - 22 - dy, COL["muted"])
            svg.text(x - 4, y - 26 - dy, text, 11, "end", COL["ink"])
    note("2026-08-24", "24 Aug: 94 Litecoin trades in one day")
    recent = max(range(len(days) - 30, len(days)), key=lambda i: tot[i])
    note(days[recent], f"{date.fromisoformat(days[recent]).strftime('%d %b')}: {tot[recent]} trades")
    legend(svg, L, 18, [("Bitcoin", COL["BTC"]), ("Litecoin", COL["LTC"]), ("Particl", COL["PART"]), ("7-day average, all chains", COL["ink"])])
    first = sum(tot[:30]) / 30
    last = sum(tot[-10:]) / 10
    caption = (f"Completed trades per day, {days[0]} to {days[-1]} (datasets/daily_swaps.csv). The daily average went from "
               f"{first:.0f} in the first 30 days to {last:.0f} in the last 10.")
    return "daily", "Completed trades per day", svg.out(), caption


def chart_sizes(sizes):
    usd = {"BTC": 70000}       # about the median Bitcoin price of priced trades since October 2026 ($74k; $64k in mid-November)
    decades = ["1e3-1e4", "1e4-1e5", "1e5-1e6", "1e6-1e7", "1e7-1e8", "1e8-1e9", "1e9-1e10"]
    svg = Svg(W, 330)
    L, T, B = 60, 40, 270
    gw = (W - L - 20) / len(decades)
    rows = defaultdict(dict)
    for r in sizes:
        rows[r["chain"]][r["lock_base_units"]] = int(r["completed"] or 0)
    share = {c: {d: rows[c].get(d, 0) / max(sum(rows[c].values()), 1) for d in decades} for c in ("BTC", "LTC")}
    ymax = nice_max(max(max(v.values()) for v in share.values()))
    n = nticks(round(100 * ymax))
    for k in range(n + 1):
        y = B - (B - T) * k / n
        svg.line(L, y, W - 20, y)
        svg.text(L - 8, y + 4, f"{100 * ymax * k / n:.0f}%", 11, "end")
    for i, d in enumerate(decades):
        lo, hi = (int(float(x)) for x in d.split("-"))
        for j, c in enumerate(("BTC", "LTC")):
            v = share[c][d]
            h = (B - T) * v / ymax
            x = L + i * gw + 10 + j * (gw - 20) / 2
            svg.rect(x, B - h, (gw - 24) / 2, h, COL[c], f"{c} {lo / 1e8:g}-{hi / 1e8:g}: {rows[c].get(d, 0)} trades ({100 * v:.1f}%)")
        def coins(x):
            return f"{x / 1e8:.8f}".rstrip("0").rstrip(".")
        svg.text(L + (i + 0.5) * gw, B + 18, f"{coins(lo)} to {coins(hi)}", 11, "middle", COL["ink"])
        dollars = lo / 1e8 * usd["BTC"]
        svg.text(L + (i + 0.5) * gw, B + 34, f"about ${dollars:,.2f}+ in BTC" if dollars < 10 else f"about ${dollars:,.0f}+ in BTC", 10, "middle")
    legend(svg, L, 20, [("Bitcoin", COL["BTC"]), ("Litecoin", COL["LTC"])])
    svg.text(W - 20, 20, "lock size in coins; dollar labels for Bitcoin at about $70,000 per BTC", 11, "end")
    caption = ("Share of completed trades by lock size since May (datasets/sizes.csv). Most Bitcoin trades are 0.001 to 0.01 BTC; "
               "a separate group of 1 to 10 BTC trades are repeat fills of large Monero quotes.")
    return "sizes", "Trade sizes", svg.out(), caption


def chart_pairs(pairs):
    # offer pair = maker's coin / coin wanted; the taker does the opposite
    label = {("XMR/BTC", "reverse"): "buy Monero with Bitcoin", ("BTC/XMR", "normal"): "sell Monero for Bitcoin",
             ("WOW/LTC", "reverse"): "buy Wownero with Litecoin", ("BTC/LTC", "normal"): "buy Bitcoin with Litecoin",
             ("LTC/XMR", "normal"): "sell Monero for Litecoin", ("XMR/LTC", "reverse"): "buy Monero with Litecoin",
             ("BTC/DOGE", "normal"): "buy Bitcoin with Dogecoin", ("LTC/BCH", "normal"): "buy Litecoin with Bitcoin Cash",
             ("LTC/BTC", "normal"): "buy Litecoin with Bitcoin"}
    rows = sorted(pairs, key=lambda r: -int(r["swaps"]))
    svg = Svg(W, 40 + 30 * len(rows))
    L = 300
    vmax = nice_max(max(int(r["swaps"]) for r in rows))
    total = sum(int(r["swaps"]) for r in rows)
    for i, r in enumerate(rows):
        y = 26 + i * 30
        n = int(r["swaps"])
        name = label.get((r["pair"], r["direction"]), f"{r['pair']} {r['direction']}")
        chain = "LTC" if "LTC" in r["pair"] and "BTC" not in r["pair"] else "BTC"
        svg.text(L - 12, y + 14, f"{name}  ({r['pair']})", 12, "end", COL["ink"])
        svg.rect(L, y, (W - L - 120) * n / vmax, 20, COL[chain], f"{n} trades, {r['refunds']} refunds")
        svg.text(L + (W - L - 120) * n / vmax + 8, y + 14, f"{n}  ({100 * n / total:.0f}%)", 12, color=COL["ink"])
    caption = (f"Quote-matched trades by pair, taker's side, since 10 October (datasets/pairs.csv, {total} trades). "
               "Takers buying Monero with Bitcoin dominate.")
    return "pairs", "What trades", svg.out(), caption


def chart_refunds(daily):
    by = defaultdict(lambda: [0, 0])
    for r in daily:
        if r["chain"] in ("BTC", "LTC") and "2026-05-01" <= r["day"]:
            k = (r["day"][:7], r["chain"])
            by[k][0] += int(r["completed"] or 0)
            by[k][1] += int(r["refunded"] or 0)
    months = sorted({k[0] for k in by})
    svg = Svg(W, 300)
    L, T, B = 48, 40, 240
    ymax = 20
    for k in range(5):
        y = B - (B - T) * k / 4
        svg.line(L, y, W - 20, y)
        svg.text(L - 8, y + 4, f"{ymax * k / 4:.0f}%", 11, "end")
    gw = (W - L - 20) / len(months)
    for i, m in enumerate(months):
        for j, c in enumerate(("BTC", "LTC")):
            done, ref = by[(m, c)]
            n = done + ref
            p = 100 * ref / n if n else 0
            bw = gw * 0.28
            x = L + i * gw + gw / 2 - bw - 4 + j * (bw + 8)
            h = (B - T) * min(p, ymax) / ymax
            svg.rect(x, B - h, bw, h, COL[c], f"{m} {c}: {ref} of {n} ({p:.1f}%)")
            svg.text(x + bw / 2, B - h - 6, f"{p:.1f}%", 12, "middle", COL["ink"])
            svg.text(x + bw / 2, B + 16, f"{ref}/{n}", 10, "middle")
        label = datetime.strptime(m, "%Y-%m").strftime("%b")
        svg.text(L + (i + 0.5) * gw, B + 34, label + (" (to the 19th)" if m == months[-1] else ""), 12, "middle", COL["ink"])
    legend(svg, L, 20, [("Bitcoin", COL["BTC"]), ("Litecoin", COL["LTC"])])
    caption = ("Failed settlements (refunds) as a share of all trades, per month (failed/total under each bar). On Bitcoin the rate fell "
               "from 10-16% before BasicSwap 0.18 hardened the refund path (24 August) to 1% in November.")
    return "refunds", "How often trades fail", svg.out(), caption


def chart_validation(v):
    rc = v["recall_completed_swaps"]
    ed = v["early_detection_btc"]
    items = [("Bitcoin trades found", rc["BTC"]["hits"], rc["BTC"]["of"], rc["BTC"]["ci95"]),
             ("Litecoin trades found", rc["LTC"]["hits"], rc["LTC"]["of"], rc["LTC"]["ci95"]),
             ("Early flags that became trades", ed["hits"], ed["of"], ed["ci95"]),
             ("Trades flagged before they settled", None, None, None)]
    svg = Svg(W, 250)
    L, R = 280, 150
    x0, x1 = L, W - R
    for k in range(0, 101, 25):
        x = x0 + (x1 - x0) * k / 100
        svg.line(x, 20, x, 200)
        svg.text(x, 218, f"{k}%", 11, "middle")
    for i, (name, hits, of, ci) in enumerate(items):
        y = 30 + i * 44
        svg.text(L - 12, y + 15, name, 13, "end", COL["ink"])
        if hits is None:
            p = ed["recall_at_lock_time"]
            svg.rect(x0, y, (x1 - x0) * p, 22, COL["accent"])
            q = ed["minutes_gained_lock_to_spend_p10_p50_p90"]
            svg.text(x0 + (x1 - x0) * p + 8, y + 15, f"{100 * p:.0f}%, a median {q[1]} min ahead", 12, color=COL["ink"])
            continue
        p = hits / of
        svg.rect(x0, y, (x1 - x0) * p, 22, COL["accent"])
        svg.line(x0 + (x1 - x0) * ci[0], y + 11, x0 + (x1 - x0) * ci[1], y + 11, COL["ink"], 2)
        svg.text(x1 + 10, y + 15, f"{hits} of {of}", 12, color=COL["ink"])
    caption = ("Accuracy, measured against trades that matched a public quote exactly (datasets/validation.json). Black lines are "
               "95% intervals; the Litecoin sample is small. Early flags: Bitcoin, 6 days in November.")
    return "validation", "How accurate it is", svg.out(), caption


# ------------------------------------------------------------------ charts of the sample run

def chart_tape(trades):
    pts = [t for t in trades if t["settled_utc"]]
    ts = [datetime.strptime(t["settled_utc"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp() for t in pts]
    t0, t1 = min(ts), max(ts)
    svg = Svg(W, 440)
    L, R = 70, 20
    lanes = {"BTC": (40, 190), "LTC": (240, 370)}
    for c, (top, bot) in lanes.items():
        amts = [float(t["amount"]) for t in pts if t["chain"] == c and t["amount"]]
        if not amts:
            continue
        lo = math.floor(math.log10(min(amts)))
        hi = math.ceil(math.log10(max(amts)))
        hi = max(hi, lo + 1)
        for e in range(lo, hi + 1):
            y = bot - (bot - top) * (e - lo) / (hi - lo)
            svg.line(L, y, W - R, y)
            svg.text(L - 8, y + 4, f"{10 ** e:g} {c}", 10, "end")
        unknown_y = bot + 16             # refunds whose lock is older than the scan: amount unknown
        svg.line(L, unknown_y, W - R, unknown_y, COL["grid"], 1, "2,3")
        svg.text(L - 8, unknown_y + 4, "unknown", 10, "end")
        for t, x_t in zip(pts, ts):
            if t["chain"] != c:
                continue
            x = L + (W - L - R) * (x_t - t0) / (t1 - t0)
            if t["amount"]:
                a = float(t["amount"])
                y = bot - (bot - top) * (math.log10(a) - lo) / (hi - lo)
                tip = f"{t['settled_utc']} UTC  {a:g} {c}  {t['outcome']}, {t['lock_to_settle_min']} min  {t['txid'][:16]}"
            else:
                y = unknown_y
                tip = f"{t['settled_utc']} UTC  amount unknown (lock older than the scan)  {t['outcome']}  {t['txid'][:16]}"
            if t["outcome"] == "refunded":
                svg.add(f'<g><title>{esc(tip)}</title><path d="M{x - 5:.1f},{y - 5:.1f}L{x + 5:.1f},{y + 5:.1f}M{x - 5:.1f},{y + 5:.1f}L{x + 5:.1f},{y - 5:.1f}" stroke="{COL["refund"]}" stroke-width="2.4"/></g>')
            else:
                svg.circle(x, y, 4.2, COL[c], tip)
        svg.text(L, top - 10, {"BTC": "Bitcoin", "LTC": "Litecoin"}[c], 12, color=COL["ink"], weight=600)
    d = datetime.fromtimestamp(t0, timezone.utc).replace(hour=0, minute=0, second=0)
    while d.timestamp() <= t1:
        if d.timestamp() >= t0:
            x = L + (W - L - R) * (d.timestamp() - t0) / (t1 - t0)
            svg.line(x, 30, x, 392, COL["muted"], 0.6, "3,3")
            svg.text(x + 4, 410, d.strftime("%d %b 00:00 UTC"), 10)
        d += timedelta(days=1)
    n_done = sum(t["outcome"] == "completed" for t in pts)
    n_ref = len(pts) - n_done
    svg.add(f'<g transform="translate({W - 300},18)"><circle cx="5" cy="-4" r="4.2" fill="{COL["ink"]}"/>'
            f'<text x="14" y="0" font-size="12" fill="{COL["ink"]}">completed</text>'
            f'<path d="M95,-9L105,1M95,1L105,-9" stroke="{COL["refund"]}" stroke-width="2.4"/>'
            f'<text x="112" y="0" font-size="12" fill="{COL["ink"]}">refunded (failed)</text></g>')
    caption = (f"The trade tape of one scan: {n_done} completed and {n_ref} failed trades, each placed at its settlement time and "
               "sized by its lock amount (log scale; three refunds whose lock came before the scan window have no amount and sit on the "
               "'unknown' line). samples/2026-11-20/trades.csv has every row with its transaction ids.")
    return "tape", "Three days of trades, one by one", svg.out(), caption


def chart_settle(trades, chains_csv):
    mins = defaultdict(list)
    for t in trades:
        if t["outcome"] == "completed" and t["lock_to_settle_min"]:
            mins[t["chain"]].append(float(t["lock_to_settle_min"]))
    bins = list(range(0, 125, 5))
    svg = Svg(W, 320)
    L, T, B = 50, 40, 260
    counts = {c: [0] * len(bins) for c in mins}
    for c, v in mins.items():
        for m in v:
            counts[c][min(int(m // 5), len(bins) - 1)] += 1
    ymax = nice_max(max(sum(counts[c][i] for c in counts) for i in range(len(bins))))
    bw = (W - L - 20) / len(bins)
    n = nticks(ymax)
    for k in range(n + 1):
        y = B - (B - T) * k / n
        svg.line(L, y, W - 20, y)
        svg.text(L - 8, y + 4, fmt_int(ymax * k / n), 11, "end")
    for i, b in enumerate(bins):
        y = B
        for c in ("BTC", "LTC"):
            n = counts.get(c, [0] * len(bins))[i]
            h = (B - T) * n / ymax
            svg.rect(L + i * bw + 1, y - h, bw - 2, h, COL[c], f"{c} {b}-{b + 5} min: {n}")
            y -= h
        if b % 15 == 0 and b < 120:
            svg.text(L + i * bw, B + 18, f"{b}" + (" min" if b == 0 else ""), 11)
    svg.text(L + (len(bins) - 1) * bw + bw / 2, B + 18, "120+", 11, "middle")
    pub = {r["chain"]: r for r in chains_csv}
    y = 64
    for c in ("BTC", "LTC"):
        if c in pub:
            r = pub[c]
            med = sorted(mins.get(c, [0]))[len(mins.get(c, [0])) // 2]
            svg.text(W - 24, y, f"{ {'BTC': 'Bitcoin', 'LTC': 'Litecoin'}[c]}: median {med:.0f} min in this sample, "
                                f"{r['lock_to_spend_min_p50']} min over six months (p90 {r['lock_to_spend_min_p90']})", 12, "end", COL[c])
            y += 18
    legend(svg, L, 20, [("Bitcoin", COL["BTC"]), ("Litecoin", COL["LTC"])])
    caption = ("Minutes from the lock to settlement for each completed trade in the sample: how long both sides have funds committed, "
               "and how long a maker carries unhedged price risk after a fill.")
    return "settle", "How long settlement takes", svg.out(), caption


def chart_timeline(trades):
    cands = [t for t in trades if t["chain"] == "BTC" and t["outcome"] == "completed" and t["lock_to_settle_min"]
             and 12 <= float(t["lock_to_settle_min"]) <= 25]
    t = cands[len(cands) // 2] if cands else None
    svg = Svg(W, 300)
    if not t:
        return "timeline", "One trade", svg.out(), "no trade in the sample"
    mins = float(t["lock_to_settle_min"])
    L, R = 70, 40
    xa, xb = L + 170, W - R - 120
    # Bitcoin lane
    svg.text(L - 10, 92, "Bitcoin", 13, "end", COL["ink"], 600)
    svg.line(L, 88, W - R, 88, COL["grid"], 3)
    svg.text(L - 10, 212, "Monero", 13, "end", COL["ink"], 600)
    svg.line(L, 208, W - R, 208, COL["grid"], 3)
    svg.circle(xa, 88, 9, COL["BTC"], None, 1)
    svg.text(xa, 60, f"Lock confirmed {t['locked_utc'][11:16]} UTC", 12, "middle", COL["ink"], 600)
    svg.text(xa, 120, f"{float(t['amount']):g} BTC into a 2-of-2 address", 12, "middle")
    svg.text(xa, 136, "looks like any multisig payment", 11, "middle")
    svg.text(xa, 152, "early flag here if the amount equals a live quote", 11, "middle", COL["accent"])
    svg.circle(xb, 88, 9, COL["BTC"], None, 1)
    svg.text(xb, 60, f"Settled {t['settled_utc'][11:16]} UTC", 12, "middle", COL["ink"], 600)
    svg.text(xb, 120, "lock spent with BasicSwap's", 12, "middle")
    svg.text(xb, 136, "fields: this is where it is", 11, "middle")
    svg.text(xb, 152, "recognised as a completed trade", 11, "middle")
    svg.add(f'<path d="M{xa + 14},88 L{xb - 14},88" stroke="{COL["BTC"]}" stroke-width="3" marker-end="url(#ah)"/>')
    svg.add('<defs><marker id="ah" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#e8891d"/></marker></defs>')
    svg.text((xa + xb) / 2, 80, f"{mins:.0f} minutes: funds committed on both chains", 12, "middle", COL["ink"])
    svg.rect(xa, 196, xb - xa, 24, "#c9ced8", None, 0.6)
    svg.text((xa + xb) / 2, 212, "the Monero side moves privately: nothing visible", 12, "middle", COL["ink"])
    svg.text(L, 262, f"A real trade from the sample: lock {t['lock_txid'][:20]}..., settlement {t['txid'][:20]}... (block {t['height']}).", 11)
    svg.text(L, 280, "Look either transaction up on any Bitcoin block explorer.", 11)
    caption = ("What one trade looks like on chain. The tracker sees the Bitcoin side twice: the lock, which reveals nothing, "
               "and the settlement, which reveals the BasicSwap script.")
    return "timeline", "One trade, step by step", svg.out(), caption


# ------------------------------------------------------------------ page

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Swap tracker reports</title>
<style>
body {{ margin: 0; background: #f6f7f9; color: #1d2433; font: 15px/1.5 Inter, "Segoe UI", Helvetica, Arial, sans-serif; }}
main {{ max-width: 1000px; margin: 0 auto; padding: 28px 16px 48px; }}
h1 {{ font-size: 26px; margin: 0 0 4px; }}
.lede {{ color: #4a5163; margin: 0 0 22px; }}
.stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 18px; }}
.stat {{ background: #fff; border: 1px solid #e1e4ea; border-radius: 10px; padding: 14px 16px; }}
.stat b {{ display: block; font-size: 24px; }}
.stat span {{ color: #5d6578; font-size: 13px; }}
figure {{ background: #fff; border: 1px solid #e1e4ea; border-radius: 10px; margin: 0 0 18px; padding: 18px 20px 14px; }}
figure h2 {{ font-size: 18px; margin: 0 0 8px; }}
figcaption {{ color: #4a5163; font-size: 13.5px; margin-top: 6px; }}
.tag {{ font-size: 11px; font-weight: 600; color: #2d6cdf; background: #eaf1fd; border-radius: 4px; padding: 2px 6px; margin-left: 8px; vertical-align: 2px; }}
footer {{ color: #6b7385; font-size: 12.5px; }}
code {{ font-size: 12.5px; }}
</style></head><body><main>
<h1>bsx-swap-tracker</h1>
<p class="lede">BasicSwap trades rebuilt from public chain data. Charts of the shipped datasets (six months, aggregated) and of one
sample scan ({sample_name}, every trade listed in <code>samples/{sample_name}/trades.csv</code>).</p>
<div class="stats" id="stats">{stats}</div>
{figures}
<footer>Generated by <code>reports/make_reports.py</code> from <code>datasets/</code> and <code>samples/{sample_name}/</code>. No network requests.</footer>
</main></body></html>
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sample", help="sample folder (default: the newest in samples/)")
    ap.add_argument("--out", default=os.path.join(HERE, "index.html"))
    a = ap.parse_args()
    sample = a.sample or sorted(p for p in glob.glob(os.path.join(ROOT, "samples", "*")) if os.path.isdir(p))[-1]
    ds = os.path.join(ROOT, "datasets")
    daily, sizes, pairs = rd(os.path.join(ds, "daily_swaps.csv")), rd(os.path.join(ds, "sizes.csv")), rd(os.path.join(ds, "pairs.csv"))
    chains = rd(os.path.join(ds, "chains.csv"))
    v = json.load(open(os.path.join(ds, "validation.json")))
    trades = rd(os.path.join(sample, "trades.csv"))
    name = os.path.basename(os.path.normpath(sample))

    done = sum(int(r["completed"]) for r in chains)
    s_done = sum(t["outcome"] == "completed" for t in trades)
    stats = [(fmt_int(done), "completed trades reconstructed since May"),
             (f"{100 * v['recall_completed_swaps']['BTC']['recall']:.1f}%", f"of checkable Bitcoin trades found ({v['recall_completed_swaps']['BTC']['hits']} of {v['recall_completed_swaps']['BTC']['of']})"),
             (f"{v['early_detection_btc']['minutes_gained_lock_to_spend_p10_p50_p90'][1]} min", "median warning before a flagged trade settles"),
             (fmt_int(s_done), f"trades in the {name} sample, listed one by one")]
    stats_html = "".join(f'<div class="stat"><b>{esc(b)}</b><span>{esc(s)}</span></div>' for b, s in stats)
    figs = [chart_daily(daily), chart_tape(trades), chart_settle(trades, chains), chart_timeline(trades), chart_sizes(sizes),
            chart_pairs(pairs), chart_refunds(daily), chart_validation(v)]
    tags = {"daily": "SIX MONTHS", "sizes": "SIX MONTHS", "tape": "SAMPLE RUN", "settle": "SAMPLE RUN", "timeline": "SAMPLE RUN",
            "pairs": "SINCE 10 OCTOBER", "refunds": "SINCE MAY", "validation": "MEASURED"}
    fig_html = "".join(f'<figure id="{fid}"><h2>{esc(title)}<span class="tag">{tags[fid]}</span></h2>'
                       f'{svg}<figcaption>{esc(cap)}</figcaption></figure>\n' for fid, title, svg, cap in figs)
    with open(a.out, "w") as f:
        f.write(PAGE.format(stats=stats_html, figures=fig_html, sample_name=esc(name)))
    print(f"{len(figs)} charts -> {a.out}")


if __name__ == "__main__":
    main()
