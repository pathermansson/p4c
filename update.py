#!/usr/bin/env python3
"""Fetch Zaptec charge history, price each charging interval with the hourly/quarter-hourly
Nord Pool spot price, and write docs/data.json for the dashboard.

Env vars: ZAPTEC_USERNAME, ZAPTEC_PASSWORD
Usage:    python update.py            # real run
          python update.py --debug    # also dump one raw session to data/raw_session_sample.json
          python update.py --demo     # write synthetic data.json (no network) to preview the dashboard
"""
import json, os, sys, random, urllib.request, urllib.parse, urllib.error
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent
CFG = json.loads((ROOT / "config.json").read_text())
TZ = ZoneInfo(CFG["timezone"])
OUT = ROOT / "docs" / "data.json"
CACHE = ROOT / "data" / "prices_cache.json"
ZAPTEC = "https://api.zaptec.com"


# ---------- HTTP helpers ----------
def http(url, data=None, headers=None, retries=3):
    headers = {"User-Agent": "Mozilla/5.0 (compatible; zaptec-cost-dashboard)", **(headers or {})}
    req = urllib.request.Request(url, data=data, headers=headers)
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if i == retries - 1:
                raise
        except urllib.error.URLError:
            if i == retries - 1:
                raise

def zaptec_token():
    body = urllib.parse.urlencode({
        "grant_type": "password",
        "username": os.environ["ZAPTEC_USERNAME"],
        "password": os.environ["ZAPTEC_PASSWORD"],
    }).encode()
    r = http(f"{ZAPTEC}/oauth/token", data=body,
             headers={"Content-Type": "application/x-www-form-urlencoded"})
    return r["access_token"]


def zget(path, token, **params):
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    return http(f"{ZAPTEC}{path}?{q}", headers={"Authorization": f"Bearer {token}"})


def parse_ts(s):
    d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ---------- Zaptec ----------
def fetch_sessions(token):
    charger = CFG.get("charger_id")
    if not charger:
        chargers = zget("/api/chargers", token)["Data"]
        if not chargers:
            sys.exit("No chargers visible to this account")
        for c in chargers:
            print("CHARGER:", c.get("Name"), "| serial:", c.get("DeviceId"), "| id:", c.get("Id"))
        charger = chargers[0]["Id"]
        if len(chargers) > 1:
            print(f"Multiple chargers found, using {chargers[0].get('Name')} ({charger}). "
                  f"Set charger_id in config.json to choose.")
    sessions, page = [], 0
    while True:
        r = zget("/api/chargehistory", token, ChargerId=charger,
                 From=CFG["history_start"] + "T00:00:00Z",
                 To=(datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z"),
                 DetailLevel=1, PageSize=500, PageIndex=page)
        sessions += r.get("Data", [])
        page += 1
        if page >= (r.get("Pages") or 1):
            break
    return sessions

def energy_segments(s):
    """Return [(start_utc, end_utc, kwh)] for a session.
    Uses EnergyDetails if present (handles cumulative or incremental values), else spreads
    total energy evenly between start and end."""
    start, end = parse_ts(s["StartDateTime"]), parse_ts(s["EndDateTime"])
    total = float(s.get("Energy") or 0)
    det = sorted(s.get("EnergyDetails") or [], key=lambda d: d["Timestamp"])
    if not det:
        return [(start, max(end, start + timedelta(minutes=1)), total)]
    vals = [float(d["Energy"]) for d in det]
    cumulative = all(b >= a for a, b in zip(vals, vals[1:])) and \
        abs(vals[-1] - total) <= max(0.1 * total, 0.05)
    segs, prev_t, prev_v = [], start, 0.0
    for d, v in zip(det, vals):
        t = parse_ts(d["Timestamp"])
        kwh = v - prev_v if cumulative else v
        if t > prev_t and kwh > 0:
            segs.append((prev_t, t, kwh))
        prev_t, prev_v = max(t, prev_t), v
    return segs or [(start, max(end, start + timedelta(minutes=1)), total)]


# ---------- Prices ----------
def load_cache():
    return json.loads(CACHE.read_text()) if CACHE.exists() else {}


def day_prices(d, cache):
    key = f"{CFG['price_area']}_{d.isoformat()}"
    if key in cache:
        return cache[key]
    url = (f"https://www.elprisetjustnu.se/api/v1/prices/{d.year}/"
           f"{d.month:02d}-{d.day:02d}_{CFG['price_area']}.json")
    data = http(url)
    if data is None:
        return None
    rows = [[x["time_start"], x["time_end"], x["SEK_per_kWh"]] for x in data]
    if d < date.today() - timedelta(days=1):  # past days never change
        cache[key] = rows
    return rows


def intervals_for(d0, d1, cache):
    out, d = [], d0
    while d <= d1:
        rows = day_prices(d, cache)
        if rows:
            out += [(parse_ts(a), parse_ts(b), p) for a, b, p in rows]
        d += timedelta(days=1)
    return sorted(out)


def grid_fee(local_dt):
    g = CFG["grid_fee_energy_ore"]
    lo, hi = g["day_hours"]
    return g["day"] if lo <= local_dt.hour < hi else g["night"]


def price_session(s, intervals):
    """Distribute energy over price intervals by time overlap and sum cost components (SEK)."""
    comp = dict(spot=0.0, markup=0.0, grid=0.0, tax=0.0, vat=0.0, kwh=0.0, unpriced_kwh=0.0)
    vat = CFG["vat_rate"]
    for t0, t1, kwh in energy_segments(s):
        dur = (t1 - t0).total_seconds()
        covered = 0.0
        for a, b, p in intervals:
            ov = (min(b, t1) - max(a, t0)).total_seconds()
            if ov <= 0:
                continue
            e = kwh * ov / dur
            covered += e
            loc = max(a, t0).astimezone(TZ)
            spot = p
            markup = CFG["supplier_markup_ore"] / 100
            grid = grid_fee(loc) / 100
            tax = CFG["energy_tax_ore"] / 100
            net = spot + markup + grid + tax
            comp["spot"] += e * spot
            comp["markup"] += e * markup
            comp["grid"] += e * grid
            comp["tax"] += e * tax
            comp["vat"] += e * net * vat
        comp["kwh"] += kwh
        comp["unpriced_kwh"] += max(0.0, kwh - covered)
    comp["total"] = comp["spot"] + comp["markup"] + comp["grid"] + comp["tax"] + comp["vat"]
    return comp


# ---------- Aggregate ----------
def aggregate(priced):
    months = defaultdict(lambda: defaultdict(float))
    for s in priced:
        m = s["start"][:7]
        for k in ("kwh", "spot", "markup", "grid", "tax", "vat", "total", "unpriced_kwh"):
            months[m][k] += s[k]
        months[m]["sessions"] += 1
    out = []
    for m in sorted(months):
        v = months[m]
        out.append({"month": m, "sessions": int(v["sessions"]),
                    **{k: round(v[k], 2) for k in ("kwh", "spot", "markup", "grid", "tax", "vat", "total")},
                    "avg_sek_per_kwh": round(v["total"] / v["kwh"], 3) if v["kwh"] else 0,
                    "unpriced_kwh": round(v["unpriced_kwh"], 2)})
    return out


def write_output(priced):
    priced.sort(key=lambda s: s["start"], reverse=True)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "currency": "SEK",
        "price_area": CFG["price_area"],
        "months": aggregate(priced),
        "sessions": [{k: (round(v, 3) if isinstance(v, float) else v) for k, v in s.items()}
                     for s in priced[:100]],
    }, indent=1))
    print(f"Wrote {OUT} ({len(priced)} sessions)")


def demo():
    random.seed(7)
    priced, d = [], date(2026, 1, 1)
    while d < date(2026, 10, 23):
        if random.random() < 0.45:
            kwh = round(random.uniform(8, 38), 1)
            avg_spot = random.uniform(0.3, 1.4) * (1.3 if d.month in (1, 2, 11, 12) else 0.8)
            c = dict(kwh=kwh, spot=kwh * avg_spot, markup=kwh * .05, grid=kwh * .30, tax=kwh * .36)
            c["vat"] = 0.25 * (c["spot"] + c["markup"] + c["grid"] + c["tax"])
            c["total"] = sum(c[k] for k in ("spot", "markup", "grid", "tax", "vat"))
            c["unpriced_kwh"] = 0
            start = datetime(d.year, d.month, d.day, 18, 0, tzinfo=TZ) + timedelta(minutes=random.randint(0, 360))
            priced.append({"start": start.isoformat(timespec="minutes"),
                           "end": (start + timedelta(hours=kwh / 7.4)).isoformat(timespec="minutes"), **c})
        d += timedelta(days=1)
    write_output(priced)


def main():
    if "--demo" in sys.argv:
        return demo()
    token = zaptec_token()
    sessions = fetch_sessions(token)
    if "--debug" in sys.argv and sessions:
        p = ROOT / "data" / "raw_session_sample.json"
        p.write_text(json.dumps(sessions[0], indent=1))
        print(f"Dumped raw session to {p}")
    if not sessions:
        print("No sessions returned")
    cache = load_cache()
    priced = []
    for s in sessions:
        if not s.get("EndDateTime") or float(s.get("Energy") or 0) <= 0:
            continue
        t0, t1 = parse_ts(s["StartDateTime"]), parse_ts(s["EndDateTime"])
        intervals = intervals_for(t0.astimezone(TZ).date(), t1.astimezone(TZ).date(), cache)
        c = price_session(s, intervals)
        priced.append({"start": t0.astimezone(TZ).isoformat(timespec="minutes"),
                       "end": t1.astimezone(TZ).isoformat(timespec="minutes"),
                       **{k: round(v, 4) for k, v in c.items()}})
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(cache))
    write_output(priced)
    bad = sum(p["unpriced_kwh"] for p in priced)
    if bad > 0.01:
        print(f"WARNING: {bad:.1f} kWh had no spot price available and is not costed")


if __name__ == "__main__":
    main()
