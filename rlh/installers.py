"""Optional Windows safety machinery for unattended sweeps. Each one changes the system, so each one:
- explains what it does and the risk, and does nothing without --yes;
- records the old value in the restore list (state_dir/restore.json) BEFORE changing anything;
- is undone by `tuner.py restore`.
"""
import json
import os
import sys

from .winsys import run

CRASH_KEY = r"HKLM\SYSTEM\CurrentControlSet\Control\CrashControl"
FAILSAFE_TASK = "RaptorLakeAutopilot Failsafe"
AUTOSTART_TASK = "RaptorLakeAutopilot Sweep"

INFO = {
    "crashdumps": (
        "Sets CrashDumpEnabled = 0 (no dump) and AutoReboot = 1.",
        "A bugcheck on unstable RAM can hang for many minutes while writing a dump. With dumps off it reboots "
        "at once and the sweep sees the crash. The cost: no dump to analyse while this is on."),
    "bcdedit": (
        "Sets recoveryenabled No and bootstatuspolicy IgnoreAllFailures on the current boot entry.",
        "After failed boots Windows would stop in Windows Recovery and wait for a click. With this it retries "
        "instead. The cost: if Windows itself breaks, it will not offer recovery by itself until you restore."),
    "failsafe": (
        "Creates a SYSTEM scheduled task that runs at every startup (tools/failsafe.ps1).",
        "If a step's boot repeats without its test starting, it imports the last good BIOS script with SCEWIN "
        "and restarts once. It runs about 15 to 20 s into a boot, so it cannot help with a step that hangs POST "
        "or crashes earlier than that. The task runs as SYSTEM: keep the harness folder writable by "
        "administrators only, or anyone who can edit the script owns the machine."),
    "autostart": (
        "Creates a scheduled task that runs `tuner.py run` with highest privileges at your logon.",
        "Needed for unattended SCEWIN sweeps (SCEWIN needs admin rights). It needs you logged on after each "
        "reboot: Windows autologon is a security trade-off the harness does not set up for you. Keep the "
        "harness folder and config writable by administrators only (an elevated task reading a file a normal "
        "user can change is a way around UAC)."),
}


class RestoreList:
    def __init__(self, state_dir):
        self.path = os.path.join(state_dir, "restore.json")

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def save(self, items):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=1)
        os.replace(tmp, self.path)

    def add(self, entry):
        items = self.load()
        if any((i["kind"], i.get("name")) == (entry["kind"], entry.get("name")) for i in items):
            return False  # keep the first (oldest) value: that is the one to go back to
        items.append(entry)
        self.save(items)
        return True


class Installers:
    def __init__(self, cfg, system, state_dir, harness_dir, out=print):
        self.cfg, self.system, self.state_dir, self.harness_dir, self.out = cfg, system, state_dir, harness_dir, out
        self.restore_list = RestoreList(state_dir)

    def describe(self, kind):
        what, risk = INFO[kind]
        return "%s\n  What: %s\n  Why and risk: %s" % (kind, what, risk)

    def install(self, kind, yes=False):
        if kind not in INFO:
            return False, "unknown installer %s (known: %s)" % (kind, ", ".join(INFO))
        self.out("WARNING: this changes your system.\n" + self.describe(kind))
        if not yes:
            return False, "nothing changed: run it again with --yes if you accept this"
        if os.name != "nt":
            return False, "installers only run on Windows"
        return getattr(self, "_" + kind)()

    def _crashdumps(self):
        old = {n: self.system.reg(CRASH_KEY, n) for n in ("CrashDumpEnabled", "AutoReboot")}
        self.restore_list.add({"kind": "crashdumps", "old": old})
        try:
            self.system.reg_set_dword(CRASH_KEY, "CrashDumpEnabled", 0)
            self.system.reg_set_dword(CRASH_KEY, "AutoReboot", 1)
        except OSError as e:
            return False, "could not write the registry (run elevated): %s" % e
        return True, "crash dumps off, automatic restart on (was %s)" % old

    def _bcdedit(self):
        cur = self.system.bcd()
        if cur is None:
            return False, "could not read bcdedit (run elevated)"
        old = {k: cur.get(k) for k in ("recoveryenabled", "bootstatuspolicy")}
        self.restore_list.add({"kind": "bcdedit", "old": old})
        for k, v in (("recoveryenabled", "No"), ("bootstatuspolicy", "IgnoreAllFailures")):
            code, out = run(["bcdedit", "/set", "{current}", k, v])
            if code != 0:
                return False, "bcdedit /set %s failed: %s" % (k, out.strip()[:200])
        return True, "Windows Recovery off for failed boots (was %s)" % old

    def _failsafe(self):
        b = self.cfg.backend
        if b.get("type") != "scewin":
            return False, "the failsafe needs the SCEWIN backend"
        fs_dir = os.path.join(self.state_dir, "failsafe")
        os.makedirs(fs_dir, exist_ok=True)
        with open(os.path.join(fs_dir, "failsafe.json"), "w", encoding="utf-8") as f:
            json.dump({"scewin": self.cfg.resolve(b.get("scewin", "SCEWIN_64.exe")),
                       "password_file": self.cfg.resolve(b["password_file"]) if b.get("password_file") else ""}, f)
        script = os.path.join(self.harness_dir, "tools", "failsafe.ps1")
        self.restore_list.add({"kind": "task", "name": FAILSAFE_TASK})
        code, out = run(["schtasks", "/Create", "/TN", FAILSAFE_TASK, "/SC", "ONSTART", "/RU", "SYSTEM", "/RL", "HIGHEST",
                         "/TR", 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%s" -Dir "%s"' % (script, fs_dir),
                         "/F"])
        return code == 0, out.strip()[:300]

    def _autostart(self):
        tuner = os.path.join(self.harness_dir, "tuner.py")
        self.restore_list.add({"kind": "task", "name": AUTOSTART_TASK})
        code, out = run(["schtasks", "/Create", "/TN", AUTOSTART_TASK, "/SC", "ONLOGON", "/RL", "HIGHEST", "/TR",
                         '"%s" "%s" run -c "%s"' % (sys.executable, tuner, os.path.abspath(self.cfg.path)), "/F"])
        return code == 0, out.strip()[:300]

    def restore(self, yes=False):
        items = self.restore_list.load()
        if not items:
            return True, "the restore list is empty: nothing to undo"
        lines = ["Will undo, newest first:"] + ["  %s %s" % (i["kind"], i.get("old") or i.get("name")) for i in reversed(items)]
        self.out("\n".join(lines))
        if not yes:
            return False, "nothing changed: run it again with --yes"
        if os.name != "nt":
            return False, "restore only runs on Windows"
        left, done = [], []
        for item in reversed(items):
            if self._undo(item):
                done.append(item)
            else:
                left.insert(0, item)
        self.restore_list.save(left)
        names = [i.get("name", i["kind"]) for i in done], [i.get("name", i["kind"]) for i in left]
        return not left, "undone: %s; left (failed, still in the list): %s" % (
            ", ".join(names[0]) or "none", ", ".join(names[1]) or "none")

    def _undo(self, item):
        k = item["kind"]
        try:
            if k == "crashdumps":
                for n, v in item["old"].items():
                    if v is not None:
                        self.system.reg_set_dword(CRASH_KEY, n, v)
                return True
            if k == "bcdedit":
                for n, v in item["old"].items():
                    args = ["bcdedit", "/set", "{current}", n, v] if v else ["bcdedit", "/deletevalue", "{current}", n]
                    if run(args)[0] != 0:
                        return False
                return True
            if k == "task":
                return run(["schtasks", "/Delete", "/TN", item["name"], "/F"])[0] == 0
        except OSError:
            return False
        return False
