import json
import os
import shutil
import tempfile
import unittest

import fakes
from rlh import scewin, testsuite
from rlh.doctor import kb, probes, report

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KB = os.path.join(HERE, "quirks")

CIM = {
    "Win32_Processor": [{"Name": "13th Gen Intel(R) Core(TM) i9-14900KS", "Manufacturer": "GenuineIntel"}],
    "Win32_BaseBoard": [{"Manufacturer": "ASRock", "Product": "Z790I Lightning WiFi"}],
    "Win32_BIOS": [{"SMBIOSBIOSVersion": "10.01"}],
    "Win32_PhysicalMemory": [
        {"PartNumber": "KF584CU40RSA-24", "Speed": 8400, "ConfiguredClockSpeed": 8400, "Capacity": 24 * 1024 ** 3,
         "SMBIOSMemoryType": 34, "ConfiguredVoltage": 1450, "DeviceLocator": "Controller0-DIMMA1"},
        {"PartNumber": "KF584CU40RSA-24", "Speed": 8400, "ConfiguredClockSpeed": 8400, "Capacity": 24 * 1024 ** 3,
         "SMBIOSMemoryType": 34, "ConfiguredVoltage": 1450, "DeviceLocator": "Controller1-DIMMB1"}],
    "Win32_DeviceGuard": [{"VirtualizationBasedSecurityStatus": 2, "SecurityServicesRunning": [2]}],
}
CPU_KEY = r"HKLM\HARDWARE\DESCRIPTION\System\CentralProcessor\0"
CRASH_KEY = r"HKLM\SYSTEM\CurrentControlSet\Control\CrashControl"


def reg(microcode=0x12F):
    return {(CPU_KEY, "Identifier"): "Intel64 Family 6 Model 183 Stepping 1",
            (CPU_KEY, "Update Revision"): bytes([0, 0, 0, 0]) + microcode.to_bytes(4, "little"),
            (CRASH_KEY, "CrashDumpEnabled"): 7, (CRASH_KEY, "AutoReboot"): 1}


def bugcheck(code):
    return {"id": 1001, "provider": "Microsoft-Windows-WER-SystemErrorReporting", "time": "2026-11-12T20:28:00Z",
            "data": {"param1": code}}


class KbTests(unittest.TestCase):
    def test_knowledge_base_is_valid(self):
        entries = kb.load(KB)
        self.assertGreaterEqual(len(entries), 40)
        for e in entries:
            for field in ("symptom", "cause", "title"):
                self.assertNotRegex(e[field], "[\u2013\u2014]", e["id"])

    def test_ops(self):
        self.assertTrue(kb.check("lt", 0x129, "0x12B"))
        self.assertFalse(kb.check("lt", 0x12F, 0x12B))
        self.assertTrue(kb.check("regex", "Fixed Mode", "fixed"))
        self.assertTrue(kb.check("contains", ["0x9F", "0x80"], "0x9f"))
        self.assertTrue(kb.check("in", "DDR5", ["ddr5"]))
        self.assertIsNone(kb.check("gt", kb.MISSING, 1))
        self.assertTrue(kb.check("exists", 0, None))

    def entry(self, **kw):
        e = {"id": "x", "title": "t", "severity": "NOTE", "confidence": "source", "symptom": "s", "cause": "c",
             "remediation": ["r"], "sources": ["s"]}
        e.update(kw)
        kb.validate(e)
        return e

    def facts(self, **kw):
        return {k: {"value": v, "source": "probe:test"} for k, v in kw.items()}

    def test_statuses(self):
        e = self.entry(platform={"cpu.family": ["raptor-lake"]}, detect=[{"fact": "cpu.microcode", "op": "lt", "value": 0x12B}])
        self.assertEqual(kb.evaluate(e, self.facts(**{"cpu.family": "raptor-lake", "cpu.microcode": 0x129}))["status"], "DETECTED")
        self.assertEqual(kb.evaluate(e, self.facts(**{"cpu.family": "raptor-lake", "cpu.microcode": 0x12F}))["status"], "NOT PRESENT")
        self.assertEqual(kb.evaluate(e, self.facts(**{"cpu.family": "raptor-lake"}))["status"], "UNKNOWN")
        self.assertEqual(kb.evaluate(e, self.facts(**{"cpu.family": "alder-lake", "cpu.microcode": 1}))["status"], "N/A")
        g = self.entry(platform={"cpu.family": ["raptor-lake"]})
        r = kb.evaluate(g, self.facts(**{"cpu.family": "raptor-lake"}))
        self.assertEqual(r["status"], "APPLIES")
        self.assertTrue(r["detection"].startswith("platform match"))
        self.assertEqual(kb.evaluate(g, {})["status"], "UNKNOWN")

    def test_detection_source_labels(self):
        e = self.entry(detect=[{"fact": "cooling.cpu_after_gpu", "op": "true"}])
        r = kb.evaluate(e, {"cooling.cpu_after_gpu": {"value": True, "source": "user"}})
        self.assertEqual(r["detection"], "stated by you")

    def test_any_mode(self):
        e = self.entry(detect=[{"fact": "a", "op": "gt", "value": 1}, {"fact": "b", "op": "gt", "value": 1}], detect_mode="any")
        self.assertEqual(kb.evaluate(e, self.facts(a=0, b=2))["status"], "DETECTED")
        self.assertEqual(kb.evaluate(e, self.facts(a=0))["status"], "UNKNOWN")
        self.assertEqual(kb.evaluate(e, self.facts(a=0, b=0))["status"], "NOT PRESENT")

    def test_validation_rejects_bad_entries(self):
        with self.assertRaises(kb.KBError):
            self.entry(severity="LIMIT")  # a LIMIT with a remedy
        with self.assertRaises(kb.KBError):
            self.entry(remediation=[])  # no remedy but not a LIMIT
        with self.assertRaises(kb.KBError):
            self.entry(confidence="sure")
        with self.assertRaises(kb.KBError):
            self.entry(detect=[{"fact": "a", "op": "approximately"}])

    def test_implications_merge_strictest(self):
        r = [{"id": "a", "status": "DETECTED", "entry": {"title": "A", "implications": [
                 {"action": "gate", "settings": ["tREFI"], "below_c": 50}, {"action": "cap", "limit": "vccsa_step_mv", "value": 20}]}},
             {"id": "b", "status": "APPLIES", "entry": {"title": "B", "implications": [
                 {"action": "gate", "settings": ["tREFI"], "below_c": 45}, {"action": "skip", "settings": ["tRDRD_sg"], "reason": "r"}]}},
             {"id": "c", "status": "NOT PRESENT", "entry": {"title": "C", "implications": [
                 {"action": "skip", "settings": ["tCL"], "reason": "no"}]}}]
        imp = kb.implications(r)
        self.assertEqual(imp["gate"], {"tREFI": 45})
        self.assertIn("tRDRD_sg", imp["skip"])
        self.assertNotIn("tCL", imp["skip"])
        self.assertEqual(imp["cap"], {"vccsa_step_mv": 20})


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.export = os.path.join(self.tmp, "export.txt")
        with open(self.export, "w", newline="") as f:
            f.write(fakes.export())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def gather(self, microcode=0x12F, events=None, whea=None, user=None, ctx=None):
        sysif = fakes.FakeSys(CIM, reg(microcode), events if events is not None else [], whea if whea is not None else [],
                              {"recoveryenabled": "Yes"})
        c = {"scewin_file": self.export}
        c.update(ctx or {})
        return probes.gather(sysif, c, user or {})

    def test_cpu_and_microcode(self):
        f, _ = self.gather(0x129)
        self.assertEqual(f["cpu.family"]["value"], "raptor-lake")
        self.assertEqual(f["cpu.microcode"]["value"], 0x129)
        self.assertEqual(f["cpu.generation"]["value"], 14)

    def test_memory_and_known_part(self):
        f, _ = self.gather()
        self.assertEqual(f["mem.type"]["value"], "DDR5")
        self.assertTrue(f["mem.nonbinary"]["value"])
        self.assertTrue(f["mem.cudimm"]["value"])
        self.assertIn("M-die", f["mem.die"]["value"])

    def test_scewin_facts(self):
        f = probes.facts_from_scewin(scewin.Script(fakes.export()))
        self.assertEqual(f["bios.vcore_mode"], "Fixed Mode")
        self.assertTrue(f["bios.vcore_fixed"])
        self.assertEqual(f["bios.vcore_fixed_mv"], 1340)
        self.assertEqual(f["bios.tx_effective_mv"], 1450)
        self.assertTrue(f["bios.tx_auto"])
        self.assertEqual(f["bios.llc"], "Level 1")
        self.assertEqual(f["bios.ac_ll_mohm"], 0.5)
        self.assertTrue(f["bios.bfg_enabled"])
        self.assertEqual(f["bios.bfg_count"], 4)
        self.assertEqual(f["bios.trefi_effective"], 32767)

    def test_whea_by_apic_and_kind(self):
        whea = [{"apic": "4", "mcistat": "0xBE00000000800400"}, {"apic": "4", "mcistat": "0x0000000000000005"},
                {"apic": "36", "mcistat": "0x0000000000000150"}]
        f, _ = self.gather(whea=whea)
        self.assertEqual(f["whea.total"]["value"], 3)
        self.assertEqual(f["whea.top_apic"]["value"], "4")
        self.assertEqual(probes.mci_kind("0x0005"), "internal parity")
        self.assertEqual(probes.mci_kind("0x0150"), "cache")
        self.assertEqual(probes.mci_kind("0x0011"), "TLB")

    def test_bugcheck_codes(self):
        f, _ = self.gather(events=[bugcheck("0x0000009f"), {"id": 41, "provider": "Microsoft-Windows-Kernel-Power",
                                                           "time": "", "data": {}}])
        self.assertEqual(f["win.bugcheck_codes"]["value"], ["0x9F"])
        self.assertEqual(f["win.unclean_shutdowns"]["value"], 1)

    def test_user_fact_never_overrides_measured(self):
        f, st = self.gather(user={"cpu.family": "alder-lake", "cooling.liquid_metal": True})
        self.assertEqual(f["cpu.family"]["value"], "raptor-lake")
        self.assertEqual(f["cooling.liquid_metal"]["source"], "user")
        self.assertTrue(any("differs" in s["status"] for s in st))

    def test_missing_tools_mean_could_not_check(self):
        f, st = probes.gather(fakes.FakeSys(events=None, whea=None), {})
        self.assertEqual(f, {})
        self.assertTrue(all(s["status"].startswith(("could not", "skipped")) for s in st))
        rep = report.run(kb.load(KB), f, st)
        self.assertEqual(rep["summary"]["DETECTED"], 0)
        self.assertGreater(rep["summary"]["UNKNOWN"], 10)

    def test_ledger_drift(self):
        path = os.path.join(self.tmp, "ledger.csv")
        rows = ["run,note,minutes,excluded,result,reason,seconds,score,errors,failed_cores,hottest_core,hottest_c,"
                "water_start,water_max,package_w,gpu_w"]
        for i in range(8):
            hot = 90 + (0 if i < 4 else 8)
            rows.append("r%d,n,10,,PASS,ok,600,9800,0,,P7,%d,35,40,400,530" % (i, hot))
        with open(path, "w") as f:
            f.write("\n".join(rows) + "\n")
        f, _ = probes.probe_ledger(None, {"ledger": path})
        self.assertEqual(f["cpu.load_temp_max_c"], 98)
        self.assertEqual(f["cpu.package_w_max"], 400)
        self.assertGreaterEqual(f["thermal.c_per_100w_drift"], 1.5)


class ReportTests(unittest.TestCase):
    def test_reference_like_machine(self):
        tmp = tempfile.mkdtemp()
        try:
            export = os.path.join(tmp, "export.txt")
            with open(export, "w", newline="") as f:
                f.write(fakes.export())
            sysif = fakes.FakeSys(CIM, reg(0x12F), [bugcheck("0x9F")], [], {"recoveryenabled": "Yes"})
            facts, st = probes.gather(sysif, {"scewin_file": export},
                                      {"cooling.cpu_after_gpu": True, "cooling.sensor_upstream_of_cpu": True},
                                      {"dimm.peak_c": 55.0, "cpu.load_temp_max_c": 106, "cpu.package_w_max": 418})
            rep = report.run(kb.load(KB), facts, st)
            status = {i["id"]: i["status"] for i in rep["items"]}
            for qid in ("asrock-auto-tx-follows-xmp", "hynix-24gb-mdie-heat", "rpl-fixed-vcore-idle",
                        "asrock-fixed-vcore-at-vrm", "win-9f-shutdown-hang", "rpl-hot-under-load",
                        "z790i-lightning-single-eps", "loop-sensor-position", "win-crashdump-hang",
                        "win-winre-after-failed-boots", "asrock-boot-failure-guard", "mrc-fast-boot-hides-retrain"):
                self.assertEqual(status.get(qid), "DETECTED", qid)
            self.assertEqual(status.get("ddr5-ondie-ecc-invisible"), "APPLIES")
            self.assertIn("rpl-microcode-old", [i["id"] for i in rep["not_present"]])
            imp = rep["implications"]
            self.assertEqual(imp["gate"]["tREFI"], 45)
            self.assertIn("tRDRD_sg", imp["skip"])
            self.assertIn("TX", imp["order_first"])
            md = report.to_markdown(rep)
            self.assertIn("## Implications for your tuning plan", md)
            self.assertIn("nothing fixes this", md)
            self.assertNotRegex(md, "[\u2013\u2014]")
            json.loads(report.to_json(rep))
        finally:
            shutil.rmtree(tmp)


class ParserTests(unittest.TestCase):
    def test_ycruncher_stop_on_error_is_not_a_failure(self):
        out = ("Stop on Error: Enabled\n\x1b[92mFFTv4 ... Passed\nN63 ... Passed\nVT3 ... Passed\n")
        st, s, m = testsuite.parse_ycruncher(out, 0)
        self.assertEqual(st, "PASS", s)
        self.assertEqual(m["yc_passed"], 3)

    def test_ycruncher_failures(self):
        self.assertEqual(testsuite.parse_ycruncher("Passed\nPassed\nException Encountered: AlgorithmFailedException\n", 0)[0], "FAIL")
        self.assertEqual(testsuite.parse_ycruncher("Passed\nPassed\nPassed\n", 1)[0], "FAIL")
        self.assertEqual(testsuite.parse_ycruncher("Passed\n", 0)[0], "FAIL")

    def test_memlat(self):
        st, s, m = testsuite.parse_memlat("72.4 125.8 large 000000AB12345678\n", 0)
        self.assertEqual((st, m["latency_ns"], m["read_gbps"]), ("PASS", 72.4, 125.8))
        self.assertEqual(testsuite.parse_memlat("", 1)[0], "FAIL")

    def test_shakedown(self):
        self.assertEqual(testsuite.parse_shakedown("verdict PASS", 0)[0], "PASS")
        self.assertEqual(testsuite.parse_shakedown("FAIL P2 WHEA", 1)[0], "FAIL")
        self.assertEqual(testsuite.parse_shakedown("", 2)[0], "ERROR")

    def test_tm5_0131_wording(self):
        log = ("-------- TestMem5 0.13.1 2026-11-14 23:50 --------\n00:00:00  Configuration: DDR5 Intel @ anta777\n"
               "00:00:00  Testing 1184 MB x 32 (77%) out of 48951 MB\n01:01:16  Testing completed\n")
        self.assertEqual(testsuite.parse_tm5(log)[0], "PASS")
        self.assertEqual(testsuite.parse_tm5(log.replace("01:01:16  Testing completed", "00:41:02  Error in test 3"))[0], "FAIL")
        self.assertEqual(testsuite.parse_tm5(log + "00:20:00  An error 87 occurred in the program\n")[0], "FAIL")
        self.assertEqual(testsuite.parse_tm5(log.replace("Testing completed", "still running"))[0], "ERROR")

    def test_memhold(self):
        ok = "filled; 30 of 30 GiB locked in physical memory\ncheck 1: 0 held\nmemhold done: 22 checks, 0 held flips, 0 transient mismatches\n"
        self.assertEqual(testsuite.parse_memhold(ok, 0)[0], "PASS")
        bad = ok + "12:00:01 FLIP held offset 0x0000001000 (GiB 0, pattern 0) expected 0 read 80\n"
        st, s, m = testsuite.parse_memhold(bad, 2)
        self.assertEqual((st, m["flips"]), ("FAIL", 1))
        self.assertEqual(testsuite.parse_memhold("could not allocate", 1)[0], "ERROR")

    def test_generic(self):
        self.assertEqual(testsuite.parse_generic("Errors: 3", 0, fail_regex=r"Errors: [1-9]")[0], "FAIL")
        self.assertEqual(testsuite.parse_generic("Errors: 0 done", 0, pass_regex="done", fail_regex=r"Errors: [1-9]")[0], "PASS")

    def test_temps(self):
        self.assertEqual(testsuite.parse_temps("12:34:56,0:45.25,1:47.00"), {"0": 45.25, "1": 47.0})
        self.assertEqual(testsuite.parse_temps("44.5\n46"), {"0": 44.5, "1": 46.0})

    def test_watch_aborts(self):
        class R:
            def __init__(self, v):
                self.v = v

            def read(self):
                return self.v
        w = testsuite.Watch(R({"0": 61.0}), None, {"dimm_temp_abort_c": 60, "core_temp_abort_c": 100})
        self.assertIn("abort limit 60", w.sample())
        w = testsuite.Watch(R({"0": 50.0}), R({"P0": 101.0}), {"dimm_temp_abort_c": 60, "core_temp_abort_c": 100})
        self.assertIn("core at 101", w.sample())
        self.assertEqual(w.summary(), "DIMM peak 50.0, at end 50.0 (C)")


if __name__ == "__main__":
    unittest.main()
