#!/bin/bash
# Snapshot an Amaranth build's bitstream into the per-board/per-PHY tree.
#
# The OLD version of this script pointed at gateware/build/top.bit, which this
# repo has never produced (builds land in gateware/build_<name>/<name>.bit).
# It therefore always failed, which is why 44 of the 52 bitstreams here have no
# .info and no record of how they were made. Do not reintroduce a fixed path.
#
# Layout:
#   bitstreams/<board>/<phy>/<stamp>_<sha>[_label].bit   designs (USB/audio)
#   bitstreams/instruments/<board>/<phy>/...             diagnostics, reusable
#   bitstreams/known-good/<board>-<phy>[-<label>].bit    verified, symlink
#   bitstreams/_legacy/                                  pre-convention
#
# Instruments are kept on purpose: the freqtest / bittest / rawmon bitstreams
# were rebuilt from scratch several times during the Oct-2026 PHY-B hunt. They
# are measurement tools, not products -- keep them and reload them.
#
# Usage:
#   bitstreams/archive.sh [DIR|FILE] [label] [--good] [--instrument]
#                         [--board B] [--phy P] [--log FILE]
#
#   DIR|FILE  a gateware/build_* dir, or a .bit directly.
#             Omitted -> the most recently modified gateware/build_*/*.bit.
#
# board/phy default from the env that selects the pins (BOARD, USB_BACKUP), but
# for a build whose XDC is on disk the CLOCK PIN is authoritative and is used
# instead: T4 -> usb1, W4 -> usb2. That makes misfiling essentially impossible.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST_ROOT="$REPO_ROOT/bitstreams"
GW="$REPO_ROOT/gateware"

SRC=""; LABEL=""; PROMOTE=0; KIND="design"; LOG_SRC="${BUILD_LOG:-}"
BOARD_IN="${BOARD:-ext}"; PHY_IN=""

while [ $# -gt 0 ]; do
    case "$1" in
        --good|-g)        PROMOTE=1 ;;
        --instrument|-i)  KIND="instrument" ;;
        --board)          BOARD_IN="$2"; shift ;;
        --phy)            PHY_IN="$2"; shift ;;
        --log)            LOG_SRC="$2"; shift ;;
        -h|--help)        sed -n '2,32p' "$0"; exit 0 ;;
        -*)               echo "ERROR: unknown option $1" >&2; exit 2 ;;
        *)  if [ -z "$SRC" ] && { [ -d "$1" ] || [ -f "$1" ]; }; then SRC="$1";
            else LABEL="$1"; fi ;;
    esac
    shift
done

# ---- resolve the source .bit -------------------------------------------------
if [ -z "$SRC" ]; then
    SRC=$(find "$GW" -maxdepth 2 -path '*/build_*/*.bit' -type f -printf '%T@ %p\n' \
          2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
    [ -n "$SRC" ] || { echo "ERROR: no gateware/build_*/*.bit found" >&2; exit 1; }
    echo "# newest build: $SRC"
fi
if [ -d "$SRC" ]; then
    BITS=$(find "$SRC" -maxdepth 1 -name '*.bit' -type f | sort)
    N=$(echo "$BITS" | grep -c . || true)
    [ "$N" -ge 1 ] || { echo "ERROR: no .bit in $SRC" >&2; exit 1; }
    if [ "$N" -gt 1 ]; then
        echo "ERROR: $N .bit files in $SRC - name one explicitly:" >&2
        echo "$BITS" >&2; exit 2
    fi
    SRC="$BITS"
fi
[ -f "$SRC" ] || { echo "ERROR: no bitstream at $SRC" >&2; exit 1; }
# Absolute BEFORE the `cd "$REPO_ROOT"` below, or a caller-relative path breaks.
SRC=$(readlink -f "$SRC")
SRC_DIR=$(dirname "$SRC")
if [ -n "$LOG_SRC" ] && [ -f "$LOG_SRC" ]; then LOG_SRC=$(readlink -f "$LOG_SRC"); fi

# ---- phy from the XDC clock pin (authoritative), else from env --------------
XDC=$(find "$SRC_DIR" -maxdepth 1 -name '*.xdc' -type f | head -1)
if [ -z "$PHY_IN" ] && [ -n "$XDC" ]; then
    if   grep -qE 'LOC[[:space:]]+T4[[:space:]].*ulpi_clock' "$XDC" 2>/dev/null \
      || grep -qE 'ulpi_clock.*LOC[[:space:]]+T4'            "$XDC" 2>/dev/null \
      || grep -qE '^[[:space:]]*set_property LOC T4 '        "$XDC" 2>/dev/null; then
        PHY_IN="usb1"
    elif grep -qE '^[[:space:]]*set_property LOC (W4|AA4) '   "$XDC" 2>/dev/null; then
        # W4 = SODIMM 85 (the original USB2 clock); AA4 = SODIMM 87, where the
        # USB2 clock was MOVED on 2026-10-28. Both mean position B.
        PHY_IN="usb2"
    fi
    # FALLBACK: a diagnostic top that never requests "ulpi_clock" (e.g. the
    # crystal-domain contention test) has NO clock pin in its XDC, so the test
    # above finds nothing and the env default silently misfiles it. The DIR pin
    # is present in every ULPI design and is just as authoritative:
    #   usb1 dir = T3 (SODIMM 49)   usb2 dir = AB6 (SODIMM 97)
    if [ -z "$PHY_IN" ]; then
        if   grep -qE '^[[:space:]]*set_property LOC T3 .*dir'  "$XDC" 2>/dev/null; then
            PHY_IN="usb1"; echo "# phy from XDC DIR pin (no clock port): usb1"
        elif grep -qE '^[[:space:]]*set_property LOC AB6 .*dir' "$XDC" 2>/dev/null; then
            PHY_IN="usb2"; echo "# phy from XDC DIR pin (no clock port): usb2"
        fi
    else
        echo "# phy from XDC clock pin: $PHY_IN"
    fi
fi
if [ -z "$PHY_IN" ]; then
    if [ "${USB_BACKUP:-}" = "1" ]; then PHY_IN="usb2"; else PHY_IN="usb1"; fi
fi

BOARD=$(echo "$BOARD_IN" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')
PHY=$(echo "$PHY_IN"     | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-')
[ -n "$BOARD" ] && [ -n "$PHY" ] || { echo "ERROR: empty board/phy" >&2; exit 2; }

cd "$REPO_ROOT"

SUM=$(sha256sum "$SRC" | cut -d' ' -f1)
# NOTE no `exit` in awk and `|| true` on the producer: an early-exiting awk
# SIGPIPEs sha256sum, and `set -o pipefail` would turn that into a fatal error.
EXISTING=$( { find "$DEST_ROOT" -name '*.bit' -type f -print0 2>/dev/null \
              | xargs -0 -r sha256sum 2>/dev/null || true; } \
            | awk -v s="$SUM" '$1==s && !f {print $2; f=1}' || true)
if [ -n "$EXISTING" ]; then
    echo "ALREADY ARCHIVED (byte-identical): $EXISTING" >&2
    echo "$EXISTING"; exit 0
fi

SHA=$(git rev-parse --short HEAD 2>/dev/null || echo nogit)
if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
    SHA="${SHA}-dirty"
fi
STAMP=$(date +%Y-%m-%d_%H%M)
BASE=$(basename "$SRC_DIR")                 # build_freqtest_usb1 -> the recipe
NAME="${STAMP}_${SHA}${LABEL:+_${LABEL}}"
if [ "$KIND" = "instrument" ]; then
    DEST_DIR="$DEST_ROOT/instruments/$BOARD/$PHY"
else
    DEST_DIR="$DEST_ROOT/$BOARD/$PHY"
fi
mkdir -p "$DEST_DIR"
cp "$SRC" "$DEST_DIR/${NAME}.bit"

# Find the build script by the BUILD_DIR it declares -- exact, and robust to
# names like build_dac_usb2.py -> build_dac_utmi_usb2/ that no amount of
# string-munging on the directory name will guess.
# NOTE `|| true` is LOAD-BEARING: with `set -euo pipefail`, a grep that matches
# nothing makes this assignment exit 1 and kills the script HERE -- after the
# .bit has been copied but before the .info is written. That is precisely how
# this tree ended up with 44 bitstreams and no rebuild recipe. It fires for any
# builder that takes its output dir from the environment (BUILD_DIR=...) rather
# than hardcoding it, e.g. build_probe.py / build_trace_ph.py / build_loopback.py.
# ARCHIVE_BUILDER=<script.py> names the builder explicitly for those.
BUILDER="${ARCHIVE_BUILDER:-}"
if [ -z "$BUILDER" ]; then
    BUILDER=$(grep -lE "(BUILD_DIR *= *|execute_local\()[\"']${BASE}[\"']" \
                 "$GW"/build_*.py 2>/dev/null | head -1 | xargs -r basename) || true
fi
if [ -z "$BUILDER" ]; then
    for cand in "$GW/${BASE}.py" "$GW/$(echo "$BASE" | sed 's/_usb1$//').py"; do
        if [ -f "$cand" ]; then BUILDER=$(basename "$cand"); break; fi
    done
fi

{
    echo "# $BOARD / $PHY / $KIND / $NAME"
    echo "# archived:  $(date -Iseconds)"
    echo "# source:    $SRC ($(stat -c '%y' "$SRC" | cut -d. -f1))"
    echo "# size:      $(stat -c '%s' "$SRC") bytes"
    echo "# sha256:    $SUM"
    echo "# build dir: $BASE"
    echo ""
    echo "## REBUILD RECIPE"
    echo "cd $GW"
    printf 'BOARD=%s' "$BOARD"
    [ "$PHY" = "usb2" ] && printf ' USB_BACKUP=1'
    for v in USB_PHASE SAMPLE_EDGE USB_PULLUP_DATA NXT_W4 USB_SWAP_D67 USB_MINIMAL; do
        eval "val=\${$v:-}"; [ -n "$val" ] && printf ' %s=%s' "$v" "$val"
    done
    echo " \\"
    echo "  CHIPDB_DIR=\${CHIPDB_DIR:-/home/lisp/FPGA/demo-projects/chipdb} \\"
    echo "  DB_DIR=\${DB_DIR:-/home/lisp/openxc7/openxc7/opt/nextpnr-xilinx/external/prjxray-db} \\"
    echo "  ../.venv/bin/python3 ${BUILDER:-<build_script.py>}"
    echo "# NOTE amaranth 0.4.5 is required - the system python has 0.5.8, which"
    echo "#      has no amaranth.vendor.xilinx. ALWAYS use ../.venv/bin/python3."
    echo "# NOTE a position-A (T4) variant of a build_*.py is made by copying it,"
    echo "#      changing BUILD_DIR, and NOT setting USB_BACKUP=1."
    echo ""
    echo "## ENV AT ARCHIVE TIME"
    for v in BOARD USB_BACKUP USB_PHASE SAMPLE_EDGE CHIPDB_DIR DB_DIR; do
        eval "val=\${$v:-}"; echo "#   $v=${val:-<unset>}"
    done
    if [ -n "$XDC" ]; then
        echo ""
        echo "## ULPI PIN ASSIGNMENTS (from $(basename "$XDC"))"
        grep -E 'set_property LOC' "$XDC" | grep -iE 'ulpi' | sed 's/^/#   /' || true
    fi
    echo ""
    git log -1 2>/dev/null || echo "(no git)"
    echo ""
    UD=$(git diff --shortstat HEAD 2>/dev/null || true)
    echo "## uncommitted diff vs HEAD: ${UD:-none}"
    git diff --stat HEAD 2>/dev/null || true
    if [ -n "$LOG_SRC" ] && [ -f "$LOG_SRC" ]; then
        echo ""
        echo "## build log highlights"
        grep -E 'Max frequency for clock|Device utilisation|overused|Checksum' \
             "$LOG_SRC" 2>/dev/null | tail -25 || true
    fi
} > "$DEST_DIR/${NAME}.info"

if [ -n "$LOG_SRC" ] && [ -f "$LOG_SRC" ]; then
    cp "$LOG_SRC" "$DEST_DIR/${NAME}.log"
fi

if [ "$PROMOTE" = "1" ]; then
    mkdir -p "$DEST_ROOT/known-good"
    LINK="$DEST_ROOT/known-good/${BOARD}-${PHY}${LABEL:+-${LABEL}}.bit"
    REL="../$BOARD/$PHY/${NAME}.bit"
    [ "$KIND" = "instrument" ] && REL="../instruments/$BOARD/$PHY/${NAME}.bit"
    ln -sfn "$REL" "$LINK"
    echo "PROMOTED: $LINK -> $REL"
fi

echo "$DEST_DIR/${NAME}.bit"
echo "$DEST_DIR/${NAME}.info"
