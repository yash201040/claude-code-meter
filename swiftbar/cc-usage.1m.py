#!/usr/bin/env python3
# <xbar.title>Claude Code usage</xbar.title>
# <xbar.desc>Account-wide 5h/7d limits and API-equivalent cost from the Claude Code status line caches.</xbar.desc>
# <swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
# <swiftbar.hideDisablePlugin>true</swiftbar.hideDisablePlugin>
# <swiftbar.hideSwiftBar>true</swiftbar.hideSwiftBar>
"""Menu-bar view of the global Claude Code numbers.

Read-only and cheap by design: it only reads small JSON files that the status
line already keeps fresh while any Claude Code session is active
(~/.cache/cc-statusline/limits.json, api-result.json). No
transcript scanning, no network, no background processes. When no session
runs, usage cannot change, so stale-but-shown is correct; the age is shown in
the menu.
"""
import base64, datetime, json, os, time

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


lim = load("limits.json") or {}
res = load("api-result.json")

# ---------------------------------------------------------------- limits
title, rows, level = [], [], 0  # level: 0 normal, 1 warn, 2 alert
for label, name, pk, rk, length, fmt, icon in (
        ("5h", "5-hour", "five_pct", "five_reset", 18000, "%H:%M", "clock"),
        ("7d", "7-day ", "seven_pct", "seven_reset", 604800, "%a %b %-d", "calendar")):
    pct, reset = num(lim.get(pk)), num(lim.get(rk))
    if pct is None:
        continue
    if reset is not None and reset <= now:
        title.append("%s –" % label)
        rows.append(("%s  window reset, no usage since" % name, "sfimage=%s %s" % (icon, MONO), None))
        continue
    p = round(pct)
    title.append("%s %d%%" % (label, p))
    cls, pc, eta = 0, 0, 0
    line = "%s %s %3d%%" % (name, bar(p), p)
    if reset is not None:
        line += "   resets %s · in %s" % (datetime.datetime.fromtimestamp(reset).strftime(fmt), dur(reset - now))
        pc, cls, eta = pace(pct, reset, length)
    pace_cls = cls
    if p >= 90: cls = 2
    elif p >= 70: cls = max(cls, 1)
    level = max(level, cls)
    color = {1: " color=" + AMBER, 2: " color=" + RED}.get(cls, "")
    warn = None
    if pace_cls:
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
if num(lim.get("at")) is not None:
    ages.append("limits %s ago" % dur(now - num(lim["at"])))
if res and num(res.get("at")) is not None:
    ages.append("costs %s ago" % dur(now - res["at"]))
item("Updated " + (", ".join(ages) or "never"), HEAD)
item("Counts Claude Code sessions on this Mac only", HEAD)
item("Refresh", "refresh=true sfimage=arrow.clockwise")
