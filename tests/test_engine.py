import copy
import json
import os
import shutil
import tempfile
import unittest

import fakes  # noqa: F401  (sets sys.path)
from rlh import backends, config, engine, scewin, sim
from rlh.journal import FakeClock, Journal, StateStore

BASE = {
    "platform": {"name": "test", "gear": 2, "profile_question": "Load XMP Setting", "profile_value": "XMP 3.0 Profile 1",
                 "slow_boot_s": 120, "no_post_gap_s": 900},
    "backend": {"type": "sim"},
    "settings": {
        "tCL": {"question": "CAS# Latency (tCL)"}, "tRCD": {"question": "RAS# to CAS# Delay (tRCD)"},
        "tRP": {"question": "Row Precharge (tRP)"}, "tRAS": {"question": "RAS# Active Time (tRAS)"},
        "tWR": {"question": "Write Recovery Time (tWR)"},
        "tRTP": {"question": "Read to Precharge (tRTP)"},
        "tRFC": {"question": "Refresh Cycle Time (tRFC)"}, "tRFC2": {"question": "Refresh Cycle Time 2 (tRFC2)"},
        "tREFI": {"question": "tREFI"},
        "TX": {"question": "VDD_IMC Voltage", "kind": "mv", "limit": "vddq_tx_mv"},
        "VDD2": {"question": "VDD_CPU Voltage", "kind": "mv", "limit": "vdd2_mv"},
    },
    "tests": {"t": {"type": "command", "command": "x"}},
    "sweep": {"tests": ["t"], "startup_delay_s": 0, "watch": ["tRFC", "tRFC2", "tCL", "tRCD", "tRP", "tWR"],
              "fallback_signature": {"tRFC": "395"}, "temp_gated": [], "linked": [["tRFC", "tRFC2"], ["tRCD", "tRP"]],
              "baseline": {"tCL": 40, "tRCD": 50, "tRP": 50, "tRAS": 96, "tWR": 24, "tRTP": 23, "tRFC": 680,
                           "tREFI": 32767}},
    "simulate": {"dimm_peak_c": 40, "trained": {"tCL": 40, "tRCD": 50, "tRP": 50, "tRAS": 96, "tWR": 24, "tRTP": 23,
                                                "tRFC": 680, "tRFC2": 680, "tREFI": 32767, "TX": 1450, "VDD2": 1450}},
    "group": [],
}


def make_cfg(groups, simulate=None, limits=None, sweep=None, tmp="."):
    d = copy.deepcopy(BASE)
    d["group"] = groups
    d["simulate"].update(simulate or {})
    d["sweep"].update(sweep or {})
    if limits:
        d["limits"] = limits
    return config.Config(d, base_dir=tmp)


class Harness:
    def __init__(self, cfg, tmp, owner=False):
        cfg.state_dir = tmp
        self.cfg = cfg
        self.clock = FakeClock()
        self.m = sim.SimMachine(cfg, self.clock)
        self.store = StateStore(os.path.join(tmp, "state.json"))
        self.journal = Journal(os.path.join(tmp, "journal.md"), self.clock, echo=False)
        self.eng = engine.Engine(cfg, self.m, self.m, self.m, self.clock, self.journal, self.store)
        ok, msg = self.eng.start()
        assert ok, msg
        self.owner = owner

    def go(self):
        return sim.drive(self.eng, self.m, owner=self.owner)

    def st(self):
        return self.store.load()

    def outcomes(self):
        return [(r["kind"], r["outcome"], r["changes"]) for r in self.st()["results"]]

    def text(self):
        with open(os.path.join(self.cfg.state_dir, "journal.md")) as f:
            return f.read()


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def run_sweep(self, groups, owner=False, **kw):
        h = Harness(make_cfg(groups, tmp=self.tmp, **kw), self.tmp, owner)
        return h, h.go()

    def test_descend_keeps_last_pass_and_stops_at_first_failure(self):
        h, phase = self.run_sweep([{"name": "tRAS", "setting": "tRAS", "values": [84, 76, 74]}],
                                  simulate={"outcomes": {"tRAS=74": "FAIL"}})
        self.assertEqual(phase, "done")
        self.assertEqual(h.st()["good"], {"tRAS": 76})
        self.assertEqual([o[1] for o in h.outcomes() if o[0] == "step"], ["PASS", "PASS", "FAIL"])
        self.assertIn("all 2 runs passed", h.text())

    def test_two_runs_needed_flaky_is_a_fail(self):
        h, _ = self.run_sweep([{"name": "tRAS", "setting": "tRAS", "values": [84]}],
                              simulate={"outcomes": {"tRAS=84": "FLAKY"}})
        r = h.st()["results"][-1]
        self.assertEqual(r["outcome"], "FAIL")
        self.assertIn("run 2 of 2", r["why"])
        self.assertEqual(h.st()["good"], {})

    def test_no_train_restores_then_verifies(self):
        h, phase = self.run_sweep([{"name": "tRFC", "settings": ["tRFC", "tRFC2"], "values": [660, 630]},
                                   {"name": "tRAS", "setting": "tRAS", "values": [84]}],
                                  simulate={"outcomes": {"tRFC=630": "NO TRAIN"}})
        self.assertEqual(phase, "done")
        outs = [o for o in h.outcomes() if o[0] == "step"]
        self.assertEqual(outs[1][1], "NO TRAIN")
        self.assertEqual(h.st()["good"], {"tRFC": 660, "tRFC2": 660, "tRAS": 84})
        t = h.text()
        self.assertIn("fallback signature", t)
        self.assertIn("Restore confirmed", t)
        self.assertLess(t.index("NO TRAIN"), t.index("Restore confirmed"))

    def test_not_applied_needs_no_restore(self):
        h, _ = self.run_sweep([{"name": "tWR", "setting": "tWR", "values": [22]}],
                              simulate={"outcomes": {"tWR=22": "NOT APPLIED"}}, sweep={"watch": ["tRFC"]})
        self.assertEqual(h.outcomes()[-1][1], "NOT APPLIED")
        self.assertNotIn("Restoring", h.text())

    def test_crash_at_boot(self):
        h, phase = self.run_sweep([{"name": "tRCD", "settings": ["tRCD", "tRP"], "values": [48]},
                                   {"name": "tRAS", "setting": "tRAS", "values": [84]}],
                                  simulate={"outcomes": {"tRCD=48": "CRASH"}})
        self.assertEqual(phase, "done")
        self.assertEqual(h.outcomes()[0][1], "CRASH")
        self.assertIn("Windows crashed after this was set", h.text())
        self.assertEqual(h.st()["good"], {"tRAS": 84})

    def test_crash_loop_failsafe_restores(self):
        h, phase = self.run_sweep([{"name": "tRCD", "settings": ["tRCD", "tRP"], "values": [48]}],
                                  simulate={"outcomes": {"tRCD=48": "CRASH_LOOP"}})
        self.assertEqual(phase, "done")
        self.assertEqual(h.outcomes()[0][1], "CRASH")
        t = h.text()
        self.assertIn("failsafe restored", t)
        self.assertIn("Restore confirmed", t)

    def test_crash_during_test(self):
        h, _ = self.run_sweep([{"name": "tREFI", "setting": "tREFI", "values": [65535]}],
                              simulate={"outcomes": {"tREFI=65535": "CRASH_TEST"}})
        r = h.st()["results"][0]
        self.assertEqual(r["outcome"], "CRASH")
        self.assertIn("If nothing follows, the PC crashed during this test", h.text())

    def test_defaults_loaded_is_no_train(self):
        h, _ = self.run_sweep([{"name": "tCL", "setting": "tCL", "values": [36]}],
                              simulate={"outcomes": {"tCL=36": "DEFAULTS"}})
        r = h.st()["results"][0]
        self.assertEqual(r["outcome"], "NO TRAIN")
        self.assertIn("loaded defaults", r["why"])

    def test_no_post_owner_mark_pauses_after_restore(self):
        h = Harness(make_cfg([{"name": "tCL", "setting": "tCL", "values": [36]}],
                             simulate={"outcomes": {"tCL=36": "NO POST"}}, tmp=self.tmp), self.tmp, owner=False)
        phase = h.go()
        self.assertEqual(phase, "done")  # without the owner it is a NO TRAIN with a hint to mark it
        self.assertIn("mark no-post", h.text())
        ok, _ = h.eng.mark("no-post", "power-cycled 5 times")
        self.assertTrue(ok)
        self.assertEqual(h.st()["results"][0]["outcome"], "NO POST")

    def test_no_post_with_owner_in_sim(self):
        h, phase = self.run_sweep([{"name": "tCL", "setting": "tCL", "values": [36]},
                                   {"name": "tRAS", "setting": "tRAS", "values": [84]}], owner=True,
                                  simulate={"outcomes": {"tCL=36": "NO POST"}})
        self.assertEqual(phase, "done")
        self.assertEqual(h.outcomes()[0][1], "NO POST")
        self.assertIn("pauses after the restore", h.text())
        self.assertIn("Owner resumed", h.text())

    def test_mark_cannot_upgrade_and_downgrade_unkeeps(self):
        h, _ = self.run_sweep([{"name": "tRAS", "setting": "tRAS", "values": [84]}])
        ok, msg = h.eng.mark("PASS")
        self.assertFalse(ok)
        ok, _ = h.eng.mark("fail", "late error seen in a long run")
        self.assertTrue(ok)
        self.assertEqual(h.st()["good"], {})

    def test_alt_group_tries_alternatives(self):
        h, _ = self.run_sweep([{"name": "alt", "mode": "alt", "steps": [{"tCL": 38}, {"tRAS": 84}, {"tRAS": 76}]}],
                              simulate={"outcomes": {"tCL=38": "FAIL"}})
        self.assertEqual([o[1] for o in h.outcomes()], ["FAIL", "PASS"])
        self.assertEqual(h.st()["good"], {"tRAS": 84})

    def test_risky_group_waits_for_optin(self):
        h = Harness(make_cfg([{"name": "r", "setting": "tRAS", "values": [84], "risky": True}], tmp=self.tmp), self.tmp)
        self.assertEqual(h.go(), "waiting-optin")
        self.assertEqual(h.st()["results"], [])
        h.eng.optin("r")
        self.assertEqual(h.go(), "done")
        self.assertEqual(h.st()["good"], {"tRAS": 84})

    def test_marginal_base_control_backoff_and_retry(self):
        groups = [{"name": "tCL", "setting": "tCL", "values": [38]},
                  {"name": "tRAS", "setting": "tRAS", "values": [84]},
                  {"name": "tREFI", "setting": "tREFI", "values": [40960]},
                  {"name": "tRFC", "settings": ["tRFC", "tRFC2"], "values": [670]}]
        h, phase = self.run_sweep(groups, simulate={"marginal": ["tCL=38"], "marginal_grace": 2},
                                  limits={"control_every": 3})
        t = h.text()
        self.assertIn("test failures in a row: a control run comes next", t)
        self.assertIn("CONTROL A FAIL", t)
        self.assertIn("Stepping back the latest kept change", t)
        self.assertIn("Retrying the steps", t)
        self.assertNotIn("tCL", h.st()["good"])
        self.assertTrue(any(r.get("unproven") for r in h.st()["results"]))
        self.assertEqual(phase, "done")

    def test_temperature_gate_skips(self):
        h, _ = self.run_sweep([{"name": "tRAS", "setting": "tRAS", "values": [84]},
                               {"name": "tREFI", "setting": "tREFI", "values": [40960]}],
                              simulate={"dimm_peak_c": 52}, sweep={"temp_gated": ["tREFI", "tCL"]})
        r = h.st()["results"][-1]
        self.assertEqual(r["outcome"], "SKIPPED")
        self.assertIn("temperature-gated", r["why"])

    def test_temperature_gate_needs_a_reading(self):
        h, _ = self.run_sweep([{"name": "tREFI", "setting": "tREFI", "values": [40960]}],
                              sweep={"temp_gated": ["tREFI"]})
        self.assertIn("no DIMM temperature reading", h.st()["results"][-1]["why"])

    def test_doctor_skip_implication(self):
        cfg = make_cfg([{"name": "tWR", "setting": "tWR", "values": [26]}], tmp=self.tmp)
        h = Harness(cfg, self.tmp)
        h.eng.implications = {"skip": {"tWR": "test reason"}}
        h.go()
        self.assertEqual(h.st()["results"][-1]["outcome"], "SKIPPED")
        self.assertIn("doctor: test reason", h.st()["results"][-1]["why"])

    def test_abort_restores_and_pauses(self):
        h = Harness(make_cfg([{"name": "tRAS", "setting": "tRAS", "values": [84]}], tmp=self.tmp), self.tmp)
        h.m.run = lambda names, label: ("ABORT", "DIMM at 61.0 C (abort limit 60 C)", {}, 61.0)
        self.assertEqual(h.go(), "paused")
        st = h.st()
        self.assertEqual(st["results"], [])
        self.assertIn("SAFETY STOP", h.text())
        self.assertIn("Restore confirmed", h.text())

    def test_deterministic(self):
        groups = [{"name": "tRFC", "settings": ["tRFC", "tRFC2"], "values": [660, 630]},
                  {"name": "tRAS", "setting": "tRAS", "values": [84, 74]}]
        sim_out = {"outcomes": {"tRFC=630": "NO TRAIN", "tRAS=74": "FAIL"}}
        texts = []
        for _ in range(2):
            tmp = tempfile.mkdtemp()
            h = Harness(make_cfg(groups, simulate=sim_out, tmp=tmp), tmp)
            h.go()
            texts.append((h.text(), json.dumps(h.st(), sort_keys=True)))
            shutil.rmtree(tmp)
        self.assertEqual(texts[0], texts[1])

    def test_twr_alone_leaves_trtp_and_is_not_skipped(self):
        # Reference machine, 13 Nov: tWR 24 -> 22 -> 20 set alone; tRTP read back 23 every time.
        h, phase = self.run_sweep([{"name": "tWR", "setting": "tWR", "values": [22, 20]}])
        self.assertEqual(phase, "done")
        self.assertEqual([o[1] for o in h.outcomes() if o[0] == "step"], ["PASS", "PASS"])
        self.assertEqual(h.st()["good"], {"tWR": 20})
        self.assertEqual(h.st()["readback"]["tRTP"], 23)
        self.assertNotIn("SKIPPED", h.text())

    def test_trtp_is_its_own_setting(self):
        h, phase = self.run_sweep([{"name": "tRTP", "setting": "tRTP", "values": [19, 15]}])
        self.assertEqual(phase, "done")
        self.assertEqual(h.st()["good"], {"tRTP": 15})
        self.assertEqual(h.st()["readback"]["tWR"], 24)

    def test_tras_floor_uses_the_readback_trtp_not_the_baseline(self):
        # The config claims tRTP 11 (our old wrong estimate); the BIOS reads back 23, so the floor is 73, not 61.
        cfg = make_cfg([{"name": "tRAS", "setting": "tRAS", "values": [70]}], tmp=self.tmp,
                       sweep={"baseline": {"tCL": 40, "tRCD": 50, "tRP": 50, "tRAS": 96, "tWR": 24, "tRTP": 11}})
        cfg.state_dir = self.tmp
        clock = FakeClock()
        m = sim.SimMachine(cfg, clock)
        eng = engine.Engine(cfg, m, m, m, clock, Journal(os.path.join(self.tmp, "journal.md"), clock, echo=False),
                            StateStore(os.path.join(self.tmp, "state.json")))
        ok, msg = eng.start()
        self.assertFalse(ok)
        self.assertIn("tRAS 70 < tRCD 50 + tRTP 23", msg)

    def test_tras_floor_moves_with_a_kept_trtp(self):
        # tRTP 19 kept -> floor 50 + 19 = 69: tRAS 72 may run. tRAS 68 is refused when the plan is checked.
        h, phase = self.run_sweep([{"name": "tRTP", "setting": "tRTP", "values": [19]},
                                   {"name": "tRAS", "setting": "tRAS", "values": [72]}])
        self.assertEqual(phase, "done")
        self.assertEqual(h.st()["good"], {"tRTP": 19, "tRAS": 72})
        self.assertEqual(h.st()["readback"]["tRTP"], 19)
        with self.assertRaises(AssertionError) as cm:
            Harness(make_cfg([{"name": "tRTP", "setting": "tRTP", "values": [19]},
                              {"name": "tRAS", "setting": "tRAS", "values": [72, 68]}], tmp=self.tmp), self.tmp)
        self.assertIn("tRAS 68 < tRCD 50 + tRTP 19", str(cm.exception))

    def test_tras_floor_when_the_trtp_step_fails(self):
        # The plan check assumed tRTP 19 would pass. It fails, so the floor stays 73 and tRAS 72 is refused.
        h, phase = self.run_sweep([{"name": "tRTP", "setting": "tRTP", "values": [19]},
                                   {"name": "tRAS", "setting": "tRAS", "values": [72]}],
                                  simulate={"outcomes": {"tRTP=19": "FAIL"}})
        self.assertEqual(phase, "done")
        self.assertEqual(h.st()["good"], {})
        self.assertIn("tRAS 72 < tRCD 50 + tRTP 23", h.st()["results"][-1]["why"])

    def test_readback_trtp_below_minimum_after_a_step_is_not_tested(self):
        # A board that does move tRTP when another timing is set is still caught, by the read-back.
        h = Harness(make_cfg([{"name": "tWR", "setting": "tWR", "values": [20]}], tmp=self.tmp), self.tmp)
        boot = h.m._boot_effects

        def coupled():
            boot()
            if h.m.nvram.get("tWR") == 20:
                h.m.effective["tRTP"] = "10"
        h.m._boot_effects = coupled
        self.assertEqual(h.go(), "done")
        r = h.st()["results"][-1]
        self.assertEqual(r["outcome"], "SKIPPED")
        self.assertIn("tRTP reads back as 10, below the minimum 12", r["why"])
        self.assertEqual(h.st()["good"], {})

    def test_state_written_atomically(self):
        h, _ = self.run_sweep([{"name": "tRAS", "setting": "tRAS", "values": [84]}])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "state.json.tmp")))
        with open(os.path.join(self.tmp, "state.json")) as f:
            json.load(f)


class FakeScewinBios:
    """Emulates SCEWIN against a synthetic export, including the quirk that matters: an import only applies a
    question whose full prompt (live value included) matches the BIOS exactly."""

    def __init__(self, text):
        self.sc = scewin.Script(text)

    def run(self, args, timeout=0, input=None):
        path = args[args.index("/s") + 1]
        if "/o" in args:
            with open(path, "w", newline="") as f:
                f.write(self.sc.text())
            return 0, "exported successfully"
        imp = scewin.Script.load(path)
        prompts = {self.sc.lines[q.start]: q for q in self.sc.questions}
        skipped = []
        for q in imp.questions:
            cur = prompts.get(imp.lines[q.start])
            if cur is None:
                skipped.append("unmatched question %s" % q.name)
                continue
            v = imp.value(q)
            if v is not None and imp.kind(q) in ("value", "options"):
                self.sc.set(cur, v)
        return 0, "Password accepted\n" + "\n".join(skipped)

    def boot(self, cfg):
        """After a reboot the prompts show what is stored (non-Auto values)."""
        for name, s in cfg.settings.items():
            q = self.sc.find(s.question, s.index, s.token)
            v = self.sc.value(q)
            if v and self.sc.label(q) is not None:
                self.sc.set_label(q, s.shown(v))


class ScewinBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = make_cfg([], tmp=self.tmp)
        self.cfg.backend = {"type": "scewin", "scewin": "SCEWIN_64.exe"}
        self.bios = FakeScewinBios(fakes.export())
        self._run = backends.run
        backends.run = self.bios.run
        self.b = backends.ScewinBackend(self.cfg, self.tmp, None)

    def tearDown(self):
        backends.run = self._run
        shutil.rmtree(self.tmp)

    def test_prepare_apply_verify_and_read(self):
        ok, msg = self.b.prepare()
        self.assertTrue(ok, msg)
        ok, msg = self.b.apply({"tCL": 38, "TX": 1350}, {"tCL": 38})
        self.assertTrue(ok, msg)
        self.bios.boot(self.cfg)
        snap = self.b.read()
        self.assertEqual(snap["effective"]["tCL"], "38")
        self.assertEqual(snap["effective"]["TX"], "1.350V")
        self.assertEqual(snap["profile"], "XMP 3.0 Profile 1")

    def test_stale_script_would_be_skipped_but_fresh_export_is_used(self):
        self.b.prepare()
        self.b.apply({"tCL": 38}, {"tCL": 38})
        self.bios.boot(self.cfg)
        # the live value in the prompt is now 38; a script built from the old export would not match
        ok, msg = self.b.apply({"tCL": 36}, {"tCL": 36})
        self.assertTrue(ok, msg)

    def test_golden_drift_is_restored(self):
        self.b.prepare()
        self.bios.sc.set(self.bios.sc.find("Load XMP Setting"), "Auto")  # the BIOS loaded defaults
        ok, msg = self.b.apply({"tCL": 40}, {})
        self.assertTrue(ok, msg)
        self.assertEqual(self.bios.sc.value(self.bios.sc.find("Load XMP Setting")), "XMP 3.0 Profile 1")

    def test_layout_change_refuses(self):
        self.b.prepare()
        self.bios.sc = scewin.Script(fakes.export(crc="12345678"))
        ok, msg = self.b.apply({"tCL": 38}, {"tCL": 38})
        self.assertFalse(ok)
        self.assertIn("layout changed", msg)

    def test_write_that_does_not_stick_is_reported(self):
        self.b.prepare()
        self.bios.run_orig = self.bios.run
        backends.run = lambda args, timeout=0, input=None: (0, "") if "/i" in args else self.bios.run_orig(args)
        ok, msg = self.b.apply({"tCL": 38}, {"tCL": 38})
        self.assertFalse(ok)
        self.assertIn("not stored", msg)

    def test_failsafe_script_matches_the_step_boot(self):
        self.b.prepare()
        self.b.apply({"tCL": 38}, {"tCL": 38})
        self.assertTrue(self.b.arm_failsafe({}, {"tCL": 38}, "tCL#1"))
        self.bios.boot(self.cfg)  # the step's boot: prompt shows 38
        lastgood = scewin.Script.load(os.path.join(self.tmp, "failsafe", "lastgood.txt"))
        q = lastgood.find("CAS# Latency (tCL)")
        self.assertEqual(lastgood.label(q), "38")
        self.assertEqual(lastgood.value(q), 0)
        code, out = self.bios.run(["x", "/i", "/s", os.path.join(self.tmp, "failsafe", "lastgood.txt")])
        self.assertNotIn("unmatched question CAS", out)
        self.assertEqual(self.bios.sc.value(self.bios.sc.find("CAS# Latency (tCL)")), 0)

    def test_failsafe_values_file_has_kept_and_pre_sweep_numbers(self):
        self.b.prepare()
        self.b.apply({"TX": 1350, "tCL": 38}, {"tCL": 38})
        self.assertTrue(self.b.arm_failsafe({"TX": 1350}, {"tCL": 38}, "tCL#1"))
        with open(os.path.join(self.tmp, "failsafe", "lastgood-values.txt")) as f:
            rows = dict(line.strip().split("=") for line in f if line.strip())
        self.assertEqual(rows.get("2CDB"), "1350")   # kept value
        self.assertEqual(rows.get("2CA5"), "0")      # the step's setting goes back to its pre-sweep value (Auto)


class VendorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_asrock_is_the_default_and_needs_a_password(self):
        from rlh import vendors
        cfg = make_cfg([], tmp=self.tmp)
        cfg.backend = {"type": "scewin", "scewin": "SCEWIN_64.exe"}
        b = backends.make(cfg, self.tmp, None)
        self.assertIsInstance(b, vendors.asrock.AsrockBackend)
        old = os.environ.pop(backends.PASSWORD_ENV, None)
        try:
            ok, msg = b.prepare()
            self.assertFalse(ok)
            self.assertIn("password", msg)
        finally:
            if old is not None:
                os.environ[backends.PASSWORD_ENV] = old

    def test_password_from_environment(self):
        cfg = make_cfg([], tmp=self.tmp)
        cfg.backend = {"type": "scewin", "scewin": "SCEWIN_64.exe"}
        b = backends.make(cfg, self.tmp, None)
        os.environ[backends.PASSWORD_ENV] = "from-env"
        try:
            self.assertEqual(b._password(), "from-env")
        finally:
            del os.environ[backends.PASSWORD_ENV]

    def test_unimplemented_vendors_say_so(self):
        from rlh import vendors
        for name in ("msi", "asus", "gigabyte"):
            cfg = make_cfg([], tmp=self.tmp)
            cfg.backend = {"type": "scewin", "vendor": name}
            with self.assertRaises(vendors.VendorNotImplemented) as e:
                backends.make(cfg, self.tmp, None)
            self.assertIn("docs/VENDORS.md", str(e.exception))
        with self.assertRaises(vendors.VendorNotImplemented):
            vendors.get("no-such-vendor")


class ConfigRuleTests(unittest.TestCase):
    def test_less_safe_limit_needs_acceptance(self):
        with self.assertRaises(config.ConfigError):
            make_cfg([], limits={"vddq_tx_mv": 1400})
        with self.assertRaises(config.ConfigError):
            make_cfg([], limits={"runs_per_step": 1})
        with self.assertRaises(config.ConfigError):
            make_cfg([], limits={"control_every": 0})
        with self.assertRaises(config.ConfigError):
            make_cfg([], limits={"sg_turnarounds_trained": False})
        cfg = make_cfg([], limits={"vddq_tx_mv": {"value": 1400, "i_accept_the_risk": True, "reason": "x"},
                                   "vdd2_mv": 1400})
        self.assertEqual(cfg.limits["vddq_tx_mv"], 1400)
        self.assertIn("vddq_tx_mv", cfg.accepted)
        self.assertNotIn("vdd2_mv", cfg.accepted)
        with self.assertRaises(config.ConfigError):
            make_cfg([], limits={"no_such_limit": 1})

    def errors(self, groups, **kw):
        from rlh import rules
        return [str(i) for i in rules.check_plan(make_cfg(groups, **kw)) if i.level == "error"]

    def test_voltage_limits(self):
        self.assertTrue(self.errors([{"name": "tx", "setting": "TX", "values": [1360]}]))
        self.assertFalse(self.errors([{"name": "tx", "setting": "TX", "values": [1350, 1300]}]))
        self.assertTrue(self.errors([{"name": "v", "setting": "VDD2", "values": [1460]}]))

    def test_odd_tcl_in_gear2(self):
        self.assertTrue(any("odd" in e for e in self.errors([{"name": "c", "setting": "tCL", "values": [39]}])))

    def test_tras_floor(self):
        # Reference machine: tRCD 50 + read-back tRTP 23 = 73. tRAS 76 is allowed, 72 and 64 are below the floor.
        self.assertFalse(self.errors([{"name": "r", "setting": "tRAS", "values": [76]}]))
        e = self.errors([{"name": "r", "setting": "tRAS", "values": [72]}])
        self.assertTrue(any("tRAS 72 < tRCD 50 + tRTP 23" in x for x in e))
        self.assertTrue(self.errors([{"name": "r", "setting": "tRAS", "values": [64]}]))

    def test_tras_floor_plan_check_uses_readback_and_earlier_steps(self):
        from rlh import rules
        groups = [{"name": "r", "setting": "tRAS", "values": [72]}]
        cfg = make_cfg(groups)
        self.assertFalse([i for i in rules.check_plan(cfg, readback={"tRTP": 19}) if i.level == "error"])
        self.assertTrue([i for i in rules.check_plan(cfg, readback={"tRTP": 23}) if i.level == "error"])
        self.assertFalse(self.errors([{"name": "p", "setting": "tRTP", "values": [19]}] + groups))

    def test_one_change_per_step(self):
        self.assertTrue(self.errors([{"name": "x", "steps": [{"tCL": 38, "tRAS": 84}]}]))
        self.assertFalse(self.errors([{"name": "x", "steps": [{"tRFC": 660, "tRFC2": 660}]}]))

    def test_sg_turnarounds_refused(self):
        d = {"tRDRD_sg": {"question": "tRDRD_sg", "index": 1}}
        cfg = make_cfg([])
        cfg.settings["tRDRD_sg"] = config.Setting("tRDRD_sg", d["tRDRD_sg"])
        from rlh import rules
        e = rules.step_issues(cfg, None, {"tRDRD_sg": 14}, {}, "x")
        self.assertTrue(any("no POST" in i.msg for i in e if i.level == "error"))

    def test_twr_has_no_floor_from_trtp(self):
        # tWR set alone leaves tRTP unchanged on this board, so nothing ties a tWR step to trtp_min.
        self.assertFalse(self.errors([{"name": "w", "setting": "tWR", "values": [22, 20, 18]}]))

    def test_twr_min_is_only_the_owners_own_floor(self):
        e = self.errors([{"name": "w", "setting": "tWR", "values": [22]}], sweep={"twr_min": 24})
        self.assertTrue(e)
        self.assertFalse(any("tRTP" in x for x in e))

    def test_trtp_min(self):
        e = self.errors([{"name": "p", "setting": "tRTP", "values": [19, 15, 11]}])
        self.assertEqual(len(e), 1)
        self.assertIn("tRTP 11 is below trtp_min 12", e[0])

    def test_sa_step_size(self):
        d = copy.deepcopy(BASE)
        d["settings"]["SA"] = {"question": "SA", "kind": "mv", "limit": "vccsa_mv"}
        d["sweep"]["baseline"]["SA"] = 1240
        d["group"] = [{"name": "sa", "setting": "SA", "values": [1270, 1290]}]
        from rlh import rules
        e = [i for i in rules.check_plan(config.Config(d)) if i.level == "error"]
        self.assertTrue(any("30 mV in one step" in i.msg for i in e))

    def test_unknown_names_rejected(self):
        with self.assertRaises(config.ConfigError):
            make_cfg([{"name": "x", "setting": "nope", "values": [1]}])
        with self.assertRaises(config.ConfigError):
            make_cfg([{"name": "x", "setting": "tCL", "values": [38], "tests": ["missing"]}])


if __name__ == "__main__":
    unittest.main()


class ManualBackendTests(unittest.TestCase):
    class UI:
        def __init__(self, answers):
            self.answers, self.said = list(answers), []

        def ask(self, prompt, choices=("y", "n")):
            return self.answers.pop(0)

        def say(self, text):
            self.said.append(text)

    class Tester:
        def __init__(self, status="PASS"):
            self.status = status

        def run(self, names, label):
            return self.status, "manual test %s" % self.status, {}, 41.0

    def test_manual_flow(self):
        from rlh import winsys
        tmp = tempfile.mkdtemp()
        try:
            cfg = make_cfg([{"name": "tRAS", "setting": "tRAS", "values": [84, 76]}], tmp=tmp)
            cfg.backend = {"type": "manual"}
            cfg.state_dir = tmp
            clock = FakeClock()
            # answers per boot: no_post, own_ok, others_changed, defaults
            ui = self.UI(["n", "y", "n", "n",  "n", "n", "n", "n",  "n", "y", "n", "n"])
            b = backends.ManualBackend(cfg, tmp, winsys.NullSystem(clock), ui)
            store = StateStore(os.path.join(tmp, "state.json"))
            eng = engine.Engine(cfg, b, winsys.NullSystem(clock), self.Tester(), clock,
                                Journal(os.path.join(tmp, "journal.md"), clock, echo=False), store)
            self.assertTrue(eng.start()[0])
            self.assertEqual(eng.run(), "rebooting")
            self.assertIn("RAS# Active Time (tRAS)  84  <- this step", ui.said[-2])
            self.assertEqual(eng.run(), "rebooting")   # 84 passes, 76 is printed
            self.assertEqual(store.load()["good"], {"tRAS": 84})
            self.assertEqual(eng.run(), "done")   # 76: the BIOS shows another value -> NOT APPLIED, then done
            st = store.load()
            self.assertEqual(st["results"][-1]["outcome"], "NOT APPLIED")
            self.assertIn("RAS# Active Time (tRAS)  84", ui.said[-1])   # the final values to enter
        finally:
            shutil.rmtree(tmp)
