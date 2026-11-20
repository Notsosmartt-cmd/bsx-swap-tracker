"""Check reports/index.html: recompute every value drawn on the charts from datasets/ and samples/ and compare (standard library only).

    python3 reports/verify_reports.py        # from the repository root; exits 1 on any mismatch
"""
import re, csv, json, collections, html, os
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
page=open("reports/index.html").read()
figs=dict(re.findall(r'<figure id="(\w+)">(.*?)</figure>', page, re.S))
titles=lambda f: [html.unescape(t) for t in re.findall(r"<title>(.*?)</title>", figs[f])]
rd=lambda p: list(csv.DictReader(open(p)))
bad=collections.Counter(); n=collections.Counter()
def check(fig, ok, what):
    n[fig]+=1
    if not ok: bad[fig]+=1; print("MISMATCH", fig, what)
d={(r["day"],r["chain"]):int(r["completed"]) for r in rd("datasets/daily_swaps.csv")}
for t in titles("daily"):
    day,ch,v=re.match(r"(\S+) (\w+): (\d+)",t).groups(); check("daily", d.get((day,ch),0)==int(v), t)
sz={(r["chain"],r["lock_base_units"]):int(r["completed"]) for r in rd("datasets/sizes.csv")}
dec=["1e3-1e4","1e4-1e5","1e5-1e6","1e6-1e7","1e7-1e8","1e8-1e9","1e9-1e10"]
for t in titles("sizes"):
    ch,lo,hi,k,p=re.match(r"(\w+) (\S+)-(\S+): (\d+) trades \(([\d.]+)%\)",t).groups()
    key=[x for x in dec if abs(float(x.split("-")[0])/1e8-float(lo))<1e-12][0]
    tot=sum(v for (c,_),v in sz.items() if c==ch)
    check("sizes", sz.get((ch,key),0)==int(k) and abs(100*int(k)/tot-float(p))<0.06, t)
pr=sorted((int(r["swaps"]),int(r["refunds"])) for r in rd("datasets/pairs.csv"))
got=sorted(tuple(map(int,re.match(r"(\d+) trades, (\d+) refunds",t).groups())) for t in titles("pairs"))
for a,b in zip(got,pr): check("pairs", a==b, (a,b))
check("pairs", len(got)==len(pr), "row count")
mo=collections.defaultdict(lambda:[0,0])
for r in rd("datasets/daily_swaps.csv"):
    if r["day"]>="2026-05-01": mo[(r["day"][:7],r["chain"])][0]+=int(r["completed"]); mo[(r["day"][:7],r["chain"])][1]+=int(r["refunded"])
for t in titles("refunds"):
    m,ch,f,tot=re.match(r"(\S+) (\w+): (\d+) of (\d+)",t).groups(); c,rf=mo[(m,ch)]; check("refunds", (rf, c+rf)==(int(f),int(tot)), t)
tr=rd("samples/2026-11-20/trades.csv")
tips=titles("tape"); check("tape", len(tips)==len(tr), f"{len(tips)} points vs {len(tr)} rows")
byid={r["txid"][:16]:r for r in tr}
for t in tips:
    r=byid.get(t.split()[-1]); ok = r is not None and (("amount unknown" in t) == (r["amount"]=="")) and (r["amount"]=="" or f"{float(r['amount']):g} {r['chain']}" in t) and r["outcome"] in t
    check("tape", ok, t)
hist=collections.Counter()
for r in tr:
    if r["outcome"]=="completed" and r["lock_to_settle_min"]: hist[(r["chain"], min(int(float(r["lock_to_settle_min"])//5),24))]+=1
for t in titles("settle"):
    ch,b,_,v=re.match(r"(\w+) (\d+)-(\d+) min: (\d+)",t).groups(); check("settle", hist.get((ch,int(b)//5),0)==int(v), t)
v=json.load(open("datasets/validation.json"))
for s in ("521 of 523","11 of 11","219 of 226",f"{round(100*v['early_detection_btc']['recall_at_lock_time'])}%, a median {v['early_detection_btc']['minutes_gained_lock_to_spend_p10_p50_p90'][1]} min ahead"):
    check("validation", s in figs["validation"], s)
chains=rd("datasets/chains.csv"); tot=sum(int(r["completed"]) for r in chains)
check("stats", f"{tot:,}" in page, tot)
tl=[r for r in tr if r["chain"]=="BTC" and r["outcome"]=="completed" and r["lock_to_settle_min"] and 12<=float(r["lock_to_settle_min"])<=25]
t=tl[len(tl)//2]; check("timeline", t["lock_txid"][:20] in figs["timeline"] and t["txid"][:20] in figs["timeline"] and f"{float(t['lock_to_settle_min']):.0f} minutes" in figs["timeline"], t["txid"])
for f in sorted(n): print(f"{f:11s} {n[f]:4d} values checked, {bad[f]} mismatches")
raise SystemExit(1 if sum(bad.values()) else 0)
