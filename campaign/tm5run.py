#!/usr/bin/env python3
"""TestMem5 0.13.1 (CoolCmd build, github.com/CoolCmd/TestMem5) through the elevated helper (helper-agent.ps1: "tm5"
starts TM5.exe from its configured folder, "tm5stop" ends it). Settings come from TM5.ini beside the exe:
config "DDR5 Intel @ anta777", at least 60 minutes, stop at the first error, no sound. Results are read from Log.txt."""
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ramsweep  # noqa: E402
import settings  # noqa: E402

DIR = settings.TM5_DIR
LOG = DIR + "/Log.txt"
MINUTES = 60


def read_log():
    try:
        return open(LOG, "rb").read().decode("utf-8", "replace")
    except OSError:
        return ""


def run(mask=None):
    """mask: logical-CPU set TM5 is confined to from launch (hex goes to the elevated helper)."""
    if not os.path.exists(DIR + "/TM5.exe") or not os.path.exists(DIR + "/TM5.dll"):
        raise RuntimeError("TM5 files missing (removed by antivirus?)")
    before = len(read_log())
    ramsweep.DIMM_SINCE = time.strftime("%H:%M:%S")
    ramsweep.agent("dimmlog %d" % (MINUTES * 60 + 1500))
    time.sleep(2)
    if not ramsweep.agent("tm5 %X" % mask if mask else "tm5"):
        raise RuntimeError("the admin helper did not answer")
    t0 = time.time()
    new = ""
    started = False
    while time.time() - t0 < MINUTES * 60 + 1800:
        time.sleep(20)
        new = read_log()[before:]
        if not started:
            if re.search(r"(?i)start testing|testing \d+ MB", new):
                started = True
            elif time.time() - t0 > 240:
                ramsweep.agent("tm5stop")
                raise RuntimeError("TM5 wrote no start line in 4 minutes; log tail: %r" % new[-200:])
        if re.search(r"(?i)testing (is )?completed|testing stopped|error in test|fatal error|critical error|crashed|occurred in the program", new):
            time.sleep(5)
            new = read_log()[before:]
            break
    mins = (time.time() - t0) / 60
    ramsweep.agent("tm5stop")
    lines = [l.strip() for l in new.splitlines() if l.strip()]
    # TM5 0.13.1 writes only "Testing completed" on a clean finish (errors get their own "Error in test N" lines)
    clean = bool(re.search(r"(?i)testing completed in .*no errors|of errors is not detected|^\S+\s+testing completed\s*$", new, re.M))
    bad = [l for l in lines if re.search(r"(?i)error in test|detected \d+ error|fatal|critical|crashed|failed to", l)]
    # A TM5 "error N occurred in the program" counts as a FAIL: once one came with no core-set move, on a set
    # looser than one that failed and tighter than one that passed, so corrupted memory under TM5 itself is the likely cause.
    if any(re.search(r"(?i)an error -?\d+ occurred in the program", l) for l in lines):
        bad = bad + [l for l in lines if re.search(r"(?i)occurred in the program", l)]
    ok = clean and not bad
    detail = "TestMem5 0.13.1, %.0f min; log: %s; %s" % (mins, " | ".join(lines[-8:])[:700], ramsweep.dimm_summary())
    return ok, detail


if __name__ == "__main__":
    print(run())
