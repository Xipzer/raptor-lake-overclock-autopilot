"""The sweep state machine. One call to Engine.run() does one boot's worth of work and returns what happened.

Phases (state["phase"]):
  apply           pick the next step, write it, arm the failsafe, journal, reboot
  booting         the machine just booted into a step: check crashes, read effective values, classify, test
  testing         set while tests run; if the next boot still sees it, the PC died during the test
  restoring       the last good set was written after a bad boot; verify it on this boot
  waiting-optin   a risky group needs `tuner.py optin <group>`
  paused          a person must look (NO POST, safety abort, tool error, control failed for good)
  done / stopped  finished / refused to continue (write failed, restore failed)

Outcomes: PASS, FAIL (test), CRASH, NO TRAIN, NOT APPLIED, NO POST; SKIPPED for steps a rule or gate refused.
"""
import re

from . import rules
from .rules import describe

STEP_OUTCOMES = ("PASS", "FAIL", "CRASH", "NO TRAIN", "NOT APPLIED", "NO POST", "SKIPPED")
MARKABLE = ("NO POST", "CRASH", "FAIL", "NO TRAIN")


class Engine:
    def __init__(self, cfg, backend, system, tester, clock, journal, store, implications=None):
        self.cfg, self.backend, self.system, self.tester = cfg, backend, system, tester
        self.clock, self.journal, self.store = clock, journal, store
        self.implications = implications or {}

    # State
    def new_state(self):
        return {"version": 1, "phase": "apply", "g": 0, "k": 0, "good": {}, "history": [], "pending": None,
                "set_at": None, "results": [], "since_control": 0, "fail_streak": 0, "baseline_labels": {},
                "readback": {}, "last_dimm_peak": None, "optin": [], "pause_reason": "", "queue": [],
                "queue_control": False, "control": {"n": 0, "backoff": 0, "unproven": []},
                "pause_after_restore": "", "boot_labels": {}}

    def start(self):
        ok, msg = self.backend.prepare()
        if not ok:
            return False, msg
        snap = self.backend.read() if self.backend.automatic else None
        issues = rules.check_plan(self.cfg, self.implications, self._ints(snap["effective"]) if snap else None)
        errors = [i for i in issues if i.level == "error"]
        if errors:
            return False, "the plan breaks a safety rule:\n" + "\n".join(str(e) for e in errors)
        st = self.new_state()
        if snap:
            st["baseline_labels"] = {n: snap["effective"].get(n) for n in self.cfg.watch}
            st["readback"] = self._ints(snap["effective"])
        self.store.save(st)
        p = self.cfg.platform
        self.journal.heading("Sweep start: %s (%s, %s, %s), backend %s" % (
            p.get("name", "this PC"), p.get("cpu", "?"), p.get("board_model", "?"), p.get("memory", "?"),
            self.backend.name))
        self.journal.write("Plan: %s. Limits: %s." % (
            "; ".join("%s (%s%s)" % (g.name, ", ".join(describe(self.cfg, s) for s in g.steps) or "no steps",
                                     ", risky" if g.risky else "") for g in self.cfg.groups),
            ", ".join("%s %s" % (k, v) for k, v in self.cfg.limits.items())))
        for w in issues:
            if w.level == "warn":
                self.journal.write(str(w))
        return True, msg

    def wanted(self, st, changes=None):
        w = dict(st["good"])
        w.update(changes or {})
        return w

    def _ints(self, eff):
        out = {}
        for n, v in (eff or {}).items():
            if v is not None and re.fullmatch(r"-?\d+", str(v).strip()):
                out[n] = int(v)
        return out

    def known(self, st):
        k = dict(self.cfg.baseline)
        k.update(st.get("readback", {}))
        k.update(st["good"])
        return k

    def group_of(self, pending):
        for g in self.cfg.groups:
            if g.name == pending.get("group"):
                return g
        return None

    def _say(self, text, important=False):
        self.journal.write(text, important)

    # One cycle
    def run(self):
        st = self.store.load()
        if st["phase"] in ("done", "stopped", "paused", "waiting-optin"):
            return st["phase"]
        self.clock.sleep(self.cfg.startup_delay_s)
        restored = self.backend.failsafe_restored()
        if restored:
            self._say("The startup failsafe restored the last good settings (%s)." % restored)
        if st["phase"] in ("booting", "testing", "restoring"):
            r = self._after_reboot(st, restored)
            if r:
                return r
        if st["phase"] == "booting":
            r = self._post_boot(st)
            if r:
                return r
        if st["phase"] == "restoring":
            r = self._verify_restore(st)
            if r:
                return r
        if st["phase"] == "apply":
            return self._apply_next(st)
        return st["phase"]

    def _after_reboot(self, st, restored):
        p = st["pending"] or {}
        crashes = self.system.crashes_since(st["set_at"]) if st["set_at"] else None
        if st["phase"] == "restoring":
            if crashes:
                st["phase"] = "stopped"
                self.store.save(st)
                self._say("**STOPPED: Windows crashed while booting the last good settings** (%s). The base itself "
                          "is not safe any more; a person must check the BIOS." % "; ".join(crashes[:3]), True)
                return "stopped"
            return None
        why = None
        if restored:
            why = "the boot repeated without the test starting, so the failsafe restored the last good settings"
            if crashes:
                why += " (events: %s)" % "; ".join(crashes[:3])
        elif crashes:
            why = "Windows crashed after this was set: %s" % "; ".join(crashes[:3])
        elif st["phase"] == "testing":
            why = "the PC restarted during the test (crash or hang; nothing logged)"
        if why is None:
            return None
        pause = ""
        if p.get("kind") == "control":
            st["results"].append(self._result(p, "CRASH", why))
            self._say("**CONTROL CRASH**: %s. The kept set itself is not stable." % why, True)
            pause = "the control of the kept set crashed; decide what to step back, then resume"
        else:
            self._fail(st, p, "CRASH", why)
        if restored:
            st["phase"] = "restoring"
            st["pause_after_restore"] = pause
            st["set_at"] = self.clock.time()
            self.store.save(st)
            return None
        return self._restore(st, "restore after a crash", pause=pause)

    # Classification
    def _norm(self, v):
        return re.sub(r"\s+", "", str(v)).casefold()

    def classify(self, st, snap, changes):
        """(outcome or None, why). None means the BIOS runs what we asked for."""
        m = snap.get("manual")
        if m:
            if m["no_post"]:
                return "NO POST", "needed manual power cycles to POST (owner)"
            if m["defaults"]:
                return "NO TRAIN", "the BIOS loaded defaults or dropped the memory profile (owner)"
            if m["others_changed"]:
                return "NO TRAIN", "other timings changed too: training fell back (owner)"
            if not m["own_ok"]:
                return "NOT APPLIED", "the BIOS shows a different value than entered (owner)"
            return None, ""
        cfg = self.cfg
        eff = snap["effective"]
        gap = self.system.boot_gap()
        normal = cfg.platform.get("normal_boot_s")
        gap_s = (" (boot %s s%s)" % (gap, ", normal ~%s s" % normal if normal else "")) if gap is not None else ""
        want = self.wanted(st, changes)
        followers = {n for n, s in cfg.settings.items() if set(s.follows) & set(changes)}

        def off(n, expect):
            got = eff.get(n)
            return got is not None and self._norm(got) != self._norm(expect)

        own = {n: (eff.get(n), cfg.settings[n].shown(v)) for n, v in changes.items()
               if v != cfg.settings[n].auto and off(n, cfg.settings[n].shown(v))}
        kept = {n: (eff.get(n), cfg.settings[n].shown(v)) for n, v in want.items()
                if n not in changes and n not in followers and v != cfg.settings[n].auto and off(n, cfg.settings[n].shown(v))}
        watched = {n: (eff.get(n), st["baseline_labels"].get(n)) for n in cfg.watch
                   if n not in want and n not in followers and st["baseline_labels"].get(n) is not None
                   and off(n, st["baseline_labels"][n])}
        sig = {n: eff.get(n) for n, s in cfg.fallback_signature.items()
               if eff.get(n) is not None and self._norm(eff[n]) == self._norm(s)
               and self._norm(cfg.settings[n].shown(want.get(n, ""))) != self._norm(s)}
        pv = cfg.platform.get("profile_value")
        slow = gap is not None and gap > cfg.platform.get("slow_boot_s", 120)
        nopost = gap is not None and gap > cfg.platform.get("no_post_gap_s", 900)
        hint = (" The boot took %d s: if you had to power-cycle it by hand, run `tuner.py mark no-post`." % gap) if nopost else ""
        if pv and snap.get("profile") is not None and self._norm(snap["profile"]) != self._norm(pv):
            return "NO TRAIN", "the memory profile is %r, not %r: the BIOS loaded defaults (Boot Failure Guard?)%s%s" % (
                snap["profile"], pv, gap_s, hint)
        if sig or watched or kept:
            parts = []
            if sig:
                parts.append("fallback signature %s" % sig)
            if watched or kept:
                parts.append("values this step did not touch changed: %s" % dict(list(watched.items()) + list(kept.items())))
            if own:
                parts.append("own: %s" % own)
            return "NO TRAIN", "memory training fell back%s; %s%s" % (gap_s, "; ".join(parts), hint)
        if own:
            if slow:
                return "NO TRAIN", "own value differs and the boot was slow%s: %s.%s" % (gap_s, own, hint)
            return "NOT APPLIED", "the BIOS adjusted the value without retraining%s: %s" % (gap_s, own)
        return None, ""

    # Post boot
    def _post_boot(self, st):
        p = st["pending"]
        changes = p.get("changes", {})
        snap = self.backend.read()
        if snap is None:
            return self._pause(st, "could not read the BIOS after the boot (SCEWIN export failed); nothing was "
                                   "classified", restore=False)
        kind, why = self.classify(st, snap, changes)
        if p["kind"] == "control" and kind:
            self._say("**CONTROL %s**: %s. The kept set itself does not boot as written." % (kind, why), True)
            st["results"].append(self._result(p, kind, why))
            return self._restore(st, "restore after a failed control boot", pause="the kept set failed to boot as "
                                 "written during a control; a person must decide what to step back")
        if kind:
            self.backend.disarm()
            self._fail(st, p, kind, why + " No test ran.")
            if kind == "NOT APPLIED":
                st["phase"] = "apply"
                self.store.save(st)
                return None
            return self._restore(st, "restore after %s" % kind,
                                 pause="NO POST: the owner had to power-cycle; check before going on" if kind == "NO POST" else "")
        eff_ints = self._ints(snap["effective"])
        # tRTP is its own setting. The guard works on the value read back after this boot, whatever the step was:
        # it fires when the step set tRTP, or when tRTP read back lower than before the step.
        trtp = self.cfg.by_role("tRTP")
        if trtp and trtp.name in eff_ints and eff_ints[trtp.name] < self.cfg.limits["trtp_min"]:
            before = st.get("readback", {}).get(trtp.name)
            if trtp.name in changes or (before is not None and eff_ints[trtp.name] < before):
                self.backend.disarm()
                self._fail(st, p, "SKIPPED", "rule: after this step tRTP reads back as %d, below the minimum %d; "
                           "not tested" % (eff_ints[trtp.name], self.cfg.limits["trtp_min"]))
                st["phase"] = "apply"
                self.store.save(st)
                return None
        st["boot_labels"] = {n: snap["effective"].get(n) for n in self.cfg.settings}
        st["boot_readback"] = eff_ints
        st["phase"] = "testing"
        st["test_started"] = self.clock.time()
        self.store.save(st)
        self.backend.disarm()
        what = "CONTROL %s (kept set, no change)" % p.get("tag", "") if p["kind"] == "control" else describe(self.cfg, changes)
        self._say("TESTING %s (the BIOS confirms it). If nothing follows, the PC crashed during this test." % what)
        return self._test(st)

    def _tests_for(self, p):
        g = self.group_of(p)
        names = list(g.tests) if g and g.tests else list(self.cfg.default_tests)
        if g and g.cpu:
            names += [t for t in self.cfg.cpu_tests if t not in names]
        return names

    def _test(self, st):
        p = st["pending"]
        runs = self.cfg.limits["runs_per_step"]
        label = "control" if p["kind"] == "control" else describe(self.cfg, p["changes"])
        summaries = []
        status = "PASS"
        for r in range(runs):
            status, summary, metrics, peak = self.tester.run(self._tests_for(p), label)
            if peak is not None:
                st["last_dimm_peak"] = peak
            summaries.append(summary)
            if status != "PASS":
                summary = "run %d of %d: %s" % (r + 1, runs, summary)
                break
            if r < runs - 1:
                self._say("Run %d of %d passed (%s); confirming." % (r + 1, runs, summary))
                self.store.save(st)
        detail = summaries[-1] if status == "PASS" else summary
        if status in ("ABORT", "ERROR"):
            self._say("**%s %s**: %s. Not classified (a limit or a tool stopped it, not the setting)."
                      % ("SAFETY STOP" if status == "ABORT" else "TOOL ERROR", label, detail), True)
            return self._restore(st, "restore after a %s" % status.lower(),
                                 pause="%s during %s: %s" % (status, label, detail))
        if p["kind"] == "control":
            return self._control_done(st, status == "PASS", detail)
        if status == "PASS":
            self._keep(st, p, "all %d runs passed; last: %s" % (runs, detail) if runs > 1 else detail)
        else:
            self._fail(st, p, "FAIL", detail)
        st["phase"] = "apply"
        self.store.save(st)
        return self._apply_next(st)

    def _result(self, p, outcome, why):
        return {"step": p.get("id"), "group": p.get("group"),
                "kind": p.get("kind"), "changes": p.get("changes", {}), "outcome": outcome, "why": why,
                "time": self.clock.stamp("%Y-%m-%d %H:%M:%S")}

    def _keep(self, st, p, detail):
        g = self.group_of(p)
        for n, v in p["changes"].items():
            st["history"].append({"setting": n, "old": st["good"].get(n), "new": v, "step": p["id"],
                                  "old_label": st["baseline_labels"].get(n)})
            st["good"][n] = v
        st["results"].append(self._result(p, "PASS", detail))
        st["readback"].update(st.get("boot_readback", {}))
        st["baseline_labels"] = {n: st["boot_labels"].get(n) for n in self.cfg.watch}
        st["since_control"] += 1
        st["fail_streak"] = 0
        self._say("**PASS %s**: %s" % (describe(self.cfg, p["changes"]), detail))
        if p.get("retry"):
            pass
        elif g and g.mode == "alt":
            st["g"], st["k"] = st["g"] + 1, 0
        else:
            st["k"] += 1
        every = self.cfg.limits["control_every"]
        if every and st["since_control"] >= every:
            st["queue_control"] = True
            self._say("%d steps kept since the last control: a control run of the kept set comes next." % st["since_control"])

    def _fail(self, st, p, outcome, why):
        g = self.group_of(p)
        st["results"].append(self._result(p, outcome, why))
        what = describe(self.cfg, p.get("changes", {}))
        tail = ""
        if p.get("retry"):
            tail = " (retry after a control; not retried again)"
        elif g and g.mode == "alt":
            st["k"] += 1
            tail = " Trying the next alternative."
        else:
            st["g"], st["k"] = st["g"] + 1, 0
            tail = " The group stops at its last good value."
        self._say("**%s %s**: %s.%s" % (outcome, what, why.rstrip("."), tail), outcome != "SKIPPED")
        if outcome == "FAIL":
            st["fail_streak"] += 1
            n = self.cfg.limits["cluster_fails"]
            if n and st["fail_streak"] >= n:
                st["queue_control"] = True
                self._say("%d test failures in a row: a control run comes next, so a marginal base is not blamed on "
                          "the newest steps." % st["fail_streak"], True)

    def _unkeep(self, st, h):
        """Undo one kept change, including what a boot is expected to show for it."""
        if h["old"] is None:
            st["good"].pop(h["setting"], None)
        else:
            st["good"][h["setting"]] = h["old"]
        if h["setting"] in st["baseline_labels"]:
            st["baseline_labels"][h["setting"]] = h.get("old_label")

    def _control_done(self, st, passed, detail):
        c = st["control"]
        tag = st["pending"].get("tag", "")
        if passed:
            st["results"].append(self._result(st["pending"], "PASS", detail))
            st["since_control"], st["fail_streak"], c["backoff"] = 0, 0, 0
            st["baseline_labels"] = {n: st["boot_labels"].get(n) for n in self.cfg.watch}
            self._say("**CONTROL %s PASS**: %s. The kept set holds%s." % (
                tag, detail, "" if c["unproven"] else "; failures since the last control stand"))
            if c["unproven"]:
                st["queue"] = c["unproven"]
                c["unproven"] = []
                self._say("Retrying the steps whose failures were on the marginal base: %s." % "; ".join(
                    describe(self.cfg, x["changes"]) for x in st["queue"]))
            st["phase"] = "apply"
            self.store.save(st)
            return self._apply_next(st)
        st["results"].append(self._result(st["pending"], "FAIL", detail))
        since = []
        for r in reversed(st["results"][:-1]):
            if r["kind"] == "control" and r["outcome"] == "PASS":
                break
            if r["kind"] == "step" and r["outcome"] == "FAIL":
                r["unproven"] = True
                since.append({"group": r["group"], "changes": r["changes"]})
        c["unproven"] = list(reversed(since)) + c["unproven"]
        self._say("**CONTROL %s FAIL**: %s. The kept set is marginal, so recent FAILs are not proof against their "
                  "values (marked unproven: %d)." % (tag, detail, len(since)), True)
        if st["history"] and c["backoff"] < self.cfg.max_backoff:
            h = st["history"].pop()
            self._unkeep(st, h)
            c["backoff"] += 1
            st["results"].append({"step": h["step"], "group": None, "kind": "backoff", "changes": {h["setting"]: h["new"]},
                                  "outcome": "UNKEPT", "why": "stepped back after a failed control",
                                  "time": self.clock.stamp("%Y-%m-%d %H:%M:%S")})
            self._say("Stepping back the latest kept change: %s %s -> %s. Control again." % (
                h["setting"], h["new"], "Auto" if h["old"] is None else h["old"]))
            st["queue_control"] = True
            st["phase"] = "apply"
            self.store.save(st)
            return self._apply_next(st)
        return self._restore(st, "restore after a failed control",
                             pause="the control failed %d times after stepping back; the base is marginal. Decide "
                                   "what to step back (see the journal), then `tuner.py resume`." % (c["backoff"] + 1))

    # Applying
    def next_step(self, st):
        cfg = self.cfg
        if st.get("queue_control"):
            st["control"]["n"] += 1
            return {"kind": "control", "group": None, "changes": {}, "tag": chr(64 + min(st["control"]["n"], 26)),
                    "id": "control-%d" % st["control"]["n"]}
        while st.get("queue"):
            item = st["queue"].pop(0)
            return {"kind": "step", "group": item["group"], "changes": item["changes"], "retry": True,
                    "id": "retry-%d" % len(st["results"])}
        while st["g"] < len(cfg.groups):
            g = cfg.groups[st["g"]]
            if st["k"] >= len(g.steps):
                st["g"], st["k"] = st["g"] + 1, 0
                continue
            changes = g.steps[st["k"]]
            p = {"kind": "step", "group": g.name, "changes": changes, "id": "%s#%d" % (g.name, st["k"] + 1)}
            if all(st["good"].get(n) == v for n, v in changes.items()):
                st["k"] += 1
                continue
            reason = rules.doctor_skip(cfg, g, changes, self.implications)
            reason = reason or rules.gate_issue(cfg, changes, st["good"], st["last_dimm_peak"], self.implications)
            if not reason:
                errs = [i for i in rules.step_issues(cfg, g, changes, self.known(st), p["id"],
                                                     self.implications.get("cap"))
                        if i.level == "error"]
                errs += [i for i in rules.voltage_issues(cfg, self.wanted(st, changes), p["id"],
                                                         self.implications.get("cap")) if i.level == "error"]
                reason = "; ".join(i.msg for i in errs) if errs else None
            if reason:
                self._fail(st, p, "SKIPPED", reason)
                continue
            if g.risky and cfg.limits["risky_needs_optin"] and g.name not in st["optin"]:
                return {"kind": "wait", "group": g.name}
            return p
        return None

    def _apply_next(self, st):
        nxt = self.next_step(st)
        if nxt and nxt["kind"] == "wait":
            st["phase"] = "waiting-optin"
            self.store.save(st)
            g = self.group_of(nxt)
            self._say("Paused before the risky group %s (%s). It can stop the PC from training and may need your "
                      "hands. Run `tuner.py optin \"%s\"` and then `tuner.py run` to go on." % (
                          g.name, g.note or "see the config", g.name), True)
            return "waiting-optin"
        if nxt is None:
            st["phase"] = "done"
            st["pending"] = None
            self.store.save(st)
            ok, msg = self.backend.apply(self.wanted(st), {})
            kept = describe(self.cfg, st["good"]) if st["good"] else "nothing"
            self._say("**SWEEP DONE.** Kept: %s. Final settings written: %s. Next: long validation (an hour of "
                      "the heaviest test, cold-boot retrains, a run with the GPU loaded) and `tuner.py restore`."
                      % (kept, msg), True)
            if self.backend.automatic and ok:
                self.backend.reboot("apply the sweep result")
                return "rebooting"
            return "done"
        st["pending"] = nxt
        st["phase"] = "booting"
        st["set_at"] = self.clock.time()
        st["queue_control"] = False if nxt["kind"] == "control" else st.get("queue_control", False)
        self.store.save(st)
        changes = nxt["changes"]
        ok, msg = self.backend.apply(self.wanted(st, changes), changes)
        if not ok:
            st["phase"] = "stopped"
            self.store.save(st)
            self._say("**STOPPED**: could not write %s (%s). Nothing was rebooted." % (describe(self.cfg, changes), msg), True)
            return "stopped"
        armed = self.backend.arm_failsafe(self.wanted(st), changes, nxt["id"])
        what = "CONTROL %s (kept set: %s)" % (nxt["tag"], describe(self.cfg, st["good"]) if st["good"] else
                                              "start values") if nxt["kind"] == "control" \
            else describe(self.cfg, changes)
        self._say("SET %s (%s; failsafe %s). Rebooting. If nothing follows, the PC did not come back from this boot."
                  % (what, msg, "armed" if armed else "not armed"))
        self.backend.reboot("test " + what)
        return "rebooting"

    def _restore(self, st, why, pause=""):
        st["phase"] = "restoring"
        st["pause_after_restore"] = pause
        st["set_at"] = self.clock.time()
        self.store.save(st)
        self.backend.disarm()
        ok, msg = self.backend.apply(self.wanted(st), {})
        if not ok:
            st["phase"] = "stopped"
            self.store.save(st)
            self._say("**STOPPED**: could not write the last good settings back (%s). Needs a person." % msg, True)
            return "stopped"
        self._say("Restoring the last good settings (%s): %s. Rebooting." % (why, msg))
        self.backend.reboot(why)
        return "rebooting"

    def _verify_restore(self, st):
        snap = self.backend.read()
        if snap is None:
            return self._pause(st, "could not read the BIOS after the restore", restore=False)
        kind, why = self.classify(st, snap, {})
        if kind:
            st["phase"] = "stopped"
            self.store.save(st)
            self._say("**STOPPED: the last good settings did not come back as written** (%s: %s). Needs a person."
                      % (kind, why), True)
            return "stopped"
        self._say("Restore confirmed: the BIOS runs the last good set (%s)." % (describe(self.cfg, st["good"]) or "start values"))
        if st.get("pause_after_restore"):
            return self._pause(st, st["pause_after_restore"], restore=False)
        st["phase"] = "apply"
        self.store.save(st)
        return None

    def _pause(self, st, reason, restore=True):
        st["phase"] = "paused"
        st["pause_reason"] = reason
        st["pause_after_restore"] = ""
        self.store.save(st)
        self._say("**PAUSED**: %s. Run `tuner.py resume` when it is safe to continue." % reason, True)
        return "paused"

    # Owner commands
    def mark(self, outcome, note=""):
        outcome = outcome.upper().replace("-", " ")
        if outcome not in MARKABLE:
            return False, "can only mark %s (a PASS is earned by a test, not by a mark)" % ", ".join(MARKABLE)
        st = self.store.load()
        steps = [r for r in st["results"] if r["kind"] == "step"]
        if not steps:
            return False, "no step result to mark"
        r = steps[-1]
        old = r["outcome"]
        r["outcome"], r["why"] = outcome, "%s (marked by the owner, was %s: %s)" % (note or "owner", old, r["why"])
        if old == "PASS":
            for h in reversed(st["history"]):
                if h["step"] == r["step"]:
                    st["history"].remove(h)
                    self._unkeep(st, h)
                    break
        msg = "Owner marked %s as %s (was %s)%s." % (describe(self.cfg, r["changes"]), outcome, old,
                                                     ": " + note if note else "")
        if outcome == "NO POST":
            reason = "marked NO POST; check the BIOS, then resume"
            if st["phase"] == "restoring":
                st["pause_after_restore"] = reason
                msg += " The sweep pauses after the restore is verified."
            elif st["phase"] not in ("done", "stopped"):
                st["phase"], st["pause_reason"] = "paused", reason
                msg += " Paused."
        self.store.save(st)
        self._say(msg, True)
        return True, msg

    def optin(self, group):
        st = self.store.load()
        if group not in [g.name for g in self.cfg.groups]:
            return False, "no group named %r" % group
        if group not in st["optin"]:
            st["optin"].append(group)
        if st["phase"] == "waiting-optin":
            st["phase"] = "apply"
        self.store.save(st)
        self._say("Owner opted in to the risky group %s." % group, False)
        return True, "opted in"

    def request_control(self):
        st = self.store.load()
        if st["phase"] not in ("apply", "paused", "waiting-optin"):
            return False, "a control can be queued between steps only (phase %s)" % st["phase"]
        st["queue_control"] = True
        self.store.save(st)
        self._say("Owner queued a control run of the kept set.")
        return True, "the next `run` starts with a control of the kept set (resume first if paused)"

    def resume(self):
        st = self.store.load()
        if st["phase"] not in ("paused", "waiting-optin", "stopped"):
            return False, "nothing to resume (phase %s)" % st["phase"]
        st["phase"], st["pause_reason"] = "apply", ""
        self.store.save(st)
        self._say("Owner resumed the sweep.")
        return True, "resumed; next `tuner.py run` continues"
