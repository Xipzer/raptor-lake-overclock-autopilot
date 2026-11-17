#!/usr/bin/env python3
"""CPU test with GPU heat: one Shakedown run (the overclock-autopilot core, shakedown-cli) with the GPU heat load
(shakedown.exe --heat) on for its whole length. On a loop shared with the GPU this is the coolant temperature real use
reaches.

Usage: heat.py run LABEL NOTE [MINUTES]     (heat-run.sh is a wrapper that runs it detached)
       heat.py on / heat.py off             (heat alone, for memory tests that want it)
"""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settings  # noqa: E402

STOP = settings.SD + "/runs/stop-heat"


def on():
    """Start the heat load; it runs until off(). Returns the process, or None if it could not start."""
    win = settings.SD_WIN.rstrip("\\")
    ps = ("$e = New-Object System.Threading.EventWaitHandle($false, 'ManualReset', 'Local\\ShakedownHeat'); "
          "$p = Start-Process '%s\\shakedown.exe' -ArgumentList '--heat' -PassThru; "
          "while (-not (Test-Path '%s\\runs\\stop-heat')) { Start-Sleep 2; if ($p.HasExited) { break } }; "
          "$e.Close(); Start-Sleep 5; if (-not $p.HasExited) { Stop-Process -Id $p.Id -Force }" % (win, win))
    try:
        if os.path.exists(STOP):
            os.remove(STOP)
        return subprocess.Popen([settings.PS, "-NoProfile", "-Command", ps], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError:
        return None


def off(proc):
    if proc is None:
        return
    open(STOP, "w").close()
    try:
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
    try:
        os.remove(STOP)
    except OSError:
        pass


def run(label, note, minutes=10):
    """A Shakedown run of `minutes` with GPU heat. Output goes to last-run.log in the state folder; the result is the
    ledger line Shakedown writes (the caller reads it)."""
    heat = on()
    time.sleep(6)
    log = settings.HOME + "/last-run.log"
    try:
        with open(log, "w") as f:
            subprocess.run(["./shakedown-cli.exe", "run", "--minutes", str(minutes), "--label", label, "--note", note],
                           cwd=settings.SD, stdout=f, stderr=subprocess.STDOUT, timeout=minutes * 60 + 1200)
    except subprocess.TimeoutExpired:
        pass
    finally:
        off(heat)
    with open(log, "a") as f:
        f.write("done\n")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["run"] and len(a) >= 3:
        run(a[1], a[2], int(a[3]) if len(a) > 3 else 10)
    elif a[:1] == ["on"]:
        p = on()
        print("heat on" if p else "heat could not start")
    elif a[:1] == ["off"]:
        open(STOP, "w").close()
    else:
        sys.exit(__doc__)
