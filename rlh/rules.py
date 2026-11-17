"""Safety limits and tuning rules, checked on the whole plan before a sweep and on every step before it is written.

Errors block. Warnings are printed and journaled. Nothing here ever raises a limit.
"""

SG_ROLES = ("tRDRD_sg", "tWRWR_sg")


class Issue:
    def __init__(self, level, where, msg):
        self.level, self.where, self.msg = level, where, msg

    def __str__(self):
        return "%s %s: %s" % (self.level.upper(), self.where, self.msg)


def describe(cfg, changes):
    if not changes:
        return "no change (control)"
    return ", ".join("%s %s" % (n, cfg.settings[n].shown(v) if n in cfg.settings else v) for n, v in changes.items())


def _is_linked(cfg, names):
    return any(set(names) == set(group) for group in cfg.linked)


def voltage_issues(cfg, values, where, caps=None):
    out = []
    caps = caps or {}
    for name, v in values.items():
        s = cfg.settings.get(name)
        if not s or not s.limit or s.limit in ("vccsa_step_mv",):
            continue
        lim = min(cfg.limits[s.limit], caps.get(s.limit, cfg.limits[s.limit]))
        if not isinstance(v, int):
            continue
        if v == s.auto:
            out.append(Issue("warn", where, "%s on Auto: the board picks the voltage (ASRock follows the XMP DRAM "
                                            "voltage), so the %s limit cannot be checked before boot" % (name, s.limit)))
        elif v > lim:
            out.append(Issue("error", where, "%s %s is above the %s limit %s" % (name, s.shown(v), s.limit, s.shown(lim))))
    return out


def step_issues(cfg, group, step, known, where, caps=None):
    """Rules for one step. `known` = current values (kept set, baseline and readbacks) used for relational rules."""
    out = voltage_issues(cfg, step, where, caps)
    L = cfg.limits
    names = list(step)
    if len(names) > 1 and L["one_change_per_step"] and not _is_linked(cfg, names):
        out.append(Issue("error", where, "changes %d settings at once (%s); declare them as a linked pair in "
                                         "[sweep] linked or split the step" % (len(names), ", ".join(names))))
    after = dict(known)
    after.update(step)
    for name, v in step.items():
        s = cfg.settings[name]
        if s.role in SG_ROLES and L["sg_turnarounds_trained"]:
            out.append(Issue("error", where, "%s is left trained by default: it tracks tCCD_L and a too-low value "
                                             "can hang training with no POST (reference machine, tRDRD_sg 14)" % name))
        if s.role == "tCL" and cfg.platform.get("gear") == 2 and L["tcl_even_gear2"] and isinstance(v, int) and v % 2:
            out.append(Issue("error", where, "tCL %d is odd; Gear 2 needs an even tCL" % v))
        if s.role == "tWR" and cfg.twr_min is not None and isinstance(v, int) and v < cfg.twr_min:
            out.append(Issue("error", where, "tWR %d is below twr_min %d (your own floor for tWR in [sweep])"
                             % (v, cfg.twr_min)))
        if s.role == "tRTP" and isinstance(v, int) and v != s.auto and v < L["trtp_min"]:
            out.append(Issue("error", where, "tRTP %d is below trtp_min %d (the DRAM minimum)" % (v, L["trtp_min"])))
    roles = {cfg.settings[n].role: n for n in after if n in cfg.settings}
    if L["tras_rule"] and any(cfg.settings[n].role in ("tRAS", "tRCD", "tRTP") for n in step):
        vals = {r: after.get(roles.get(r)) for r in ("tRAS", "tRCD", "tRTP")}
        if all(isinstance(vals[r], int) and vals[r] > 0 for r in vals):
            if vals["tRAS"] < vals["tRCD"] + vals["tRTP"]:
                out.append(Issue("error", where, "tRAS %d < tRCD %d + tRTP %d: below the read-path floor (tRCD + the "
                                                 "read-back tRTP), it gains nothing and risks errors"
                                 % (vals["tRAS"], vals["tRCD"], vals["tRTP"])))
        elif "tRAS" in [cfg.settings[n].role for n in step]:
            out.append(Issue("warn", where, "cannot check tRAS >= tRCD + tRTP: give the tRCD and tRTP the BIOS reads "
                                            "back in [sweep.baseline] (or let a readback fill them in)"))
    if group is not None and group.risky and L["risky_needs_optin"]:
        out.append(Issue("info", where, "risky group: waits for `tuner.py optin %s`" % group.name))
    return out


def sa_step_issues(cfg, group):
    out = []
    sa = [n for n, s in cfg.settings.items() if s.limit == "vccsa_mv"]
    for name in sa:
        prev = cfg.baseline.get(name)
        for step in group.steps:
            v = step.get(name)
            if not isinstance(v, int) or v == 0:
                continue
            if prev is not None and v - prev > cfg.limits["vccsa_step_mv"]:
                out.append(Issue("error", "group %s" % group.name, "%s goes up %d mV in one step (%s -> %s); the limit is "
                                 "%d mV (step SA slowly and watch for hard freezes: the SA bug)"
                                 % (name, v - prev, prev, v, cfg.limits["vccsa_step_mv"])))
            if prev is None:
                out.append(Issue("warn", "group %s" % group.name, "%s: no baseline value, so the size of the first SA "
                                 "step cannot be checked; add it to [sweep.baseline]" % name))
            prev = v
    return out


def check_plan(cfg, implications=None, readback=None):
    """Rules for the whole plan. `readback` = values the BIOS shows now; they override [sweep.baseline].
    The plan is checked as if every earlier step passes (so a tRAS step may rely on an earlier tRTP step).
    Each step is checked again, against the kept set and the read-back values, before it is written."""
    out = []
    caps = (implications or {}).get("cap", {})
    known = dict(cfg.baseline)
    known.update(readback or {})
    for gi, g in enumerate(cfg.groups):
        out += sa_step_issues(cfg, g)
        for k, step in enumerate(g.steps):
            out += step_issues(cfg, g, step, known, "%s step %d (%s)" % (g.name, k + 1, describe(cfg, step)), caps)
            known.update({n: v for n, v in step.items() if isinstance(v, int) and v != cfg.settings[n].auto})
    for name in cfg.temp_gated:
        s = cfg.by_role(name) or cfg.settings.get(name)
        if s is None:
            continue
        out.append(Issue("info", "temperature gate", "%s is only tightened when the last DIMM peak was below %d C"
                         % (s.name, cfg.limits["temp_gate_c"])))
    for key, a in cfg.accepted.items():
        out.append(Issue("warn", "limits", "%s raised from %r to %r with i_accept_the_risk (%s)"
                         % (key, a["default"], a["value"], a["reason"] or "no reason given")))
    return out


def tightens(cfg, name, new, current):
    role = cfg.settings[name].role
    if not isinstance(new, int):
        return True
    if current is None or current == cfg.settings[name].auto:
        return True
    return new > current if role == "tREFI" else new < current


def gate_issue(cfg, step, good, last_peak, implications=None):
    """Temperature gate for tREFI up and tCL down (plus any gate the doctor added). None when the step may run."""
    gates = {}
    for role in cfg.temp_gated:
        s = cfg.by_role(role)
        if s:
            gates[s.name] = cfg.limits["temp_gate_c"]
    for role, below in (implications or {}).get("gate", {}).items():
        s = cfg.by_role(role)
        if s:
            gates[s.name] = min(below, gates.get(s.name, below))
    for name, v in step.items():
        if name not in gates:
            continue
        if not tightens(cfg, name, v, good.get(name, cfg.baseline.get(name))):
            continue
        if last_peak is None:
            if cfg.limits["temp_gate_requires_reading"]:
                return "%s is temperature-gated and there is no DIMM temperature reading yet" % name
            continue
        if last_peak >= gates[name]:
            return "%s is temperature-gated: the last DIMM peak was %.1f C, the gate is below %d C" % (name, last_peak, gates[name])
    return None


def doctor_skip(cfg, group, step, implications):
    if not implications or group.ignore_doctor:
        return None
    for role, reason in implications.get("skip", {}).items():
        s = cfg.by_role(role)
        if s and s.name in step:
            return "doctor: %s" % reason
    return None
