#!/usr/bin/env python3
"""
Flight price tracker (runs automatically on GitHub; you never need to run it yourself).

Reads config.json, asks Google Flights (through SerpApi) for the cheapest
direct / 1-stop fare, adds an estimated checked-bag cost, appends the result to
data.json, and sends a phone notification when the price looks good.
The dashboard (index.html) reads data.json.
"""

import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
DATA_FILE = ROOT / "data.json"
SERPAPI_KEY = os.getenv("SERPAPI_KEY", "")
NTFY_TOPIC = os.getenv("NTFY_TOPIC", "")


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def log(msg):
    print(msg, flush=True)


# ------------------------------ storage --------------------------------------

def load_data():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {"observations": [], "alerts": []}


def save_data(data):
    data["updated"] = now_iso()
    data["origin"] = CFG["origin"]
    data["currency"] = CFG["currency"]
    data["checked_bags"] = CFG.get("checked_bags", 0)
    data["min_history"] = CFG.get("min_history", 7)
    DATA_FILE.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")


# ------------------------------ prices ---------------------------------------

def search(dest, trip):
    params = {
        "engine": "google_flights",
        "departure_id": CFG["origin"],
        "arrival_id": dest,
        "outbound_date": trip["depart"],
        "currency": CFG["currency"],
        "hl": "en",
        "adults": CFG.get("adults", 1),
        "stops": 1 if CFG.get("max_stops", 1) == 0 else 2,  # 1 = nonstop, 2 = up to 1 stop
        "api_key": SERPAPI_KEY,
    }
    if trip.get("return"):
        params["type"] = 1
        params["return_date"] = trip["return"]
    else:
        params["type"] = 2
    url = "https://serpapi.com/search.json?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=90) as resp:
        return json.load(resp)


def bag_fee(airlines, one_way):
    """Estimated cost of the checked bag(s) for the whole trip."""
    bags = CFG.get("checked_bags", 0)
    if not bags:
        return 0.0
    table = CFG.get("bag_fee_per_bag_each_way", {})
    default = table.get("default", 0)
    fee = max((table.get(a, default) for a in airlines), default=default)
    return float(fee * bags * (1 if one_way else 2))


def cheapest_offer(data, trip):
    offers = (data.get("best_flights") or []) + (data.get("other_flights") or [])
    best = None
    for o in offers:
        base = o.get("price")
        legs = o.get("flights") or []
        if base is None or not legs:
            continue
        stops = max(len(legs) - 1, 0)
        if stops > CFG.get("max_stops", 1):
            continue
        airlines = []
        for leg in legs:
            a = leg.get("airline")
            if a and a not in airlines:
                airlines.append(a)
        fee = bag_fee(airlines, one_way=not trip.get("return"))
        total = float(base) + fee
        if best is None or total < best["price"]:
            best = {
                "price": total,
                "base_price": float(base),
                "bag_fee": fee,
                "stops": stops,
                "airlines": ", ".join(airlines),
                "via": ", ".join((l.get("id") or l.get("name", "?")) for l in (o.get("layovers") or [])),
                "duration_min": o.get("total_duration"),
            }
    if best:
        pi = data.get("price_insights") or {}
        rng = (pi.get("typical_price_range") or []) + [None, None]
        best["level"] = pi.get("price_level")
        best["tlow"], best["thigh"] = rng[0], rng[1]
        best["link"] = (data.get("search_metadata") or {}).get("google_flights_url", "")
    return best


# ------------------------------ is it good? ----------------------------------

def percentile(vals, pct):
    k = (len(vals) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def same_trip(o, dest, trip):
    return (o["dest"] == dest and o["depart"] == trip["depart"]
            and (o.get("ret") or "") == (trip.get("return") or ""))


def evaluate(history, obs):
    hist = sorted(o["price"] for o in history)
    price, reasons = obs["price"], []
    cur = CFG["currency"]
    tp = CFG.get("target_price")
    if tp and price <= tp:
        reasons.append(f"at or below your target of {tp:,.0f} {cur}")
    if len(hist) >= CFG.get("min_history", 7):
        med = statistics.median(hist)
        if price < hist[0]:
            reasons.append(f"lowest price seen so far (previous low {hist[0]:,.0f})")
        elif price <= percentile(hist, CFG.get("percentile", 15)):
            reasons.append(f"in the cheapest {CFG.get('percentile', 15)}% of {len(hist)} past prices")
        drop = CFG.get("drop_vs_median", 0.10)
        if price <= med * (1 - drop):
            reasons.append(f"{(1 - price / med) * 100:.0f}% below the usual price ({med:,.0f})")
    if CFG.get("use_google_level", True) and obs.get("level") == "low":
        reasons.append("Google rates the fare as LOW for this route")
    return reasons


def should_alert(alerts, key, price):
    mine = [a for a in alerts if a["key"] == key]
    if not mine:
        return True
    last = max(mine, key=lambda a: a["ts"])
    age = datetime.now(timezone.utc) - datetime.fromisoformat(last["ts"])
    if age > timedelta(days=CFG.get("alert_cooldown_days", 3)):
        return True
    return price < last["price"] * 0.97


def notify(title, body, link=""):
    if not NTFY_TOPIC:
        log("(no NTFY_TOPIC set, skipping phone notification)")
        return False
    try:
        headers = {"Title": title, "Tags": "airplane", "Priority": "high"}
        if link:
            headers["Click"] = link
        req = urllib.request.Request(f"https://ntfy.sh/{NTFY_TOPIC}",
                                     data=body.encode("utf-8"), headers=headers, method="POST")
        urllib.request.urlopen(req, timeout=30).read()
        return True
    except Exception as e:  # noqa: BLE001
        log(f"Notification failed: {e}")
        return False


# ------------------------------ main -----------------------------------------

def main():
    if not SERPAPI_KEY:
        sys.exit("SERPAPI_KEY is missing. Add it under Settings > Secrets and variables > Actions.")
    first_run = not DATA_FILE.exists()
    data = load_data()
    ok_count = 0
    cur = CFG["currency"]

    for dest in CFG["destinations"]:
        for trip in CFG["trips"]:
            label = f"{CFG['origin']}->{dest} {trip['depart']}" + (f" / {trip['return']}" if trip.get("return") else "")
            try:
                raw = search(dest, trip)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
                log(f"{label}: request failed ({e})")
                continue
            if raw.get("error"):
                log(f"{label}: API message: {raw['error']}")
                continue
            obs = cheapest_offer(raw, trip)
            if not obs:
                log(f"{label}: no flights found with {CFG.get('max_stops', 1)} stop(s) or fewer")
                continue
            ok_count += 1
            obs.update(ts=now_iso(), dest=dest, depart=trip["depart"], ret=trip.get("return") or "")

            history = [o for o in data["observations"] if same_trip(o, dest, trip)]
            reasons = evaluate(history, obs)
            data["observations"].append(obs)

            kind = "direct" if obs["stops"] == 0 else f"1 stop via {obs['via'] or '?'}"
            log(f"{label}: {obs['price']:,.0f} {cur} incl. est. bags ({kind}, {obs['airlines']})")

            key = f"{dest}|{trip['depart']}|{trip.get('return') or ''}"
            if reasons and should_alert(data["alerts"], key, obs["price"]):
                mins = obs["duration_min"]
                dur = f"{mins // 60}h{mins % 60:02d}" if mins else "?"
                body = (f"{CFG['origin']} -> {dest}\n"
                        f"Depart {trip['depart']}" + (f", return {trip['return']}" if trip.get("return") else " (one-way)") + "\n"
                        f"{obs['price']:,.0f} {cur} with {CFG.get('checked_bags', 0)} checked bag(s) estimated "
                        f"(fare {obs['base_price']:,.0f} + bags {obs['bag_fee']:,.0f})\n"
                        f"{kind}, {obs['airlines']}, {dur}\n\n" + "\n".join(f"- {r}" for r in reasons))
                if notify(f"Good fare: {CFG['origin']}-{dest} {obs['price']:,.0f} {cur}", body, obs.get("link", "")):
                    data["alerts"].append({"ts": now_iso(), "key": key, "price": obs["price"]})
                    log("  -> alert sent: " + "; ".join(reasons))
            time.sleep(2)

    if ok_count == 0:
        sys.exit("No prices could be fetched. Check the log above (API key? quota? dates in the past?).")
    save_data(data)
    if first_run:
        notify("Flight tracker is working",
               f"First check done: {ok_count} route(s) recorded. You'll get alerts when prices look good.")


if __name__ == "__main__":
    main()
