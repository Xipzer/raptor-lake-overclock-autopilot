"""Load and validate the harness config (one TOML file).

Safety limits have responsible defaults. A config may make a limit stricter freely. Making it less safe needs the
table form with an explicit acceptance, for example:

    [limits.vddq_tx_mv]
    value = 1400
    i_accept_the_risk = true
    reason = "my kit needs it to train at 8600"

A less safe plain value is an error, never a silent change.
"""
import os
import sys

try:
    import tomllib
except ImportError:  # Python < 3.11
    sys.exit("This harness needs Python 3.11 or newer (for tomllib).")


class ConfigError(Exception):
    pass


# name: (default, direction, meaning). direction: "max" = higher is less safe, "min" = lower is less safe,
# "true" = turning it off is less safe.
LIMITS = {
    "cpu_vcore_fixed_mv": (1400, "max", "highest fixed CPU core voltage a step may set"),
    "vddq_tx_mv": (1350, "max", "highest VDDQ_TX (ASRock: VDD_IMC) a step may set"),
    "vdd2_mv": (1450, "max", "highest VDD2 (ASRock: VDD_CPU) a step may set"),
    "vccsa_mv": (1300, "max", "highest VCCSA a step may set"),
    "vccsa_step_mv": (20, "max", "largest VCCSA increase between two steps (SA bug: hard freezes above a chip-specific point)"),
    "dimm_vdd_mv": (1500, "max", "highest DIMM VDD a step may set"),
    "dimm_vddq_mv": (1500, "max", "highest DIMM VDDQ a step may set"),
    "dimm_temp_abort_c": (60, "max", "abort a test when any DIMM sensor reads this or more"),
    "core_temp_abort_c": (100, "max", "abort a test when the hottest core reads this or more"),
    "temp_gate_c": (45, "max", "tREFI up / tCL down only when the last DIMM peak was below this"),
    "temp_gate_requires_reading": (True, "true", "with no DIMM reading, gated steps are skipped"),
    "runs_per_step": (2, "min", "passing runs needed before a value is kept"),
    "control_every": (3, "max", "run a control of the kept set after this many kept steps (0 = never)"),
    "cluster_fails": (3, "max", "consecutive test failures that trigger a control run (0 = never)"),
    "one_change_per_step": (True, "true", "a step changes one setting (or one declared linked pair)"),
    "tcl_even_gear2": (True, "true", "tCL must be even in Gear 2"),
    "tras_rule": (True, "true", "tRAS >= tRCD + tRTP, with the tRTP the BIOS reads back"),
    "trtp_min": (12, "min", "a tRTP step below this is rejected, and so is any step after which tRTP reads back below it"),
    "sg_turnarounds_trained": (True, "true", "tRDRD_sg / tWRWR_sg are not swept (no-POST risk, tied to tCCD_L)"),
    "risky_needs_optin": (True, "true", "groups marked risky wait for an explicit opt-in"),
}

OUTCOMES = ("PASS", "FAIL", "CRASH", "NO TRAIN", "NOT APPLIED", "NO POST")
SETTING_KINDS = ("int", "mv", "option", "ratio")


def _less_safe(direction, default, value):
    if direction == "max":
        return value > default
    if direction == "min":
        return value < default
    return bool(default) and not value


def resolve_limits(raw):
    out, accepted = {}, {}
    raw = raw or {}
    for key in raw:
        if key not in LIMITS:
            raise ConfigError("unknown limit %r (known: %s)" % (key, ", ".join(sorted(LIMITS))))
    for key, (default, direction, _) in LIMITS.items():
        item = raw.get(key, default)
        accept = False
        reason = ""
        if isinstance(item, dict):
            if "value" not in item:
                raise ConfigError("limit %s: table form needs value" % key)
            accept = item.get("i_accept_the_risk") is True
            reason = item.get("reason", "")
            item = item["value"]
        if type(item) is not type(default):
            raise ConfigError("limit %s: expected %s, got %r" % (key, type(default).__name__, item))
        if key in ("control_every", "cluster_fails") and item == 0:
            unsafe = True  # 0 switches the safeguard off
        else:
            unsafe = _less_safe(direction, default, item)
        if unsafe and not accept:
            raise ConfigError("limit %s = %r is less safe than the default %r. If you mean it, write it as a table "
                              "with i_accept_the_risk = true (and a reason)." % (key, item, default))
        out[key] = item
        if unsafe:
            accepted[key] = {"value": item, "default": default, "reason": reason}
    return out, accepted


class Setting:
    def __init__(self, name, d):
        self.name = name
        self.question = d.get("question")
        if not self.question:
            raise ConfigError("setting %s: question (the BIOS name) is required" % name)
        self.index = d.get("index")
        self.token = d.get("token")
        self.kind = d.get("kind", "int")
        if self.kind not in SETTING_KINDS:
            raise ConfigError("setting %s: kind must be one of %s" % (name, SETTING_KINDS))
        self.limit = d.get("limit")
        if self.limit and self.limit not in LIMITS:
            raise ConfigError("setting %s: unknown limit %s" % (name, self.limit))
        self.role = d.get("role", name)
        self.follows = list(d.get("follows", []))
        self.auto = d.get("auto", 0)

    def shown(self, value):
        """How the BIOS prompt shows a value (the live-value label)."""
        if self.kind == "mv" and isinstance(value, int):
            return "%.3fV" % (value / 1000)
        return str(value)


class Group:
    def __init__(self, d, settings):
        self.name = d.get("name")
        if not isinstance(self.name, str) or not self.name:
            raise ConfigError("every [[group]] needs a name")
        self.mode = d.get("mode", "descend")
        if self.mode not in ("descend", "alt"):
            raise ConfigError("group %s: mode must be descend or alt" % self.name)
        self.risky = bool(d.get("risky", False))
        self.tests = d.get("tests")
        self.note = d.get("note", "")
        self.cpu = bool(d.get("cpu", False))
        self.ignore_doctor = bool(d.get("ignore_doctor", False))
        if "steps" in d:
            self.steps = [dict(s) for s in d["steps"]]
        else:
            names = d.get("settings") or ([d["setting"]] if "setting" in d else [])
            if not names:
                raise ConfigError("group %s: give setting, settings or steps" % self.name)
            self.steps = [{n: v for n in names} for v in d.get("values", [])]
        for s in self.steps:
            for n in s:
                if n not in settings:
                    raise ConfigError("group %s: unknown setting %s (define it under [settings.%s])" % (self.name, n, n))


class Test:
    def __init__(self, name, d):
        self.name = name
        self.type = d.get("type", "command")
        self.d = d


class Config:
    def __init__(self, data, path=None, base_dir=None):
        self.path = path
        self.base_dir = base_dir or (os.path.dirname(os.path.abspath(path)) if path else os.getcwd())
        self.data = data
        self.platform = data.get("platform", {})
        self.backend = data.get("backend", {"type": "manual"})
        if self.backend.get("type") not in ("scewin", "manual", "sim"):
            raise ConfigError("backend.type must be scewin, manual or sim")
        if self.backend.get("type") == "scewin":
            from . import vendors
            try:
                vendors.get(self.backend.get("vendor", "asrock"))
            except vendors.VendorNotImplemented as e:
                raise ConfigError(str(e))
        self.limits, self.accepted = resolve_limits(data.get("limits"))
        self.settings = {n: Setting(n, d) for n, d in data.get("settings", {}).items()}
        self.groups = [Group(g, self.settings) for g in data.get("group", [])]
        names = [g.name for g in self.groups]
        if len(set(names)) != len(names):
            raise ConfigError("group names must be unique")
        sweep = data.get("sweep", {})
        self.sweep = sweep
        self.watch = list(sweep.get("watch", []))
        for n in self.watch + list(sweep.get("fallback_signature", {})):
            if n not in self.settings:
                raise ConfigError("sweep: unknown setting %s" % n)
        self.fallback_signature = {k: str(v) for k, v in sweep.get("fallback_signature", {}).items()}
        self.linked = [list(x) for x in sweep.get("linked", [])]
        self.temp_gated = list(sweep.get("temp_gated", ["tREFI", "tCL"]))
        self.twr_min = sweep.get("twr_min")
        self.baseline = dict(sweep.get("baseline", {}))
        self.max_backoff = int(sweep.get("max_backoff", 2))
        self.startup_delay_s = int(sweep.get("startup_delay_s", 45))
        self.tests = {n: Test(n, d) for n, d in data.get("tests", {}).items()}
        self.default_tests = list(sweep.get("tests", list(self.tests)))
        for g in self.groups:
            for t in (g.tests or []) + self.default_tests:
                if t not in self.tests:
                    raise ConfigError("unknown test %s (define it under [tests.%s])" % (t, t))
        self.cpu_tests = list(sweep.get("cpu_tests", []))
        self.journal = data.get("journal", {})
        self.paths = data.get("paths", {})
        self.state_dir = self.resolve(self.paths.get("state_dir", "state"))
        self.simulate = data.get("simulate", {})
        self.temps = data.get("temps", {})
        self.doctor = data.get("doctor", {})

    def resolve(self, p):
        p = os.path.expandvars(os.path.expanduser(p))
        return p if os.path.isabs(p) else os.path.join(self.base_dir, p)

    def by_role(self, role):
        for s in self.settings.values():
            if s.role == role:
                return s
        return None


def load(path, overrides=None):
    with open(path, "rb") as f:
        data = tomllib.load(f)
    for k, v in (overrides or {}).items():
        data.setdefault(k, {}).update(v)
    return Config(data, path)
