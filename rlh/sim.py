"""A simulated PC for dry runs and unit tests: backend, Windows queries and tests in one object. No hardware.

[simulate] in the config scripts what happens, keyed by "setting=value":
    outcomes = { "tRFC=630" = "NO TRAIN", "tRRD_L=10" = "CRASH", "tRDRD_sg=14" = "NO POST",
                 "tRAS=64" = "FAIL", "tWRRD_dg=48" = "FLAKY", "tRCD=48" = "CRASH_LOOP",
                 "tWTR_L=36" = "NOT APPLIED", "tCL=36" = "DEFAULTS", "tREFI=65535" = "CRASH_TEST" }
    marginal = ["tCL=38"]   # the kept set holding this fails tests after the first `marginal_grace` runs
    dimm_peak_c = 52        # DIMM peak every test reports (drives the temperature gate)
    trained = { tRFC = 922, tCL = 40 }   # what the BIOS shows for settings the sweep never wrote
Everything not listed passes.
"""
from .journal import FakeClock

FLAG_OUTCOMES = ("PASS", "FAIL", "FLAKY", "CRASH", "CRASH_LOOP", "CRASH_TEST", "NO TRAIN", "NO POST",
                 "NOT APPLIED", "DEFAULTS", "WHEA")


class SimCrash(Exception):
    """The simulated PC died mid-test: the engine's process ends here, like a real crash."""


class SimMachine:
    automatic = True
    name = "sim"

    def __init__(self, cfg, clock=None):
        self.cfg = cfg
        self.clock = clock or FakeClock()
        sim = cfg.simulate
        self.outcomes = {k: v.upper() for k, v in sim.get("outcomes", {}).items()}
        for v in self.outcomes.values():
            if v not in FLAG_OUTCOMES:
                raise ValueError("simulate.outcomes: unknown outcome %s" % v)
        self.marginal = list(sim.get("marginal", []))
        self.grace = int(sim.get("marginal_grace", 1))
        self.dimm_peak = sim.get("dimm_peak_c", 50.0)
        self.trained = dict(sim.get("trained", {}))
        self.nvram = {}
        self.events = []
        self.gap = 45
        self.effective = {}
        self.profile = cfg.platform.get("profile_value")
        self.armed = None
        self.lastgood = None
        self.restored = None
        self.reboot_pending = False
        self.runs_this_boot = 0
        self.marginal_runs = 0
        self.boots = 0
        self._boot_effects()

    # Helpers
    def _active(self):
        """The scripted outcome for what NVRAM holds now (first match in config order), or None."""
        for key, out in self.outcomes.items():
            name, _, val = key.partition("=")
            if name in self.nvram and str(self.nvram[name]) == val:
                return out
        return None

    def _shown(self, name, value):
        return self.cfg.settings[name].shown(value)

    def _boot_effects(self):
        self.boots += 1
        self.runs_this_boot = 0
        out = self._active()
        self.last_boot = out
        self.gap = 45
        eff = {}
        for n, s in self.cfg.settings.items():
            v = self.nvram.get(n, s.auto)
            if v == s.auto:
                v = self.trained.get(n, self.cfg.baseline.get(n, s.auto))
            eff[n] = self._shown(n, v)
        self.profile = self.cfg.platform.get("profile_value")
        if out in ("NO TRAIN", "NO POST", "DEFAULTS"):
            sig = self.cfg.fallback_signature
            for n in self.cfg.settings:
                eff[n] = sig.get(n, "29") if out != "DEFAULTS" else eff[n]
            self.gap = {"NO TRAIN": 200, "NO POST": 1800, "DEFAULTS": 300}[out]
            if out == "DEFAULTS":
                self.profile = "Auto"
        elif out == "NOT APPLIED":
            for key, o in self.outcomes.items():
                name, _, val = key.partition("=")
                if o == "NOT APPLIED" and str(self.nvram.get(name)) == val:
                    eff[name] = self._shown(name, int(val) + 6) if val.lstrip("-").isdigit() else "Auto"
        self.effective = eff

    # Machine actions
    def boot(self):
        """Power on after a reboot request. CRASH logs a crash; CRASH_LOOP crashes twice so the failsafe fires."""
        self.reboot_pending = False
        self.clock.sleep(self.gap)
        out = self._active()
        if out == "CRASH":
            self.events.append((self.clock.time(), "41 (simulated crash at boot)"))
            self.clock.sleep(60)
        if out == "CRASH_LOOP" and self.armed:
            self.events.append((self.clock.time(), "41 (simulated crash at boot)"))
            self.events.append((self.clock.time() + 30, "41 (simulated crash at boot)"))
            self.nvram = dict(self.lastgood)
            self.restored = "%s (simulated failsafe)" % self.armed
            self.armed = None
            self.clock.sleep(120)
        self._boot_effects()

    # Backend interface
    def prepare(self):
        return True, "simulated BIOS ready"

    def apply(self, wanted, step=None):
        self.nvram = dict(wanted)
        return True, "stored (simulated)"

    def read(self):
        return {"effective": dict(self.effective), "stored": dict(self.nvram), "profile": self.profile, "manual": None}

    def arm_failsafe(self, good, step, tag):
        self.armed, self.lastgood = tag, dict(good)
        return True

    def disarm(self):
        self.armed = None

    def failsafe_restored(self):
        r, self.restored = self.restored, None
        return r

    def reboot(self, why):
        self.reboot_pending = True

    # System interface
    def crashes_since(self, since):
        return [e for t, e in self.events if t >= since]

    def whea_since(self, since):
        return []

    def boot_gap(self):
        return self.gap

    # Tester interface
    def run(self, names, label):
        self.clock.sleep(8 * 60)
        self.runs_this_boot += 1
        out = self._active()
        peak = self.dimm_peak
        temps = "DIMM peak %.1f (C, simulated)" % peak
        if out == "CRASH_TEST":
            self.events.append((self.clock.time(), "41 (simulated crash during the test)"))
            raise SimCrash()
        if out == "FAIL" or (out == "FLAKY" and self.runs_this_boot > 1):
            return "FAIL", "simulated test failure (y-cruncher: ConvolutionFailedException); %s" % temps, {}, peak
        if out == "WHEA":
            return "FAIL", "WHEA 1 (simulated); %s" % temps, {"whea": 1}, peak
        for key in self.marginal:
            name, _, val = key.partition("=")
            if str(self.nvram.get(name)) == val:
                self.marginal_runs += 1
                if self.marginal_runs > self.grace:
                    return "FAIL", "simulated late failure on a marginal base (%s); %s" % (key, temps), {}, peak
        return "PASS", "simulated pass, 72.0 ns, 125.0 GB/s; %s" % temps, {"latency_ns": 72.0}, peak


def drive(engine, machine, max_cycles=500, owner=False):
    """Run the engine boot after boot until it finishes, pauses or waits. Returns the final phase.
    owner=True plays a cooperative owner: marks NO POST after a scripted one, opts in to risky groups and resumes
    after pauses, so a dry run can walk the whole plan."""
    phase = None
    for _ in range(max_cycles):
        no_post = machine.last_boot == "NO POST"
        try:
            phase = engine.run()
        except SimCrash:
            machine.reboot_pending = True
            phase = "crashed"
        if owner and no_post:
            engine.mark("NO POST", "simulated owner: power-cycled by hand to get a picture")
        if machine.reboot_pending:
            machine.boot()
            continue
        if phase in ("done", "stopped"):
            return phase
        if phase in ("paused", "waiting-optin"):
            if not owner:
                return phase
            st = engine.store.load()
            if phase == "waiting-optin":
                g = engine.cfg.groups[st["g"]].name
                engine.optin(g)
            else:
                engine.resume()
    return phase
