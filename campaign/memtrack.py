#!/usr/bin/env python3
"""Memory-only test track, with the CPU kept cool. No BIOS writes in the test itself.

Rules built in:
- No GPU heat load anywhere.
- The stress load runs on the E-cores first; P-cores are added one pair at a time only while the hottest core stays
  under RAMP_BELOW, in the order of plan.MASKS (coolest P-cores first; the hottest never).
  The load is confined from its first fraction of a second (y-cruncher: a waiter sets the mask as the process appears;
  TM5: the elevated helper sets it at launch). At STEP_DOWN the last P-core pair is taken away again and not re-added.
- A guard reads the core temperatures every 20 s and ends the load if the hottest core reaches STOP_AT
  (two readings in a row, or STOP_AT + 4 once). A guard stop is not a memory result.
- No BIOS writes: a failure stops the track and reports; the owner or a later run decides.
- Every result states package watts, hottest core and the DIMM temperatures reached.
- Not started while a program listed in [memtrack] block_processes is open (for example a browser holding data you
  cannot afford to lose to a crash). It is never touched; if it appears mid-run that is journalled and pinged once.
- memtrack-state.json names the item that is running, so that after an unexpected restart the next boot
  ("--boot") reports it and runs the disk checks instead of staying silent.

Usage: memtrack.py [hold] [stress] [tm5]            run now (default: all three, in that order)
       memtrack.py --restart [items]                clean restart in 90 s, then run the items after the boot
       memtrack.py --boot                           what memtrack.service runs at every boot
       memtrack.py --check                          5-minute look at the guard and the core sets (not a memory result)
"""
import json
import os
import re
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import finalrun  # noqa: E402
import ramsweep  # noqa: E402
import settings  # noqa: E402
from plan import MASKS, RAMP_BELOW, STEP_DOWN, STOP_AT, SWEEP_FROM  # noqa: E402

SD = settings.SD
TASKKILL = settings.TASKKILL
HOME = settings.HOME
STATE = HOME + "/memtrack-state.json"
TASKLIST = settings.TASKLIST
BLOCK = [p.lower() for p in settings.get("memtrack", "block_processes", [])]

# The guard thread and the test both talk to the elevated helper through one cmd/ack file pair: one at a time.
_agent, _agent_lock = ramsweep.agent, threading.Lock()


def _agent_locked(cmd):
    with _agent_lock:
        return _agent(cmd)


ramsweep.agent = _agent_locked


def load_state():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {}


def save_state(**kw):
    st = load_state()
    st.update(kw)
    with open(STATE + ".tmp", "w") as f:
        json.dump(st, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(STATE + ".tmp", STATE)


def blocked_open():
    """True if a program from [memtrack] block_processes is running."""
    if not BLOCK:
        return False
    try:
        tl = subprocess.run([TASKLIST], capture_output=True, text=True, timeout=30).stdout.lower()
    except (subprocess.TimeoutExpired, OSError):
        return False
    return any(p in tl for p in BLOCK)


def sensors():
    """Package watts and per-core temperatures from the Shakedown helper. None if it could not be read."""
    try:
        out = subprocess.run(["./shakedown-cli.exe", "sensors"], cwd=SD, capture_output=True, text=True, timeout=60,
                             errors="replace").stdout
    except (subprocess.TimeoutExpired, OSError):
        return None
    m = re.search(r"Package:\s*(\d+) C,\s*(\d+) W", out)
    w = re.search(r"Water:\s*([\d.]+) C", out)
    cores = {c.group(1): int(c.group(2)) for c in re.finditer(r"(?m)^(P\d+|E\d+)\s+\S+\s+\d+\s+\d+\s+(\d+) C", out)}
    busy = {c.group(1): int(c.group(2)) for c in re.finditer(r"(?m)^(P\d+|E\d+)\s+\S+\s+\d+\s+\d+\s+\d+ C\s+(\d+)%", out)}
    if not m or not cores:
        return None
    hot = max(cores, key=cores.get)
    return {"pkg_c": int(m.group(1)), "pkg_w": int(m.group(2)), "water": float(w.group(1)) if w else -1, "cores": cores,
            "max": cores[hot], "hot": hot, "pmax": max(v for k, v in cores.items() if k[0] == "P"), "busy": busy}


def end_loads():
    # y-cruncher's worker is a child with its own name: ending only y-cruncher.exe leaves the load running
    subprocess.run([ramsweep.PS, "-NoProfile", "-Command",
                    "Get-Process -Name %s -ErrorAction SilentlyContinue | Stop-Process -Force" % YC_PROCS], capture_output=True)
    for exe in ("memhold.exe", "Cinebench.exe"):
        subprocess.run([TASKKILL, "/F", "/IM", exe], capture_output=True)
    ramsweep.agent("tm5stop")


YC_PROCS = "'y-cruncher','??-* ~ *'"   # the launcher and the worker it starts (e.g. "14-BDW ~ Kurumi.exe")


def y_mask(mask, wait=0):
    """Confine y-cruncher to the given logical CPUs (same-user processes, no admin needed). y-cruncher.exe is only a
    launcher: the work is done by a child process that it starts at once and that does not follow a later change to
    the launcher's core set (found 14 Nov 17:07: the launcher was confined, the worker ran on all 32). So the worker
    is what gets the mask. And a mask given in the worker's first instant is undone by its own start-up (17:09: set at
    birth, read back as all cores later; set a second after start, it holds). So with wait this polls every 0.1 s for
    the worker to appear and then keeps the mask in place for 20 s, well into the load. True only if a worker was
    confined. The guard reads the mask back at every reading and re-applies it if it ever differs."""
    try:
        r = subprocess.run([ramsweep.PS, "-NoProfile", "-Command",
                            "$m = [IntPtr]%dL; $t = Get-Date; $seen = $null; do { $w = 0;"
                            " foreach ($x in @(Get-Process -Name %s -ErrorAction SilentlyContinue)) { try {"
                            " if ($x.ProcessorAffinity -ne $m) { $x.ProcessorAffinity = $m };"
                            " if ($x.ProcessName -ne 'y-cruncher') { $w++ } } catch { } };"
                            " if ($w -and -not $seen) { $seen = Get-Date };"
                            " if ($seen -and (((Get-Date) - $seen).TotalSeconds -ge %d)) { 'set'; break };"
                            " Start-Sleep -Milliseconds 100 } while (((Get-Date) - $t).TotalSeconds -lt %d)" % (
                                mask, YC_PROCS, 20 if wait else 0, wait + 25)],
                           capture_output=True, text=True, timeout=wait + 90)
        return "set" in r.stdout
    except (subprocess.TimeoutExpired, OSError):
        return False


def y_cpus():
    """Which logical CPUs the y-cruncher worker may use right now (hex), read back from Windows. '' if not running."""
    r = subprocess.run([ramsweep.PS, "-NoProfile", "-Command",
                        "Get-Process -Name '??-* ~ *' -ErrorAction SilentlyContinue | % { '{0:X}' -f [int64]$_.ProcessorAffinity }"],
                       capture_output=True, text=True)
    return r.stdout.strip().splitlines()[0].strip() if r.stdout.strip() else ""


def tm5_mask(mask, wait=0):
    """TM5 runs elevated, so its core set goes through the elevated helper."""
    return ramsweep.agent("tm5aff %X" % mask)


class Guard(threading.Thread):
    """Reads temperatures while a load runs, steps the load's core set up while it is cool and down when it is warm,
    ends the load if it gets too hot. target: "y" (y-cruncher), "tm5" (already confined to MASKS[0] at launch) or None.
    Use as: g = Guard("y"); g.start(); ...load...; g.finish()."""

    def __init__(self, target, step_s=150):
        super().__init__(daemon=True)
        self.target, self.step_s, self.level, self.done, self.tripped = target, step_s, 0, False, False
        self.setter = {"y": y_mask, "tm5": tm5_mask}.get(target)
        self.applied = target == "tm5"
        self.ceiling = len(MASKS) - 1
        self.peak, self.peak_w, self.peak_core, self.reads, self.hot_streak = 0, 0, "", 0, 0
        self.by_level = {}
        self.blocked_seen = False
        self.stray = 0
        self.reapplied = 0
        self.last_step = time.time()
        self.log = open(HOME + "/guard.csv", "a")

    def _first_mask(self):
        if y_mask(MASKS[0][1], wait=900):
            self.applied, self.last_step = True, time.time()

    def run(self):
        if self.target == "y":
            threading.Thread(target=self._first_mask, daemon=True).start()
        while not self.done:
            s = sensors()
            if s:
                self.reads += 1
                if s["max"] > self.peak:
                    self.peak, self.peak_core = s["max"], s["hot"]
                self.peak_w = max(self.peak_w, s["pkg_w"])
                if self.applied and time.time() - self.last_step > 40:     # the first readings after a step belong to both
                    c, w = self.by_level.get(self.level, (0, 0))
                    self.by_level[self.level] = (max(c, s["max"]), max(w, s["pkg_w"]))
                self.log.write("%s,%d,%d,%s,%.1f,%s\n" % (time.strftime("%H:%M:%S"), s["pkg_w"], s["max"], s["hot"], s["water"],
                                                         MASKS[self.level][0] if self.applied else "-"))
                self.log.flush()
                if self.target == "y" and self.applied:
                    cur = y_cpus()
                    if cur and int(cur, 16) != MASKS[self.level][1]:
                        self.reapplied += 1
                        y_mask(MASKS[self.level][1])
                if self.setter and self.applied and time.time() - self.last_step > 25:
                    allowed = MASKS[self.level][1]
                    stray = [k for k, v in s["busy"].items() if k[0] == "P" and v >= 60 and not (allowed >> (2 * int(k[1:])) & 3)]
                    self.stray = self.stray + 1 if len(stray) >= 3 else 0
                    if self.stray >= 2:
                        self.tripped = True
                        end_loads()
                        ramsweep.note("**GUARD STOP**: the load is not staying on its core set (%s): %s are busy, package %d W, "
                                      "hottest core %d C. Load ended; this is not a memory result." % (
                                          MASKS[self.level][0], " ".join(stray), s["pkg_w"], s["max"]), ping=True)
                        return
                self.hot_streak = self.hot_streak + 1 if s["max"] >= STOP_AT else 0
                if self.hot_streak >= 2 or s["max"] >= STOP_AT + 4:
                    self.tripped = True
                    end_loads()
                    ramsweep.note("**GUARD STOP**: hottest core %s at %d C (limit %d), package %d W, core set %s. Load ended; "
                                  "this is not a memory result." % (s["hot"], s["max"], STOP_AT, s["pkg_w"],
                                                                    MASKS[self.level][0] if self.applied else "n/a"), ping=True)
                    return
                if self.setter and self.applied:
                    if s["max"] >= STEP_DOWN and self.level > 0:
                        self.level -= 1
                        self.ceiling = self.level
                        self.setter(MASKS[self.level][1])
                        self.last_step = time.time()
                    elif (self.level < self.ceiling and s["max"] < RAMP_BELOW and s["pkg_w"] > 90
                          and time.time() - self.last_step > self.step_s):
                        self.level += 1
                        self.setter(MASKS[self.level][1])
                        self.last_step = time.time()
                if not self.blocked_seen and self.reads % 3 == 0 and blocked_open():
                    self.blocked_seen = True
                    ramsweep.note("A blocked program ([memtrack] block_processes) was opened while a memory test is "
                                  "running. Nothing was touched; the owner should know.", ping=True)
            for _ in range(20):
                if self.done:
                    break
                time.sleep(1)

    def finish(self):
        self.done = True
        self.join(timeout=90)
        self.log.close()
        sets = "; ".join("%s: up to %d C, %d W" % (MASKS[k][0], v[0], v[1]) for k, v in sorted(self.by_level.items()))
        return "CPU: peak %d C on %s, up to %d W package%s%s" % (
            self.peak, self.peak_core or "?", self.peak_w, " (%s)" % sets if sets else "",
            ("; core set had to be re-applied %d times" % self.reapplied if self.reapplied else "")
            if self.applied or not self.setter else "; the core set was never applied")


NOT_RUN = []   # items that could not be started: never reported as passed


def need_dimm_sensors():
    """The DIMM temperature reading is what makes a fan-off result usable; refuse to run blind."""
    if "no reading" in ramsweep.dimm_now():
        finalrun.init_dimm()      # opens HWiNFO for about 40 s: fan control may change meanwhile
    return "no reading" not in ramsweep.dimm_now()


def run_hold(minutes=45, gib=30):
    g = Guard("y")
    g.start()
    ok = finalrun.hold(minutes, "MEMORY TRACK at-rest test (CPU kept cool, no GPU heat)", gib=gib, gpu_heat=False)
    cpu = g.finish()
    ramsweep.note("...same test: %s." % cpu)
    return (ok and not g.tripped), g.tripped


def run_stress(minutes=60):
    g = Guard("y")
    g.start()
    ok = finalrun.stress(minutes, "MEMORY TRACK y-cruncher %d min (CPU kept cool, no GPU heat)" % minutes)
    cpu = g.finish()
    ramsweep.note("...same test: %s." % cpu)
    return (ok and not g.tripped), g.tripped


def run_tm5():
    import tm5run
    # TM5 is confined once, at launch, to the widest cool set, and then left alone: the 20:15 run ended with a program
    # error (not a memory error) about 25 s after the core set was moved under it, so no moves mid-run. At that set
    # TM5 ran at up to 63 C. The guard still watches and can end it; it steps down only at STEP_DOWN.
    g = Guard("tm5")
    g.level = g.ceiling = len(MASKS) - 1
    g.start()
    try:
        ok, detail = tm5run.run(mask=MASKS[-1][1])
    except Exception as e:  # noqa: BLE001
        ok, detail = None, "not completed (%s: %s)" % (type(e).__name__, str(e)[:120])
    cpu = g.finish()
    ramsweep.note("**MEMORY TRACK TM5 %s**: %s; %s." % ("PASS" if ok else "not run" if ok is None else "FAIL", detail, cpu))
    if ok is None:
        NOT_RUN.append("tm5")
    return (bool(ok) and not g.tripped) if ok is not None else True, g.tripped


def main(items):
    if blocked_open():
        ramsweep.note("Memory track not started: a program from [memtrack] block_processes is open. Nothing was "
                      "touched.", ping=True)
        save_state(phase="waiting-blocked-process", items=items)
        return
    if not need_dimm_sensors():
        ramsweep.note("Memory track not started: no DIMM temperature reading.", ping=True)
        save_state(phase="no-dimm-reading")
        return
    idle = sensors()
    ramsweep.note("## MEMORY TRACK (%s) on the current set, DIMM fan state as the owner left it. Idle: %s; DIMMs %s." % (
        ", ".join(items), "package %d W, hottest core %d C, water %.1f C" % (idle["pkg_w"], idle["max"], idle["water"]) if idle
        else "no CPU reading", ramsweep.dimm_now()))
    if idle is None:
        ramsweep.note("Memory track not started: the core temperatures cannot be read, so the guard would be blind.", ping=True)
        save_state(phase="no-cpu-reading")
        return
    for item in items:
        save_state(phase="running:" + item, t=int(time.time()), items=items)
        ok, tripped = {"hold": run_hold, "stress": run_stress, "tm5": run_tm5}[item]()
        if tripped:
            save_state(phase="guard-stop:" + item)
            ramsweep.note("Memory track stopped by the temperature guard during '%s'." % item, ping=True)
            return "guard"
        if not ok:
            save_state(phase="failed:" + item)
            ramsweep.note("**MEMORY TRACK: '%s' FAILED.**%s" % (item, " The chain gives it back and goes on." if load_state().get("chain") else
                          " Stopped here; nothing was changed (no BIOS write, no reboot)."), ping=True)
            return "fail"
    ran = [i for i in items if i not in NOT_RUN]
    save_state(phase="done", not_run=NOT_RUN)
    ramsweep.note("**MEMORY TRACK DONE**: %s passed on the current set%s." % (
        ", ".join(ran) or "nothing", "; NOT RUN (no result): " + ", ".join(NOT_RUN) if NOT_RUN else ""), ping=True)
    return "pass" if not NOT_RUN else "notrun"


LASTGOOD = HOME + "/memtrack-lastgood.json"


def write_full(f, why):
    """Writes the whole set (the writer copies the golden script over every field not passed), then reads the
    stored values back. True only if every field is stored as intended."""
    ok, msg = ramsweep.write_bios(f)
    try:
        import nvram
        a = nvram.Script(open(os.path.join(ramsweep.STAGE, "after.txt"), encoding="utf-8", errors="replace").read())
        bad = {t: (a.value(t), v) for t, v in f.items() if a.value(t) != v}
    except Exception as e:  # noqa: BLE001
        bad = {"read-back": str(e)}
    ramsweep.note("Full set written for %s: %s; read back: %s." % (why, msg, "all %d as intended" % len(f) if not bad else "MISMATCH %s" % bad))
    return ok and not bad


def chain_next(outcome):
    """Run a queue of RAM probes unattended (state "chain": true, "queue": [...]). After each TM5 probe: a pass makes the set
    the new last good; anything else gives the probe back. Then the next queued probe (write, restart, TM5), and when
    the queue is empty, hand the remaining single-run probes to the sweep (ramsweep.py, failsafe and step cards)."""
    st = load_state()
    if not st.get("chain"):
        return
    good = json.load(open(LASTGOOD))
    cur = finalrun.load()
    probe = st.get("probe")
    if outcome == "pass":
        good = dict(cur["final"])
        json.dump(good, open(LASTGOOD, "w"), indent=1)
        if probe:
            ramsweep.note("**CHAIN: KEPT %s** (TM5 60 min clean)." % probe["name"])
    elif probe:
        ramsweep.note("**CHAIN: GIVEN BACK %s** (TM5 outcome: %s). Last good stays: %s." % (
            probe["name"], outcome, ", ".join("%s %s" % (ramsweep.NAMES.get(t, t), v) for t, v in good.items() if t in probe["changes"])))
    queue = st.get("queue", [])
    if queue:
        nxt = queue.pop(0)
        f = dict(good)
        f.update({t: v for t, v in nxt["changes"].items()})
        ramsweep.note("CHAIN next: %s on the last good set, then TM5 60 min (%d more queued after it)." % (nxt["name"], len(queue)))
        if not write_full(f, nxt["name"]):
            save_state(chain=False, phase="chain-stopped")
            ramsweep.note("Chain stopped: the write did not store as intended. Nothing restarted.", ping=True)
            return
        cur["final"] = f
        finalrun.save(cur)
        save_state(queue=queue, probe=nxt)
        time.sleep(20)
        restart(["tm5"], "chain probe " + nxt["name"])
        return
    # Queue empty: hand over to the sweep. Its writes are always good + step, so a given-back probe is undone there.
    cur["final"] = good
    finalrun.save(cur)
    sw = ramsweep.load()
    names = [g[0] for g in ramsweep.GROUPS]
    sw.update({"good": dict(good), "g": names.index(SWEEP_FROM), "k": 0, "step": None, "phase": "apply", "dirty": False})
    ramsweep.save(sw)
    open(HOME + "/owner-awake", "w").write("chain hand-over: risky steps allowed unattended\n")
    save_state(chain=False, phase="handed-to-sweep", probe=None)
    ramsweep.note("CHAIN: TM5 probes done; handing the remaining probes to the sweep from '%s' (one y-cruncher run each, "
                  "failsafe armed, step card before each). Base: %s." % (SWEEP_FROM, ", ".join(
                      "%s %s" % (ramsweep.NAMES.get(t, t), v) for t, v in good.items())), ping=True)
    subprocess.Popen([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "ramsweep.py")],
                     start_new_session=True)


def restart(items, why=None):
    """Clean restart, then the items after the boot (run "memtrack.py --boot" at every boot)."""
    if blocked_open():
        ramsweep.note("Restart not done: a program from [memtrack] block_processes is open.", ping=True)
        return
    save_state(phase="pending-boot", t=int(time.time()), items=items)
    closed = ramsweep.release_gpu()
    with open(HOME + "/expect-down-until", "w") as f:
        f.write(str(int(time.time()) + 8 * 60) + "\n")
    ramsweep.note("Restart in 90 s (%s). Closed before it: %s. After the boot: shutdown/boot check, BIOS set check by export, "
                  "then the memory track (%s)." % (why or "memory track", closed or "nothing", ", ".join(items)))
    subprocess.run(["sync"])
    subprocess.run([settings.SHUTDOWN, "/r", "/t", "90", "/c",
                    "Restart, then RAM tests. To cancel: shutdown /a"])


def boot():
    st = load_state()
    ph = st.get("phase", "")
    if ph.startswith("running:"):
        item = ph.split(":", 1)[1]
        save_state(phase="crashed:" + item)
        time.sleep(100)
        ev = ramsweep.crashes_since(st.get("t", time.time() - 6 * 3600))
        whea = ramsweep.whea_errors(st.get("t", time.time() - 6 * 3600))
        ramsweep.note("**MEMORY TRACK: the PC restarted by itself during '%s'.** Windows records: %s; WHEA: %s. Nothing is "
                      "changed automatically; disk checks follow." % (item, " | ".join(ev)[:400] or "none (hard reset)",
                                                                       " | ".join(whea)[:300] or "none"), ping=True)
        clean, msg = ramsweep.integrity()
        ramsweep.note("Integrity after that restart: %s%s" % (msg, "" if clean else " **NOT CLEAN**"), ping=True)
        if clean and load_state().get("chain"):
            chain_next("crash")
        return
    if ph != "pending-boot":
        return
    save_state(phase="booted")
    time.sleep(120)     # let Windows and its start-up programs settle
    gap = ramsweep.boot_gap()
    ev = ramsweep.crashes_since(st["t"])
    try:
        ok, msg = finalrun.verify(finalrun.load()["final"])
    except Exception as e:  # noqa: BLE001
        ok, msg = False, "check failed (%s)" % type(e).__name__
    pend = subprocess.run([ramsweep.PS, "-NoProfile", "-Command",
                           "(Get-ItemProperty 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Session Manager' -Name "
                           "PendingFileRenameOperations -ErrorAction SilentlyContinue) -ne $null"],
                          capture_output=True, text=True).stdout.strip()
    ramsweep.note("Restart done: shutdown to boot %d s (normal about 20; over 120 means the shutdown hung), crash records "
                  "since: %s; BIOS set by export: %s; Windows still wants a restart: %s." % (
                      gap, " | ".join(ev)[:300] or "none", "as expected" if ok else "NOT as expected: " + msg, pend or "?"))
    if not ok or ev:
        save_state(phase="boot-check-failed")
        ramsweep.note("Memory track not started: the boot check above is not clean.", ping=True)
        if load_state().get("chain"):
            chain_next("boot-check-failed")
        return
    outcome = main(st.get("items") or ["hold", "stress", "tm5"])
    chain_next(outcome or "notrun")


def check():
    """Five minutes of y-cruncher under the guard with fast steps: shows that the core set is applied and what each set
    costs in temperature. Not a memory result."""
    g = Guard("y", step_s=50)
    g.start()
    ok, out, bad = finalrun.yc(["stress", "-M:30G", "-D:30", "-TL:300", "VT3", "FFTv4", "N63"], 900)
    cpu = g.finish()
    ramsweep.note("Guard check (5 min y-cruncher, fast steps, not a memory result): %s; y-cruncher %s; guard stop: %s." % (
        cpu, "clean" if ok else "ERROR " + (bad[0][:80] if bad else "?"), "yes" if g.tripped else "no"))


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["--boot"]:
        boot()
    elif a[:1] == ["--restart"]:
        restart(a[1:] or ["hold", "stress", "tm5"])
    elif a[:1] == ["--check"]:
        check()
    elif a[:1] == ["--chain-next"]:
        chain_next(a[1])
    else:
        main(a or ["hold", "stress", "tm5"])
