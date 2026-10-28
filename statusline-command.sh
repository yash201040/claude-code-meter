#!/usr/bin/env bash
# Claude Code status line: bash + jq + git, with transcript scans in statusline-scan.py (python3).
#
# Line 1: cwd, git branch (* dirty, +staged ~unstaged ?untracked ↑ahead ↓behind ✗conflicts), PR badge
# Line 2: [model · window] ⚡effort🧠  context tokens + bar + %  session name  [permission mode]
# Line 3: lines +a/-r │ in · out · main/agents/wf share │ age · ran (model time) │ cost 🔥$/hr of model time
#         (burn colour: green under $25/hr, yellow $25–50/hr, red from $50/hr)
# Line 4 (only when a window will cap before it resets): 5h/7d ▲pace× caps ~eta │ this session's share
#
# All values come from the JSON Claude Code pipes in on stdin (see
# https://code.claude.com/docs/en/statusline), plus transcript totals from
# statusline-scan.py, which keeps its incremental cache in ~/.cache/cc-statusline/.
# Env overrides: CC_BILLING_DAY (billing-cycle start day, default 1),
# CC_PLAN_PRICE (monthly plan price; default read from ~/.claude.json).

input=$(cat)

# ----------------------------------------------------------------------------
# Colors (ANSI-C quoting → literal escape bytes, so printf '%s' renders them)
# ----------------------------------------------------------------------------
ESC=$'\033'; BEL=$'\007'
C_RESET="${ESC}[0m"; C_BOLD="${ESC}[1m"
C_RED="${ESC}[31m"; C_GREEN="${ESC}[32m"; C_YELLOW="${ESC}[33m"
C_BLUE="${ESC}[34m"; C_MAGENTA="${ESC}[35m"; C_CYAN="${ESC}[36m"; C_GREY="${ESC}[90m"
SEP="${C_GREY}│${C_RESET}"

now=$(date +%s)

# ----------------------------------------------------------------------------
# Pull every field we need in ONE jq pass. Join with the Unit-Separator byte
# (\x1f) — a NON-whitespace delimiter, so `read` preserves empty fields instead
# of collapsing consecutive tabs (which would misalign the columns).
# ----------------------------------------------------------------------------
vals=$(echo "$input" | jq -r '[
  .model.display_name,
  .cwd,
  .session_name,
  .session_id,
  .context_window.used_percentage,
  .context_window.context_window_size,
  (.context_window.total_input_tokens // .context_window.current_usage.input_tokens),
  .effort.level,
  .thinking.enabled,
  .cost.total_cost_usd,
  .cost.total_duration_ms,
  .cost.total_api_duration_ms,
  .cost.total_lines_added,
  .cost.total_lines_removed,
  .rate_limits.five_hour.used_percentage,
  .rate_limits.five_hour.resets_at,
  .rate_limits.seven_day.used_percentage,
  .rate_limits.seven_day.resets_at,
  .pr.number,
  .pr.review_state,
  .pr.url,
  (.permission_mode // .approval_mode // .mode),
  .transcript_path
] | map(if . == null then "" else . end | tostring) | join("")')

IFS=$'\037' read -r model raw_cwd session_name session_id used window_size used_tokens \
  effort thinking cost dur_ms api_ms added removed \
  five_pct five_reset seven_pct seven_reset pr_num pr_state pr_url perm_raw transcript_path <<< "$vals"

short_cwd="${raw_cwd/#$HOME/~}"

# Publish the account-wide 5h/7d limits for statusline-global.sh (the shared
# usage panel): whichever session rendered last has the freshest numbers.
if [[ "$five_pct" =~ ^[0-9.]+$ ]] || [[ "$seven_pct" =~ ^[0-9.]+$ ]]; then
  mkdir -p "$HOME/.cache/cc-statusline"
  lim="$HOME/.cache/cc-statusline/limits.json"
  printf '{"at":%s,"five_pct":"%s","five_reset":"%s","seven_pct":"%s","seven_reset":"%s"}\n' \
    "$now" "$five_pct" "$five_reset" "$seven_pct" "$seven_reset" > "$lim.$$" && mv -f "$lim.$$" "$lim"
fi
added=${added:-0}; removed=${removed:-0}
[[ "$added" =~ ^[0-9]+$ ]] || added=0
[[ "$removed" =~ ^[0-9]+$ ]] || removed=0

# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
# Format a raw token count as 125K / 1.2M (bash arithmetic; macOS awk-safe).
format_tokens() {
  local n="$1"
  [[ "$n" =~ ^[0-9]+$ ]] || { echo ""; return; }
  if   [ "$n" -ge 1000000000 ]; then echo "$(( n / 1000000000 )).$(( (n % 1000000000) / 100000000 ))B"
  elif [ "$n" -ge 1000000 ];    then echo "$(( n / 1000000 )).$(( (n % 1000000) / 100000 ))M"
  elif [ "$n" -ge 1000 ];       then echo "$(( n / 1000 ))K"
  else echo "${n}"; fi
}

# Format a duration in seconds as 2h14m / 18m / 45s.
format_dur() {
  local s="$1"
  [[ "$s" =~ ^-?[0-9]+$ ]] || { echo ""; return; }
  [ "$s" -lt 0 ] && s=0
  if   [ "$s" -ge 86400 ]; then echo "$(( s / 86400 ))d$(( (s % 86400) / 3600 ))h"
  elif [ "$s" -ge 3600 ];  then echo "$(( s / 3600 ))h$(( (s % 3600) / 60 ))m"
  elif [ "$s" -ge 60 ];    then echo "$(( s / 60 ))m"
  else echo "${s}s"; fi
}

# Green < 70%, yellow 70–89%, red >= 90% (for rate-limit gauges).
color_for_pct() {
  local p="$1"
  if   [ "$p" -ge 90 ]; then printf '%s' "$C_RED"
  elif [ "$p" -ge 70 ]; then printf '%s' "$C_YELLOW"
  else printf '%s' "$C_GREEN"; fi
}

# Rate-limit burn pace: used% relative to elapsed% of the window. >1 means
# you'll hit the cap before it resets. Args: pct reset_epoch window_secs.
# Echoes "pace class eta": class 0=safe(<1.0) 1=close(1.0-1.5) 2=hot(>=1.5);
# eta = seconds until 100% at the current burn rate (0 when safe).
pace_info() {
  awk -v pct="$1" -v reset="$2" -v len="$3" -v now="$now" 'BEGIN{
    rem = reset - now; if (rem < 0) rem = 0; if (rem > len) rem = len;
    el = len - rem; if (el < 60) el = 60;
    pace = (pct / 100) / (el / len); if (pace > 99) pace = 99;
    # epsilon absorbs FP error at the thresholds (0.3/0.2 = 1.4999999999999998
    # must classify as 1.5x red, not yellow)
    cls = (pace >= 1.5 - 1e-9 ? 2 : (pace >= 1.0 - 1e-9 ? 1 : 0));
    # under 5 minutes into a window the pace is mostly noise - no warning yet
    if (el < 300) cls = 0;
    eta = (cls > 0 && pct > 0 ? el * (100 - pct) / pct : 0);
    printf "%.1f %d %.0f", pace, cls, eta }'
}

# ----------------------------------------------------------------------------
# Context: used tokens, integer %, color, and a 10-cell fill bar
# ----------------------------------------------------------------------------
pct=""
if [[ "$used" =~ ^[0-9.]+$ ]]; then
  pct=$(printf '%.0f' "$used" 2>/dev/null)
elif [[ "$used_tokens" =~ ^[0-9]+$ ]] && [[ "$window_size" =~ ^[0-9]+$ ]] && [ "$window_size" -gt 0 ]; then
  pct=$(( used_tokens * 100 / window_size ))
fi

# Zoned context gauge: green up to 200K tokens, yellow up to 500K, red beyond.
# Each bar cell takes the color of the zone its range starts in, so 700K/1M
# draws 2 green + 3 yellow + 2 red cells + 3 empty. On windows too small for
# those thresholds to bite, they shrink to 70% / 90% of the window (140K/180K
# on a 200K window) so the gauge still warns before auto-compact.
ctx_green_max=200000; ctx_yellow_max=500000
ctx_color="$C_GREEN"
ws=0; [[ "$window_size" =~ ^[0-9]+$ ]] && ws=$window_size
tok=""
if [[ "$used_tokens" =~ ^[0-9]+$ ]]; then tok=$used_tokens
elif [[ "$pct" =~ ^[0-9]+$ ]] && [ "$ws" -gt 0 ]; then tok=$(( pct * ws / 100 )); fi
if [ "$ws" -gt 0 ]; then
  g=$ctx_green_max;  [ $(( ws * 70 / 100 )) -lt "$g" ] && g=$(( ws * 70 / 100 ))
  y=$ctx_yellow_max; [ $(( ws * 90 / 100 )) -lt "$y" ] && y=$(( ws * 90 / 100 ))
  zone_color() { # token position -> color
    if   [ "$1" -ge "$y" ]; then printf '%s' "$C_RED"
    elif [ "$1" -ge "$g" ]; then printf '%s' "$C_YELLOW"
    else printf '%s' "$C_GREEN"; fi
  }
  [ -n "$tok" ] && ctx_color="$(zone_color "$tok")"
elif [[ "$pct" =~ ^[0-9]+$ ]]; then
  ctx_color="$(color_for_pct "$pct")"
fi

bar=""
if [[ "$pct" =~ ^[0-9]+$ ]]; then
  width=10; filled=$(( pct * width / 100 ))
  [ "$filled" -gt "$width" ] && filled=$width
  [ "$filled" -lt 0 ] && filled=0
  empty=$(( width - filled ))
  for ((i=0;i<filled;i++)); do
    if [ "$ws" -gt 0 ]; then bar="${bar}$(zone_color $(( i * ws / width )))▓"
    else bar="${bar}${ctx_color}▓"; fi
  done
  bar="${bar}${C_GREY}"
  for ((i=0;i<empty;i++));  do bar="${bar}░"; done
  bar="${bar}${ctx_color}"
fi

# Window size for the model label: 1000000 -> 1M, 200000 -> 200K.
win_label=""
if [ "$ws" -gt 0 ]; then
  if   [ $(( ws % 1000000 )) -eq 0 ]; then win_label="$(( ws / 1000000 ))M"
  elif [ $(( ws % 1000 )) -eq 0 ];    then win_label="$(( ws / 1000 ))K"
  else win_label="$(format_tokens "$ws")"; fi
fi

ctx_block=""
if [[ "$used_tokens" =~ ^[0-9]+$ ]]; then
  ctx_block="${ctx_color}$(format_tokens "$used_tokens") ctx ${bar} ${pct:-0}%${C_RESET}"
elif [[ "$pct" =~ ^[0-9]+$ ]]; then
  ctx_block="${ctx_color}ctx ${bar} ${pct}%${C_RESET}"
fi

# ----------------------------------------------------------------------------
# Whole-session token totals, summed from the transcript JSONL. The status-line
# payload only reports CURRENT context usage, so true cumulative input/output
# require parsing transcript_path. Cached by file signature (mtime+size) so we
# only re-parse when the transcript actually grows.
# ----------------------------------------------------------------------------
sess_in=""; sess_out=""
m_in=0; m_out=0; s_in=0; s_out=0; w_in=0; w_out=0
s_add=0; s_rem=0; w_add=0; w_rem=0
api_line=""; sess_share_seg=""
# Window starts (epoch) for the API≈ line; the scanner treats non-numbers as
# "window unavailable".
s5=""; s7=""
[[ "$five_reset"  =~ ^[0-9]+$ ]] && s5=$(( five_reset  - 18000 ))
[[ "$seven_reset" =~ ^[0-9]+$ ]] && s7=$(( seven_reset - 604800 ))
# statusline-scan.py does all transcript work incrementally (byte offsets per
# file, only appended bytes are parsed) and keeps the all-session API≈ scan off
# the render path (shared result, refreshed in the background). Line 1: this
# session's tokens + agent edit lines; line 2: API≈ line; line 3: window share.
{ IFS= read -r scan_tot; IFS= read -r api_line; IFS= read -r sess_share_seg; } < <(
  python3 "$HOME/.claude/statusline-scan.py" "$session_id" "$transcript_path" "${s5:-x}" "${s7:-x}" 2>/dev/null)
read -r m_in m_out s_in s_out w_in w_out s_add s_rem w_add w_rem <<< "$scan_tot"
if [ -f "$transcript_path" ]; then
  sess_in=$(( ${m_in:-0} + ${s_in:-0} + ${w_in:-0} ))
  sess_out=$(( ${m_out:-0} + ${s_out:-0} + ${w_out:-0} ))
fi

# ----------------------------------------------------------------------------
# Session-mix line: how this session's tokens split between the main chat,
# Task subagents, and Workflow agents — share % (of in+out), in/out each, and
# per-origin ±lines edited (shown only for origins that edited files).
# Hidden when the session is 100% main chat (the line would only repeat the
# in/out totals already on the insights line).
# ----------------------------------------------------------------------------
mix_line=""
if [ "$(( ${s_in:-0} + ${s_out:-0} + ${w_in:-0} + ${w_out:-0} ))" -gt 0 ]; then
  mix_tot=$(( sess_in + sess_out ))
  # Rendered on the session-usage line (line 3), right after the in/out totals.
  mix_line=""
  add_mix() { # label in out — share of the session's tokens (in+out) per origin; the totals are already in the in/out segment.
    # No ±lines here: line 3's velocity already reports total edits, and the
    # per-origin split summed to the same number (pure redundancy).
    local share=$(( ($2 + $3) * 100 / mix_tot ))
    [ -n "$mix_line" ] && mix_line="${mix_line} ${C_GREY}·${C_RESET} "
    mix_line="${mix_line}$1 ${C_BOLD}${share}%${C_RESET}"
  }
  add_mix "main" "${m_in:-0}" "${m_out:-0}"
  [ "$(( ${s_in:-0} + ${s_out:-0} ))" -gt 0 ] && add_mix "agents" "$s_in" "$s_out"
  [ "$(( ${w_in:-0} + ${w_out:-0} ))" -gt 0 ] && add_mix "wf" "$w_in" "$w_out"
fi

# ----------------------------------------------------------------------------
# Effort + thinking indicator
# ----------------------------------------------------------------------------
eff_part=""
[ -n "$effort" ] && eff_part="⚡${effort}"
[ "$thinking" = "true" ] && eff_part="${eff_part}🧠"

# ----------------------------------------------------------------------------
# Session identity
# ----------------------------------------------------------------------------
if [ -n "$session_name" ]; then session_label="$session_name"
elif [ -n "$session_id" ]; then session_label="${session_id:0:8}"
else session_label=""; fi

# ----------------------------------------------------------------------------
# Permission mode
# ----------------------------------------------------------------------------
case "$perm_raw" in
  plan)     perm_label="Plan" ;;
  acceptEdits|autoEdit) perm_label="Auto-edit" ;;
  bypassPermissions|fullAuto) perm_label="Full-auto" ;;
  auto)     perm_label="Auto" ;;
  default|"") perm_label="" ;;
  *)        perm_label="$perm_raw" ;;
esac
perm_part=""
[ -n "$perm_label" ] && perm_part="  ${C_MAGENTA}[${perm_label}]${C_RESET}"

# ----------------------------------------------------------------------------
# Rich git status
# ----------------------------------------------------------------------------
git_part=""
# One `git status` call (porcelain v2 + branch header) instead of eight.
if gs=$(git -C "$raw_cwd" --no-optional-locks status --porcelain=v2 --branch --untracked-files=all 2>/dev/null); then
  branch="detached"; oid=""; staged=0; unstaged=0; untracked=0; conflicts=0; ahead=0; behind=0
  while IFS= read -r l; do
    case "$l" in
      "# branch.head "*) branch=${l#\# branch.head } ;;
      "# branch.oid "*)  oid=${l#\# branch.oid } ;;
      "# branch.ab "*)   ab=${l#\# branch.ab +}; ahead=${ab%% *}; behind=${ab##*-} ;;
      "1 "*|"2 "*)       [ "${l:2:1}" != "." ] && staged=$(( staged + 1 ))
                         [ "${l:3:1}" != "." ] && unstaged=$(( unstaged + 1 )) ;;
      "u "*)             conflicts=$(( conflicts + 1 )); unstaged=$(( unstaged + 1 )) ;;
      "? "*)             untracked=$(( untracked + 1 )) ;;
    esac
  done <<< "$gs"
  [ "$branch" = "(detached)" ] && branch=${oid:0:7}
  dirty=""
  [ $(( staged + unstaged )) -gt 0 ] && dirty="*"

  git_part="  ${C_BLUE}${branch}${C_RESET}${C_YELLOW}${dirty}${C_RESET}"
  [ "$staged"    -gt 0 ] 2>/dev/null && git_part="${git_part} ${C_GREEN}+${staged}${C_RESET}"
  [ "$unstaged"  -gt 0 ] 2>/dev/null && git_part="${git_part} ${C_YELLOW}~${unstaged}${C_RESET}"
  [ "$untracked" -gt 0 ] 2>/dev/null && git_part="${git_part} ${C_GREY}?${untracked}${C_RESET}"
  [ "$ahead"     -gt 0 ] 2>/dev/null && git_part="${git_part} ${C_CYAN}↑${ahead}${C_RESET}"
  [ "$behind"    -gt 0 ] 2>/dev/null && git_part="${git_part} ${C_CYAN}↓${behind}${C_RESET}"
  [ "$conflicts" -gt 0 ] 2>/dev/null && git_part="${git_part} ${C_RED}✗${conflicts}${C_RESET}"
fi

# ----------------------------------------------------------------------------
# PR badge (clickable via OSC 8)
# ----------------------------------------------------------------------------
pr_part=""
if [ -n "$pr_num" ]; then
  case "$pr_state" in
    approved)          pr_icon="✓"; pr_col="$C_GREEN" ;;
    changes_requested) pr_icon="✗"; pr_col="$C_RED" ;;
    pending)           pr_icon="⌛"; pr_col="$C_YELLOW" ;;
    draft)             pr_icon="✎"; pr_col="$C_GREY" ;;
    *)                 pr_icon="";  pr_col="$C_CYAN" ;;
  esac
  pr_text="PR#${pr_num}${pr_icon:+ $pr_icon}"
  [ -n "$pr_url" ] && pr_text="${ESC}]8;;${pr_url}${BEL}${pr_text}${ESC}]8;;${BEL}"
  pr_part="  ${pr_col}${pr_text}${C_RESET}"
fi

# ----------------------------------------------------------------------------
# Line 2 segments: tokens, cost+burn, code velocity, timers+api, block, limits
# ----------------------------------------------------------------------------
# Whole-session input / output tokens (summed from the transcript)
tok_seg=""
if [[ "$sess_in" =~ ^[0-9]+$ ]] || [[ "$sess_out" =~ ^[0-9]+$ ]]; then
  tok_seg="${C_GREY}in${C_RESET} $(format_tokens "${sess_in:-0}") ${C_GREY}· out${C_RESET} $(format_tokens "${sess_out:-0}")"
fi

# Cost + burn rate ($/hr of model time, so idle time does not dilute it)
cost_seg=""
if [[ "$cost" =~ ^[0-9.]+$ ]]; then
  cost_seg="${C_GREY}api${C_RESET} ${C_BOLD}$(printf '$%.2f' "$cost")${C_RESET}"
  if [[ "$api_ms" =~ ^[0-9]+$ ]] && [ "$api_ms" -gt 0 ]; then
    burn=$(awk -v c="$cost" -v d="$api_ms" 'BEGIN{ if (d>0) printf "%.2f", c*3600000/d }')
    if [ -n "$burn" ]; then
      # Fixed bands: green under $25/hr, yellow $25–50/hr, red from $50/hr.
      bcls=$(awk -v b="$burn" 'BEGIN{print (b>=50 ? 2 : (b>=25 ? 1 : 0))}')
      case "$bcls" in 2) bcol="$C_RED" ;; 1) bcol="$C_YELLOW" ;; *) bcol="$C_GREEN" ;; esac
      cost_seg="${cost_seg} ${bcol}🔥\$${burn}/hr${C_RESET}"
    fi
  fi
fi

# Code velocity (lines ±) this session: main loop (payload counter) plus
# subagent + workflow edits (derived from their transcripts above).
vel_seg=""
tot_add=$(( ${added:-0} + ${s_add:-0} + ${w_add:-0} ))
tot_rem=$(( ${removed:-0} + ${s_rem:-0} + ${w_rem:-0} ))
if [ "$tot_add" -gt 0 ] 2>/dev/null || [ "$tot_rem" -gt 0 ] 2>/dev/null; then
  vel_seg="${C_GREY}lines${C_RESET} ${C_GREEN}+${tot_add}${C_RESET}/${C_RED}-${tot_rem}${C_RESET}"
fi

# Session age (wall clock since start, idle time included) + model time
# (total_api_duration_ms: time spent waiting on model responses, i.e. the
# model thinking and writing; tool runs and idle time are not in it).
time_seg=""
if [[ "$dur_ms" =~ ^[0-9]+$ ]] && [ "$dur_ms" -gt 0 ]; then
  time_seg="${C_GREY}age${C_RESET} $(format_dur $(( dur_ms / 1000 )))"
  if [[ "$api_ms" =~ ^[0-9]+$ ]]; then
    time_seg="${time_seg} ${C_GREY}· ran${C_RESET} $(format_dur $(( api_ms / 1000 )))"
  fi
fi

# Rate-limit pace: a ▲ warning per window that will cap before it resets at
# the current burn rate (pace > 1), collected into caps_line below.
warn_for() { # label pct reset_epoch window_secs
  [[ "$2" =~ ^[0-9.]+$ ]] && [[ "$3" =~ ^[0-9]+$ ]] || return 0
  local pace cls eta
  read -r pace cls eta <<< "$(pace_info "$2" "$3" "$4")"
  case "$cls" in
    2) printf '%s' "${C_RED}$1 ▲${pace}× caps ~$(format_dur "$eta")${C_RESET}" ;;
    1) printf '%s' "${C_YELLOW}$1 ▲${pace}× caps ~$(format_dur "$eta")${C_RESET}" ;;
  esac
}
block_warn=$(warn_for 5h "$five_pct" "$five_reset" 18000)
limit_warn=$(warn_for 7d "$seven_pct" "$seven_reset" 604800)

# Cap-warning line: only present when at least one window will cap before
# it resets at the current burn rate.
caps_line=""
for w in "$block_warn" "$limit_warn"; do
  [ -n "$w" ] || continue
  [ -n "$caps_line" ] && caps_line="${caps_line} ${SEP} "
  caps_line="${caps_line}${w}"
done

# ----------------------------------------------------------------------------
# statusline-scan.py also prices usage across ALL sessions for the 5h, 7d and
# billing-month windows (api_line). The status line doesn't print it; the
# SwiftBar menu and statusline-global.sh read the same cached result.
# ----------------------------------------------------------------------------

# Append this session's window share to the cap-warning line (only visible
# when a window is on pace to cap before it resets).
if [ -n "$caps_line" ] && [ -n "$sess_share_seg" ]; then
  caps_line="${caps_line} ${SEP} ${sess_share_seg}"
fi

# ----------------------------------------------------------------------------
# Assemble
# ----------------------------------------------------------------------------
# Line 1: the folder you're in, with its git branch + PR badge
line1="${short_cwd}${git_part}${pr_part}"

# Line 2: model, effort/thinking, context, session, mode
# Drop a "(1M context)" suffix from the display name: the size is shown after it.
model="${model% (*context)}"
line_model="[${C_CYAN}${model}${C_RESET}${win_label:+ ${C_GREY}·${C_RESET} ${C_CYAN}${win_label}${C_RESET}}]"
[ -n "$eff_part" ]      && line_model="${line_model} ${C_GREY}${eff_part}${C_RESET}"
[ -n "$ctx_block" ]     && line_model="${line_model}  ${ctx_block}"
[ -n "$session_label" ] && line_model="${line_model}  ${C_GREY}${session_label}${C_RESET}"
line_model="${line_model}${perm_part}"

line2=""
add_seg() { [ -n "$1" ] || return; [ -n "$line2" ] && line2="${line2} ${SEP} "; line2="${line2}$1"; }
add_seg "$vel_seg"
# The origin split is a share of the same token totals, so it joins that segment.
[ -n "$mix_line" ] && tok_seg="${tok_seg}${tok_seg:+ ${C_GREY}·${C_RESET} }${mix_line}"
add_seg "$tok_seg"
add_seg "$time_seg"
add_seg "$cost_seg"

printf '%s\n' "$line1"
printf '%s\n' "$line_model"
[ -n "$line2" ] && printf '%s\n' "$line2"
[ -n "$caps_line" ] && printf '%s\n' "$caps_line"

# ----------------------------------------------------------------------------
# Set the terminal window/tab title to the session's working directory.
# The status line's stdout is captured by Claude Code, so we write the OSC
# title sequence straight to the controlling terminal (/dev/tty) instead.
# OSC 0 sets both the window and tab/icon title. Guarded so it's a no-op when
# there's no writable tty (e.g. headless/background runs).
# ----------------------------------------------------------------------------
if [ -n "$raw_cwd" ]; then
  { printf '\033]0;%s\007' "$raw_cwd" > /dev/tty; } 2>/dev/null || true
fi
