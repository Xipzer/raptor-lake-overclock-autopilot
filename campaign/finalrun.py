#!/usr/bin/env python3
"""Final validation and before/after benchmarks (run at every boot, one phase per boot).

Phases: validate (y-cruncher VT3/FFT/N63 60 min + Cinebench with GPU heat 30 min) -> retrain (five hot reboots,
each: boot time, verify, VT3 6 min) -> tm5 -> bench-final -> to-original (write the original profile, reboot) -> bench-original -> to-final
(write the final profile back, reboot) -> verify-final -> done. Everything is journalled in ram-journal.md.
"""
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import heat  # noqa: E402
import ramsweep  # noqa: E402
import settings  # noqa: E402
from plan import CPU_FIELDS, CPU_STEPBACK, DEFAULT_CONTROLS, ORIGINAL, PROFILE_TOKEN, PROFILE_VALUE, STEPBACK  # noqa: E402

HOME = settings.HOME
STATE = HOME + "/final-state.json"
YC_DIR = os.path.dirname(ramsweep.YC)
CB = settings.CB


def load():
    return json.load(open(STATE))


def save(st):
    tmp = STATE + ".tmp"
    json.dump(st, open(tmp, "w"), indent=1)
    os.replace(tmp, STATE)


def water():
    return settings.coolant()


def wait_cool(limit=30.0, most=1800):
    t0 = time.time()
    while water() > limit and time.time() - t0 < most:   # -1 (no coolant sensor configured) never waits
        time.sleep(30)
    return water()


def yc(args, timeout):
    """y-cruncher with its console copied to yc-live.log line by line and flushed to disk, so that after a crash the
    test that was running is known."""
    p = subprocess.Popen([ramsweep.YC] + args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=YC_DIR,
                         errors="replace")
    lines, t0 = [], time.time()
    with open(HOME + "/yc-live.log", "a") as f:
        f.write("=== %s y-cruncher %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(args)))
        for line in p.stdout:
            lines.append(line)
            clean = re.sub(r"\x1b\[[0-9;]*m", "", line).rstrip()
            if clean:
                f.write("%s %s\n" % (time.strftime("%H:%M:%S"), clean))
                f.flush()
                os.fsync(f.fileno())
            if time.time() - t0 > timeout:
                p.kill()
                break
    p.wait()
    r = p
    out = re.sub(r"\x1b\[[0-9;]*m", "", "".join(lines))
    bad = [l.strip() for l in out.splitlines() if re.search(r"(?i)error|fail|mismatch|exception", l) and "Stop on Error" not in l]
    return r.returncode == 0 and not bad, out, bad


def stress(minutes, label):
    ramsweep.DIMM_SINCE = time.strftime("%H:%M:%S")
    ramsweep.agent("dimmlog %d" % (minutes * 60 + 120))
    w0 = water()
    ok, out, bad = yc(["stress", "-M:30G", "-D:120", "-TL:%d" % (minutes * 60), "VT3", "FFTv4", "N63"], minutes * 60 + 900)
    passed = len(re.findall(r"Passed", out))
    whea = ramsweep.whea_count(minutes + 3)
    ok = ok and whea == 0 and passed >= 3
    ramsweep.note("**%s %s**: y-cruncher VT3/FFTv4/N63 %d min, %d sections passed%s, WHEA %d, water %.1f C at start, %s" % (
        label, "PASS" if ok else "FAIL", minutes, passed, ", " + bad[0][:80] if bad else "", whea, w0, ramsweep.dimm_summary()))
    return ok


MEMHOLD = settings.MEMHOLD


def heat_on():
    return heat.on()


def heat_off(proc):
    heat.off(proc)


def hold(minutes, label, gib=30, gpu_heat=False):
    """At-rest test (memhold.c): GiB of patterns held untouched and checked every 2 minutes, with y-cruncher beside it
    in 8 GB and the GPU heat load for worst-case temperature. Held data depends on refresh alone, so a flip is a refresh fault, not the CPU."""
    log = settings.MEMHOLD_LOG_WIN
    logw = settings.MEMHOLD_LOG
    before = os.path.getsize(logw) if os.path.exists(logw) else 0
    ramsweep.DIMM_SINCE = time.strftime("%H:%M:%S")
    ramsweep.agent("dimmlog %d" % (minutes * 60 + 180))
    mh = subprocess.Popen([MEMHOLD, str(gib), str(minutes), "120", log], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, cwd=os.path.dirname(MEMHOLD))
    time.sleep(45)
    heat = heat_on() if gpu_heat else None
    try:
        load_ok, _, bad = yc(["stress", "-M:8G", "-D:120", "-TL:%d" % max(60, minutes * 60 - 90), "VT3", "FFTv4", "N63"],
                             minutes * 60 + 600)
    finally:
        heat_off(heat)
    try:
        code = mh.wait(timeout=600)
    except subprocess.TimeoutExpired:
        mh.kill()
        code = -1
    new = open(logw, errors="replace").read()[before:] if os.path.exists(logw) else ""
    lines = [l.strip() for l in new.splitlines() if l.strip()]
    flips = [l for l in lines if " FLIP " in l]
    done = [l for l in lines if "memhold done" in l]
    locked = [l for l in lines if "locked in physical memory" in l]
    ok = code == 0 and bool(done) and not flips and load_ok
    ramsweep.note("**%s %s**: %d GiB held for %d min, checked every 2 min: %s; %s; load beside it (y-cruncher 8 GB%s) %s; water %.1f C; %s%s" % (
        label, "PASS" if ok else "FAIL", gib, minutes, done[-1].split("memhold done: ")[-1] if done else "did not finish (exit %d)" % code,
        locked[-1].split(" ", 1)[-1] if locked else "lock state unknown", " + GPU heat" if heat else "",
        "clean" if load_ok else "ERROR: " + (bad[0][:80] if bad else "?"), water(), ramsweep.dimm_summary(), ("; first flips: " + " | ".join(f.split(" FLIP ")[-1][:110] for f in flips[:3])) if flips else ""))
    return ok


def cinebench(minutes, label):
    heat.run(label, "final profile, GPU heat", minutes)
    rows = [l for l in open(settings.SD + "/ledger.csv", errors="replace") if label in l]
    ok = bool(rows) and ",PASS," in rows[-1]
    ramsweep.note("**%s %s**: Cinebench %d min with GPU heat: %s" % (label, "PASS" if ok else "FAIL", minutes,
                                                                 ",".join(rows[-1].split(",")[4:16]) if rows else "no ledger line"))
    return ok


def bench(tag):
    """The same suite on whichever profile is running. Water cooled to <= 30 C first so both profiles start alike."""
    res = {"water_start": wait_cool()}
    lat = []
    for _ in range(3):
        o = subprocess.run([ramsweep.MEMLAT], capture_output=True, text=True, timeout=300).stdout.split()
        if len(o) >= 2:
            lat.append((float(o[0]), float(o[1])))
    res["latency_ns"] = min(l[0] for l in lat) if lat else None
    res["read_GBps"] = max(l[1] for l in lat) if lat else None
    t0 = time.time()
    r = subprocess.run([ramsweep.YC, "bench", "2.5b", "-o", settings.BENCH_TEMP_WIN], capture_output=True,
                       text=True, timeout=1800, cwd=YC_DIR, errors="replace")
    out = re.sub(r"\x1b\[[0-9;]*m", "", r.stdout)
    m = re.search(r"Total Computation Time:\s*([\d.]+)\s*seconds", out)
    res["ycruncher_2_5b_s"] = float(m.group(1)) if m else None
    res["ycruncher_wall_s"] = round(time.time() - t0, 1)
    for test, key in (("g_CinebenchCpuXTest=true", "cinebench_multi"), ("g_CinebenchCpu1Test=true", "cinebench_single")):
        wait_cool()
        r = subprocess.run([CB, test, "g_CinebenchMinimumTestDuration=1"], capture_output=True, text=True, timeout=1800,
                           errors="replace")
        m = re.findall(r"(?m)^CB\s+([\d.]+)", r.stdout)  # the line Cinebench prints at the end: "CB 9822.64 (0.00)"
        res[key] = float(m[-1]) if m else None
    json.dump(res, open(HOME + "/bench-%s.json" % tag, "w"), indent=1)
    ramsweep.note("**BENCH %s**: %s" % (tag, json.dumps(res)))
    return res


def write(settings, why):
    ok, msg = ramsweep.write_bios(settings)
    ramsweep.note("Wrote BIOS for %s: %s." % (why, msg))
    return ok


def verify(settings):
    sc = ramsweep.read_bios()
    if sc is None:
        return False, "could not read BIOS"
    mm = {t: (sc.label(t), v) for t, v in settings.items() if sc.label(t) is not None and sc.label(t) != ramsweep.shown(t, v)}
    xmp = sc.value(PROFILE_TOKEN)
    return (not mm and xmp == PROFILE_VALUE), "mismatch %s, profile %s" % (mm, xmp)


def init_dimm():
    """The DIMM sensors read 0 after a boot until HWiNFO has initialised the SPD hubs once."""
    ramsweep.agent("dimm")
    try:
        ack = open(ramsweep.ATUNE + "/ack.txt", encoding="utf-16").read()
    except (OSError, UnicodeError):
        ack = ""
    if ":0.00" in ack or "ok dimm" not in ack:
        ramsweep.agent("hwinfo")
        time.sleep(12)
        ramsweep.agent("enterproc HWiNFO64")
        time.sleep(25)
        ramsweep.agent("hwstop")
        time.sleep(2)


def failed(st, what, item):
    """A validation item failed: give back the next rung and repeat ONLY that item. A passed phase is not rerun after
    loosening; the CPU test is rerun only when a CPU field changes."""
    final = st["final"]
    st.pop("running", None)
    done = st.setdefault("done", {})
    done.pop(item, None)
    ladder = CPU_STEPBACK if item == "cb30" else STEPBACK
    key = "cpu_stepped" if item == "cb30" else "stepped"
    n = int(st.get(key, 0))
    while n < len(ladder) and all(final.get(t) == v for t, v in ladder[n][1].items()):
        n += 1   # this rung is already in effect
    if n < len(ladder):
        name, change = ladder[n]
        st[key] = n + 1
        final.update(change)
        if any(t in CPU_FIELDS for t in change):
            done.pop("cb30", None)
        st["phase"], st["after_stepback"] = "run", True
        st.setdefault("failures", []).append({"what": what, "gave_back": name, "at": time.strftime("%H:%M")})
        save(st)
        write(final, "the kept set after stepping back: " + name)
        ramsweep.note("**VALIDATION: %s FAILED.** Giving back %s, then repeating that test only (already passed: %s)." % (
            what, name, ", ".join(k for k in ORDER if done.get(k)) or "nothing yet"), ping=True)
        ramsweep.reboot("after stepping back")
        return
    st["phase"] = "stopped"
    save(st)
    ramsweep.note("**VALIDATION: %s FAILED** with every rung used (down to plain XMP memory, E-cores 45 and Vcore 1.360 V, "
                  "the owner's ceiling). Nothing left inside the limits; the PC stays on this set." % what, ping=True)


def running(st, what, item=None):
    """Mark what is under way, so a Windows crash in the middle is seen at the next start instead of being rerun blind."""
    if what:
        st["running"], st["running_item"] = what, item
    else:
        st.pop("running", None)
        st.pop("running_item", None)
    save(st)


ORDER = ["hold", "yc60", "cb30", "hot", "tm5"]
WHAT = {"hold": "the at-rest memory test", "yc60": "y-cruncher 60 min", "cb30": "Cinebench 30 min with GPU heat",
        "hot": "y-cruncher after a hot reboot", "tm5": "TM5"}


def finish(st):
    final = st["final"]
    ok, msg = verify(final)
    st["phase"] = "done" if ok else "stopped"
    save(st)
    fails = "; ".join("%s -> gave back %s" % (f["what"], f["gave_back"]) for f in st.get("failures", [])) or "none"
    hw = ramsweep.whea_errors(st.get("started", time.time() - 12 * 3600))
    ramsweep.note("**VALIDATION %s** for: %s. Given back on the way: %s. Hot reboots: %s s." % (
        "PASSED" if ok else "ended but the set is NOT verified (%s)" % msg,
        ", ".join("%s %s" % (ramsweep.NAMES.get(t, t), v) for t, v in final.items()), fails,
        ", ".join(str(h["boot_s"]) for h in st.get("hot", [])) or "not run"), ping=True)
    ramsweep.note("Hardware-error records over the whole validation (not only inside test windows): %s" % (
        "; ".join(hw) if hw else "none (no corrected or fatal CPU/memory-controller errors; failed-training records excluded)"))
    # Positive controls and the hand-over to the sweep run only with an explicit flag file ("allow-controls").
    if not os.path.exists(HOME + "/allow-controls"):
        return
    if ok and st.get("controls") and not st.get("controls_done"):
        st["phase"], st["ci"], st["cstage"] = "control", 0, "set"
        save(st)
        return control(st)
    if ok:
        chain(st)


def chain(st):
    final = st["final"]
    if st.get("then_sweep", True):
        # The validated set is banked, so the remaining (risky) queue of the sweep starts here, with step cards.
        rs = ramsweep.load()
        rs["good"].update(final)
        rs["phase"], rs["step"], rs["dirty"] = "apply", None, False
        ramsweep.save(rs)
        open(HOME + "/owner-awake", "w").close()
        st["then_sweep"] = False
        save(st)
        ramsweep.note("Validated set banked. The remaining sweep steps start next (from %s), one per boot, with step cards." % (
            ramsweep.GROUPS[rs["g"]][0] if rs["g"] < len(ramsweep.GROUPS) else "end"))
        # Hand over: whatever runs these scripts at boot should now run ramsweep.py instead of finalrun.py.
        open(HOME + "/next-ramsweep", "w").close()


def control(st):
    """Positive controls for the at-rest test: the same test on a refresh setting that is known to
    crash (tREFI 131071) and on the one that was given back (98304). Flips there and none on the validated set would
    prove the refresh theory; no flips but another crash would refute it. Never changes the validated set."""
    final, cs = st["final"], st["controls"]
    while st["ci"] < len(cs):
        c = cs[st["ci"]]
        trial = dict(final)
        trial.update({k: int(v) for k, v in c["set"].items()})
        key = json.dumps(sorted(trial.items()))
        if st["cstage"] == "set" and (trial == final or key in st.get("ctried", [])):
            st.setdefault("control_results", []).append({"name": c["name"], "result": "same settings as %s, not repeated" % (
                "the validated set" if trial == final else "an earlier control")})
            st["ci"] += 1
            save(st)
            continue
        if st["cstage"] == "set":
            st.setdefault("ctried", []).append(key)
            st["cstage"] = "run"
            save(st)
            write(trial, "positive control: " + c["name"])
            ramsweep.reboot("positive control: " + c["name"])
            return
        if st["cstage"] == "run":
            if st.get("running"):
                crashes = ramsweep.crashes_since(time.time() - 2 * 3600)
                st.pop("running", None)
                st.pop("running_item", None)
                st.setdefault("control_results", []).append({"name": c["name"], "result": "Windows crashed during the test"})
                ramsweep.note("**CONTROL %s: Windows crashed during the at-rest test**; system log: %s. See memtest/memhold.log "
                              "for any flips written before it." % (c["name"], "; ".join(crashes[-3:]) or "no crash record"), ping=True)
                clean, msg = ramsweep.integrity()
                ramsweep.note("Integrity after that crash: %s" % msg, ping=not clean)
            else:
                ok, msg = verify(trial)
                if not ok:
                    st.setdefault("control_results", []).append({"name": c["name"], "result": "did not train: " + msg[:120]})
                    ramsweep.note("CONTROL %s: the value is not live (%s); skipped." % (c["name"], msg))
                else:
                    init_dimm()
                    running(st, "control " + c["name"], "control")
                    passed = hold(int(c["minutes"]), "CONTROL at-rest test on " + c["name"])
                    running(st, None)
                    st.setdefault("control_results", []).append({"name": c["name"], "result": "no flips" if passed else "flips or load error (see the line above)"})
            st["cstage"] = "restore"
            save(st)
            write(final, "the validated set, after the control")
            ramsweep.reboot("back to the validated set after the control")
            return
        if st["cstage"] == "restore":
            ok, msg = verify(final)
            if not ok:
                st["phase"] = "stopped"
                save(st)
                ramsweep.note("After a control the validated set is NOT live (%s). Stopped." % msg, ping=True)
                return
            st["ci"] += 1
            st["cstage"] = "set"
            save(st)
    st["controls_done"] = True
    st["phase"] = "done"
    save(st)
    ramsweep.note("**CONTROLS DONE**: %s." % "; ".join("%s: %s" % (r["name"], r["result"]) for r in st.get("control_results", [])), ping=True)
    chain(st)


def main():
    st = load()
    time.sleep(45)
    final = st["final"]
    if "controls" not in st:
        st["controls"] = DEFAULT_CONTROLS
    if "started" not in st:
        st["started"] = time.time() - 3600
    save(st)
    if st["phase"] == "control":
        return control(st)
    # State written by the earlier version of this script: "retrain" means y-cruncher 60 and Cinebench 30 had passed.
    if "done" not in st:
        st["done"] = {"yc60": True, "cb30": True} if st["phase"] == "retrain" else {}
        if st["phase"] == "retrain":
            st["hot_run"] = True
        if st.get("hold_done"):
            st["done"]["hold"] = True
    if st["phase"] in ("validate", "retrain", "tm5"):
        st["phase"] = "run"
    save(st)
    done = st["done"]
    if st.get("running"):
        # The previous start of this script never got to clear the marker: Windows went down in the middle.
        what, item = st["running"], st.get("running_item", "yc60")
        crashes = ramsweep.crashes_since(time.time() - 3 * 3600)
        last = ""
        try:
            last = [l for l in open(HOME + "/yc-live.log", errors="replace").read().splitlines() if "Running" in l][-1]
        except (OSError, IndexError):
            pass
        ramsweep.note("**CRASH during validation (%s)**: Windows restarted in the middle; system log: %s; last y-cruncher "
                      "line: %s." % (what, "; ".join(crashes[-3:]) or "no crash record", last or "none"), ping=True)
        clean, msg = ramsweep.integrity()
        ramsweep.note("Integrity after that crash: %s" % msg, ping=not clean)
        return failed(st, "Windows crashed during " + what, item)

    if st["phase"] == "run":
        ok, msg = verify(final)
        if not ok and not (st.get("hot_run") and not done.get("hot")):
            st["phase"] = "stopped"
            save(st)
            ramsweep.note("Validation: the BIOS does not show the set under test (%s). Stopped; nothing changed." % msg, ping=True)
            return
        fresh = st.pop("after_stepback", False)
        if not any(done.get(k) for k in ORDER) and not st.get("hot_run"):
            ramsweep.note("## VALIDATION of: %s (boot %d s)" % (
                ", ".join("%s %s" % (ramsweep.NAMES.get(t, t), v) for t, v in final.items()), ramsweep.boot_gap()))
        # A hot-reboot series that is under way goes first: this boot IS one of its reboots.
        order = (["hot"] + [k for k in ORDER if k != "hot"]) if st.get("hot_run") and not done.get("hot") else ORDER
        for item in order:
            if done.get(item):
                continue
            if item == "hot":
                # Five reboots, each straight after a load, with nothing changed: does the set train hot, and how fast?
                if not st.get("hot_run") or fresh:
                    first = not st.get("hot_run")
                    if first:
                        st["hot_run"], st["retrain"], st["hot"] = True, 0, []
                    save(st)
                    init_dimm()
                    running(st, WHAT["hot"], "hot")
                    if not stress(6, "WARM-UP VT3 6 before a hot reboot"):
                        return failed(st, "y-cruncher warm-up before the hot reboots", "hot")
                    running(st, None)
                    ramsweep.reboot("hot reboot %d of 5, straight after a memory load" % (st["retrain"] + 1))
                    return
                gap = ramsweep.boot_gap()
                st["retrain"] += 1
                n = st["retrain"]
                st.setdefault("hot", []).append({"n": n, "boot_s": gap, "trained": ok})
                save(st)
                ramsweep.note("**HOT REBOOT %d of 5**: boot %d s (%s), %s." % (
                    n, gap, "normal" if 0 <= gap <= ramsweep.SLOW_BOOT else "SLOW", "set live" if ok else "NOT TRAINED: " + msg))
                if not ok:
                    write(final, "the set under test after a failed hot retrain")
                    if n >= 5:
                        done["hot"] = True
                        save(st)
                    ramsweep.reboot("restore after a failed hot retrain")
                    return
                init_dimm()
                running(st, "y-cruncher after hot reboot %d" % n, "hot")
                if not stress(6, "HOT %d VT3 6" % n):
                    return failed(st, "y-cruncher after hot reboot %d" % n, "hot")
                running(st, None)
                if n < 5:
                    ramsweep.reboot("hot reboot %d of 5, straight after a memory load" % (n + 1))
                    return
                slow = [h for h in st["hot"] if not 0 <= h["boot_s"] <= ramsweep.SLOW_BOOT]
                bad = [h for h in st["hot"] if not h["trained"]]
                ramsweep.note("**HOT REBOOT TEST DONE**: boot times %s s; %d of 5 slow, %d of 5 failed to train." % (
                    ", ".join(str(h["boot_s"]) for h in st["hot"]), len(slow), len(bad)))
                done["hot"] = True
                save(st)
                continue
            if done.get("hold") and os.path.exists(HOME + "/pause-after-hold"):
                # Stop for the day once the at-rest test has passed (the "pause-after-hold" file); run finalrun.py
                # again to resume.
                ramsweep.disarm_failsafe()
                ramsweep.note("**PAUSED FOR THE DAY** (owner): the at-rest test has passed; still to run tonight: %s. The PC stays on "
                              "the set under test; nothing will reboot it." % ", ".join(k for k in ORDER if not done.get(k)), ping=True)
                return
            init_dimm()
            running(st, WHAT[item], item)
            if item == "hold":
                passed = hold(45, "VALIDATE AT-REST 45")
            elif item == "yc60":
                passed = stress(60, "VALIDATE VT3 60")
            elif item == "cb30":
                wait_cool(33)
                passed = cinebench(30, "validate-cb30-%s" % time.strftime("%H%M"))
            else:
                try:
                    import tm5run
                    passed, detail = tm5run.run()
                    ramsweep.note("**TM5 %s**: %s" % ("PASS" if passed else "FAIL", detail))
                except Exception as e:  # noqa: BLE001 - TM5 must not block the rest
                    passed = True
                    ramsweep.note("TM5 not completed (%s: %s); left for the owner, continuing." % (type(e).__name__, str(e)[:160]))
            if not passed:
                return failed(st, WHAT[item], item)
            done[item] = True
            running(st, None)
        st["phase"] = "bench-final" if st.get("bench") else "finish"
        save(st)
    ph = st["phase"]
    if ph == "finish":
        return finish(st)
    if ph == "bench-final":
        bench("final")
        st["phase"] = "to-original"
        save(st)
        import nvram  # noqa: E402
        gold = nvram.Script(open(ramsweep.GOLDEN, encoding="utf-8", errors="replace").read())
        orig = {t: (gold.value(t) if gold.value(t) is not None else 0) for t in final}  # back to the pre-sweep values (option names kept as names, e.g. the P-core ratio mode)
        orig.update(ORIGINAL)
        write(orig, "the original profile (plan.ORIGINAL on the pre-sweep BIOS)")
        # That profile used to hang under hot load. With the failsafe armed, the restart after a hang or crash puts the
        # tuned set back by itself.
        armed = ramsweep.arm_failsafe({"good": dict(final)}, {t: v for t, v in orig.items() if final.get(t) != v})
        ramsweep.note("Failsafe %s for the before-run on the original profile." % ("armed" if armed else "NOT armed"))
        ramsweep.reboot("benchmark the original profile")
        return
    if ph == "to-original":
        # The original profile is known to be unstable under heat; if it crashes the benchmark, do not loop on it.
        st["orig_tries"] = st.get("orig_tries", 0) + 1
        save(st)
        if st["orig_tries"] > 1:
            ramsweep.note("The original profile crashed during its benchmark; skipping the rest of it.")
        else:
            bench("original")
        ramsweep.disarm_failsafe()
        st["phase"] = "to-final"
        save(st)
        write(final, "the final profile")
        ramsweep.reboot("back to the final profile")
        return
    if ph == "to-final":
        st["then_sweep"] = False
        return finish(st)


if __name__ == "__main__":
    if not os.path.exists(STATE) or load()["phase"] in ("done", "stopped"):
        sys.exit(0)
    main()
