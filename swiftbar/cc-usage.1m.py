#!/usr/bin/env python3
# <xbar.title>Claude Code usage</xbar.title>
# <xbar.desc>Account-wide 5h/7d limits (with per-model weekly limits such as Fable) and API-equivalent cost.</xbar.desc>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
# <swiftbar.hideSwiftBar>true</swiftbar.hideSwiftBar>
"""Menu-bar view of the global Claude Code numbers.

Limits come from Anthropic's usage endpoint, the same one /usage reads, because
only it reports the per-model weekly limits (such as Fable). The request uses
the access token Claude Code keeps in the macOS Keychain; the token is read for
the request only, never stored or refreshed here. At most one request a minute;
only the limit figures are cached (~/.cache/cc-statusline/usage-api.json, 0600).
If the request fails, the menu falls back to the all-models figures the status
line writes (limits.json). Costs come from api-result.json. No transcript
scanning and no background processes.
"""
import base64, datetime, getpass, json, os, ssl, subprocess, time, urllib.request

CACHE = os.path.expanduser("~/.cache/cc-statusline")
now = time.time()
MONO = "font=Menlo size=12 trim=false"
HEAD = "size=11 color=#8a8a8a"
AMBER, RED = "#c98a00,#ffcc4d", "#d12d2d,#ff6b6b"


def load(name):
    try:
        with open(os.path.join(CACHE, name)) as f:
            return json.load(f)
    except Exception:
        return None


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def dur(s):
    s = max(0, int(s))
    if s >= 86400: return "%dd%dh" % (s // 86400, s % 86400 // 3600)
    if s >= 3600: return "%dh%dm" % (s // 3600, s % 3600 // 60)
    if s >= 60: return "%dm" % (s // 60)
    return "%ds" % s


def usd(v):
    return "$%d" % int(v) if v >= 100 else "$%.2f" % v


def tok(n):
    if n >= 10**9: return "%d.%dB" % (n // 10**9, n % 10**9 // 10**8)
    if n >= 10**6: return "%d.%dM" % (n // 10**6, n % 10**6 // 10**5)
    if n >= 1000: return "%dK" % (n // 1000)
    return str(n)


def bar(pct, width=10):
    filled = max(1 if pct > 0 else 0, min(width, int(round(pct * width / 100))))
    return "▓" * filled + "░" * (width - filled)


def pace(pct, reset, length):
    """Same rule as the status line: (pace, class 0 safe/1 close/2 hot, eta secs)."""
    el = length - min(max(reset - now, 0), length)
    el = max(el, 60)
    p = min((pct / 100) / (el / length), 99)
    cls = 2 if p >= 1.5 - 1e-9 else 1 if p >= 1.0 - 1e-9 else 0
    if el < 300: cls = 0
    eta = el * (100 - pct) / pct if cls and pct > 0 else 0
    return p, cls, eta


def item(text, params=""):
    print(text + (" | " + params if params else ""))


def account():
    """The Claude account Claude Code is signed in to, from its own config (no network)."""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    path = os.path.join(cfg, ".claude.json") if cfg else os.path.expanduser("~/.claude.json")
    try:
        with open(path) as f:
            a = json.load(f).get("oauthAccount") or {}
    except Exception:
        return None
    name = a.get("displayName") or a.get("fullName") or a.get("emailAddress")
    if not name:
        return None
    return name, a.get("emailAddress") or "", a.get("planDisplayName") or ""


def account_rows():
    acc = account()
    if not acc:
        return ["Not signed in to Claude | " + HEAD]
    name, email, plan = acc
    clean_ = lambda t: t.replace("|", "/")
    rows = ["Signed in as %s%s | sfimage=person.crop.circle" % (clean_(name), " · " + clean_(plan) if plan else "")]
    if email and email != name:
        rows.append("--%s | %s" % (clean_(email), HEAD))
    return rows


USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_TTL = 55  # SwiftBar reruns the plugin every minute


def iso_ts(v):
    try:
        return datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def access_token():
    try:
        raw = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", "Claude Code-credentials",
                              "-a", getpass.getuser(), "-w"], capture_output=True, text=True, timeout=10).stdout
        oauth = json.loads(raw).get("claudeAiOauth") or {}
    except Exception:
        return None
    exp = num(oauth.get("expiresAt"))
    if exp and exp / 1000 < now:
        return None  # expired: Claude Code refreshes it on its next request
    return oauth.get("accessToken")


def account_id():
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    path = os.path.join(cfg, ".claude.json") if cfg else os.path.expanduser("~/.claude.json")
    try:
        with open(path) as f:
            return (json.load(f).get("oauthAccount") or {}).get("accountUuid")
    except Exception:
        return None


def tls():
    """python.org builds ship without a CA bundle until "Install Certificates" is
    run; macOS keeps the system roots at /etc/ssl/cert.pem, so use that then."""
    paths = ssl.get_default_verify_paths()
    if not (paths.cafile or (paths.capath and os.listdir(paths.capath))) and os.path.exists("/etc/ssl/cert.pem"):
        return ssl.create_default_context(cafile="/etc/ssl/cert.pem")
    return ssl.create_default_context()


def fetch_limits():
    """[(label, short, pct, reset, length)] from the usage endpoint, cached for a minute.
    The cache belongs to one account: after a switch it is ignored, so the new
    account's limits are fetched at once and the old ones are never shown for it."""
    path = os.path.join(CACHE, "usage-api.json")
    acct = account_id()
    cached = load("usage-api.json")
    if cached and cached.get("acct") != acct:
        cached = None
    if cached and now - num(cached.get("at") or 0) < USAGE_TTL:
        return cached.get("limits"), cached.get("at")
    tok = access_token()
    if not tok:
        return (cached or {}).get("limits"), (cached or {}).get("at")
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": "Bearer " + tok, "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code-meter"})
    try:
        with urllib.request.urlopen(req, timeout=8, context=tls()) as r:
            body = json.load(r)
    except Exception:
        return (cached or {}).get("limits"), (cached or {}).get("at")
    rows = []
    for l in body.get("limits") or []:
        pct, reset = num(l.get("percent")), iso_ts(l.get("resets_at"))
        if pct is None:
            continue
        model = (((l.get("scope") or {}).get("model") or {}).get("display_name") or "").strip()
        kind, group = str(l.get("kind") or ""), str(l.get("group") or "")
        weekly = group == "weekly" or kind.startswith("weekly")
        five = "five" in kind or "session" in kind or "five" in group or "session" in group
        # "standard" tells the all-models week apart from the per-model ones below it.
        base, short = ("7-day standard", "7d") if weekly else ("5-hour", "5h") if five else (group or kind, group or kind)
        if model:
            base, short = "%s %s" % ("7-day" if weekly else base, model), model
        rows.append([base, short, pct, reset, 604800 if weekly else 18000 if five else None])
    os.makedirs(CACHE, exist_ok=True)
    tmp = path + ".%d" % os.getpid()
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump({"at": now, "acct": acct, "limits": rows}, f)
    os.replace(tmp, path)
    return rows, now


lim = load("limits.json") or {}
res = load("api-result.json")
api_rows, api_at = None, None
try:
    api_rows, api_at = fetch_limits()
except Exception:
    pass
if api_rows:
    limit_rows = api_rows
else:  # offline or signed out: the all-models figures the status line saw
    limit_rows = [[name, label, num(lim.get(pk)), num(lim.get(rk)), length]
                  for label, name, pk, rk, length in (("5h", "5-hour", "five_pct", "five_reset", 18000),
                                                      ("7d", "7-day standard", "seven_pct", "seven_reset", 604800))]

# ---------------------------------------------------------------- limits
title, rows, level = [], [], 0  # level: 0 normal, 1 warn, 2 alert
width = max([len(r[0]) for r in limit_rows] + [6])
for name, label, pct, reset, length in limit_rows:
    if pct is None:
        continue
    weekly = length == 604800
    fmt, icon = ("%a %b %-d", "calendar") if weekly else ("%H:%M", "clock")
    name = name.ljust(width)
    # Only the standard (all-models) limits go in the menu-bar title and set its
    # colour; per-model limits such as Fable stay in the drop-down.
    std = label in ("5h", "7d")
    if reset is not None and reset <= now:
        if std:
            title.append("%s –" % label)
        rows.append(("%s  window reset, no usage since" % name, "sfimage=%s %s" % (icon, MONO), None))
        continue
    p = round(pct)
    if std:
        title.append("%s %d%%" % (label, p))
    cls, pc, eta = 0, 0, 0
    line = "%s %s %3d%%" % (name, bar(p), p)
    if reset is not None:
        line += "   resets %s · in %s" % (datetime.datetime.fromtimestamp(reset).strftime(fmt), dur(reset - now))
        if length:
            pc, cls, eta = pace(pct, reset, length)
    pace_cls = cls
    if p >= 90: cls = 2
    elif p >= 70: cls = max(cls, 1)
    if std:
        level = max(level, cls)
    color = {1: " color=" + AMBER, 2: " color=" + RED}.get(cls, "")
    warn = None
    if pace_cls and p < 100:  # a full bar says it all
        warn = ("%s ▲ %.1f× the refill pace · caps in ~%s" % (name.strip(), pc, dur(eta)), "sfimage=exclamationmark.triangle " + MONO + color)
    rows.append((line, "sfimage=%s %s%s" % (icon, MONO, color), warn))

# Title icon: the Claude desktop app's own menu-bar icon (a template image, so
# macOS tints it for light/dark menu bars), read from the installed app at run
# time rather than shipped with the plugin. Without the app, the asterisk SF
# Symbol stands in (tinted with the text when a limit is close).
TRAY = "/Applications/Claude.app/Contents/Resources/TrayIconTemplate@2x.png"
try:
    with open(TRAY, "rb") as f:
        icon = "templateImage=%s width=16 height=16" % base64.b64encode(f.read()).decode()
except OSError:
    icon = "sfimage=asterisk" + {1: " sfcolor=" + AMBER, 2: " sfcolor=" + RED}.get(level, "")
color = {1: " color=" + AMBER, 2: " color=" + RED}.get(level, "")
print((" · ".join(title) or "Claude –") + " | " + icon + color)
print("---")
for line in account_rows():
    print(line)
print("---")
item("Usage limits", HEAD)
if rows:
    for line, params, warn in rows:
        item(line, params)
        if warn:
            item(*warn)
else:
    item("No limits reported yet: open a Claude Code session")

# ---------------------------------------------------------------- spend
if res and res.get("w"):
    w = res["w"]
    sm = datetime.datetime.fromtimestamp(res["sm"])
    five_live = num(lim.get("five_reset")) is not None and num(lim.get("five_reset")) > now
    spend = []
    for k, label in (("5h", "This 5-hour window"), ("7d", "This 7-day window"),
                     ("mo", "Since " + sm.strftime("%b ") + str(sm.day))):
        if k == "5h" and not five_live:
            continue
        if w[k]["in"] > 0:
            spend.append((label, usd(w[k]["cost"])))
    try:
        price = float(res.get("price") or 0)
    except ValueError:
        price = 0
    mo = w["mo"]
    print("---")
    item("API-equivalent spend (what this usage would cost on the API)", HEAD)
    for label, value in spend:
        item("%-20s %8s" % (label, value), MONO + " sfimage=dollarsign.circle")
    if price > 0:
        mult = mo["cost"] / price
        item("%-20s %8s" % ("Plan value used", ("%.0f×" if mult >= 10 else "%.1f×") % mult)
             + "  of %s $%s" % (res.get("plan", "plan"), res["price"]), MONO + " sfimage=creditcard")

    # details submenu
    item("Details", "sfimage=list.bullet")
    if mo["in"] > 0:
        item("--Prompt cache hits   %d%%" % round(mo["cr"] * 100 / mo["in"]), MONO)
    tot = mo["cost"]
    mix = []
    for t in ("fable", "opus", "sonnet", "haiku", "other"):
        share = round(res.get("tiers", {}).get(t, 0) * 100 / tot) if tot > 0 else 0
        if share > 0:
            mix.append("%s %d%%" % (t, share))
    if mix:
        item("--Models (by cost)    " + " · ".join(mix), MONO)
    item("--Tokens this cycle   %s in · %s out" % (tok(mo["in"]), tok(mo["out"])), MONO)

# ---------------------------------------------------------------- footer
print("---")
ages = []
if api_rows and api_at:
    ages.append("limits %s ago" % dur(now - num(api_at)))
elif num(lim.get("at")) is not None:
    ages.append("limits %s ago (offline: all models only)" % dur(now - num(lim["at"])))
if res and num(res.get("at")) is not None:
    ages.append("costs %s ago" % dur(now - res["at"]))
item("Updated " + (", ".join(ages) or "never"), HEAD)
item("Counts Claude Code sessions on this Mac only", HEAD)
item("Refresh", "refresh=true sfimage=arrow.clockwise")
