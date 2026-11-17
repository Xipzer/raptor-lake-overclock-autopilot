"""Ways to apply BIOS settings. The sweep state machine is the same for all of them; the interface is in
backend_api.py and the board vendor plugins are in vendors/.

- ScewinBackend: writes the BIOS from Windows with AMI SCEWIN and reboots by itself. Vendor plugins build on it
  (vendors/asrock.py is the supported one).
- ManualBackend: prints exactly what to enter in the BIOS, then asks the user what happened after the boot.
- The simulator (sim.py) implements the same interface with no hardware.

A snapshot (what read() returns):
    {"effective": {setting: live value shown by the BIOS or None},
     "stored": {setting: value in NVRAM or None},
     "profile": the memory profile question's value (None if not configured),
     "manual": None or the user's answers (manual mode without SCEWIN)}
"""
import os
import shutil

from . import scewin
from .backend_api import BiosBackend
from .winsys import run

PASSWORD_ENV = "RLAP_BIOS_PASSWORD"


class UI:
    """Console questions. Tests and the simulator pass their own."""

    def ask(self, prompt, choices=("y", "n")):
        while True:
            a = input("%s [%s] " % (prompt, "/".join(choices))).strip().lower()
            if a in choices:
                return a

    def say(self, text):
        print(text)


class ScewinBackend(BiosBackend):
    automatic = True
    name = "scewin"
    needs_password = False

    def __init__(self, cfg, state_dir, system, ui=None):
        b = cfg.backend
        self.cfg, self.state_dir, self.system, self.ui = cfg, state_dir, system, ui or UI()
        self.exe = cfg.resolve(b.get("scewin", "SCEWIN_64.exe"))
        self.password_file = cfg.resolve(b["password_file"]) if b.get("password_file") else None
        self.stage = os.path.join(state_dir, "stage")
        self.golden_path = os.path.join(state_dir, "golden.txt")
        self.restore_drift = b.get("restore_golden_drift", True)
        self.fs_dir = os.path.join(state_dir, "failsafe")
        os.makedirs(self.stage, exist_ok=True)

    # SCEWIN calls
    def _password(self):
        """From [backend] password_file, else the RLAP_BIOS_PASSWORD environment variable. Never from code."""
        if self.password_file:
            with open(self.password_file, encoding="utf-8-sig") as f:
                return f.read().strip()
        return os.environ.get(PASSWORD_ENV) or None

    def export(self, name="export.txt"):
        path = os.path.join(self.stage, name)
        if os.path.exists(path):
            os.remove(path)
        code, out = run([self.exe, "/o", "/s", path], timeout=300)
        if code is None or not os.path.exists(path):
            self.last_output = out
            return None
        return scewin.Script.load(path)

    def _import(self, script):
        path = os.path.join(self.stage, "import.txt")
        script.save(path)
        args = [self.exe, "/i", "/s", path]
        pw = self._password()
        if pw:
            args += ["/cpwd", pw]
        code, out = run(args, timeout=300)
        out = (out or "").replace(pw, "<password>") if pw else (out or "")
        return code, out

    def q(self, script, name):
        s = self.cfg.settings[name]
        return script.find(s.question, s.index, s.token)

    def golden(self):
        return scewin.Script.load(self.golden_path)

    # Interface
    def prepare(self):
        if self.needs_password and not self._password():
            return False, ("this board blocks SCEWIN imports without the BIOS admin password: set [backend] "
                           "password_file or the %s environment variable" % PASSWORD_ENV)
        cur = self.export()
        if cur is None:
            return False, "SCEWIN export failed: %s" % getattr(self, "last_output", "")[-300:]
        missing = []
        for name in self.cfg.settings:
            try:
                self.q(cur, name)
            except scewin.QuestionError as e:
                missing.append(str(e))
        if missing:
            return False, "questions not found in this BIOS: " + "; ".join(missing)
        if not os.path.exists(self.golden_path):
            shutil.copyfile(os.path.join(self.stage, "export.txt"), self.golden_path)
        return True, "golden script saved (the BIOS as it was before the sweep)"

    def _build(self, cur, wanted):
        """The current export, with values that drifted from the golden script copied back (this also recovers
        after a BIOS loaded defaults), then the wanted values. Built from a fresh export because SCEWIN matches
        prompts including the live value."""
        gold = self.golden()
        if gold.header("HIICrc32") != cur.header("HIICrc32"):
            raise RuntimeError("the BIOS setup layout changed since the sweep started (BIOS update or reset?); "
                               "stop and start a new sweep")
        own = {self.q(cur, n).token for n in wanted}
        if self.restore_drift:
            for gq in gold.single():
                cq = cur.by_token.get(gq.token, [])
                if gq.token in own or len(cq) != 1:
                    continue
                gv = gold.value(gq)
                if gv is not None and cur.value(cq[0]) != gv:
                    try:
                        cur.set(cq[0], gv)
                    except ValueError:
                        pass
        for name, v in wanted.items():
            cur.set(self.q(cur, name), v)
        return cur

    def apply(self, wanted, step=None):
        cur = self.export()
        if cur is None:
            return False, "could not read the BIOS (SCEWIN export failed)"
        try:
            script = self._build(cur, wanted)
        except (RuntimeError, scewin.QuestionError, ValueError) as e:
            return False, str(e)
        code, out = self._import(script)
        skipped = scewin.unmatched(out)
        after = self.export("after.txt")
        if after is None:
            return False, "import ran (exit %s) but the re-export failed" % code
        bad = {}
        for name, v in wanted.items():
            got = after.value(self.q(after, name))
            if str(got) != str(v):
                bad[name] = got
        if bad:
            hint = " SCEWIN said: %s" % "; ".join(skipped[:3]) if skipped else ""
            prot = " (runtime variable protection? ASRock needs a BIOS admin password and /cpwd)" \
                if "password" in out.lower() or not self.password_file else ""
            return False, "not stored: %s%s%s" % (bad, hint, prot)
        return True, "stored (verified by re-export)"

    def read(self):
        cur = self.export()
        if cur is None:
            return None
        eff, stored = {}, {}
        for name in self.cfg.settings:
            q = self.q(cur, name)
            eff[name] = cur.label(q)
            stored[name] = cur.value(q)
        prof = None
        pq = self.cfg.platform.get("profile_question")
        if pq:
            prof = cur.value(cur.find(pq, self.cfg.platform.get("profile_index")))
        return {"effective": eff, "stored": stored, "profile": prof, "manual": None}

    def arm_failsafe(self, good, step, tag):
        """Leave the last good script for the startup failsafe task. After the step's boot the prompts will show
        the step's values, so those prompts are rewritten to match, or SCEWIN would skip them."""
        cur = self.export()
        if cur is None:
            return False
        gold = self.golden()
        try:
            script = self._build(cur, good)
            for name, v in step.items():
                q = self.q(script, name)
                if script.label(q) is not None:
                    script.set_label(q, self.cfg.settings[name].shown(v))
                if name not in good:
                    gv = gold.value(self.q(gold, name))
                    script.set(q, gv if gv is not None else self.cfg.settings[name].auto)
        except (RuntimeError, scewin.QuestionError, ValueError):
            return False
        # Failsafe v2 takes a fresh export at restore time and only needs numbers by token: every kept value and the
        # pre-sweep value of anything the step touches. A pre-built script alone did nothing whenever training had
        # fallen back, because SCEWIN matches prompts that include the live value. lastgood.txt stays as its fallback.
        vals = {}
        for name in list(good) + [n for n in step if n not in good]:
            v = good.get(name)
            if v is None:
                v = gold.value(self.q(gold, name))
            if isinstance(v, int):
                vals[self.q(script, name).token] = v
        os.makedirs(self.fs_dir, exist_ok=True)
        script.save(os.path.join(self.fs_dir, "lastgood.txt"))
        _durable(os.path.join(self.fs_dir, "lastgood-values.txt"), "".join("%s=%d\n" % kv for kv in vals.items()))
        _durable(os.path.join(self.fs_dir, "armed"), tag)
        return True

    def disarm(self):
        for n in ("armed", "seen"):
            p = os.path.join(self.fs_dir, n)
            if os.path.exists(p):
                os.remove(p)

    def failsafe_restored(self):
        p = os.path.join(self.fs_dir, "restored")
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8-sig", errors="replace") as f:
            text = f.read().strip()
        os.remove(p)
        return text or "yes"

    def reboot(self, why):
        self.system.reboot(why)


def _durable(path, text):
    """Write and flush to disk: a reboot seconds later once left a file ending in zero bytes."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())


class ManualBackend(BiosBackend):
    """You enter the values in the BIOS yourself. If a SCEWIN path is configured, it is used read-only (export)
    to check what the BIOS really applied; otherwise the harness asks you."""
    automatic = False
    name = "manual"

    def __init__(self, cfg, state_dir, system, ui=None):
        self.cfg, self.system, self.ui = cfg, system, ui or UI()
        self.reader = ScewinBackend(cfg, state_dir, system, ui) if cfg.backend.get("scewin") else None
        self.pending = None

    def prepare(self):
        if self.reader:
            return self.reader.prepare()
        return True, "manual mode: save your current BIOS profile to a slot or USB before you start"

    def apply(self, wanted, step=None):
        self.pending = (wanted, step or {})
        lines = ["", "Enter these values in the BIOS, save, and let it boot. Leave everything else as it is.", ""]
        width = max([len(self.cfg.settings[n].question) for n in wanted] + [10])
        for name, v in wanted.items():
            s = self.cfg.settings[name]
            mark = "  <- this step" if name in (step or {}) else ""
            lines.append("  %-*s  %s%s" % (width, s.question, s.shown(v), mark))
        lines += ["", "After Windows is back, run `tuner.py run` again (or let the autostart task do it).",
                  "If it does not POST and you have to power-cycle or clear CMOS, run `tuner.py mark no-post` "
                  "after it is back."]
        self.ui.say("\n".join(lines))
        return True, "printed for manual entry"

    def read(self):
        if self.reader:
            snap = self.reader.read()
            if snap is not None:
                return snap
        a = {}
        a["no_post"] = self.ui.ask("Did you have to power-cycle by hand (or clear CMOS) to get a picture?") == "y"
        a["own_ok"] = self.ui.ask("Does the BIOS (or HWiNFO / Timing Configurator) show the values you entered?") == "y"
        a["others_changed"] = self.ui.ask("Did other timings change too (for example tRFC 395, low speed, "
                                          "or a much slower boot)?") == "y"
        a["defaults"] = self.ui.ask("Did the BIOS load defaults or turn the XMP/EXPO profile off?") == "y"
        return {"effective": {}, "stored": {}, "profile": None, "manual": a}

    def arm_failsafe(self, good, step, tag):
        return False

    def disarm(self):
        pass

    def failsafe_restored(self):
        return None

    def reboot(self, why):
        self.ui.say("Now restart into the BIOS and enter the values above (%s)." % why)


def make(cfg, state_dir, system, ui=None):
    t = cfg.backend.get("type")
    if t == "scewin":
        from . import vendors
        return vendors.make_backend(cfg.backend.get("vendor", "asrock"), cfg, state_dir, system, ui)
    if t == "manual":
        return ManualBackend(cfg, state_dir, system, ui)
    raise ValueError("backend %s is made by the simulator" % t)
