#!/usr/bin/env python3
"""Transcript scanner for statusline-command.sh: the slow parts of the status
line, done incrementally so a render costs about what was written since the
last one.

  statusline-scan.py <session_id> <transcript_path> <s5> <s7>

prints three lines:
  1. m_in m_out s_in s_out w_in w_out s_add s_rem w_add w_rem   (this session)
  2. the API-equivalent cost line (ANSI-colored, empty when nothing to show)
  3. this session's share of the 5h/7d windows (ANSI-colored, may be empty)

Session totals: transcripts are append-only, so each file's byte offset is kept
and only bytes appended since are parsed. Cache: ~/.cache/cc-statusline/.

API≈ line (all sessions, 5h / 7d / billing month): the scan is shared by every
open session and never blocks a render once it has run: a result older than
SCAN_TTL (or for other window starts) is shown as-is while a detached
background refresh brings it up to date. Only the very first scan is inline.

Pricing per MTok (platform.claude.com, 2026-05-26): fable $10/$50, opus $5/$25,
sonnet $3/$15, haiku $1/$5; 5m cache writes 1.25x input, 1h writes 2x, reads 0.1x.
"""
import datetime, glob, json, os, subprocess, sys, tempfile, time

HOME = os.path.expanduser("~")
PROJ = os.path.join(HOME, ".claude", "projects")
CACHE_DIR = os.path.join(HOME, ".cache", "cc-statusline")
FILES_CACHE = os.path.join(CACHE_DIR, "files.json")
RESULT = os.path.join(CACHE_DIR, "api-result.json")
LOCK = os.path.join(CACHE_DIR, "scan.lock")
SCAN_TTL = 60
NONE_TS = 9999999999
PRICES = {"fable": (10, 50), "opus": (5, 25), "sonnet": (3, 15), "haiku": (1, 5), "other": (5, 25)}

ESC = "\033"
RESET, BOLD = ESC + "[0m", ESC + "[1m"
RED, GREEN, YELLOW, CYAN, GREY = (ESC + "[31m", ESC + "[32m", ESC + "[33m", ESC + "[36m", ESC + "[90m")
SEP = GREY + "│" + RESET


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save(path, obj):
    os.makedirs(CACHE_DIR, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CACHE_DIR)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, path)


def read_new(path, off):
    """Complete lines appended after byte `off` -> (lines, new_off)."""
    with open(path, "rb") as f:
        f.seek(off)
        data = f.read()
    end = data.rfind(b"\n")
    if end < 0:
        return [], off
    return data[: end + 1].split(b"\n"), off + end + 1


def tier(model):
    for t in ("fable", "opus", "sonnet", "haiku"):
        if t in model:
            return t
    return "other"


def parse_ts(s):
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0


def usage_of(e):
    msg = e.get("message") if isinstance(e.get("message"), dict) else {}
    u = msg.get("usage") or e.get("usage")
    return msg, (u if isinstance(u, dict) else None)


def dedup_key(e, msg):
    mid = msg.get("id")
    return (mid + "|" + (e.get("requestId") or "")) if mid else None


def dedup(entries, out_idx):
    """Resumed/forked sessions copy history and streamed messages are written
    more than once with growing output: keep the max-output snapshot per key."""
    best, rest = {}, []
    for en in entries:
        k = en[0]
        if k is None:
            rest.append(en)
        elif k not in best or en[out_idx] > best[k][out_idx]:
            best[k] = en
    return list(best.values()) + rest


def fmt_tok(n):
    if n >= 10**9: return "%d.%dB" % (n // 10**9, (n % 10**9) // 10**8)
    if n >= 10**6: return "%d.%dM" % (n // 10**6, (n % 10**6) // 10**5)
    if n >= 1000: return "%dK" % (n // 1000)
    return str(n)


def fmt_usd(v):
    return "$%d" % int(v) if v >= 100 else "$%.2f" % v


# ---------------------------------------------------------------------------
# this session: tokens per origin + agent edit lines
# ---------------------------------------------------------------------------
def lns(s):
    parts = (s or "").split("\n")
    return parts[:-1] if parts and parts[-1] == "" else parts


def edit_lines(name, inp):
    """Write = full content added; Edit/MultiEdit = multiset line diff of
    old_string -> new_string (unchanged lines cancel out)."""
    if name == "Write":
        return len(lns(inp.get("content"))), 0
    edits = inp.get("edits") or [{"old_string": inp.get("old_string"), "new_string": inp.get("new_string")}]
    add = rem = 0
    for ed in edits:
        o, n = lns(ed.get("old_string")), lns(ed.get("new_string"))
        oc = {}
        for l in o:
            oc[l] = oc.get(l, 0) + 1
        nc = {}
        for l in n:
            nc[l] = nc.get(l, 0) + 1
        c = sum(min(v, oc.get(k, 0)) for k, v in nc.items())
        add += len(n) - c
        rem += len(o) - c
    return add, rem


def scan_session_file(path, rec, want_edits):
    lines, rec["off"] = read_new(path, rec["off"])
    for line in lines:
        has_usage = b'"usage"' in line
        has_tool = want_edits and (b'"tool_use"' in line or b'"is_error":true' in line)
        if not (has_usage or has_tool):
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if not isinstance(e, dict):
            continue
        msg, u = usage_of(e)
        if u is not None:
            rec["e"].append([
                dedup_key(e, msg),
                (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0)
                + (u.get("cache_read_input_tokens") or 0),
                u.get("output_tokens") or 0,
            ])
        if has_tool and isinstance(msg.get("content"), list):
            for c in msg["content"]:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "tool_result" and c.get("is_error") is True:
                    rec["err"].append(c.get("tool_use_id"))
                elif (msg.get("role") == "assistant" and c.get("type") == "tool_use"
                      and c.get("name") in ("Edit", "Write", "MultiEdit") and c.get("id")):
                    rec["tu"][c["id"]] = edit_lines(c["name"], c.get("input") or {})


def session_totals(sid, transcript):
    if not transcript or not os.path.isfile(transcript):
        return [0] * 10
    cpath = os.path.join(CACHE_DIR, "sess-%s.json" % (sid or "default"))
    cache = load(cpath, {})
    files = {"main": [transcript], "sub": [], "wf": []}
    side = transcript[:-6] if transcript.endswith(".jsonl") else transcript
    if os.path.isdir(side):
        for p in glob.glob(os.path.join(side, "**", "*.jsonl"), recursive=True):
            files["wf" if "/subagents/workflows/" in p else "sub"].append(p)
    out, changed = [], False
    agg = {}
    for cat, paths in files.items():
        entries, tus, errs = [], {}, set()
        for p in paths:
            try:
                size = os.stat(p).st_size
            except OSError:
                continue
            rec = cache.get(p)
            if rec is None or size < rec["off"]:
                rec = {"off": 0, "e": [], "tu": {}, "err": []}
            if size > rec["off"]:
                scan_session_file(p, rec, cat != "main")
                changed = True
            cache[p] = rec
            entries.extend(rec["e"])
            tus.update(rec["tu"])
            errs.update(rec["err"])
        ti = to = 0
        for en in dedup(entries, 2):
            ti += en[1]; to += en[2]
        la = lr = 0
        for k, (a, r) in tus.items():
            if k not in errs:
                la += a; lr += r
        agg[cat] = (ti, to, la, lr)
    if changed:
        save(cpath, cache)
    m, s, w = agg["main"], agg["sub"], agg["wf"]
    return [m[0], m[1], s[0], s[1], w[0], w[1], s[2], s[3], w[2], w[3]]


# ---------------------------------------------------------------------------
# all sessions: API-equivalent cost per window
# ---------------------------------------------------------------------------
def billing_start():
    try:
        day = int(os.environ.get("CC_BILLING_DAY") or 1)
    except ValueError:
        day = 1
    day = max(1, min(28, day))
    now = datetime.datetime.now()
    y, m = now.year, now.month
    if now.day < day:
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return int(datetime.datetime(y, m, day).timestamp())


def plan():
    price = os.environ.get("CC_PLAN_PRICE")
    if price:
        return price, "plan"
    oa = load(os.path.join(HOME, ".claude.json"), {}).get("oauthAccount") or {}
    t = oa.get("organizationRateLimitTier") or oa.get("organizationType") or ""
    if "max_20x" in t: return "200", "Max"
    if "max" in t: return "100", "Max"
    if "pro" in t: return "20", "Pro"
    return "20", "plan"


def extract_usage(path, off):
    out = []
    lines, off = read_new(path, off)
    for line in lines:
        if b'"usage"' not in line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if not isinstance(e, dict):
            continue
        msg, u = usage_of(e)
        if u is None:
            continue
        model = msg.get("model") or "unknown"
        if model == "<synthetic>":
            continue
        cc = u.get("cache_creation") or {}
        out.append([
            dedup_key(e, msg), parse_ts(e.get("timestamp") or ""), e.get("sessionId") or "", tier(model),
            u.get("input_tokens") or 0, u.get("cache_creation_input_tokens") or 0,
            cc.get("ephemeral_1h_input_tokens") or 0, u.get("cache_read_input_tokens") or 0,
            u.get("output_tokens") or 0,
        ])
    return out, off


def cost(en):
    pi, po = PRICES[en[3]]
    i, cw, e1, r, o = en[4], en[5], en[6], en[7], en[8]
    return (i * pi + (cw - e1) * pi * 1.25 + e1 * pi * 2 + r * pi * 0.1 + o * po) / 1e6


def refresh(s5, s7):
    sm = billing_start()
    earliest = min(s5, s7, sm)
    cache = load(FILES_CACHE, {})
    seen = set()
    for path in glob.glob(os.path.join(PROJ, "**", "*.jsonl"), recursive=True):
        try:
            st = os.stat(path)
        except OSError:
            continue
        if st.st_mtime < earliest:
            continue  # append-only: untouched since the earliest window opened
        seen.add(path)
        rec = cache.get(path)
        if rec is None or st.st_size < rec["off"]:
            rec = {"off": 0, "e": []}
        if st.st_size > rec["off"]:
            new, rec["off"] = extract_usage(path, rec["off"])
            rec["e"].extend(new)
        cache[path] = rec
    for path in list(cache):
        if path not in seen:
            del cache[path]
    save(FILES_CACHE, cache)

    w = {k: {"in": 0, "out": 0, "cost": 0.0, "cr": 0} for k in ("5h", "7d", "mo")}
    tiers, sess5, sess7 = {}, {}, {}
    for en in dedup([en for rec in cache.values() for en in rec["e"]], 8):
        t, c = en[1], cost(en)
        for k, start in (("5h", s5), ("7d", s7), ("mo", sm)):
            if t >= start:
                w[k]["in"] += en[4] + en[5] + en[7]; w[k]["out"] += en[8]
                w[k]["cost"] += c; w[k]["cr"] += en[7]
        if t >= sm:
            tiers[en[3]] = tiers.get(en[3], 0) + c
        if en[2]:
            if t >= s5: sess5[en[2]] = sess5.get(en[2], 0) + c
            if t >= s7: sess7[en[2]] = sess7.get(en[2], 0) + c
    price, name = plan()
    save(RESULT, {"at": time.time(), "s5": s5, "s7": s7, "sm": sm, "w": w, "tiers": tiers,
                  "sess5": sess5, "sess7": sess7, "price": price, "plan": name})


def api_result(s5, s7):
    res = load(RESULT, None)
    fresh = (res is not None and time.time() - res["at"] < SCAN_TTL
             and res["s5"] == s5 and res["s7"] == s7 and res["sm"] == billing_start())
    if fresh:
        return res
    try:
        locked = time.time() - os.stat(LOCK).st_mtime < 120
    except OSError:
        locked = False
    if res is None and not locked:
        refresh(s5, s7)  # first ever scan: inline, like the old jq pass
        return load(RESULT, None)
    if not locked:
        os.makedirs(CACHE_DIR, exist_ok=True)
        open(LOCK, "w").close()
        subprocess.Popen(
            ["nice", "-n", "10", sys.executable, os.path.abspath(__file__), "--refresh", str(s5), str(s7)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    return res


def api_lines(sid, res):
    if not res:
        return "", ""
    w = res["w"]
    if not (w["mo"]["in"] > 0 or w["5h"]["in"] > 0):
        return "", ""
    dot = " " + GREY + "·" + RESET + " "
    segs = []
    sm = datetime.datetime.fromtimestamp(res["sm"])
    for k, label in (("5h", "5h"), ("7d", "7d"), ("mo", sm.strftime("%b") + str(sm.day) + "→")):
        if w[k]["in"] > 0:
            segs.append("%s %s%s%s" % (label, BOLD, fmt_usd(w[k]["cost"]), RESET))
    try:
        p = float(res["price"])
    except ValueError:
        p = 0
    mult = w["mo"]["cost"] / p if p > 0 else 0
    mult_s = ("%.0f" if mult >= 10 else "%.1f") % mult
    mult_seg = "%s%s%s× %s $%s%s" % (CYAN, BOLD, mult_s, res["plan"], res["price"], RESET)
    tot = w["mo"]["cost"]
    mix = []
    for t in ("fable", "opus", "sonnet", "haiku", "other"):
        pct = round(res["tiers"].get(t, 0) * 100 / tot) if tot > 0 else 0
        if pct > 0:
            mix.append("%s %d%%" % (t, pct))
    cache_pct = round(w["mo"]["cr"] * 100 / w["mo"]["in"]) if w["mo"]["in"] > 0 else 0
    cache_col = GREEN if cache_pct >= 80 else YELLOW if cache_pct >= 50 else RED
    cache_seg = "%scache %d%%%s" % (cache_col, cache_pct, RESET)
    tok_seg = "%s/%s tok" % (fmt_tok(w["mo"]["in"]), fmt_tok(w["mo"]["out"]))
    line = " ".join([GREY + "API≈" + RESET, dot.join(segs), SEP, mult_seg, SEP, cache_seg, SEP,
                     GREY + " · ".join(mix) + " · " + tok_seg + RESET])

    share = []
    for k, sk in (("5h", "sess5"), ("7d", "sess7")):
        if w[k]["cost"] > 0:
            share.append("%s %d%%" % (k, round(res[sk].get(sid, 0) * 100 / w[k]["cost"])))
    share_seg = (GREY + "this session " + " · ".join(share) + RESET) if share and sid else ""
    return line, share_seg


def main():
    if sys.argv[1] == "--refresh":
        try:
            refresh(int(sys.argv[2]), int(sys.argv[3]))
        finally:
            try:
                os.unlink(LOCK)
            except OSError:
                pass
        return
    sid, transcript = sys.argv[1], sys.argv[2]
    s5 = int(sys.argv[3]) if sys.argv[3].isdigit() else NONE_TS
    s7 = int(sys.argv[4]) if sys.argv[4].isdigit() else NONE_TS
    try:
        tot = session_totals(sid, transcript)
    except Exception:
        tot = [0] * 10
    res = None
    try:
        res = api_result(s5, s7)
        line, share = api_lines(sid, res)
    except Exception:
        line, share = "", ""
    sys.stdout.write(" ".join(map(str, tot)) + "\n" + line + "\n" + share + "\n")


if __name__ == "__main__":
    main()
