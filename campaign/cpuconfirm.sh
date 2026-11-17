#!/usr/bin/env bash
# Confirmation of the CPU set the sweep kept: Cinebench with GPU heat for MINUTES (default 30), after a 2-minute
# settle. Checks first that the BIOS shows the kept set, and journals the result with the WHEA count.
# Usage: cpuconfirm.sh LABEL [MINUTES]
here="$(cd "$(dirname "$0")" && pwd)"
label="$1"; mins="${2:-30}"
sleep 120
python3 - "$here" <<'PY'
import sys; sys.path.insert(0, sys.argv[1]); import ramsweep
st = ramsweep.load(); sc = ramsweep.read_bios()
want = ramsweep.wanted(st, None)
mm = {ramsweep.NAMES.get(t, t): (sc.label(t), v) for t, v in want.items() if sc and sc.label(t) is not None and sc.label(t) != ramsweep.shown(t, v)}
ramsweep.note("CPU confirmation starting: BIOS after the boot %s; boot %d s. Cinebench with GPU heat on the kept CPU set." % ("as kept" if sc and not mm else "MISMATCH %s" % mm, ramsweep.boot_gap()))
PY
python3 "$here/heat.py" run "$label" "confirm: kept CPU set, GPU heat" "$mins"
python3 - "$here" "$label" "$mins" <<'PY'
import sys; sys.path.insert(0, sys.argv[1]); import ramsweep, settings
label, mins = sys.argv[2], int(sys.argv[3])
rows = [l for l in open(settings.SD + "/ledger.csv", errors="replace") if label in l]
row = rows[-1] if rows else ""
ok = ",PASS," in row
ramsweep.note("**CPU CONFIRM %s %s**: Cinebench %d min + GPU heat: %s; WHEA in the last %d min: %d" % (label, "PASS" if ok else "FAIL", mins, ",".join(row.split(",")[4:16]) if row else "no ledger line", mins + 5, ramsweep.whea_count(mins + 5)), ping=True)
PY
