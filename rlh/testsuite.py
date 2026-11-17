"""Tests as pluggable commands with pass/fail parsers, plus temperature watchdogs.

Each test returns {"name", "status", "summary", "metrics"} where status is
  PASS, FAIL (the test found an error), ABORT (a safety limit stopped it) or ERROR (the tool could not run:
  a tool problem is never reported as a hardware failure).
"""
import os
import re
import shlex
import subprocess
import time

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _args(cmd):
    return cmd if isinstance(cmd, list) else shlex.split(cmd, posix=os.name != "nt")


# Parsers (pure functions, unit tested)

def parse_ycruncher(out, code, min_passed=3):
    """y-cruncher stress output. Its settings banner prints "Stop on Error: Enabled", which a naive error search
    counts as a failure (our first baseline was wrongly marked FAIL that way), so that line is excluded."""
    out = ANSI.sub("", out or "")
    passed = len(re.findall(r"\bPassed\b", out))
    bad = [l.strip() for l in out.splitlines()
           if re.search(r"(?i)error|fail|mismatch|exception", l) and not re.search(r"(?i)stop on error", l)]
    ok = code == 0 and passed >= min_passed and not bad
    summary = "y-cruncher exit %s, %d sections passed%s" % (code, passed, ", " + bad[0][:100] if bad else "")
    return ("PASS" if ok else "FAIL"), summary, {"yc_passed": passed}


def parse_memlat(out, code):
    nums = re.findall(r"-?\d+(?:\.\d+)?", out or "")
    if code != 0 or len(nums) < 2:
        return "FAIL", "memlat failed (exit %s)" % code, {}
    lat, bw = float(nums[0]), float(nums[1])
    return "PASS", "%.1f ns, %.1f GB/s" % (lat, bw), {"latency_ns": lat, "read_gbps": bw}


def parse_shakedown(out, code):
    last = [l for l in (out or "").splitlines() if re.search(r"\b(PASS|FAIL)\b", l)]
    tail = last[-1].strip()[:160] if last else ""
    if code == 0:
        return "PASS", "Shakedown PASS %s" % tail, {}
    if code == 1:
        return "FAIL", "Shakedown FAIL %s" % tail, {}
    return "ERROR", "Shakedown could not run (exit %s)" % code, {}


def parse_memhold(out, code):
    """memhold (tools/memhold.c), the at-rest test: data held untouched depends on refresh alone, so any flip is a
    refresh or retention fault. Exit 0 no flips, 2 flips, 1 could not run."""
    lines = [l.strip() for l in (out or "").splitlines() if l.strip()]
    flips = [l for l in lines if re.search(r"(^|\s)FLIP\s", l)]
    done = [l for l in lines if "memhold done" in l]
    unlocked = any("WARNING: unlocked pages" in l for l in lines)
    m = {"flips": len(flips)}
    if code == 1 or (code not in (0, 2) and not done):
        return "ERROR", "memhold could not run (exit %s)" % code, m
    if flips or code == 2:
        return "FAIL", "memhold: %d flip line(s); first: %s" % (len(flips), flips[0].split("FLIP", 1)[-1][:110] if flips else "?"), m
    if not done:
        return "ERROR", "memhold did not finish (exit %s)" % code, m
    note = "; WARNING: not every page was locked, flips may be hidden" if unlocked else ""
    return "PASS", "memhold: %s%s" % (done[-1].split("memhold done: ")[-1], note), m


TM5_STARTED = re.compile(r"(?i)start testing|testing \d+ MB")
TM5_FINISHED = re.compile(r"(?i)testing (is )?completed|testing stopped|error in test|fatal error|critical error|crashed|"
                          r"occurred in the program")


def parse_tm5(log):
    """New text in TestMem5's Log.txt for one run (TM5 0.13.1). It writes only "Testing completed" on a clean finish;
    errors get their own "Error in test N" lines. "An error N occurred in the program" counts as a FAIL on a set under
    test: it came once on a set looser than one that failed, so corrupted memory under TM5 is the likely cause."""
    lines = [l.strip() for l in (log or "").splitlines() if l.strip()]
    clean = bool(re.search(r"(?i)testing completed in .*no errors|of errors is not detected|^\S+\s+testing completed\s*$",
                           log or "", re.M))
    bad = [l for l in lines if re.search(r"(?i)error in test|detected \d+ error|fatal|critical|crashed|failed to|"
                                          r"occurred in the program", l)]
    tail = " | ".join(lines[-4:])[:300]
    if bad:
        return "FAIL", "TM5: %s" % bad[0][:160], {}
    if not clean:
        return "ERROR", "TM5 did not report a clean finish: %s" % tail, {}
    return "PASS", "TM5 clean: %s" % tail, {}


def parse_generic(out, code, pass_regex=None, fail_regex=None, ok_codes=(0,)):
    out = ANSI.sub("", out or "")
    if fail_regex:
        m = re.search(fail_regex, out, re.M)
        if m:
            return "FAIL", "matched failure pattern: %s" % m.group(0)[:100], {}
    if code not in ok_codes:
        return "FAIL", "exit code %s" % code, {}
    if pass_regex and not re.search(pass_regex, out, re.M):
        return "FAIL", "pass pattern not found", {}
    return "PASS", "exit %s" % code, {}


def parse_temps(text):
    """DIMM temperatures from a reader's output: "slot:temp" pairs (dimmtemp.exe prints time,0:45.25,1:47.00),
    or bare numbers one per line. Returns {slot: celsius}."""
    out = {}
    text = re.sub(r"^\s*\d{1,2}:\d{2}:\d{2},?", "", text or "")
    pairs = re.findall(r"(\w+):(-?\d+(?:\.\d+)?)", text)
    if pairs:
        for k, v in pairs:
            out[k] = float(v)
        return out
    for i, v in enumerate(re.findall(r"-?\d+(?:\.\d+)?", text or "")):
        out[str(i)] = float(v)
    return out


class TempReader:
    """A command that prints temperatures. `init_command` runs once when every sensor reads 0, because DDR5 SPD hub
    sensors read 0 after a cold boot until a tool (HWiNFO, for example) initialises them."""

    def __init__(self, d, resolve):
        self.cmd = [resolve(a) if i == 0 else a for i, a in enumerate(_args(d["command"]))] if d.get("command") else None
        self.init = _args(d["init_command"]) if d.get("init_command") else None
        self.init_wait = d.get("init_wait_s", 30)
        self.tried_init = False

    def read_raw(self):
        """One reading, zeros included (the doctor wants to see them)."""
        if not self.cmd:
            return None
        try:
            r = subprocess.run(self.cmd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        return parse_temps(r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "")

    def read(self):
        t = self.read_raw()
        if t is None:
            return None
        if t and all(v == 0 for v in t.values()) and self.init and not self.tried_init:
            self.tried_init = True
            try:
                subprocess.run(self.init, timeout=self.init_wait + 60)
            except (OSError, subprocess.SubprocessError):
                pass
            return self.read()
        return {k: v for k, v in t.items() if v != 0} or None


class Watch:
    """Peak and last temperatures during one test, and the abort decision."""

    def __init__(self, dimm, core, limits):
        self.dimm, self.core, self.limits = dimm, core, limits
        self.dimm_peak, self.dimm_last, self.core_peak = {}, {}, None

    def sample(self):
        if self.dimm:
            t = self.dimm.read()
            if t:
                for k, v in t.items():
                    self.dimm_peak[k] = max(self.dimm_peak.get(k, -99), v)
                self.dimm_last = t
                if max(t.values()) >= self.limits["dimm_temp_abort_c"]:
                    return "DIMM at %.1f C (abort limit %d C)" % (max(t.values()), self.limits["dimm_temp_abort_c"])
        if self.core:
            t = self.core.read()
            if t:
                hot = max(t.values())
                self.core_peak = max(self.core_peak or -99, hot)
                if hot >= self.limits["core_temp_abort_c"]:
                    return "core at %.0f C (abort limit %d C)" % (hot, self.limits["core_temp_abort_c"])
        return None

    def peak(self):
        return max(self.dimm_peak.values()) if self.dimm_peak else None

    def summary(self):
        if not self.dimm_peak:
            return "DIMM temps: no readings"
        ks = sorted(self.dimm_peak)
        return "DIMM peak %s, at end %s (C)" % ("/".join("%.1f" % self.dimm_peak[k] for k in ks),
                                               "/".join("%.1f" % self.dimm_last.get(k, 0) for k in ks))


def run_watched(args, cwd, timeout, watch, poll_s=5):
    """Run a test program, sampling temperatures; kill it when a limit is reached. (code, output, abort reason)."""
    try:
        p = subprocess.Popen(args, cwd=cwd or None, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             errors="replace")
    except OSError as e:
        return None, str(e), None
    import threading
    chunks = []
    t = threading.Thread(target=lambda: chunks.append(p.stdout.read()), daemon=True)
    t.start()
    t0 = time.time()
    abort = None
    while p.poll() is None:
        if time.time() - t0 > timeout:
            p.kill()
            abort = None
            chunks.append("\n[harness] timeout after %d s\n" % timeout)
            break
        if watch:
            abort = watch.sample()
            if abort:
                p.kill()
                break
        time.sleep(poll_s)
    t.join(10)
    if watch and not abort:
        watch.sample()
    return p.returncode if p.returncode is not None else -1, "".join(chunks), abort


class Tester:
    """Runs the configured tests for one step on real hardware."""

    def __init__(self, cfg, system, clock):
        self.cfg, self.system, self.clock = cfg, system, clock
        t = cfg.temps
        self.dimm = TempReader(t["dimm"], cfg.resolve) if t.get("dimm") else None
        self.core = TempReader(t["core"], cfg.resolve) if t.get("core") else None
        self.last_output = ""

    def run(self, names, label):
        """Run tests in order; stop at the first non-PASS. Returns (status, summary, metrics, dimm_peak)."""
        watch = Watch(self.dimm, self.core, self.cfg.limits)
        started = self.clock.time()
        parts, metrics = [], {}
        status = "PASS"
        for name in names:
            r = self.one(self.cfg.tests[name], label, watch, started)
            parts.append(r["summary"])
            metrics.update(r["metrics"])
            if r["status"] != "PASS":
                status = r["status"]
                break
        parts.append(watch.summary())
        return status, "; ".join(parts), metrics, watch.peak()

    def one(self, test, label, watch, started):
        d = test.d
        typ = test.type
        if typ == "whea":
            ev = self.system.whea_since(started - 60)
            if ev is None:
                st = "ERROR" if d.get("required", True) else "PASS"
                return {"name": test.name, "status": st, "summary": "WHEA: could not read the event log", "metrics": {}}
            return {"name": test.name, "status": "PASS" if not ev else "FAIL", "summary": "WHEA %d" % len(ev),
                    "metrics": {"whea": len(ev)}}
        if typ == "tm5":
            return self.tm5(test, watch)
        cmd = _args(d.get("command", ""))
        if not cmd:
            return {"name": test.name, "status": "ERROR", "summary": "%s: no command configured" % test.name, "metrics": {}}
        cmd[0] = self.cfg.resolve(cmd[0])
        if not os.path.exists(cmd[0]) and not shutil_which(cmd[0]):
            return {"name": test.name, "status": "ERROR", "summary": "%s: %s not found" % (test.name, cmd[0]), "metrics": {}}
        args = cmd + [a.replace("{label}", label) for a in d.get("args", [])]
        if typ == "memlat" and d.get("large_pages"):
            args.append("--large-pages")
        if typ == "memhold":
            args += [str(d.get("gib", 16)), str(d.get("minutes", 45)), str(d.get("check_every_s", 120)),
                     self.cfg.resolve(d.get("log", "memhold.log"))]
        if typ == "shakedown":
            args += ["run", "--minutes", str(d.get("minutes", 10)), "--label", re.sub(r"[^A-Za-z0-9-]", "-", label)[:60],
                     "--note", label]
        code, out, abort = run_watched(args, d.get("cwd") and self.cfg.resolve(d["cwd"]), d.get("timeout_s", 1800),
                                       watch if d.get("watch", True) else None)
        self.last_output = out
        if code is None:
            return {"name": test.name, "status": "ERROR", "summary": "%s could not start: %s" % (test.name, out[:120]), "metrics": {}}
        if abort:
            return {"name": test.name, "status": "ABORT", "summary": "%s stopped: %s" % (test.name, abort), "metrics": {}}
        if typ == "ycruncher":
            st, s, m = parse_ycruncher(out, code, d.get("min_passed", 3))
        elif typ == "memlat":
            st, s, m = parse_memlat(out, code)
        elif typ == "shakedown":
            st, s, m = parse_shakedown(out, code)
        elif typ == "memhold":
            st, s, m = parse_memhold(out, code)
        else:
            st, s, m = parse_generic(out, code, d.get("pass_regex"), d.get("fail_regex"), tuple(d.get("ok_codes", [0])))
        return {"name": test.name, "status": st, "summary": s, "metrics": m}


    def tm5(self, test, watch):
        """TestMem5 is a window program that writes its result to Log.txt beside it. Configure TM5.ini yourself
        (for example the "DDR5 Intel @ anta777" config, stop at the first error). Run it from an elevated harness:
        TM5 needs admin rights."""
        d = test.d
        exe = self.cfg.resolve(d.get("command", ""))
        log = self.cfg.resolve(d.get("log", os.path.join(os.path.dirname(exe), "Log.txt")))
        minutes = int(d.get("minutes", 60))
        if not os.path.exists(exe):
            return {"name": test.name, "status": "ERROR", "summary": "TM5 not found: %s" % exe, "metrics": {}}

        def read():
            try:
                with open(log, "rb") as f:
                    return f.read().decode("utf-8", "replace")
            except OSError:
                return ""
        before = len(read())
        try:
            p = subprocess.Popen([exe], cwd=os.path.dirname(exe))
        except OSError as e:
            return {"name": test.name, "status": "ERROR", "summary": "TM5 could not start: %s" % e, "metrics": {}}
        t0, new, started, abort = time.time(), "", False, None
        try:
            while time.time() - t0 < minutes * 60 + 1800:
                time.sleep(20)
                new = read()[before:]
                if not started:
                    if TM5_STARTED.search(new):
                        started = True
                    elif time.time() - t0 > 240:
                        return {"name": test.name, "status": "ERROR", "summary": "TM5 wrote no start line in 4 minutes",
                                "metrics": {}}
                if TM5_FINISHED.search(new):
                    time.sleep(5)
                    new = read()[before:]
                    break
                if watch:
                    abort = watch.sample()
                    if abort:
                        return {"name": test.name, "status": "ABORT", "summary": "TM5 stopped: %s" % abort, "metrics": {}}
        finally:
            if p.poll() is None:
                p.kill()
        st, s, m = parse_tm5(new)
        return {"name": test.name, "status": st, "summary": "%s (%.0f min)" % (s, (time.time() - t0) / 60), "metrics": m}


def shutil_which(p):
    import shutil
    return shutil.which(p)
