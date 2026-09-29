import time, json, datetime as dt, sys, random
import requests, pandas as pd, yfinance as yf

HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
       "Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}
NOW = dt.datetime.utcnow()
R = {"A": {"errors": []}, "B": {"errors": []}, "C": {"errors": []}}

def err(t, m):
    m = str(m)[:300]
    if m not in R[t]["errors"] and len(R[t]["errors"]) < 5:
        R[t]["errors"].append(m)

def close_series(df, sym=None):
    """Extract Close series from yf.download result (multi or single)."""
    if df is None or df.empty: return None
    try:
        if isinstance(df.columns, pd.MultiIndex):
            lv0 = df.columns.get_level_values(0)
            if "Close" in lv0:
                sub = df["Close"]
                if isinstance(sub, pd.DataFrame):
                    if sym is not None and sym in sub.columns: s = sub[sym]
                    elif sub.shape[1] == 1: s = sub.iloc[:, 0]
                    else: return None
                else: s = sub
            else:
                s = df.xs("Close", axis=1, level=1)
                s = s[sym] if sym in s.columns else s.iloc[:, 0]
        else:
            s = df["Close"]
        return s.dropna()
    except Exception as e:
        return None

# ---------- TEST A
tA = time.time()
meta = {}
try:
    r = requests.get("https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true", headers=HDR, timeout=20)
    R["A"]["screener_status"] = r.status_code
    rows = r.json()["data"]["rows"]
    for x in rows:
        try: mc = float(str(x.get("marketCap", "")).replace(",", "") or 0)
        except: mc = 0
        s = x.get("symbol", "").strip()
        if mc > 1e9 and "^" not in s and "/" not in s:
            meta[s] = {"name": x.get("name"), "ipo": x.get("ipoyear") or x.get("ipoYear") or None}
except Exception as e:
    err("A", f"screener: {e!r}")
symbols = sorted(meta)
print("symbols", len(symbols), flush=True)
data = {}   # sym -> series
rate_limited = False
for i in range(0, len(symbols), 100):
    chunk = symbols[i:i+100]
    try:
        df = yf.download(chunk, period="2y", interval="1d", auto_adjust=False, threads=True, progress=False, group_by="column")
        for s in chunk:
            c = close_series(df, s) if len(chunk) > 1 else close_series(df, s)
            if c is not None and len(c) > 0:
                data[s] = c.tail(300)
    except Exception as e:
        err("A", repr(e))
        if "rate" in str(e).lower() or "429" in str(e): rate_limited = True
    time.sleep(2)
try:
    import yfinance.shared as sh
    for s, m in getattr(sh, "_ERRORS", {}).items():
        err("A", f"{s}: {m}")
        if "rate" in str(m).lower() or "429" in str(m) or "Too Many" in str(m): rate_limited = True
except Exception: pass
failed = [s for s in symbols if s not in data]
R["A"].update(total=len(symbols), success=len(data), pct=round(100*len(data)/max(1, len(symbols)), 2),
              rate_limited=rate_limited, duration=round(time.time()-tA, 1))
print("A", R["A"], flush=True)

# ---------- TEST B
tB = time.time()
cat = {"SYMBOL_FORMAT": [], "SHORT_HISTORY": [], "NO_DATA": []}
fixed_hist = {}
def fetch(sym):
    try:
        df = yf.download(sym, period="2y", interval="1d", auto_adjust=False, progress=False, threads=False)
        return close_series(df, sym)
    except Exception as e:
        err("B", f"{sym}: {e!r}"); return None
for s in failed:
    alts = []
    if "." in s: alts.append(s.replace(".", "-"))
    if "-" in s: alts.append(s.replace("-", "."))
    fixed = None
    for a in alts:
        c = fetch(a)
        if c is not None and len(c) > 0:
            fixed = (a, c); break
    if fixed:
        cat["SYMBOL_FORMAT"].append((s, fixed[0], len(fixed[1]))); fixed_hist[s] = fixed[1]; continue
    c = fetch(s)
    if c is not None and len(c) > 0:
        cat["SHORT_HISTORY"].append((s, str(c.index[0].date()), len(c))); fixed_hist[s] = c
    else:
        cat["NO_DATA"].append(s)
    time.sleep(0.3)
total = len(symbols)
ok_after_fmt = len(data) + len(cat["SYMBOL_FORMAT"])
hist_len = {s: len(c) for s, c in data.items()}
for s, c in fixed_hist.items(): hist_len[s] = len(c)
elig = [s for s in symbols if hist_len.get(s, 0) >= 126]
# tickers with >=126 days of history: those that have data with >=126 rows (NO_DATA excluded from denominator only if truly lacking history; count as failures of the rest)
# denominator: tickers that plausibly have >=126 days = ok tickers with >=126 rows + NO_DATA tickers (unknown, counted as failures) 
denom_126 = len(elig) + len(cat["NO_DATA"])
R["B"].update(counts={k: len(v) for k, v in cat.items()}, ok_after_fmt=ok_after_fmt,
              rate_after_fmt=round(100*ok_after_fmt/max(1,total), 2),
              elig126=len(elig), denom126_incl_nodata=denom_126,
              rate126_excl_nodata=round(100*len(elig)/max(1, len(elig)+0), 2),
              rate126_incl_nodata=round(100*len(elig)/max(1, denom_126), 2),
              duration=round(time.time()-tB, 1))
print("B", R["B"], flush=True)

# ---------- TEST C
tC = time.time()
today = NOW.date(); frm = today - dt.timedelta(days=365)
yahoo_ok = [s for s in data if len(data[s]) >= 250]
random.seed(1); ysample = random.sample(yahoo_ok, min(10, len(yahoo_ok)))
bad = cat["NO_DATA"] + [x[0] for x in cat["SHORT_HISTORY"]]
csyms = ysample + bad[:10]
cres = {}; special = []
for s in csyms:
    url = f"https://api.nasdaq.com/api/quote/{s}/historical?assetclass=stocks&fromdate={frm}&todate={today}&limit=400"
    d = {"status": None}
    try:
        r = requests.get(url, headers=HDR, timeout=20); d["status"] = r.status_code
        ct = r.headers.get("content-type", "")
        if r.status_code in (403, 429) or "html" in ct.lower() or r.text.lstrip().startswith("<"):
            special.append(f"{s}: status {r.status_code} ct={ct} body={r.text[:80]!r}")
        j = r.json()
        rows = (j.get("data") or {}).get("tradesTable", {}).get("rows") or []
        d["rows"] = len(rows)
        if rows:
            px = {}
            for x in rows:
                px[dt.datetime.strptime(x["date"], "%m/%d/%Y").date()] = float(x["close"].replace("$", "").replace(",", ""))
            d["range"] = f"{min(px)}..{max(px)}"; d["px"] = px
        else:
            d["msg"] = str(j.get("status"))[:150]
    except Exception as e:
        err("C", f"{s}: {e!r}"); d["error"] = repr(e)[:150]
    cres[s] = d
    time.sleep(1)
match = {}
for s in ysample:
    px = cres[s].get("px")
    if not px: continue
    ys = data[s]; ys.index = pd.to_datetime(ys.index).tz_localize(None) if ys.index.tz else pd.to_datetime(ys.index)
    common = [d for d in px if pd.Timestamp(d) in ys.index]
    if len(common) < 5: match[s] = f"only {len(common)} common dates"; continue
    random.seed(2); pick = random.sample(common, 5)
    diffs = [abs(px[d]-float(ys[pd.Timestamp(d)]))/float(ys[pd.Timestamp(d)])*100 for d in pick]
    match[s] = {"max_diff_pct": round(max(diffs), 3), "ok": all(x <= 0.5 for x in diffs)}
csucc = [s for s in csyms if cres[s].get("rows", 0) > 0]
R["C"].update(requested=len(csyms), success=len(csucc), duration=round(time.time()-tC, 1), special=special)

# ---------- OUTPUT
stamp = NOW.strftime("%Y-%m-%d_%H%M")
out = f"tests_cloud/followup_results_{stamp}_UTC.md"
n126 = R["B"]["rate126_incl_nodata"]
yahoo_yes = n126 >= 99 and not rate_limited
nm = [m for m in match.values() if isinstance(m, dict)]
match_ok = len(nm) > 0 and all(m["ok"] for m in nm)
if len(csucc) >= 16 and match_ok: nv = "YES"
elif len(csucc) == 0 or (nm and not match_ok): nv = "NO"
else: nv = "UNCERTAIN"
yv = "YES" if yahoo_yes else ("UNCERTAIN" if (n126 >= 95 and not rate_limited) else "NO")
L = [f"# Follow-up connectivity test — {NOW:%Y-%m-%d %H:%M} UTC", "",
     "| Test | Status | Key numbers | Duration |", "|---|---|---|---|",
     f"| A Yahoo bulk | {'OK' if R['A']['pct']>0 else 'FAIL'} | {R['A']['success']}/{R['A']['total']} ({R['A']['pct']}%), rate-limit errors: {'yes' if rate_limited else 'no'} | {R['A']['duration']}s |",
     f"| B Diagnosis | done | {R['B']['counts']}; after fmt fix {R['B']['rate_after_fmt']}%; ≥126d {n126}% | {R['B']['duration']}s |",
     f"| C Nasdaq hist | {'OK' if csucc else 'FAIL'} | {len(csucc)}/{len(csyms)} returned rows; price match {sum(m['ok'] for m in nm)}/{len(nm)} | {R['C']['duration']}s |", "",
     "## Test A", f"Screener status: {R['A'].get('screener_status')}, tickers: {total}", f"First errors: {R['A']['errors']}", "",
     "## Test B", f"Failed in A: {len(failed)}", f"Category counts: {R['B']['counts']}",
     f"Success rate after symbol-format fix: {ok_after_fmt}/{total} = {R['B']['rate_after_fmt']}%",
     f"Success among tickers with ≥126 days history: {len(elig)}/{denom_126} = {n126}% (NO_DATA tickers counted as failures; excluding them it is 100% by construction)",
     f"First errors: {R['B']['errors']}", "", "### SYMBOL_FORMAT (orig -> working, rows)"]
L += [f"- {a} -> {b} ({n})" for a, b, n in cat["SYMBOL_FORMAT"]]
L += ["", "### SHORT_HISTORY (symbol, first date, rows, name, IPO year)"]
L += [f"- {s}, {f}, {n}, {meta[s]['name']}, {meta[s]['ipo']}" for s, f, n in cat["SHORT_HISTORY"]]
L += ["", "### NO_DATA (symbol — name — IPO year)"]
L += [f"- {s} — {meta[s]['name']} — {meta[s]['ipo']}" for s in cat["NO_DATA"]]
L += ["", "## Test C", f"Success {len(csucc)}/{len(csyms)}; first errors: {R['C']['errors']}", f"Captcha/HTML/403/429 notes: {special or 'none'}", "",
      "| Symbol | Group | Status | Rows | Range | Yahoo match |", "|---|---|---|---|---|---|"]
for s in csyms:
    d = cres[s]
    L.append(f"| {s} | {'yahoo-ok' if s in ysample else 'yahoo-bad'} | {d.get('status')} | {d.get('rows', d.get('error', ''))} | {d.get('range', d.get('msg', ''))} | {match.get(s, '-')} |")
L += ["", f"**Yahoo viable as primary price source from cloud: {yv}**", f"**Nasdaq historical viable as fallback: {nv}**", ""]
open(out, "w").write("\n".join(L))
print(out); print("\n".join(L[:8])); print(yv, nv)
