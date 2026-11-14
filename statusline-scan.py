#!/usr/bin/env python3
"""Transcript scanner for statusline-command.sh: the slow parts of the status
line, done incrementally so a render costs about what was written since the
last one.

  statusline-scan.py <session_id> <transcript_path> <s5> <s7>

prints four lines:
  1. m_in m_out s_in s_out w_in w_out s_add s_rem w_add w_rem   (this session)
  2. the API-equivalent cost line (ANSI-colored, empty when nothing to show)
  3. this session's share of the 5h/7d windows (ANSI-colored, may be empty)
  4. the account burn rate: API-equivalent $ of every session in the last hour,
     per hour of model time across them (a plain number, may be empty)

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
# A refresh holding the lock longer than this is taken to have died.
LOCK_STALE = 300
# The burn rate is not shown from a result older than this: after a long idle it
# would describe hours ago, until the background refresh lands.
ACC_MAX_AGE = 600
# Bumped when the per-entry layout in files.json changes: the cache is rebuilt once.
CACHE_VERSION = 2
# A reply that came this long after its request is counted as this long: past it,
# the gap is more likely a stall or a queued prompt than the model working.
MAX_CALL_SECS = 600
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


def extract_usage(path, rec):
    """Usage entries appended since rec["off"]. Each entry also carries when its
    request went out: the time of the user prompt or tool result written just
    before the reply (kept in rec["q"] across incremental reads)."""
    out = []
    lines, rec["off"] = read_new(path, rec["off"])
    for line in lines:
        is_user = b'"type":"user"' in line
        if not is_user and b'"usage"' not in line:
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if not isinstance(e, dict):
            continue
        if is_user and e.get("type") == "user":
            rec["q"] = parse_ts(e.get("timestamp") or "")
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
            u.get("output_tokens") or 0, rec.get("q") or 0,
        ])
    return out


def cost(en):
    pi, po = PRICES[en[3]]
    i, cw, e1, r, o = en[4], en[5], en[6], en[7], en[8]
    return (i * pi + (cw - e1) * pi * 1.25 + e1 * pi * 2 + r * pi * 0.1 + o * po) / 1e6


def take_lock():
    """True when this process now holds the scan lock (created atomically, so two
    renders at the same moment cannot both start a refresh)."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(LOCK, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            try:
                if time.time() - os.stat(LOCK).st_mtime < LOCK_STALE:
                    return False
                os.unlink(LOCK)  # left by a refresh that died
            except OSError:
                pass
            continue
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    return False


def drop_lock():
    try:
        with open(LOCK) as f:
            if f.read().strip() != str(os.getpid()):
                return  # not ours: a stale-lock takeover happened meanwhile
        os.unlink(LOCK)
    except OSError:
        pass


def lock_held():
    try:
        return time.time() - os.stat(LOCK).st_mtime < LOCK_STALE
    except OSError:
        return False


def refresh(s5, s7):
    sm = billing_start()
    # Which files stay cached is fixed, not set by the caller's windows: callers
    # with different windows would otherwise evict and re-read each other's files.
    earliest = min(sm, int(time.time()) - 604800 - 18000)
    cache = load(FILES_CACHE, {})
    changed = cache.get("_v") != CACHE_VERSION
    if changed:
        cache = {}  # older layout without request times: rescan once
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
            rec["e"].extend(extract_usage(path, rec))
            changed = True
        cache[path] = rec
    for path in list(cache):
        if path not in seen and path != "_v":
            del cache[path]
            changed = True
    cache.pop("_v", None)
    if changed:  # several MB: not rewritten when no transcript grew
        cache["_v"] = CACHE_VERSION
        save(FILES_CACHE, cache)
        del cache["_v"]

    # "1h" is a rolling last hour across every session. Its spend divided by the
    # model time in it is the account's burn rate, so idle time does not dilute
    # it. Model time is the union of every call's request-to-reply span, so
    # sessions and subagents running at once count their shared time once.
    s1 = int(time.time()) - 3600
    w = {k: {"in": 0, "out": 0, "cost": 0.0, "cr": 0} for k in ("1h", "5h", "7d", "mo")}
    tiers, sess5, sess7, spans = {}, {}, {}, []
    for en in dedup([en for rec in cache.values() for en in rec["e"]], 8):
        t, c = en[1], cost(en)
        q = en[9] if len(en) > 9 else 0
        if t >= s1 and 0 < q < t:
            spans.append((max(q, t - MAX_CALL_SECS, s1), t))
        for k, start in (("1h", s1), ("5h", s5), ("7d", s7), ("mo", sm)):
            if t >= start:
                w[k]["in"] += en[4] + en[5] + en[7]; w[k]["out"] += en[8]
                w[k]["cost"] += c; w[k]["cr"] += en[7]
        if t >= sm:
            tiers[en[3]] = tiers.get(en[3], 0) + c
        if en[2]:
            if t >= s5: sess5[en[2]] = sess5.get(en[2], 0) + c
            if t >= s7: sess7[en[2]] = sess7.get(en[2], 0) + c
    active, end = 0.0, 0.0
    for a, b in sorted(spans):
        if b <= end:
            continue
        active += b - max(a, end)
        end = b
    w["1h"]["active"] = active
    price, name = plan()
    save(RESULT, {"at": time.time(), "s5": s5, "s7": s7, "sm": sm, "w": w, "tiers": tiers,
                  "sess5": sess5, "sess7": sess7, "price": price, "plan": name})


def api_result(s5, s7):
    res = load(RESULT, None)
    if not (isinstance(res, dict) and isinstance(res.get("w"), dict) and "at" in res):
        res = None  # missing or damaged: rebuilt below
    fresh = (res is not None and time.time() - res["at"] < SCAN_TTL
             and res.get("s5") == s5 and res.get("s7") == s7 and res.get("sm") == billing_start())
    if fresh:
        return res
    if res is None:
        # First ever scan: inline, by one session only; the others show nothing yet.
        if not take_lock():
            return None
        try:
            refresh(s5, s7)
        finally:
            drop_lock()
        return load(RESULT, None)
    if not lock_held():  # the child takes the lock itself; a loser exits at once
        os.makedirs(CACHE_DIR, exist_ok=True)
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
        if not take_lock():
            return  # another refresh is running
        try:
            refresh(int(sys.argv[2]), int(sys.argv[3]))
        finally:
            drop_lock()
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
    acc = ""
    try:
        h = res["w"]["1h"]
        # Under a minute of model time the rate is mostly noise; show nothing yet.
        if h["cost"] > 0 and h.get("active", 0) >= 60 and time.time() - res["at"] < ACC_MAX_AGE:
            acc = "%.2f" % (h["cost"] * 3600 / h["active"])
    except Exception:
        pass  # a result saved before the 1h window existed: shown after the next refresh
    sys.stdout.write(" ".join(map(str, tot)) + "\n" + line + "\n" + share + "\n" + acc + "\n")


if __name__ == "__main__":
    main()
