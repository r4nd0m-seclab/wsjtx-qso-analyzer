#!/usr/bin/env bash
# =============================================================================
# manage_patterns.sh — store & swap antenna pattern variations
#
# The QSO Analyzer scores each decode against a per-band MMANA-GAL far-field
# gain table (10 CSV files, one per band). Because the physical antenna is
# changed frequently, this script keeps several named variations on disk and
# swaps any one of them into the live `patterns/` directory the app reads.
#
# Layout (all relative to this script's directory):
#   patterns-library/<set-name>/*.csv   named variations (source of truth)
#   patterns-library/active             one-line pointer to the deployed set
#   patterns/                           deployed working copy (mounted by app)
#
# A "set" is 10 CSV files named by nominal band frequency, e.g.
#   1.840.csv 3.573.csv 5.357.csv 7.074.csv 10.136.csv
#   14.074.csv 18.100.csv 21.074.csv 24.915.csv 28.074.csv
#
# Usage:
#   ./manage_patterns.sh list
#   ./manage_patterns.sh current
#   ./manage_patterns.sh save  <set-name> [src-dir]
#   ./manage_patterns.sh activate <set-name> [--no-restart]
#   ./manage_patterns.sh rm     <set-name>
#
#   save    Copy a set of band CSVs into the library under <set-name> and make
#           it active. [src-dir] defaults to ./patterns. Pass a directory that
#           already holds the 10 band CSVs (e.g. freshly renamed MMANA exports
#           or a prior library set).
#   activate Point patterns/ at <set-name>, update the active pointer, and
#           restart the running app container so it reloads the patterns.
#           Use --no-restart to only swap files without restarting.
# =============================================================================

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$DIR/patterns-library"
ACTIVE_PTR="$LIB/active"
LIVE="$DIR/patterns"
CONTAINER="wsjtx-qso-analyzer"

# The 10 nominal band frequencies we expect, in MHz (3-decimal naming).
BANDS=(1.840 3.573 5.357 7.074 10.136 14.074 18.100 21.074 24.915 28.074)

die() { echo "ERROR: $*" >&2; exit 1; }
have_container() { podman ps --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER"; }
restart_app() {
  if have_container; then
    echo ">> Restarting $CONTAINER to reload patterns..."
    podman restart "$CONTAINER" >/dev/null
    echo ">> $CONTAINER restarted."
  else
    echo ">> (no running $CONTAINER found; start it with ./run.sh when ready)"
  fi
}

# Validate a directory holds the expected band CSVs; prints warnings.
check_bands() {
  local dir="$1" missing=()
  for f in "${BANDS[@]}"; do
    [ -f "$dir/$f.csv" ] || missing+=("$f.csv")
  done
  if [ "${#missing[@]}" -gt 0 ]; then
    echo "  warning: missing expected band files: ${missing[*]}" >&2
  fi
  local n; n=$(find "$dir" -maxdepth 1 -name '*.csv' 2>/dev/null | wc -l)
  echo "  $n CSV file(s) present"
}

active_name() {
  if [ -f "$ACTIVE_PTR" ]; then
    tr -d '[:space:]' < "$ACTIVE_PTR"
  else
    echo ""
  fi
}

cmd="${1:-help}"; shift || true
case "$cmd" in
  list)
    [ -d "$LIB" ] || die "library $LIB not found"
    act="$(active_name)"
    echo "Available pattern sets (active: ${act:-<none>})"
    echo "------------------------------------------------------"
    for d in "$LIB"/*/; do
      [ -d "$d" ] || continue
      name="$(basename "$d")"
      mark=" "
      [ "$name" = "$act" ] && mark="*"
      n=$(find "$d" -maxdepth 1 -name '*.csv' 2>/dev/null | wc -l | tr -d ' ')
      printf ' %s  %-32s %s band(s)\n' "$mark" "$name" "$n"
    done
    echo "------------------------------------------------------"
    echo "* = currently deployed to patterns/"
    ;;

  current)
    act="$(active_name)"
    if [ -n "$act" ]; then
      echo "Active set: $act"
      [ -d "$LIB/$act" ] && check_bands "$LIB/$act"
    else
      echo "No active set recorded."
    fi
    ;;

  save)
    name="${1:?usage: save <set-name> [src-dir]}"
    src="${2:-$LIVE}"
    [ -d "$src" ] || die "source dir not found: $src"
    case "$name" in
      *[!A-Za-z0-9._-]*) die "set name may contain only letters, numbers, . _ -";;
    esac
    mkdir -p "$LIB/$name"
    cp -f "$src"/*.csv "$LIB/$name"/ 2>/dev/null || die "no CSV files to copy from $src"
    echo "Saved set '$name' from $src:"
    check_bands "$LIB/$name"
    printf '%s\n' "$name" > "$ACTIVE_PTR"
    echo "Active pointer set to: $name"
    echo "Run: ./manage_patterns.sh activate $name   (to deploy + restart)"
    ;;

  activate)
    name="${1:?usage: activate <set-name> [--no-restart]}"
    [ -n "${1:-}" ] || die "no set name given"
    [ -d "$LIB/$name" ] || die "set not found: $name (see: ./manage_patterns.sh list)"
    shift
    do_restart=1
    for a in "$@"; do
      case "$a" in --no-restart) do_restart=0;; *) die "unknown flag: $a";; esac
    done
    # Refresh the live dir: copy the set in and drop any stale band files.
    mkdir -p "$LIVE"
    find "$LIVE" -maxdepth 1 -name '*.csv' -delete
    cp -f "$LIB/$name"/*.csv "$LIVE"/
    printf '%s\n' "$name" > "$ACTIVE_PTR"
    echo "Deployed set '$name' to patterns/:"
    check_bands "$LIVE"
    if [ "$do_restart" -eq 1 ]; then
      restart_app
    else
      echo ">> (--no-restart given; app not restarted — restart to apply)"
    fi
    ;;

  rm)
    name="${1:?usage: rm <set-name>}"
    act="$(active_name)"
    [ "$name" = "$act" ] && die "cannot remove the active set ('$name'); activate another first"
    [ -d "$LIB/$name" ] || die "set not found: $name"
    rm -rf "$LIB/$name"
    echo "Removed set '$name'."
    ;;

  help|*)
    sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
    ;;
esac
