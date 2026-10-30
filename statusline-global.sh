#!/usr/bin/env bash
# Account-wide Claude Code usage panel: 5h / 7d limit gauges and the API≈ line,
# shown once (e.g. in a Herdr pane) instead of in every session's status line.
#
#   statusline-global.sh [interval_secs]    # default 30; Ctrl-C to quit
#
# Limits come from ~/.cache/cc-statusline/limits.json, written by
# statusline-command.sh on every render of any session; the API≈ line comes
# from statusline-scan.py's shared all-sessions scan.

interval="${1:-30}"
LIM="$HOME/.cache/cc-statusline/limits.json"
SCAN="$HOME/.claude/statusline-scan.py"

ESC=$'\033'
C_RESET="${ESC}[0m"; C_BOLD="${ESC}[1m"
C_RED="${ESC}[31m"; C_GREEN="${ESC}[32m"; C_YELLOW="${ESC}[33m"; C_GREY="${ESC}[90m"
SEP="${C_GREY}│${C_RESET}"

format_dur() {
  local s="$1"
  [[ "$s" =~ ^-?[0-9]+$ ]] || { echo ""; return; }
  [ "$s" -lt 0 ] && s=0
  if   [ "$s" -ge 86400 ]; then echo "$(( s / 86400 ))d$(( (s % 86400) / 3600 ))h"
  elif [ "$s" -ge 3600 ];  then echo "$(( s / 3600 ))h$(( (s % 3600) / 60 ))m"
  elif [ "$s" -ge 60 ];    then echo "$(( s / 60 ))m"
  else echo "${s}s"; fi
}

color_for_pct() {
  if   [ "$1" -ge 90 ]; then printf '%s' "$C_RED"
  elif [ "$1" -ge 70 ]; then printf '%s' "$C_YELLOW"
  else printf '%s' "$C_GREEN"; fi
}

# Same pace rule as the status line: "pace class eta" (class 0 safe, 1 close, 2 hot).
pace_info() {
  awk -v pct="$1" -v reset="$2" -v len="$3" -v now="$now" 'BEGIN{
    rem = reset - now; if (rem < 0) rem = 0; if (rem > len) rem = len;
    el = len - rem; if (el < 60) el = 60;
    pace = (pct / 100) / (el / len); if (pace > 99) pace = 99;
    cls = (pace >= 1.5 - 1e-9 ? 2 : (pace >= 1.0 - 1e-9 ? 1 : 0));
    if (el < 300) cls = 0;
    eta = (cls > 0 && pct > 0 ? el * (100 - pct) / pct : 0);
    printf "%.1f %d %.0f", pace, cls, eta }'
}

# gauge <label> <pct> <reset_epoch> <window_secs> <reset date format>
gauge() {
  local label="$1" pct="$2" reset="$3" len="$4" fmt="$5" p bar="" i filled clk out
  [[ "$pct" =~ ^[0-9.]+$ ]] || return
  if [[ "$reset" =~ ^[0-9]+$ ]] && [ "$reset" -le "$now" ]; then
    printf '%s' "${C_GREY}${label} window reset · waiting for a session to report${C_RESET}"; return
  fi
  p=$(printf '%.0f' "$pct"); filled=$(( p / 10 )); [ "$filled" -gt 10 ] && filled=10
  for ((i=0;i<filled;i++)); do bar="${bar}▓"; done
  bar="${bar}${C_GREY}"; for ((i=filled;i<10;i++)); do bar="${bar}░"; done
  out="$(color_for_pct "$p")${label} ${p}% ${bar}${C_RESET}"
  if [[ "$reset" =~ ^[0-9]+$ ]]; then
    clk=$(date -r "$reset" "+$fmt" 2>/dev/null || date -d "@$reset" "+$fmt" 2>/dev/null)
    out="${out} ${C_GREY}↻ ${clk} ($(format_dur $(( reset - now ))))${C_RESET}"
    read -r pace cls eta <<< "$(pace_info "$pct" "$reset" "$len")"
    case "$cls" in
      2) out="${out} ${C_RED}▲${pace}× caps ~$(format_dur "$eta")${C_RESET}" ;;
      1) out="${out} ${C_YELLOW}▲${pace}× caps ~$(format_dur "$eta")${C_RESET}" ;;
    esac
  fi
  printf '%s' "$out"
}

render() {
  now=$(date +%s)
  local at="" fp="" fr="" sp="" sr="" s5=x s7=x g5 g7 api age
  if [ -f "$LIM" ]; then
    IFS=$'\037' read -r at fp fr sp sr <<< "$(jq -r '[.at,.five_pct,.five_reset,.seven_pct,.seven_reset] | map(tostring) | join("\u001f")' "$LIM" 2>/dev/null)"
  fi
  [[ "$fr" =~ ^[0-9]+$ ]] && s5=$(( fr - 18000 ))
  [[ "$sr" =~ ^[0-9]+$ ]] && s7=$(( sr - 604800 ))
  # Line 2 of the scanner's output is the API≈ line; it also keeps the shared scan fresh.
  api=$(python3 "$SCAN" "" "" "$s5" "$s7" 2>/dev/null | sed -n 2p)

  age="no session has reported limits yet"
  [[ "$at" =~ ^[0-9]+$ ]] && age="limits from $(format_dur $(( now - at ))) ago"
  g5=$(gauge 5h "$fp" "$fr" 18000 '%H:%M')
  g7=$(gauge 7d "$sp" "$sr" 604800 '%b %-d')

  printf '%s' "${ESC}[H${ESC}[2J"
  printf '%s\n' "${C_BOLD}Claude usage · all sessions${C_RESET}  ${C_GREY}$(date '+%H:%M:%S') · ${age}${C_RESET}"
  [ -n "$g5" ] && printf '%s\n' "$g5"
  [ -n "$g7" ] && printf '%s\n' "$g7"
  [ -n "$api" ] && printf '%s\n' "$api"
}

printf '%s' "${ESC}[?25l"; trap 'printf "%s" "${ESC}[?25h"; exit' INT TERM
while :; do render; sleep "$interval"; done
