"""Pulls Observe/Report stats for the media kit page. Run nightly by .github/workflows/media-kit-stats.yml."""
import json, os, urllib.request, urllib.parse, urllib.error, datetime as dt

problems, notes = [], []
now = dt.datetime.now(dt.timezone.utc)
out = {"updated": now.replace(microsecond=0).isoformat().replace("+00:00", "Z")}


def call(url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers or {})
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:400]}".replace("\n", " "))


def month_starts(n):
    """First day of each of the last n complete months, oldest first."""
    y, m, res = now.year, now.month, []
    for _ in range(n):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        res.append(dt.datetime(y, m, 1, tzinfo=dt.timezone.utc))
    return sorted(res)


def next_month(d):
    return dt.datetime(d.year + (d.month == 12), d.month % 12 + 1, 1, tzinfo=dt.timezone.utc)


# ---------------- Google Analytics 4 ----------------
try:
    prop = os.environ.get("GA4_PROPERTY_ID", "").strip()
    if not prop:
        raise RuntimeError("the GA4_PROPERTY_ID variable is empty")
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport"
    h = {"Authorization": "Bearer " + os.environ["ACCESS_TOKEN"]}
    last28 = [{"startDate": "28daysAgo", "endDate": "yesterday"}]

    def rows(body):
        r = call(url, body, h)
        return [([d["value"] for d in x.get("dimensionValues", [])],
                 [float(m["value"]) for m in x["metricValues"]]) for x in r.get("rows", [])]

    def top(dim, met, n):
        return {"dateRanges": last28, "dimensions": [{"name": dim}], "metrics": [{"name": met}],
                "orderBys": [{"metric": {"metricName": met}, "desc": True}], "limit": n}

    t = rows({"dateRanges": last28, "metrics": [{"name": "activeUsers"}, {"name": "screenPageViews"},
                                                 {"name": "sessions"}, {"name": "userEngagementDuration"}]})
    users, views, sessions, eng = t[0][1] if t else (0, 0, 0, 0)
    out["ga4"] = {
        "window": "Last 28 days",
        "users": int(users), "views": int(views), "sessions": int(sessions),
        "avgEngagementSec": round(eng / users) if users else 0,
        "sources": [{"name": d[0], "value": int(m[0])} for d, m in rows(top("sessionSource", "sessions", 8))],
        "pages": [{"name": d[0], "value": int(m[0])} for d, m in rows(top("pageTitle", "screenPageViews", 8))],
        "countries": [{"name": d[0], "value": int(m[0])} for d, m in rows(top("country", "activeUsers", 5))],
        "monthly": sorted([{"month": d[0][:4] + "-" + d[0][4:], "users": int(m[0]), "views": int(m[1])}
                           for d, m in rows({"dateRanges": [{"startDate": "365daysAgo", "endDate": "yesterday"}],
                                             "dimensions": [{"name": "yearMonth"}],
                                             "metrics": [{"name": "activeUsers"}, {"name": "screenPageViews"}]})],
                          key=lambda x: x["month"]),
    }
except Exception as e:
    problems.append("GOOGLE ANALYTICS FAILED: " + str(e))

# ---------------- YouTube (public channel totals) ----------------
try:
    key = os.environ.get("YT_API_KEY", "").strip()
    if not key:
        raise RuntimeError("the YT_API_KEY secret is empty")
    q = urllib.parse.urlencode({"part": "statistics", "forHandle": os.environ.get("YT_HANDLE", "@Observe-Report"), "key": key})
    items = call("https://www.googleapis.com/youtube/v3/channels?" + q).get("items", [])
    if not items:
        raise RuntimeError("no channel found for the handle")
    s = items[0]["statistics"]
    out["youtube"] = {"subscribers": int(s.get("subscriberCount", 0)),
                      "totalViews": int(s.get("viewCount", 0)),
                      "videos": int(s.get("videoCount", 0))}
except Exception as e:
    problems.append("YOUTUBE FAILED: " + str(e))

# ---------------- Instagram ----------------
IG = "https://graph.instagram.com"
ig_token = os.environ.get("IG_TOKEN", "").strip()
if ig_token:
    try:
        def ig(path, **params):
            params["access_token"] = ig_token
            return call(f"{IG}/{path}?" + urllib.parse.urlencode(params))

        me = ig("me", fields="user_id,username,followers_count,media_count")
        res = {"username": me.get("username"), "followers": me.get("followers_count")}

        def total(metric, since, until):
            """Sum a total_value metric over a window, splitting into <=30-day chunks."""
            tot, s = 0, since
            while s < until:
                e = min(until, s + dt.timedelta(days=30))
                r = ig("me/insights", metric=metric, period="day", metric_type="total_value",
                       since=int(s.timestamp()), until=int(e.timestamp()))
                for d in r.get("data", []):
                    tot += int(d.get("total_value", {}).get("value", 0))
                s = e
            return tot

        start30 = now - dt.timedelta(days=30)
        for name, metric in [("views", "views"), ("reach", "reach"), ("engagements", "total_interactions")]:
            try:
                res[name] = total(metric, start30, now)
            except Exception as e:
                notes.append(f"Instagram {metric}: {e}")

        monthly = []
        for ms in month_starts(6):
            try:
                monthly.append({"month": ms.strftime("%Y-%m"), "views": total("views", ms, next_month(ms))})
            except Exception as e:
                notes.append(f"Instagram {ms:%Y-%m}: {e}")
        res["monthly"] = monthly

        # top posts from the last 90 days, ranked by views
        posts = []
        media = ig("me/media", fields="id,caption,permalink,timestamp", limit=30).get("data", [])
        for m in media:
            ts = dt.datetime.fromisoformat(m["timestamp"].replace("+0000", "+00:00"))
            if ts < now - dt.timedelta(days=90):
                continue
            try:
                r = ig(f"{m['id']}/insights", metric="views")
                v = int(r["data"][0]["values"][0]["value"]) if r.get("data") else 0
            except Exception:
                continue
            title = (m.get("caption") or "Instagram post").strip().split("\n")[0][:90]
            posts.append({"title": title, "url": m.get("permalink", ""), "views": v, "date": ts.date().isoformat()})
        res["top"] = sorted(posts, key=lambda p: -p["views"])[:3]

        # audience (Instagram only shares this once an account has 100+ followers)
        for b in ["age", "gender"]:
            try:
                r = ig("me/insights", metric="follower_demographics", period="lifetime", timeframe="this_month",
                       metric_type="total_value", breakdown=b)
                vals = r["data"][0]["total_value"]["breakdowns"][0]["results"]
                res[b] = sorted([{"name": x["dimension_values"][0], "value": x["value"]} for x in vals],
                                key=lambda x: -x["value"])
            except Exception:
                pass
        out["instagram"] = res
    except Exception as e:
        problems.append("INSTAGRAM FAILED: " + str(e))

    # keep the 60-day token alive: refresh it once a week (Mondays), or on any manual run
    if now.weekday() == 0 or os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        try:
            r = call(f"{IG}/refresh_access_token?" + urllib.parse.urlencode(
                {"grant_type": "ig_refresh_token", "access_token": ig_token}))
            newtok = r.get("access_token")
            if newtok:
                print("::add-mask::" + newtok)
                p = os.path.join(os.environ.get("RUNNER_TEMP", "/tmp"), "ig_new_token")
                open(p, "w").write(newtok)
                out.setdefault("instagram", {})["tokenRefreshed"] = now.date().isoformat()
        except Exception as e:
            notes.append("Instagram token refresh skipped: " + str(e))

# ---------------- save ----------------
os.makedirs("data", exist_ok=True)
prev = {}
if os.path.exists("data/stats-auto.json"):
    try:
        prev = json.load(open("data/stats-auto.json"))
    except Exception:
        pass
# if a source failed today, keep yesterday's numbers rather than blanking the page
for k in ["ga4", "youtube", "instagram"]:
    if k not in out and k in prev:
        out[k] = prev[k]
        out.setdefault("stale", []).append(k)
json.dump(out, open("data/stats-auto.json", "w"), indent=1)

hist_path = "data/stats-history.json"
hist = json.load(open(hist_path)) if os.path.exists(hist_path) else {}
day = hist.setdefault(now.date().isoformat(), {})
if "youtube" in out and "youtube" not in out.get("stale", []):
    day.update(yt_subs=out["youtube"]["subscribers"], yt_views=out["youtube"]["totalViews"])
if "instagram" in out and "instagram" not in out.get("stale", []):
    day.update(ig_followers=out["instagram"].get("followers"))
json.dump(dict(sorted(hist.items())), open(hist_path, "w"), indent=1)

print("Saved:", ", ".join(k for k in ["ga4", "youtube", "instagram"] if k in out))
for n in notes:
    print("::warning::" + n)
for p in problems:
    print("::error::" + p)
if problems and not any(k in out for k in ["ga4", "youtube", "instagram"]):
    raise SystemExit(1)
