#!/usr/bin/env python3
"""Autonomous RAM (and CPU) step discovery through SCEWIN. Run it at every boot (for example as a systemd user
service in WSL, or a logon task); each start does one boot's worth of work and exits.

Each step: BIOS settings = golden script (the BIOS as it was before the sweep) + every value that has passed so far +
the step's own change. Written with SCEWIN through the elevated agent task (tools/windows/scewin-agent.ps1), then a
reboot. After the boot the effective values the BIOS reports are checked, then the tests run (latency/bandwidth,
y-cruncher memory-heavy stress, WHEA log). A failed test, a crash during a test, or a boot that came up on defaults
(Boot Failure Guard) counts as a fail: the group stops at its last good value and the next group starts. State and
journal are written before every reboot.

Setup: campaign/campaign.toml (see campaign.example.toml), the plan in campaign/plan.py, ram-state.json in the state
folder ({"g": 0, "k": 0, "good": {}, "phase": "apply", "step": null, "results": []}) and golden.txt (a SCEWIN export
taken before anything was changed). This is the reference campaign's script, kept close to how it ran: the harness
(tuner.py) is the general tool.
"""
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nvram  # noqa: E402
import settings  # noqa: E402
from plan import (COMMAND_RATE, CPU_TOKENS, EXPECT, FAST_FROM, GROUPS, NAMES, ECORE, PCORE_ALL, PCORE_MODE, PERCORE,  # noqa: E402
                  PROFILE_TOKEN, PROFILE_VALUE, READS_LOWER, RING, VCORE, VOLT)

HOME = settings.HOME
STATE = HOME + "/ram-state.json"
JOURNAL = HOME + "/ram-journal.md"
STAGE = settings.STAGE
GOLDEN = HOME + "/golden.txt"
YC = settings.YC
MEMLAT = settings.MEMLAT
PS = settings.PS
FS = settings.FS
ATUNE = settings.HELPER
YC_ARGS = settings.YC_ARGS
SLOW_BOOT = settings.SLOW_BOOT  # boot_gap() seconds (shutdown event to OS start); normal is ~45


def shown(tok, v):
    if tok in READS_LOWER and v:
        return str(v - READS_LOWER[tok])
    if tok == COMMAND_RATE:
        return "%dN" % v
    return "%.3fV" % (v / 1000) if tok in VOLT else str(v)


def now():
    return time.strftime("%H:%M:%S")


def note(line, ping=False):
    text = "- %s %s" % (now(), line)
    for path in [JOURNAL] + settings.JOURNAL_COPIES:
        try:
            with open(path, "a") as f:
                f.write(text + "\n")
                f.flush()
                os.fsync(f.fileno())  # a reboot seconds later once left this file ending in zero bytes
        except OSError:
            pass
    settings.shell("mirror_command", text + "\n")
    if ping:
        settings.shell("ping_command", "RAM sweep: " + line + "\n")


def load():
    with open(STATE) as f:
        return json.load(f)


def save(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE)


def scewin(request, timeout=180):
    for f in ("result.txt",):
        try:
            os.remove(os.path.join(STAGE, f))
        except FileNotFoundError:
            pass
    with open(os.path.join(STAGE, "request.txt"), "w") as f:
        f.write(request)
    subprocess.run([settings.SCHTASKS, "/Run", "/TN", settings.TASK_SCEWIN], capture_output=True)
    t0 = time.time()
    while time.time() - t0 < timeout:
        p = os.path.join(STAGE, "result.txt")
        if os.path.exists(p):
            # The helper writes the file in one go at the end, but it can be visible before its text is; wait for
            # the closing "done" line.
            try:
                text = open(p, encoding="utf-8-sig", errors="replace").read()
            except OSError:
                text = ""
            if "\ndone " in text or text.startswith("done "):
                return text
        time.sleep(2)
    return ""


def read_bios():
    out = scewin("export")
    if "exported successfully" not in out:
        return None
    return nvram.Script(open(os.path.join(STAGE, "export.txt"), encoding="utf-8", errors="replace").read())


def wanted(st, step):
    s = dict(st["good"])
    s.update(step or {})
    return s


def write_bios(settings):
    # SCEWIN matches a question by its prompt, and the prompt carries the value the BIOS shows (e.g. "tRFC 922"),
    # so the script must be the current export. Every value that differs from the golden script is copied over
    # first, which also restores everything after Boot Failure Guard loaded defaults.
    cur = read_bios()
    if cur is None:
        return False, "could not read the BIOS"
    gold = nvram.Script(open(GOLDEN, encoding="utf-8", errors="replace").read())
    for tok, blocks in gold.by_token.items():
        if len(blocks) != 1 or len(cur.by_token.get(tok, [])) != 1 or tok in settings:
            continue
        gv = gold.value(tok)
        if gv is not None and cur.value(tok) != gv:
            try:
                cur.set(tok, gv)
            except (ValueError, KeyError):
                pass
    sc = cur
    for tok, val in settings.items():
        sc.set(tok, val)
    with open(os.path.join(STAGE, "import.txt"), "w", encoding="utf-8", newline="") as f:
        f.write(sc.text())
    out = scewin("import", timeout=300)
    if "Password accepted" not in out:
        return False, out[-400:]
    after = nvram.Script(open(os.path.join(STAGE, "after.txt"), encoding="utf-8", errors="replace").read())
    bad = [t for t, v in settings.items() if after.value(t) != v]
    return not bad, "not stored: %s" % bad if bad else "stored"


def arm_failsafe(st, step):
    """Before booting into a step: leave the last good BIOS script for the startup failsafe task. After that boot
    the BIOS prompts show the step's values, and SCEWIN matches by prompt, so those labels are rewritten to match."""
    cur = read_bios()
    if cur is None:
        return False
    gold = nvram.Script(open(GOLDEN, encoding="utf-8", errors="replace").read())
    good = wanted(st, None)
    for tok, blocks in gold.by_token.items():
        if len(blocks) != 1 or len(cur.by_token.get(tok, [])) != 1 or tok in good:
            continue
        gv = gold.value(tok)
        if gv is not None and cur.value(tok) != gv:
            try:
                cur.set(tok, gv)
            except (ValueError, KeyError):
                pass
    for tok, val in good.items():
        cur.set(tok, val)
    for tok, val in step.items():
        if cur.label(tok) is not None:
            cur.set_label(tok, shown(tok, val))
        if tok not in good:
            gv = gold.value(tok)
            cur.set(tok, gv if gv is not None else 0)
    # Failsafe v2 takes a fresh export itself and only needs the numbers: every kept value, the pre-sweep value of
    # anything else this step touches, and the CPU fields. lastgood.txt stays as its fallback.
    vals = {}
    for tok in list(good) + list(step) + sorted(CPU_TOKENS):
        v = good.get(tok, gold.value(tok))
        if tok in PERCORE and tok not in good:
            # The failsafe cannot switch the ratio mode back; every core at the kept all-core ratio is the same thing.
            v = good.get(PCORE_ALL, gold.value(PCORE_ALL))
        if isinstance(v, int):
            vals[tok] = v
    for name, text, kw in (("lastgood.txt", cur.text(), {"encoding": "utf-8", "newline": ""}),
                           ("lastgood-values.txt", "".join("%s=%d\n" % kv for kv in vals.items()), {}),
                           ("armed", describe(step).replace(" ", "_"), {})):
        with open(FS + "/" + name, "w", **kw) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
    return True


def disarm_failsafe():
    for name in ("armed", "seen"):
        try:
            os.remove(FS + "/" + name)
        except FileNotFoundError:
            pass


def step_card(st, step, name):
    """What is being set, what the owner may see and the exact fix, for whoever helps the owner when the PC cannot
    report (a second machine, a phone). Written to current-step.md in the state folder and, for risky groups, passed
    to [notify] step_card_command. Sent before the reboot; failures to send never stop the sweep."""
    cpu = any(t in CPU_TOKENS or t in PERCORE or t == PCORE_MODE for t in step)
    # The value to put back is the kept one, else what the pre-sweep (golden) export stores. Never "Auto" for a CPU
    # field: Vcore on Auto is adaptive, far above the owner's 1.36 V ceiling (a card once said "Auto" here).
    gold = nvram.Script(open(GOLDEN, encoding="utf-8", errors="replace").read())

    def back(t):
        v = st["good"].get(t, gold.value(t))
        if t == VCORE:
            return "Fixed Voltage back to %.3f V (leave CPU Core/Cache Voltage on Fixed Mode, never Auto)" % (v / 1000)
        if t == RING:
            return "CPU Cache Ratio back to the number %s" % v
        if t in (PCORE_ALL, ECORE):
            return "%s (All Core) back to the number %s" % ("CPU P-Core Ratio" if t == PCORE_ALL else "CPU E-Core Ratio", v)
        return "%s back to %s" % (NAMES.get(t, t), shown(t, v) if v not in (None, 0) else "Auto")
    fix = "; ".join(back(t) for t in step)
    if PCORE_MODE in step and PCORE_MODE not in st["good"]:
        fix = "CPU P-Core Ratio back to 'All Core' (the All Core number stays %s); nothing else" % st["good"].get(
            PCORE_ALL, gold.value(PCORE_ALL))
    lines = ["# STEP CARD %s %s" % (time.strftime("%Y-%m-%d"), now()), "",
             "Being set now: **%s** (group %s). Reboot follows within seconds." % (describe(step), name),
             "Expectation: %s" % EXPECT.get(name, "soft failure expected (test error or fallback train); should not need the owner."),
             "Normal: Windows is back about 2.5 minutes after the reboot, then two 8-minute memory runs%s." % (
                 " and 10 minutes of Cinebench with GPU heat" if "cpu:" in name else ""),
             "", "If it goes wrong, the ONLY thing to undo is this step:", "",
             "1. Blue screens in a loop at startup, Windows Recovery (winload.efi errors) or the Clover menu: wait 3 minutes (the "
             "failsafe may fix it by itself). If it keeps looping: Del at power-on, OC Tweaker > %s, set %s, F10. Change nothing else, "
             "do not set other fields to Auto." % ("CPU Configuration / Voltage Configuration" if cpu else "DRAM Configuration", fix),
             "2. Black screen, no picture: wait 12 minutes first (the board retries training by itself and that has recovered "
             "unattended before). Still black: hold power 5 s, power on, up to 5 times. When the picture returns Windows starts on slow "
             "fail-safe memory and the sweep restores the last good set by itself.",
             "3. Frozen picture in Windows: hold power 5 s, power on. The sweep restores by itself.",
             "", "Last good set (what the sweep restores): %s; CPU P-cores %sx, ring %s, Vcore FIXED %.3f V." % (", ".join(
                 "%s %s" % (NAMES.get(t, t), v) for t, v in st["good"].items()), st["good"].get(PCORE_ALL, gold.value(PCORE_ALL)),
                 st["good"].get(RING, gold.value(RING)), st["good"].get(VCORE, gold.value(VCORE)) / 1000),
             "If Windows is up but the sweep says nothing for 5 minutes, it is waiting on a restore-confirm boot; do not touch the BIOS."]
    card = "\n".join(lines) + "\n"
    try:
        with open(HOME + "/current-step.md", "w") as f:
            f.write(card)
        if name.startswith("risky:"):
            settings.shell("step_card_command", card)
        return True
    except Exception as e:  # noqa: BLE001 - the card is best effort
        note("Step card not sent: %s" % e)
        return False


def release_gpu():
    """Before a restart, close what holds the graphics card ([reboot] in the config). Three 0x9F shutdown hangs
    (six-minute "slow boots") on the reference machine named the GPU driver, with a model server holding about 31 GB
    of VRAM being torn down while Windows was already shutting down. Best effort; never blocks the restart."""
    try:
        closed = []
        for unit in settings.get("reboot", "stop_units", []):
            subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True, timeout=120)
            closed.append(unit)
        tl = subprocess.run([settings.TASKLIST], capture_output=True, text=True, timeout=20).stdout.lower()
        for exe in settings.get("reboot", "close_processes", []):
            if exe.lower() in tl:
                subprocess.run([settings.TASKKILL, "/IM", exe], capture_output=True, timeout=20)
                closed.append(exe)
        if closed:
            time.sleep(4)
        return ", ".join(closed)
    except Exception:  # noqa: BLE001
        return ""


def reboot(why):
    release_gpu()
    with open(HOME + "/expect-down-until", "w") as f:
        f.write(str(int(time.time()) + 4 * 60) + "\n")
    note("Rebooting (%s). DIMMs at reboot: %s." % (why, dimm_now()))
    subprocess.run(["sync"])
    time.sleep(3)
    subprocess.run([settings.SHUTDOWN, "/r", "/t", "5", "/c", "RAM sweep: " + why])
    time.sleep(600)


def whea_errors(since_epoch):
    """Corrected and fatal hardware-error records since a time, whole session. Id 1 (the firmware's record of a failed
    memory training) is left out; ids 18/19/20/46/47 are CPU or memory-controller errors."""
    start = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(since_epoch))
    r = subprocess.run([PS, "-NoProfile", "-Command",
                        "Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-WHEA-Logger';"
                        " Id=18,19,20,46,47; StartTime=[datetime]'%s'} -ErrorAction SilentlyContinue | Sort-Object TimeCreated |"
                        " ForEach-Object { $x=[xml]$_.ToXml(); $d=@{}; foreach ($n in $x.Event.EventData.Data) { $d[$n.Name]=$n.'#text' };"
                        " $_.TimeCreated.ToString('MM-dd HH:mm:ss') + ' id ' + $_.Id + ' APIC ' + $d['ApicId'] + ' bank ' +"
                        " $d['MCABank'] + ' status ' + $d['MciStat'] }" % start], capture_output=True, text=True)
    return [l.strip() for l in r.stdout.splitlines() if l.strip()]


def whea_count(minutes):
    r = subprocess.run([PS, "-NoProfile", "-Command",
                        "@(Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-WHEA-Logger';"
                        " StartTime=(Get-Date).AddMinutes(-%d)} -ErrorAction SilentlyContinue).Count" % minutes],
                       capture_output=True, text=True)
    try:
        return int(r.stdout.strip())
    except ValueError:
        return -1


DIMM_SINCE = "00:00:00"


def agent(cmd):
    """One command to the elevated helper (tools/windows/helper-agent.ps1)."""
    ack = ATUNE + "/ack.txt"
    try:
        os.remove(ack)
    except FileNotFoundError:
        pass
    with open(ATUNE + "/cmd.txt", "w") as f:
        f.write(cmd)
    for _ in range(30):
        if os.path.exists(ack):
            return True
        time.sleep(1)
    subprocess.run([settings.SCHTASKS, "/Run", "/TN", settings.TASK_HELPER], capture_output=True)
    for _ in range(30):
        if os.path.exists(ack):
            return True
        time.sleep(1)
    return False


def dimm_summary():
    """Peak and last DIMM SPD hub temperatures from this test's log, per slot."""
    try:
        rows = [l.strip().split(",") for l in open(ATUNE + "/dimmlog.csv") if l.strip()]
        rows = [r for r in rows if r[0] >= DIMM_SINCE]
    except OSError:
        return "DIMM temps: no log"
    peak, last = {}, {}
    for r in rows:
        for cell in r[1:]:
            slot, t = cell.split(":")
            t = float(t)
            peak[slot] = max(peak.get(slot, -99), t)
            last[slot] = t
    if not peak:
        return "DIMM temps: no readings"
    return "DIMM peak %s, at end %s (C)" % ("/".join("%.1f" % peak[k] for k in sorted(peak)),
                                          "/".join("%.1f" % last[k] for k in sorted(last)))


def integrity():
    """chkdsk /scan C:, sfc /scannow and the WSL disk's kernel log. Returns (clean, summary). Up to ~20 minutes."""
    def read(name):
        try:
            return open(ATUNE + "/" + name, "rb").read().replace(b"\x00", b"").decode("utf-8", "replace")
        except OSError:
            return ""
    for name in ("chkdsk-C.txt", "sfc.txt"):
        try:
            os.remove(ATUNE + "/" + name)
        except OSError:
            pass
    agent("chkdsk C")
    time.sleep(2)
    agent("sfc")
    c = f = ""
    for _ in range(80):
        time.sleep(15)
        c, f = read("chkdsk-C.txt"), read("sfc.txt")
        if ("found no problems" in c or "found problems" in c) and re.search(r"Windows Resource Protection (did not find|found|could not)", f):
            break
    ext4 = subprocess.run("dmesg | grep -i -E 'EXT4-fs (error|warning)|I/O error' | tail -2", shell=True,
                          capture_output=True, text=True).stdout.strip()
    c_ok = "found no problems" in c
    f_ok = "did not find any integrity violations" in f
    msg = "chkdsk /scan C: %s; sfc: %s; WSL ext4: %s" % (
        "no problems" if c_ok else "NOT CLEAN or no result (%s)" % c.strip()[-120:].replace("\n", " "),
        "no integrity violations" if f_ok else "NOT CLEAN or no result (%s)" % f.strip()[-120:].replace("\n", " "),
        "no errors" if not ext4 else ext4[:160])
    return c_ok and f_ok and not ext4, msg


def dimm_now():
    """DIMM temperatures right now, for the reboot line: slow trains follow hot reboots, so every reboot gets a number."""
    try:
        if not agent("dimm"):
            return "no reading (helper not running)"
        ack = open(ATUNE + "/ack.txt", encoding="utf-16").read()
        m = re.search(r"0:([\d.]+),2:([\d.]+)", ack)
        if not m or float(m.group(1)) == 0:
            return "no reading (sensors not initialised)"
        return "%s/%s C" % (m.group(1), m.group(2))
    except Exception as e:  # noqa: BLE001 - never block a reboot on a temperature
        return "no reading (%s)" % type(e).__name__


def test():
    """Returns (passed, summary)."""
    global DIMM_SINCE
    DIMM_SINCE = time.strftime("%H:%M:%S")
    try:
        os.remove(ATUNE + "/dimmlog.csv")
    except (FileNotFoundError, PermissionError):
        pass  # a logger from the previous test may still hold it; only rows from now on are used
    # The DIMM sensors read 0 after a cold boot until HWiNFO has initialised the SPD hubs once.
    agent("dimm")
    try:
        ack = open(ATUNE + "/ack.txt", encoding="utf-16").read()
    except (OSError, UnicodeError):
        ack = ""
    if ":0.00" in ack or "ok dimm" not in ack:
        agent("hwinfo")
        time.sleep(12)
        agent("enterproc HWiNFO64")
        time.sleep(25)
        agent("hwstop")
        time.sleep(2)
    agent("dimmlog 600")
    lat = subprocess.run([MEMLAT], capture_output=True, text=True, timeout=300).stdout.split()
    lat_s = "%s ns, %s GB/s" % (lat[0], lat[1]) if len(lat) >= 2 else "latency test failed"
    t0 = time.time()
    try:
        r = subprocess.run([YC] + YC_ARGS, capture_output=True, text=True, timeout=900,
                           cwd=os.path.dirname(YC), errors="replace")
        out, code = r.stdout + r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        out, code = "timeout", -1
    out = re.sub(r"\x1b\[[0-9;]*m", "", out)
    passed_n = len(re.findall(r"Passed", out))
    bad = [l.strip() for l in out.splitlines()
           if re.search(r"(?i)error|fail|mismatch|exception", l) and "Stop on Error" not in l]
    whea = whea_count(int((time.time() - t0) / 60) + 2)
    ok = code == 0 and passed_n >= 3 and not bad and whea == 0
    detail = "%s; y-cruncher exit %d, %d sections passed%s; WHEA %d; %s" % (
        lat_s, code, passed_n, ", " + bad[0][:80] if bad else "", whea, dimm_summary())
    with open(HOME + "/ram-last-yc.txt", "w") as f:
        f.write(out[-20000:])
    return ok, detail


def boot_gap():
    """Seconds between the last Windows shutdown and the next boot: the POST and memory training time."""
    r = subprocess.run([PS, "-NoProfile", "-Command",
                        "$e = Get-WinEvent -FilterHashtable @{LogName='System'; Id=12,13; StartTime=(Get-Date).AddHours(-1)}"
                        " -ErrorAction SilentlyContinue | Sort TimeCreated; $b = $e | ? Id -eq 12 | Select -Last 1;"
                        " $s = $e | ? { $_.Id -eq 13 -and $_.TimeCreated -lt $b.TimeCreated } | Select -Last 1;"
                        " [int]($b.TimeCreated - $s.TimeCreated).TotalSeconds"], capture_output=True, text=True)
    try:
        return int(r.stdout.strip())
    except ValueError:
        return -1


def crashes_since(epoch):
    """Bugchecks and unclean restarts Windows logged after the step was set (Kernel-Power 41, WER 1001, 6008).
    The window starts 20 s after the set time, never before it: records from an earlier crash that the previous
    boot logged must not count against the next step (a false CRASH for ring 51 on 13 Nov came from that)."""
    start = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(epoch + 20))
    r = subprocess.run([PS, "-NoProfile", "-Command",
                        "Get-WinEvent -FilterHashtable @{LogName='System'; Id=41,1001,6008; StartTime=[datetime]'%s'}"
                        " -ErrorAction SilentlyContinue | %% { $_.Id.ToString() + ' ' + (($_.Message -split [char]10)[0]).Substring(0,"
                        " [math]::Min(120, (($_.Message -split [char]10)[0]).Length)) }" % start], capture_output=True, text=True)
    return [l.strip() for l in r.stdout.splitlines() if l.strip()]


def fail_step(st):
    """After a failed step: an "alt:" group moves to its next alternative, any other group ends."""
    if st["g"] < len(GROUPS) and "alt:" in GROUPS[st["g"]][0]:
        st["k"] += 1
    else:
        st["g"], st["k"] = st["g"] + 1, 0
    st["step"], st["phase"] = None, "apply"


def cpu_test(step):
    """Cinebench with GPU heat through the core tool (shakedown-cli); PASS only if it says PASS."""
    # Shakedown turns every character that is not a letter, digit or "-" into "-" in the run name.
    label = "sweep-" + re.sub(r"[^A-Za-z0-9-]", "-", describe(step).replace(" ", ""))
    minutes = 30 if st_group_name().find("cb30") >= 0 else 10
    # Near the voltage floor 30 minutes (earlier failures came at 15 and 26 minutes).
    import heat
    heat.run(label, describe(step) + ", GPU heat", minutes)
    rows = [l for l in open(settings.SD + "/ledger.csv", errors="replace") if label in l]
    last = rows[-1].split(",") if rows else []
    ok = bool(last) and "PASS" in rows[-1]
    extra = ""
    if PCORE_MODE in step:
        # No live value in the BIOS for per-core ratios: read the measured clocks from the Shakedown run file.
        import glob
        files = sorted(glob.glob(settings.SD + "/runs/*%s.txt" % label))
        mhz = {}
        if files:
            for l in open(files[-1], errors="replace"):
                m = re.match(r"\s*P(\d)\s+(\d+)\s*/", l)
                if m:
                    mhz[int(m.group(1))] = int(m.group(2))
        want = {i: step[t] * 100 for i, t in enumerate(PERCORE)}
        applied = bool(mhz) and all(abs(mhz.get(i, 0) - want[i]) < 60 for i in want)
        extra = "; measured P-core MHz %s (%s)" % (" ".join(str(mhz.get(i, "?")) for i in range(8)),
                                                  "mix applied" if applied else "MIX NOT APPLIED")
        ok = ok and applied
    return ok, "Cinebench+GPU heat: %s%s" % (",".join(last[4:16]).strip() if last else "no ledger line", extra)


def st_group_name():
    try:
        st = load()
        return GROUPS[st["g"]][0] if st["g"] < len(GROUPS) else ""
    except Exception:  # noqa: BLE001
        return ""


def describe(step):
    if PCORE_MODE in step:
        at57 = [i for i, t in enumerate(PERCORE) if step.get(t) == 57]
        return "P-cores 58 mix (%s at 57)" % " ".join("P%d" % i for i in at57)
    return ", ".join("%s %s%s" % (NAMES.get(t, t), v, " (entered; reads %s)" % shown(t, v) if t in READS_LOWER and v else "")
                     for t, v in step.items())


def next_step(st):
    while st["g"] < len(GROUPS):
        name, steps = GROUPS[st["g"]]
        if name.startswith("risky:") and st["k"] < len(steps) and not os.path.exists(HOME + "/owner-awake"):
            return "WAIT"
        if st["k"] < len(steps):
            if not name.startswith("control:") and all(st["good"].get(t) == v for t, v in steps[st["k"]].items()):  # already kept
                st["k"] += 1
                continue
            return steps[st["k"]]
        st["g"], st["k"] = st["g"] + 1, 0
    return None


def main():
    st = load()
    time.sleep(45)  # let the desktop and the agents start after a boot
    sc = None
    for _ in range(4):
        sc = read_bios()
        if sc is not None:
            break
        time.sleep(30)
    if sc is None:
        note("Could not read the BIOS settings (ScewinAgent). Stopping; nothing changed.", ping=True)
        return
    step = st.get("step")

    if os.path.exists(FS + "/restored"):
        note("The startup failsafe task restored the last good settings (%s). Log: %s" % (
            open(FS + "/restored").read().strip(), open(FS + "/failsafe.log", encoding="utf-8-sig", errors="replace").read()[-300:]))
        os.remove(FS + "/restored")
    if st["phase"] in ("booting", "testing"):
        crashed = crashes_since(st.get("set_at", time.time() - 3600))
        if crashed:
            why = "Windows crashed after this step was set: %s" % "; ".join(crashed[:3])
            note("**CRASH %s: %s.**" % (describe(step), why), ping=True)
            st["results"].append({"step": step, "pass": False, "why": why, "kind": "crash"})
            fail_step(st)
            st["dirty"] = True
            save(st)

    if st["phase"] == "recheck":
        want = wanted(st, step)
        mismatch = {NAMES.get(t, t): (sc.label(t), v) for t, v in want.items()
                    if sc.label(t) is not None and sc.label(t) != shown(t, v)}
        gap = boot_gap()
        first, detail = st.get("boot_s"), st.get("detail", "")
        alt = "alt:" in GROUPS[st["g"]][0] if st["g"] < len(GROUPS) else False
        # Owner's decision (13 Nov 23:20): a value that passes its tests and trains, even slowly, is KEPT, with the
        # caveat documented (caveats.md). Only a control boot that does not train at all drops it.
        if sc.value(PROFILE_TOKEN) != PROFILE_VALUE or mismatch:
            why = ("tests passed, but the control reboot (hot, unchanged) did not train: %s; first boot %s s. Proven unreliable. %s" % (
                mismatch, first, detail))
            note("**FAIL %s**: %s" % (describe(step), why), ping=True)
            st["results"].append({"step": step, "pass": False, "why": why, "boot_s": first, "control_s": gap})
            fail_step(st)
            st["dirty"] = True
        else:
            if gap > SLOW_BOOT or gap < 0:
                why = ("KEPT WITH CAVEAT (owner's rule): slow first boot (%s s) and the control reboot, hot and unchanged, was slow "
                       "again (%d s), so this value trains slowly when hot; %s" % (first, gap, detail))
                st.setdefault("caveats", []).append({"step": step, "first_s": first, "control_s": gap})
            else:
                why = "slow first boot (%s s) but the control reboot, hot and unchanged, was normal (%d s); %s" % (first, gap, detail)
            st["results"].append({"step": step, "pass": True, "why": why, "boot_s": first, "control_s": gap})
            st["good"].update(step)
            if alt:
                st["g"], st["k"] = st["g"] + 1, 0
            else:
                st["k"] += 1
            st["step"], st["phase"] = None, "apply"
            note("**PASS %s**: %s" % (describe(step), why))
        save(st)

    if st["phase"] == "restoring":
        # A failed step was undone by writing the last good set and rebooting. Nothing new is written until this
        # boot shows that set live again (fault found 13 Nov: tCL 36 was written straight from a failing session).
        want = wanted(st, None)
        mismatch = {NAMES.get(t, t): (sc.label(t), v) for t, v in want.items()
                    if sc.label(t) is not None and sc.label(t) != shown(t, v)}
        gap = boot_gap()
        if sc.value(PROFILE_TOKEN) != PROFILE_VALUE or mismatch:
            st["phase"] = "stopped"
            save(st)
            note("**RESTORE NOT CONFIRMED** (boot %d s): the last good set is not what the BIOS shows: %s. Stopped; nothing more written." % (gap, mismatch), ping=True)
            return
        note("Restore confirmed by export (boot %d s%s): last good set is live. Going on." % (gap, "; SLOW TRAIN" if gap > SLOW_BOOT else ""))
        last = st["results"][-1] if st["results"] else {}
        if last.get("kind") == "crash" or "restarted during the test" in last.get("why", ""):
            # Rule: disk and system-file checks after every unclean stop, on the restored set, before anything else.
            clean, msg = integrity()
            note("Integrity after the unclean stop (%s): %s" % (describe(last.get("step") or {}), msg), ping=not clean)
            if not clean:
                st["phase"] = "stopped"
                save(st)
                return
        st["phase"] = "apply"
        save(st)

    if st["phase"] == "booting":
        want = wanted(st, step)
        labels = {t: sc.label(t) for t in want}
        mismatch = {t: (labels[t], v) for t, v in want.items() if labels[t] is not None and labels[t] != shown(t, v)}
        xmp = sc.value(PROFILE_TOKEN)
        if xmp != PROFILE_VALUE or mismatch:
            gap = boot_gap()
            if xmp != PROFILE_VALUE:
                kind, why = "NO TRAIN", "came up on defaults (Boot Failure Guard), boot took %d s" % gap
            elif any(t not in step for t in mismatch) or gap > 150:
                kind, why = "NO TRAIN", "memory training fell back to safe timings (boot %d s, normal ~45 s; values it did not set changed too): %s" % (gap, mismatch)
            else:
                kind, why = "NOT APPLIED", "BIOS adjusted the value without retraining (boot %d s): %s" % (gap, mismatch)
            note("**%s %s: %s. No stress test ran.**" % (kind, describe(step), why), ping=kind == "NO TRAIN" and xmp != PROFILE_VALUE)
            st["results"].append({"step": step, "pass": False, "why": why})
            fail_step(st)
            save(st)
            disarm_failsafe()
            ok, msg = write_bios(wanted(st, None))
            note("Restoring last good settings: %s." % msg)
            st["phase"], st["dirty"] = ("restoring" if ok else "stopped"), False
            save(st)
            if not ok:
                note("Could not write the restore. Stopped.", ping=True)
                return
            reboot("restore after a failed step")
            return
        st["phase"] = "testing"
        gap = boot_gap()
        st["boot_s"] = gap
        save(st)
        disarm_failsafe()
        note("TESTING %s (BIOS confirms it; boot took %d s%s). If nothing follows, the PC crashed during this test." % (
            describe(step), gap, ", SLOW TRAIN: normal is ~45 s, so training needed retries and this value is marginal even if it passes" if gap > SLOW_BOOT else ""))
        ok, detail = test()
        # Access timings fail fast and loudly, so the last probes get one 8-minute run each; whatever is kept is then
        # confirmed by the re-validation (at-rest test, 60-minute run), not by a second short run.
        names = [g[0] for g in GROUPS]
        single = FAST_FROM in names and st["g"] >= names.index(FAST_FROM)
        if ok and single:
            detail = "one run (confirmation comes from the re-validation): " + detail
        elif ok:
            note("First run passed (%s); confirming with a second run." % detail)
            ok, detail2 = test()
            detail = "run 1 and run 2 passed; run 2: " + detail2 if ok else "run 1 passed, run 2 FAILED: " + detail2
        if ok and st["g"] < len(GROUPS) and "cpu:" in GROUPS[st["g"]][0]:
            ok2, d2 = cpu_test(step)
            ok, detail = ok2, detail + "; " + d2
        if ok and st.get("boot_s", 0) > SLOW_BOOT:
            # Control: a slow first boot may come from the change itself or from hot sticks,
            # not from the value. Reboot once more, hot and unchanged; only a second slow boot proves it marginal.
            st["phase"], st["detail"] = "recheck", detail
            save(st)
            note("Tests passed for %s after a slow first boot (%d s): %s. CONTROL: rebooting now, hot and with nothing changed; "
                 "a normal boot keeps the value, a second slow boot keeps it with a documented caveat, a failed train drops it." % (describe(step), st["boot_s"], detail))
            reboot("control reboot, nothing changed")
            return
        st["results"].append({"step": step, "pass": ok, "why": detail, "boot_s": st.get("boot_s")})
        if not ok:
            st["dirty"] = True
        alt = "alt:" in GROUPS[st["g"]][0] if st["g"] < len(GROUPS) else False
        if ok:
            st["good"].update(step)
            if alt:
                st["g"], st["k"] = st["g"] + 1, 0
            else:
                st["k"] += 1
            note("**PASS %s**: %s" % (describe(step), detail))
        elif alt:
            st["k"] += 1
            note("**FAIL %s**: %s. Trying the next alternative." % (describe(step), detail), ping=True)
        else:
            st["g"], st["k"] = st["g"] + 1, 0
            note("**FAIL %s**: %s. Group stops at its last good value." % (describe(step), detail), ping=True)
        st["step"], st["phase"] = None, "apply"
        if not ok and st["g"] < len(GROUPS) and GROUPS[st["g"]][0].startswith("control:") and st["k"] >= len(GROUPS[st["g"]][1]):
            st["phase"] = "stopped"
            note("**CONTROL FAILED on every alternative**: the kept base itself does not pass. Sweep stopped; the latest "
                 "additions have to be stepped back by hand.", ping=True)
        save(st)

    elif st["phase"] == "testing":
        why = "the PC restarted during the test (crash or hang)"
        note("**FAIL %s: %s.**" % (describe(step), why), ping=True)
        st["results"].append({"step": step, "pass": False, "why": why})
        fail_step(st)
        st["dirty"] = True
        save(st)

    if st["phase"] == "apply" and st.get("dirty"):
        # The failed step's values are still live. Put the last good set back and confirm it on the next boot
        # before anything else is set.
        disarm_failsafe()
        ok, msg = write_bios(wanted(st, None))
        note("Restoring last good settings after the failed step: %s." % msg)
        st["phase"], st["dirty"] = ("restoring" if ok else "stopped"), False
        save(st)
        if not ok:
            note("Could not write the restore. Stopped.", ping=True)
            return
        reboot("restore after a failed step")
        return

    if st["phase"] == "apply":
        nxt = next_step(st)
        if nxt == "WAIT":
            st["phase"] = "waiting-owner"
            save(st)
            note("Paused before %s: it can stop the PC from training (needs the owner if it hangs). Create owner-awake in the state folder and start ramsweep again to go on." % GROUPS[st["g"]][0], ping=True)
            return
        if nxt is None:
            st["phase"] = "done"
            save(st)
            ok, msg = write_bios(wanted(st, None))
            good = ", ".join("%s %s" % (NAMES.get(t, t), v) for t, v in st["good"].items()) or "none"
            note("**DISCOVERY DONE.** Kept: %s. BIOS set to that (%s). Next: long validation, then the file checks." % (
                good, msg), ping=True)
            reboot("apply the discovery result")
            return
        st["step"], st["phase"], st["set_at"] = nxt, "booting", time.time()
        save(st)
        ok, msg = write_bios(wanted(st, nxt))
        if not ok:
            note("Could not write %s (%s). Stopping." % (describe(nxt), msg), ping=True)
            st["phase"] = "stopped"
            save(st)
            return
        armed = arm_failsafe(st, nxt)
        carded = step_card(st, nxt, GROUPS[st["g"]][0])
        note("SET %s (%s; failsafe %s; step card %s)." % (describe(nxt), msg, "armed" if armed else "NOT armed",
                                                         "written" if carded else "NOT written"))
        reboot("test " + describe(nxt))


if __name__ == "__main__":
    st = load() if os.path.exists(STATE) else None
    if st is None or st["phase"] in ("done", "stopped"):
        sys.exit(0)
    main()
