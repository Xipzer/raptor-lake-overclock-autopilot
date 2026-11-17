#!/usr/bin/env python3
"""Raptor Lake OC Autopilot command line. Run `python tuner.py -h`. Read README.md before using anything that writes."""
import argparse
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from rlh import config as config_mod  # noqa: E402
from rlh import backends, engine as engine_mod, rules, scewin, sim, testsuite, vendors, winsys  # noqa: E402
from rlh.doctor import kb, probes, report  # noqa: E402
from rlh.installers import Installers  # noqa: E402
from rlh.journal import Clock, FakeClock, Journal, StateStore  # noqa: E402


def load_cfg(path):
    try:
        return config_mod.load(path)
    except config_mod.ConfigError as e:
        sys.exit("Config error: %s" % e)
    except FileNotFoundError:
        sys.exit("No such config: %s" % path)


def implications_path(cfg):
    return cfg.resolve(cfg.sweep.get("doctor_implications", os.path.join(cfg.state_dir, "doctor", "implications.json")))


def load_implications(cfg, quiet=False):
    if not cfg.sweep.get("use_doctor", True):
        return {}
    p = implications_path(cfg)
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        imp = json.load(f)
    if not quiet:
        print("Using doctor implications from %s (from: %s)" % (p, ", ".join(imp.get("from", [])) or "none"))
    return imp


def build_engine(cfg, clock=None, ui=None, echo=True):
    clock = clock or Clock()
    os.makedirs(cfg.state_dir, exist_ok=True)
    system = winsys.make(clock)
    backend = backends.make(cfg, cfg.state_dir, system, ui)
    tester = testsuite.Tester(cfg, system, clock)
    j = cfg.journal
    journal = Journal(os.path.join(cfg.state_dir, "journal.md"), clock, j.get("mirror_command"), j.get("notify_command"), echo)
    store = StateStore(os.path.join(cfg.state_dir, "state.json"))
    return engine_mod.Engine(cfg, backend, system, tester, clock, journal, store, load_implications(cfg))


def cmd_check(a):
    cfg = load_cfg(a.config)
    imp = load_implications(cfg)
    issues = rules.check_plan(cfg, imp)
    for i in issues:
        print(i)
    for role in imp.get("order_first", []):
        s = cfg.by_role(role)
        if s and cfg.groups and s.name not in [n for g in cfg.groups[:2] for st in g.steps for n in st]:
            print("WARN doctor: %s should be tuned first; it is not in the first groups" % role)
    errors = [i for i in issues if i.level == "error"]
    print("%d errors, %d warnings. %s" % (len(errors), sum(1 for i in issues if i.level == "warn"),
                                          "The plan is safe to start." if not errors else "Fix the errors first."))
    return 1 if errors else 0


def cmd_plan(a):
    cfg = load_cfg(a.config)
    imp = load_implications(cfg)
    known = dict(cfg.baseline)
    for g in cfg.groups:
        flags = [g.mode] + (["risky: needs optin"] if g.risky else []) + (["cpu tests"] if g.cpu else [])
        print("%s [%s]%s" % (g.name, ", ".join(flags), "  # " + g.note if g.note else ""))
        for k, step in enumerate(g.steps):
            notes = [str(i) for i in rules.step_issues(cfg, g, step, known, "", imp.get("cap")) if i.level != "info"]
            known.update({n: v for n, v in step.items() if isinstance(v, int) and v != cfg.settings[n].auto})
            skip = rules.doctor_skip(cfg, g, step, imp)
            gated = [n for n in step if (cfg.settings[n].role in cfg.temp_gated or cfg.settings[n].role in imp.get("gate", {}))]
            print("  %d. %s%s%s%s" % (k + 1, rules.describe(cfg, step), "  [SKIP %s]" % skip if skip else "",
                                     "  [temperature-gated]" if gated else "",
                                     "".join("\n       " + n for n in notes)))
    print("\nTests per step: %s, %d passing runs each; control every %s kept steps; DIMM abort %d C; core abort %d C."
          % (", ".join(cfg.default_tests), cfg.limits["runs_per_step"], cfg.limits["control_every"] or "never",
             cfg.limits["dimm_temp_abort_c"], cfg.limits["core_temp_abort_c"]))
    return 0


def cmd_start(a):
    cfg = load_cfg(a.config)
    eng = build_engine(cfg)
    if eng.store.exists() and not a.force:
        sys.exit("A sweep state already exists in %s. Use `status`, or --force to start over (the journal is kept)." % cfg.state_dir)
    if cfg.backend["type"] == "scewin" and os.name != "nt":
        sys.exit("The SCEWIN backend runs on Windows only. Use `simulate` here.")
    ok, msg = eng.start()
    print(msg)
    if ok:
        print("State in %s. Next: `python tuner.py run -c %s`." % (cfg.state_dir, a.config))
    return 0 if ok else 1


def cmd_run(a):
    cfg = load_cfg(a.config)
    if cfg.backend["type"] == "sim":
        sys.exit("This config uses the sim backend: use `simulate`.")
    if cfg.backend["type"] == "manual":
        cfg.startup_delay_s = 0
    eng = build_engine(cfg)
    if not eng.store.exists():
        sys.exit("No sweep yet. Run `start` first.")
    print("Result: %s" % eng.run())
    return 0


def cmd_simulate(a):
    cfg = load_cfg(a.config)
    cfg.backend = {"type": "sim"}
    cfg.startup_delay_s = 0
    tmp = None
    if a.state_dir:
        cfg.state_dir = os.path.abspath(a.state_dir)
        if os.path.exists(cfg.state_dir):
            shutil.rmtree(cfg.state_dir)
    else:
        tmp = tempfile.mkdtemp(prefix="tuner-sim-")
        cfg.state_dir = tmp
    os.makedirs(cfg.state_dir, exist_ok=True)
    clock = FakeClock()
    m = sim.SimMachine(cfg, clock)
    journal = Journal(os.path.join(cfg.state_dir, "journal.md"), clock, echo=not a.quiet)
    store = StateStore(os.path.join(cfg.state_dir, "state.json"))
    imp = {} if a.no_doctor else load_implications(cfg, quiet=a.quiet)
    eng = engine_mod.Engine(cfg, m, m, m, clock, journal, store, imp)
    ok, msg = eng.start()
    if not ok:
        print(msg)
        return 1
    phase = sim.drive(eng, m, owner=a.owner)
    st = store.load()
    print("\nSimulation ended: %s after %d simulated boots, %.1f simulated hours." % (
        phase, m.boots, (clock.time() - 1_700_000_000) / 3600))
    print("Kept: %s" % (rules.describe(cfg, st["good"]) if st["good"] else "nothing"))
    counts = {}
    for r in st["results"]:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    print("Outcomes: %s" % ", ".join("%s %d" % kv for kv in sorted(counts.items())))
    print("Journal: %s" % os.path.join(cfg.state_dir, "journal.md"))
    return 0


def cmd_status(a):
    cfg = load_cfg(a.config)
    store = StateStore(os.path.join(cfg.state_dir, "state.json"))
    if not store.exists():
        print("No sweep state in %s." % cfg.state_dir)
        return 0
    st = store.load()
    print("Phase: %s%s" % (st["phase"], " (%s)" % st["pause_reason"] if st.get("pause_reason") else ""))
    if st.get("pending"):
        print("Pending: %s %s" % (st["pending"]["kind"], rules.describe(cfg, st["pending"].get("changes", {}))))
    if st["g"] < len(cfg.groups):
        print("Group: %s, step %d" % (cfg.groups[st["g"]].name, st["k"] + 1))
    print("Kept: %s" % (rules.describe(cfg, st["good"]) if st["good"] else "nothing yet"))
    print("Last DIMM peak: %s" % st.get("last_dimm_peak"))
    for r in st["results"][-a.last:]:
        print("  %s %-11s %s: %s%s" % (r["time"], r["outcome"], rules.describe(cfg, r["changes"]), r["why"][:140],
                                       " [unproven]" if r.get("unproven") else ""))
    return 0


def cmd_mark(a):
    cfg = load_cfg(a.config)
    ok, msg = build_engine(cfg).mark(a.outcome, a.note or "")
    print(msg)
    return 0 if ok else 1


def cmd_optin(a):
    cfg = load_cfg(a.config)
    g = next((g for g in cfg.groups if g.name == a.group), None)
    if g and g.note:
        print("Group %s: %s" % (g.name, g.note))
    ok, msg = build_engine(cfg).optin(a.group)
    print(msg)
    return 0 if ok else 1


def cmd_resume(a):
    cfg = load_cfg(a.config)
    ok, msg = build_engine(cfg).resume()
    print(msg)
    return 0 if ok else 1


def cmd_control(a):
    cfg = load_cfg(a.config)
    ok, msg = build_engine(cfg).request_control()
    print(msg)
    return 0 if ok else 1


def cmd_install(a):
    cfg = load_cfg(a.config)
    ok, msg = Installers(cfg, winsys.make(Clock()), cfg.state_dir, HERE).install(a.what, a.yes)
    print(msg)
    return 0 if ok else 1


def cmd_restore(a):
    cfg = load_cfg(a.config)
    ok, msg = Installers(cfg, winsys.make(Clock()), cfg.state_dir, HERE).restore(a.yes)
    print(msg)
    if a.bios:
        golden = os.path.join(cfg.state_dir, "golden.txt")
        print("BIOS: the pre-sweep settings are in %s. Writing them back is a BIOS write: import that file with "
              "SCEWIN yourself after checking it, or enter your saved BIOS profile by hand." % golden)
    return 0 if ok else 1


def cmd_names(a):
    sc = scewin.Script.load(a.export)
    qs = sc.search(a.grep) if a.grep else sc.questions
    seen = {}
    for q in qs:
        seen[q.name] = seen.get(q.name, 0) + 1
    for q in qs:
        dup = " (name used %d times: set index)" % seen[q.name] if seen[q.name] > 1 else ""
        print("%-55s token %-5s value %-22s live %s%s" % (q.name[:55], q.token, str(sc.value(q))[:22], sc.label(q), dup))
    return 0


def cmd_vendors(a):
    print("Board vendor plugins (choose with [backend] vendor = \"...\"; details in docs/VENDORS.md):")
    for name, title, status, notes in vendors.table():
        print("  %-9s %-9s %-16s %s" % (name, title, status, notes))
    print("Any board works with [backend] type = \"manual\": you type each value in the BIOS.")
    return 0


def cmd_doctor(a):
    cfg = load_cfg(a.config) if a.config else None
    out_dir = a.out or (os.path.join(cfg.state_dir, "doctor") if cfg else os.path.abspath("doctor-report"))
    os.makedirs(out_dir, exist_ok=True)
    clock = Clock()
    sysif = winsys.make(clock) if not a.no_probes else winsys.NullSystem(clock)
    ctx = {"days": a.days, "scewin_file": a.scewin_file, "ledger": a.ledger}
    if cfg and cfg.temps.get("dimm") and not a.no_probes:
        ctx["dimm_reader"] = testsuite.TempReader(cfg.temps["dimm"], cfg.resolve)
    if a.scewin_export:
        if not cfg or os.name != "nt":
            sys.exit("--scewin-export needs a config with backend.scewin, on Windows")
        b = backends.ScewinBackend(cfg, out_dir, sysif)  # export only: no password needed
        if b.export("doctor-export.txt") is None:
            print("SCEWIN export failed (run elevated?); continuing without it")
        else:
            ctx["scewin_file"] = os.path.join(b.stage, "doctor-export.txt")
    extra = {}
    if a.facts:
        with open(a.facts, encoding="utf-8") as f:
            extra = json.load(f)
    if cfg:
        st_path = os.path.join(cfg.state_dir, "state.json")
        if os.path.exists(st_path):
            with open(st_path, encoding="utf-8") as f:
                peak = json.load(f).get("last_dimm_peak")
            if peak is not None:
                extra.setdefault("dimm.peak_c", peak)
    user = dict(cfg.doctor.get("facts", {})) if cfg else {}
    facts, statuses = probes.gather(sysif, ctx, user, extra)
    entries = kb.load(a.kb or os.path.join(HERE, "quirks"))
    rep = report.run(entries, facts, statuses, {"config": cfg.platform.get("name") if cfg else None})
    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
        f.write(report.to_markdown(rep))
    with open(os.path.join(out_dir, "report.json"), "w", encoding="utf-8") as f:
        f.write(report.to_json(rep))
    with open(os.path.join(out_dir, "implications.json"), "w", encoding="utf-8") as f:
        json.dump(rep["implications"], f, indent=1)
    s = rep["summary"]
    print("Doctor: %d detected, %d apply to your platform, %d not present, %d could not be checked." % (
        s["DETECTED"], s["APPLIES"], s["NOT PRESENT"], s["UNKNOWN"]))
    for i in rep["items"]:
        if i["status"] == "DETECTED" or i["severity"] in ("WARN", "LIMIT"):
            print("  [%s] %-8s %s" % (i["severity"], i["status"].lower(), i["title"]))
    print("Report: %s" % os.path.join(out_dir, "report.md"))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="tuner.py", description="Raptor Lake OC Autopilot: safety-first BIOS tuning sweeps "
                                "for Intel 13th/14th gen CPUs and DDR5, and a read-only quirk doctor.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def c(name, fn, help, cfg=True):
        s = sub.add_parser(name, help=help)
        if cfg:
            s.add_argument("-c", "--config", required=True)
        s.set_defaults(fn=fn)
        return s

    c("check", cmd_check, "validate the config and the plan against the safety rules (no hardware)")
    c("plan", cmd_plan, "list every step with rule notes (no hardware)")
    s = c("start", cmd_start, "save the pre-sweep BIOS script and create the sweep state")
    s.add_argument("--force", action="store_true")
    c("run", cmd_run, "do one boot's worth of the sweep (writes the BIOS and reboots with the SCEWIN backend)")
    s = c("simulate", cmd_simulate, "walk the whole config on a simulated PC (no hardware)")
    s.add_argument("--state-dir")
    s.add_argument("--owner", action="store_true", help="play a cooperative owner: opt in, mark NO POST, resume")
    s.add_argument("--quiet", action="store_true")
    s.add_argument("--no-doctor", action="store_true", help="ignore doctor implications")
    s = c("status", cmd_status, "show the sweep state")
    s.add_argument("--last", type=int, default=12)
    s = c("mark", cmd_mark, "correct the last step's outcome (no-post, crash, fail, no-train)")
    s.add_argument("outcome")
    s.add_argument("--note")
    s = c("optin", cmd_optin, "allow a risky group")
    s.add_argument("group")
    c("resume", cmd_resume, "continue after a pause")
    c("control", cmd_control, "queue a control run of the kept set before the next step")
    s = c("install", cmd_install, "optional safety machinery: failsafe, crashdumps, bcdedit, autostart")
    s.add_argument("what", choices=["failsafe", "crashdumps", "bcdedit", "autostart"])
    s.add_argument("--yes", action="store_true")
    s = c("restore", cmd_restore, "undo everything the installers changed")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--bios", action="store_true", help="also say where the pre-sweep BIOS script is")
    s = c("names", cmd_names, "list question names in a SCEWIN export (read-only)", cfg=False)
    s.add_argument("export")
    s.add_argument("--grep")
    c("vendors", cmd_vendors, "list board vendor plugins and their status", cfg=False)
    s = c("doctor", cmd_doctor, "read-only quirk detection for this machine", cfg=False)
    s.add_argument("-c", "--config")
    s.add_argument("--scewin-file", help="a SCEWIN export to read")
    s.add_argument("--scewin-export", action="store_true", help="run SCEWIN /o (read-only export) first")
    s.add_argument("--facts", help="JSON of extra facts (for dry runs on fake data)")
    s.add_argument("--ledger", help="Shakedown ledger.csv")
    s.add_argument("--days", type=int, default=30, help="event log window")
    s.add_argument("--no-probes", action="store_true", help="do not query this machine (files and facts only)")
    s.add_argument("--kb", help="knowledge base folder (default: quirks/)")
    s.add_argument("--out")
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
