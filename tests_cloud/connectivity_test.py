"""One-time cloud connectivity test for the swing-screener routine."""
import os, sys, time, json, datetime as dt, platform, re
import requests, pandas as pd, yfinance as yf, feedparser
from lxml import etree

T0 = time.time()
NOW = dt.datetime.now(dt.timezone.utc)
SEC_EMAIL = os.environ.get("SEC_EMAIL", "")
SECRETS = [s for s in (SEC_EMAIL, os.environ.get("TELEGRAM_BOT_TOKEN", ""), os.environ.get("TELEGRAM_CHAT_ID", "")) if s]
SEC_HDR = {"User-Agent": f"swing-screener-test {SEC_EMAIL}"}
NAS_HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
           "Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"}
TO = 20
R = {}  # name -> dict(status, key, dur, errors, extra)

def scrub(s):
    s = str(s)
    for x in SECRETS:
        s = s.replace(x, "<redacted>")
    return s

class Test:
    def __init__(self, name): self.name, self.errs, self.key, self.status, self.extra = name, [], "", "FAIL", {}; self.t = time.time()
    def err(self, e):
        e = scrub(e)[:300]
        if e not in self.errs: self.errs.append(e)
    def done(self, status, key=""):
        self.status, self.key = status, key
        R[self.name] = dict(status=status, key=key, dur=round(time.time() - self.t, 1), errors=self.errs[:5], extra=self.extra)
        print(self.name, status, key, R[self.name]["dur"], "s", self.errs[:2], flush=True)

_last_sec = [0.0]
def sec_get(url, t=None, **kw):
    w = 0.3 - (time.time() - _last_sec[0])
    if w > 0: time.sleep(w)
    _last_sec[0] = time.time()
    r = requests.get(url, headers=SEC_HDR, timeout=TO, **kw)
    if r.status_code == 403 and t: t.extra["403s"] = t.extra.get("403s", 0) + 1
    return r

def bdays(start, n, step):
    d = start
    while n:
        d += dt.timedelta(days=step)
        if d.weekday() < 5: n -= 1
    return d

# TEST 1
t = Test("1 Environment")
try:
    ip = requests.get("https://api.ipify.org", timeout=TO).text.strip()
    try:
        c = requests.get(f"https://ipapi.co/{ip}/country/", timeout=TO).text.strip()[:40]
    except Exception as e:
        c = f"unknown ({e.__class__.__name__})"
    t.extra["ip"], t.extra["country"] = ip, c
    t.done("PASS", f"ip={ip} country={c}")
except Exception as e:
    t.err(e); t.done("SKIPPED", "ipify blocked/failed")

# TEST 2
t = Test("2 Nasdaq screener")
tickers = []
try:
    r = requests.get("https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true", headers=NAS_HDR, timeout=TO)
    r.raise_for_status()
    rows = r.json()["data"]["rows"]
    df = pd.DataFrame(rows)
    df["mc"] = pd.to_numeric(df.get("marketCap", pd.Series(dtype=float)).astype(str).str.replace(r"[$,]", "", regex=True), errors="coerce")
    ok = df[(df.mc > 1e9) & ~df.symbol.str.contains(r"\^|/", regex=True)]
    tickers = sorted(set(s.strip() for s in ok.symbol))
    fields = {f: (f in df.columns) for f in ("marketCap", "sector", "industry")}
    t.extra["fields"] = fields
    t.done("PASS", f"rows={len(rows)} fields={fields} tickers>1B={len(tickers)}")
except Exception as e:
    t.err(e)
    try:
        j = sec_get("https://www.sec.gov/files/company_tickers_exchange.json", t).json()
        d = pd.DataFrame(j["data"], columns=j["fields"])
        tickers = list(d[d.exchange.isin(["NYSE", "Nasdaq"])].ticker)[:1500]
        t.done("FAIL", f"nasdaq failed; SEC fallback tickers={len(tickers)}")
    except Exception as e2:
        t.err(e2); t.done("FAIL", "nasdaq and SEC fallback failed")
if not tickers:
    tickers = ["AAPL", "MSFT", "JPM", "XOM", "NVDA"]
    t.extra["emergency_list"] = True

# TEST 3
t = Test("3 Yahoo bulk prices")
RL = re.compile(r"rate.?limit|too many requests|401|403|429|unauthorized|forbidden", re.I)
rl_hits = []
def complete(df, tk):
    try:
        s = df["Close"][tk] if isinstance(df.columns, pd.MultiIndex) else df["Close"]
        return s.dropna().shape[0]
    except Exception:
        return 0
import io, logging, contextlib
def dl(chunk):
    buf = io.StringIO(); h = logging.StreamHandler(buf); lg = logging.getLogger("yfinance"); lg.addHandler(h)
    try:
        with contextlib.redirect_stderr(buf), contextlib.redirect_stdout(buf):
            df = yf.download(chunk, period="2y", auto_adjust=False, threads=True, progress=False)
    except Exception as e:
        df = pd.DataFrame(); buf.write(repr(e))
    finally:
        lg.removeHandler(h)
    out = buf.getvalue()
    for line in out.splitlines():
        if RL.search(line): rl_hits.append(line[:200]); t.err(line)
    if "Failed download" in out or "Failed downloads" in out:
        for line in out.splitlines()[:3]: t.err(line)
    return df
def run(lst, label):
    good, bad = {}, []
    for i in range(0, len(lst), 100):
        ch = lst[i:i + 100]
        df = dl(ch)
        for tk in ch:
            n = complete(df, tk) if not df.empty else 0
            # last 300 trading days: 2y period trimmed
            n = min(n, 300)
            (good.__setitem__(tk, n) if n >= 250 else bad.append(tk))
        if i + 100 < len(lst): time.sleep(2)
        if (i // 100) % 5 == 0: print(label, i, len(good), len(bad), flush=True)
    return good, bad
good, bad = run(tickers, "bulk")
t.extra.update(requested=len(tickers), complete=len(good), failed=len(bad))
rec = 0
if bad:
    time.sleep(2)
    g2, b2 = run(bad, "retry"); rec = len(g2)
t.extra["recovered"] = rec
etfs = ["SPY", "XLK", "XLF", "XLV", "XLY", "XLP", "XLE", "XLI", "XLB", "XLU", "XLRE", "XLC"]
edf = dl(etfs)
eok = sum(1 for e in etfs if not edf.empty and complete(edf, e) >= 250)
t.extra.update(etf_ok=eok, rate_limit_errors=bool(rl_hits), rl_samples=len(rl_hits))
rate = 100 * (len(good) + rec) / max(1, len(tickers))
t.extra["rate_pct"] = round(rate, 1); t.extra["raw_rate_pct"] = round(100 * len(good) / max(1, len(tickers)), 1)
t.done("PASS" if rate >= 98 and not rl_hits else "PARTIAL" if rate >= 50 else "FAIL",
       f"req={len(tickers)} complete={len(good)} failed={len(bad)} recovered={rec} ETFs={eok}/12 ratelimit={'yes' if rl_hits else 'no'}")
t.extra["minutes"] = round((time.time() - t.t) / 60, 2)

# TEST 4
t = Test("4 Yahoo per-ticker")
res = {}
for s in ("AAPL", "MSFT", "JPM"):
    tk = yf.Ticker(s); r = {}
    try: r["news"] = len(tk.news or [])
    except Exception as e: t.err(f"{s} news: {e!r}"); r["news"] = None
    try: r["earnings_dates"] = len(tk.get_earnings_dates(limit=12))
    except Exception as e: t.err(f"{s} earnings: {e!r}"); r["earnings_dates"] = None
    try: r["mcap"] = tk.fast_info["market_cap"]
    except Exception as e: t.err(f"{s} fast_info: {e!r}"); r["mcap"] = None
    res[s] = r
t.extra["res"] = res
okc = sum(v is not None and v != 0 for r in res.values() for v in r.values())
t.done("PASS" if okc == 9 else "PARTIAL" if okc else "FAIL", json.dumps(res))

# TEST 5
t = Test("5 Stooq")
rows, html, ok = {}, 0, 0
for s in tickers[:20]:
    try:
        r = requests.get(f"https://stooq.com/q/d/l/?s={s.lower()}.us&i=d", timeout=TO,
                         headers={"User-Agent": NAS_HDR["User-Agent"]})
        body = r.text
        if body.lstrip()[:15].lower().startswith(("<!doctype", "<html")) or "captcha" in body.lower()[:2000]:
            html += 1; t.err(f"{s}: HTML/captcha response status={r.status_code}")
        elif body.startswith("Date,Open"):
            n = body.count("\n") - 1; rows[s] = n; ok += 1
        else:
            t.err(f"{s}: non-CSV status={r.status_code} body={body[:80]!r}")
    except Exception as e:
        t.err(f"{s}: {e!r}")
t.extra.update(rows=rows, html=html)
t.done("PASS" if ok == 20 else "PARTIAL" if ok else "FAIL", f"ok={ok}/20 html_or_captcha={html} rows_median={sorted(rows.values())[len(rows)//2] if rows else 0}")

# TEST 6
t = Test("6 SEC EDGAR")
sub = {}
parts = {}
try:
    r = sec_get("https://www.sec.gov/files/company_tickers.json", t); r.raise_for_status()
    ct = r.json(); parts["a"] = f"OK {len(ct)} cos"
    cikmap = {v["ticker"]: v["cik_str"] for v in ct.values()}
except Exception as e:
    t.err(f"a: {e!r}"); parts["a"] = "FAIL"; cikmap = {}
ten = ["AAPL", "MSFT", "JPM", "XOM", "NVDA", "AMZN", "UNH", "CAT", "KO", "BA"]
fallback = {"AAPL": 320193, "MSFT": 789019, "JPM": 19617, "XOM": 34088, "NVDA": 1045810, "AMZN": 1018724, "UNH": 731766, "CAT": 18230, "KO": 21344, "BA": 12927}
n = 0
for s in ten:
    cik = int(cikmap.get(s, fallback[s]))
    try:
        r = sec_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", t); r.raise_for_status()
        sub[s] = (cik, r.json()); n += 1
    except Exception as e: t.err(f"b {s}: {e!r}")
parts["b"] = f"{n}/10"
sizes = []
for s in ten[:3]:
    cik = int(cikmap.get(s, fallback[s]))
    try:
        r = sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", t); r.raise_for_status()
        sizes.append(f"{s}={len(r.content)/1e6:.1f}MB")
    except Exception as e: t.err(f"c {s}: {e!r}")
parts["c"] = ",".join(sizes) or "FAIL"
d = NOW.date(); found = None
for _ in range(10):
    if d.weekday() < 5:
        q = (d.month - 1) // 3 + 1
        url = f"https://www.sec.gov/Archives/edgar/daily-index/{d.year}/QTR{q}/form.{d:%Y%m%d}.idx"
        try:
            r = sec_get(url, t)
            if r.status_code == 200:
                L = r.text.splitlines()
                found = (d, sum("8-K" in l for l in L), sum(l.startswith("4 ") for l in L)); break
            else: t.err(f"d {d}: HTTP {r.status_code}")
        except Exception as e: t.err(f"d {d}: {e!r}")
    d -= dt.timedelta(days=1)
parts["d"] = f"{found[0]} 8-K lines={found[1]} form4 lines={found[2]}" if found else "FAIL"
parsed = 0; tried = 0
for s, (cik, j) in sub.items():
    if parsed >= 3 or tried >= 8: break
    rc = j["filings"]["recent"]
    for form, acc, doc in zip(rc["form"], rc["accessionNumber"], rc["primaryDocument"]):
        if form == "4":
            tried += 1
            xml = re.sub(r"^xsl[^/]+/", "", doc)
            url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{xml}"
            try:
                r = sec_get(url, t); r.raise_for_status()
                root = etree.fromstring(r.content)
                if root.tag == "ownershipDocument": parsed += 1
                else: t.err(f"e {s}: unexpected root {root.tag}")
            except Exception as e: t.err(f"e {s}: {e!r}")
            break
parts["e"] = f"{parsed}/3 parsed"
t.extra["parts"] = parts
allok = parts["a"].startswith("OK") and n == 10 and len(sizes) == 3 and found and parsed == 3
anyok = n or sizes or found or parsed
t.done("PASS" if allok else "PARTIAL" if anyok else "FAIL", "; ".join(f"{k}:{v}" for k, v in parts.items()) + f"; 403s={t.extra.get('403s', 0)}")

# TEST 7
t = Test("7 Nasdaq earnings cal")
nb = bdays(NOW.date(), 1, 1)
try:
    r = requests.get(f"https://api.nasdaq.com/api/calendar/earnings?date={nb}", headers=NAS_HDR, timeout=TO); r.raise_for_status()
    rows_ = (r.json().get("data") or {}).get("rows") or []
    t.done("PASS" if rows_ else "PARTIAL", f"date={nb} rows={len(rows_)}")
except Exception as e:
    t.err(e); t.done("FAIL", f"date={nb}")

# TEST 8
t = Test("8 Google News RSS")
cnt = {}
for s in ("AAPL", "NVDA", "JPM"):
    try:
        r = requests.get(f"https://news.google.com/rss/search?q={s}+stock&hl=en-US&gl=US&ceid=US:en", timeout=TO)
        r.raise_for_status(); cnt[s] = len(feedparser.parse(r.content).entries)
    except Exception as e: t.err(f"{s}: {e!r}"); cnt[s] = 0
t.done("PASS" if all(cnt.values()) else "PARTIAL" if any(cnt.values()) else "FAIL", json.dumps(cnt))

# TEST 9
t = Test("9 Telegram")
tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
if tok and chat:
    try:
        r = requests.post(f"https://api.telegram.org/bot{tok}/sendMessage", json={"chat_id": chat, "text": "swing-screener cloud connectivity test ✅"}, timeout=TO)
        r.raise_for_status(); t.done("PASS", "sent")
    except Exception as e: t.err(e); t.done("FAIL", "send failed")
else:
    t.done("SKIPPED", "env vars not set")

# OUTPUT
total = round(time.time() - T0, 1)
t3 = R["3 Yahoo bulk prices"]["extra"]; s3 = R["3 Yahoo bulk prices"]
raw, tot = t3["raw_rate_pct"], t3["rate_pct"]
if raw >= 98 and not t3["rate_limit_errors"]: verdict, why = "YES", f"{raw}% of tickers had complete data on first pass with no rate-limit errors."
elif tot < 50 or t3["rate_limit_errors"] and raw < 90: verdict, why = "NO", f"only {raw}% complete on first pass ({tot}% after retry); rate-limit errors: {'yes' if t3['rate_limit_errors'] else 'no'}."
else: verdict, why = "UNCERTAIN", f"{raw}% complete on first pass ({tot}% after retry), rate-limit errors: {'yes' if t3['rate_limit_errors'] else 'no'}; below the 98%/no-error bar for YES."
stamp = NOW.strftime("%Y-%m-%d_%H%M")
lines = [f"# Cloud connectivity test — {NOW:%Y-%m-%d %H:%M} UTC", "",
         f"Python {platform.python_version()}; pandas {pd.__version__}; yfinance {yf.__version__}; requests {requests.__version__}; lxml {etree.LXML_VERSION}; feedparser {feedparser.__version__}", "",
         "## Summary", "", "| Test | Status | Key numbers | Duration |", "|---|---|---|---|"]
for k, v in R.items():
    lines.append(f"| {k} | {v['status']} | {scrub(v['key']).replace('|', '/')} | {v['dur']}s |")
lines.append(f"| Total script runtime | — | — | {total}s |")
lines += ["", "## TEST 3 detail", "",
          f"- Success rate (first pass): {raw}% ; after single retry: {tot}%",
          f"- Requested {t3['requested']}, complete {t3['complete']}, failed {t3['failed']}, recovered on retry {t3['recovered']}",
          f"- Total time: {t3['minutes']} minutes",
          f"- Rate-limit / 401 / 403 / 429 errors: {'YES' if t3['rate_limit_errors'] else 'NO'}",
          f"- Sector ETFs + SPY complete: {t3['etf_ok']}/12", "",
          "## Environment", "", f"- Egress IP/country: {R['1 Environment']['extra'].get('ip', 'n/a')} / {R['1 Environment']['extra'].get('country', 'n/a')}", "",
          "## Error details (first 5 distinct per test)", ""]
for k, v in R.items():
    lines.append(f"### {k}")
    lines += [f"- `{e}`" for e in v["errors"]] or ["- none"]
    if v["extra"].get("parts"): lines.append(f"- parts: {v['extra']['parts']}; 403 responses: {v['extra'].get('403s', 0)}")
    lines.append("")
lines += ["## Verdict", "", f"**Yahoo bulk prices viable from cloud: {verdict}** — {why}", ""]
out = scrub("\n".join(lines))
path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"results_{stamp}_UTC.md")
open(path, "w").write(out)
print(out)
