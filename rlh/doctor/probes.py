"""Read-only probes that turn a machine into facts. Each probe tolerates a missing tool, missing rights or a
non-Windows host and then reports "could not check" instead of guessing. No probe changes anything, and none
collects serial numbers, user names or network identities.

A probe is a function (sysif, ctx) -> (facts {name: value}, note). `sysif` is winsys.WindowsSystem (or a fake in
tests); `ctx` carries options such as a SCEWIN export path.
"""
import csv
import re
import time

from .. import scewin

# Intel family 6 models (CPUID) -> codename
INTEL_MODELS = {0xB7: "raptor-lake", 0xBF: "raptor-lake", 0xBA: "raptor-lake", 0x97: "alder-lake",
                0x9A: "alder-lake", 0xC6: "arrow-lake", 0xC5: "arrow-lake"}

# Known DDR5 part numbers -> die. Only entries with a teardown or vendor source.
KNOWN_PARTS = [
    (r"^KF584CU40RSA?-24", {"mem.die": "SK hynix 24 Gb M-die", "mem.cudimm": True},
     "TechPowerUp teardown of the Kingston FURY Renegade CUDIMM DDR5-8400 2x24 GB"),
]


def _first(rows, key):
    for r in rows or []:
        v = r.get(key)
        if v not in (None, ""):
            return v
    return None


def probe_cpu(sysif, ctx):
    f = {}
    rows = sysif.cim("Win32_Processor", props=["Name", "Manufacturer", "NumberOfCores", "NumberOfLogicalProcessors"])
    name = _first(rows, "Name") or sysif.reg(r"HKLM\HARDWARE\DESCRIPTION\System\CentralProcessor\0", "ProcessorNameString")
    if name:
        f["cpu.brand"] = re.sub(r"\s+", " ", str(name)).strip()
        m = re.search(r"i[3579]-(1[2-4])\d{3}", f["cpu.brand"])
        if m:
            f["cpu.generation"] = int(m.group(1))
        f["cpu.vendor"] = "intel" if "intel" in f["cpu.brand"].lower() else ("amd" if "amd" in f["cpu.brand"].lower() else "other")
    ident = sysif.reg(r"HKLM\HARDWARE\DESCRIPTION\System\CentralProcessor\0", "Identifier")
    if ident:
        m = re.search(r"Family (\d+) Model (\d+)", str(ident))
        if m and m.group(1) == "6" and "intel" in str(f.get("cpu.vendor", "intel")):
            f["cpu.model"] = int(m.group(2))
            if int(m.group(2)) in INTEL_MODELS:
                f["cpu.family"] = INTEL_MODELS[int(m.group(2))]
    rev = sysif.reg(r"HKLM\HARDWARE\DESCRIPTION\System\CentralProcessor\0", "Update Revision")
    if isinstance(rev, (bytes, bytearray)) and len(rev) >= 8:
        # 8 bytes: the microcode revision is the high dword, little-endian
        f["cpu.microcode"] = int.from_bytes(bytes(rev[4:8]), "little")
    elif isinstance(rev, int):
        f["cpu.microcode"] = rev >> 32 if rev > 0xFFFFFFFF else rev
    note = "ok" if f else "could not check (no CIM/registry access)"
    return f, note


def probe_board(sysif, ctx):
    f = {}
    rows = sysif.cim("Win32_BaseBoard", props=["Manufacturer", "Product"])
    if rows:
        vendor = (_first(rows, "Manufacturer") or "").strip()
        f["board.vendor_raw"] = vendor
        low = vendor.lower()
        f["board.vendor"] = next((v for v in ("asrock", "asus", "msi", "gigabyte", "evga") if v in low or
                                  (v == "msi" and "micro-star" in low)), "other")
        f["board.model"] = (_first(rows, "Product") or "").strip()
    bios = sysif.cim("Win32_BIOS", props=["SMBIOSBIOSVersion", "ReleaseDate"])
    if bios:
        f["bios.version"] = _first(bios, "SMBIOSBIOSVersion")
    return f, "ok" if f else "could not check"


def probe_memory(sysif, ctx):
    rows = sysif.cim("Win32_PhysicalMemory", props=["PartNumber", "Manufacturer", "Speed", "ConfiguredClockSpeed",
                                                    "Capacity", "SMBIOSMemoryType", "ConfiguredVoltage", "DeviceLocator"])
    if not rows:
        return {}, "could not check"
    f = {}
    types = {34: "DDR5", 26: "DDR4"}
    t = _first(rows, "SMBIOSMemoryType")
    if t is not None:
        f["mem.type"] = types.get(int(t), "other")
    f["mem.count"] = len(rows)
    parts = sorted({(r.get("PartNumber") or "").strip() for r in rows if r.get("PartNumber")})
    f["mem.part_numbers"] = parts
    caps = [int(r["Capacity"]) // (1024 ** 3) for r in rows if r.get("Capacity")]
    if caps:
        f["mem.module_gb"] = max(caps)
        f["mem.nonbinary"] = any(c in (12, 24, 48, 96) for c in caps)
    sp = _first(rows, "ConfiguredClockSpeed") or _first(rows, "Speed")
    if sp:
        f["mem.speed"] = int(sp)
    v = _first(rows, "ConfiguredVoltage")
    if v:
        f["mem.configured_mv"] = int(v)
    f["mem.slots_used"] = sorted({(r.get("DeviceLocator") or "").strip() for r in rows})
    for pattern, add, _src in KNOWN_PARTS:
        if any(re.search(pattern, p, re.I) for p in parts):
            f.update(add)
    if "mem.cudimm" not in f and parts:
        f["mem.cudimm"] = any(re.search(r"^KF\d{3}CU|^F5-\d+.*CK\b|CKD", p, re.I) for p in parts) or None
    return f, "ok"


def probe_vbs(sysif, ctx):
    rows = sysif.cim("Win32_DeviceGuard", namespace=r"root\Microsoft\Windows\DeviceGuard",
                     props=["VirtualizationBasedSecurityStatus", "SecurityServicesRunning"])
    if not rows:
        return {}, "could not check"
    r = rows[0]
    running = r.get("SecurityServicesRunning") or []
    return {"win.vbs_running": r.get("VirtualizationBasedSecurityStatus") == 2,
            "win.hvci_running": 2 in running}, "ok"


def probe_crashdump(sysif, ctx):
    key = r"HKLM\SYSTEM\CurrentControlSet\Control\CrashControl"
    f = {}
    v = sysif.reg(key, "CrashDumpEnabled")
    if v is not None:
        f["win.crashdump_enabled"] = int(v)
    v = sysif.reg(key, "AutoReboot")
    if v is not None:
        f["win.autoreboot"] = int(v)
    return f, "ok" if f else "could not check"


def probe_bcd(sysif, ctx):
    b = sysif.bcd()
    if b is None:
        return {}, "could not check (bcdedit needs an elevated prompt)"
    return {"win.bcd_recoveryenabled": b.get("recoveryenabled", "Yes"),
            "win.bcd_bootstatuspolicy": b.get("bootstatuspolicy", "DisplayAllFailures")}, "ok"


def probe_events(sysif, ctx):
    days = ctx.get("days", 30)
    since = time.time() - days * 86400
    ev = sysif.events("System", (41, 1001, 6008), since=since, count=500)
    if ev is None:
        return {}, "could not check"
    codes = []
    for e in ev:
        if e["id"] == 1001 and "SystemErrorReporting" in e["provider"]:
            c = e["data"].get("param1") or e["data"].get("BugcheckCode") or ""
            m = re.search(r"0x([0-9a-f]+)", c, re.I)
            if m:
                codes.append("0x%X" % int(m.group(1), 16))
            elif c.isdigit():
                codes.append("0x%X" % int(c))
    f = {"win.bugcheck_codes": codes, "win.unclean_shutdowns": sum(1 for e in ev if e["id"] in (41, 6008)),
         "win.event_days": days}
    return f, "ok"


def apic_table(whea):
    by = {}
    for e in whea:
        a = e.get("apic")
        if a not in (None, ""):
            by[str(a)] = by.get(str(a), 0) + 1
    return dict(sorted(by.items(), key=lambda kv: (-kv[1], int(kv[0]) if kv[0].isdigit() else 0)))


def mci_kind(mcistat):
    """Shakedown's reading of the low 16 bits of MCi_STATUS: 0005 internal parity, 001x TLB, 01xx cache."""
    try:
        v = int(str(mcistat), 16) & 0xFFFF
    except ValueError:
        return "unknown"
    if v == 0x0005:
        return "internal parity"
    if v & 0xFFF0 == 0x0010:
        return "TLB"
    if v & 0xFF00 == 0x0100:
        return "cache"
    return "other"


def probe_whea(sysif, ctx):
    days = ctx.get("days", 30)
    ev = sysif.whea_since(time.time() - days * 86400)
    if ev is None:
        return {}, "could not check"
    kinds = {}
    for e in ev:
        k = mci_kind(e.get("mcistat") or "")
        kinds[k] = kinds.get(k, 0) + 1
    by = apic_table(ev)
    f = {"whea.total": len(ev), "whea.by_apic": by, "whea.kinds": kinds, "whea.days": days}
    if by:
        f["whea.top_apic"] = next(iter(by))
    return f, "ok"


# SCEWIN export: fact -> regexes on normalised question names, first match in file order wins.
SCEWIN_FACTS = {
    "bios.vcore_mode": [r"^CPU Core/Cache Voltage$", r"^CPU Core Voltage Mode$", r"^Core Voltage Mode$",
                        r"^CPU Core/Cache Voltage Mode$"],
    "bios.vcore_fixed_mv": [r"^Fixed Voltage\(V\)$", r"^CPU Core Voltage Override$"],
    "bios.llc": [r"^CPU Core/Cache Load-Line Calibration$", r"^CPU Load-?line Calibration$", r"^Load-?Line Calibration"],
    "bios.ac_ll": [r"^AC Loadline$", r"^CPU Vcore Compensation$"],
    "bios.dc_ll": [r"^DC Loadline$"],
    "bios.xmp": [r"^Load XMP Setting$", r"^Ai Overclock Tuner$", r"^Extreme Memory Profile"],
    "bios.tx": [r"^VDD_IMC Voltage$", r"^CPU VDDQ Voltage$", r"VDDQ TX"],
    "bios.vdd2": [r"^VDD_CPU Voltage$", r"^Memory Controller Voltage$", r"^CPU VDD2 Voltage$"],
    "bios.vccsa": [r"^VCCSA Voltage$", r"^CPU System Agent Voltage$", r"^SA Voltage", r"^System Agent Voltage$"],
    "bios.dram_vdd": [r"^DRAM VDD Voltage$", r"^DRAM Voltage$"],
    "bios.bfg_enabled": [r"^Boot Failure Guard Message$", r"^Boot Failure Guard$"],
    "bios.bfg_count": [r"^Boot Failure Guard Count$"],
    "bios.mrc_fast_boot": [r"^MRC Fast Boot$", r"^Memory Fast Boot$"],
    "bios.realtime_timing": [r"^Realtime Memory Timing$"],
    "bios.timing_optimization": [r"^ASRock Timing Optimization$"],
    "bios.gear": [r"^DRAM Gear Mode$", r"^Gear Mode$"],
    "bios.publish_hii": [r"^Publish HII Resources$"],
    "bios.voltage_mode": [r"^Voltage Mode$"],
    "bios.trefi": [r"^tREFI$"],
    "bios.tcl": [r"^CAS# Latency \(tCL\)$", r"^DRAM CAS# Latency$"],
}
MV_FACTS = ("bios.tx", "bios.vdd2", "bios.vccsa", "bios.dram_vdd")


def _volts_to_mv(label):
    m = re.match(r"\s*(\d+(?:\.\d+)?)\s*V", str(label or ""))
    return int(round(float(m.group(1)) * 1000)) if m else None


def facts_from_scewin(sc):
    """Facts from a parsed SCEWIN export. Voltages: the stored value (0 = Auto) and the effective value the
    prompt shows (what the BIOS actually runs)."""
    f = {"scewin.questions": len(sc.questions), "scewin.hii_crc": sc.header("HIICrc32")}
    for fact, pats in SCEWIN_FACTS.items():
        q = None
        for p in pats:
            hits = sc.search(p)
            if hits:
                q = hits[0]
                break
        if q is None:
            continue
        val, label = sc.value(q), sc.label(q)
        if fact in MV_FACTS:
            f[fact + "_mv"] = val
            eff = _volts_to_mv(label)
            if eff is not None:
                f[fact + "_effective_mv"] = eff
            f[fact + "_auto"] = val == 0
        elif fact in ("bios.ac_ll", "bios.dc_ll") and isinstance(val, int):
            f[fact + "_mohm"] = val / 100.0 if "Compensation" not in q.name else val
        elif fact in ("bios.trefi", "bios.tcl"):
            if label and label.strip().isdigit():
                f[fact + "_effective"] = int(label)
            f[fact] = val
        else:
            f[fact] = label if (val is None and label) else val
            if label and fact == "bios.llc":
                f["bios.llc"] = label if val in (None, "Auto") else val
    if "bios.vcore_mode" in f and isinstance(f["bios.vcore_mode"], str):
        f["bios.vcore_fixed"] = bool(re.search(r"fixed|override|manual", f["bios.vcore_mode"], re.I))
    if "bios.bfg_enabled" in f:
        f["bios.bfg_enabled"] = str(f["bios.bfg_enabled"]).lower() == "enabled"
    return f


def probe_scewin(sysif, ctx):
    path = ctx.get("scewin_file")
    if not path:
        return {}, "skipped (no export given: pass --scewin-file, or --scewin-export to run SCEWIN /o read-only)"
    try:
        sc = scewin.Script.load(path)
    except OSError as e:
        return {}, "could not read %s: %s" % (path, e)
    f = facts_from_scewin(sc)
    f["scewin.export_ok"] = len(sc.questions) > 0
    return f, "ok (%d questions)" % len(sc.questions)


def probe_dimm(sysif, ctx):
    reader = ctx.get("dimm_reader")
    if reader is None:
        return {}, "skipped (no [temps.dimm] reader configured)"
    raw = reader.read_raw() if hasattr(reader, "read_raw") else reader.read()
    if raw is None:
        return {}, "could not check (reader failed)"
    f = {"dimm.temps": raw, "dimm.all_zero": bool(raw) and all(v == 0 for v in raw.values())}
    nz = [v for v in raw.values() if v != 0]
    if nz:
        f["dimm.now_c"] = max(nz)
    return f, "ok"


def probe_ledger(sysif, ctx):
    """Shakedown ledger.csv: hottest core, package power and core-to-water per 100 W over passing runs (a drift
    upward over weeks at the same load is the first sign of a pump-out or a dried liquid metal job)."""
    path = ctx.get("ledger")
    if not path:
        return {}, "skipped (no --ledger)"
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as fh:
            rows = list(csv.DictReader(fh))
    except OSError:
        return {}, "could not read the ledger"
    f = {}
    hot = [int(r["hottest_c"]) for r in rows if (r.get("hottest_c") or "").isdigit()]
    if hot:
        f["cpu.load_temp_max_c"] = max(hot)
    pw = [int(r["package_w"]) for r in rows if (r.get("package_w") or "").isdigit()]
    if pw:
        f["cpu.package_w_max"] = max(pw)
    series = []
    for r in rows:
        try:
            if r.get("result") == "PASS" and int(r["package_w"]) >= 150:
                series.append((float(r["hottest_c"]) - float(r["water_max"])) / int(r["package_w"]) * 100)
        except (KeyError, ValueError, ZeroDivisionError):
            continue
    if len(series) >= 6:
        k = max(3, len(series) // 4)
        first, last = sum(series[:k]) / k, sum(series[-k:]) / k
        f["thermal.c_per_100w_first"] = round(first, 2)
        f["thermal.c_per_100w_last"] = round(last, 2)
        f["thermal.c_per_100w_drift"] = round(last - first, 2)
    return f, "ok (%d runs)" % len(rows)


PROBES = [("cpu", probe_cpu), ("board", probe_board), ("memory", probe_memory), ("vbs", probe_vbs),
          ("crashdump", probe_crashdump), ("bcd", probe_bcd), ("events", probe_events), ("whea", probe_whea),
          ("scewin", probe_scewin), ("dimm", probe_dimm), ("ledger", probe_ledger)]


def gather(sysif, ctx, user_facts=None, extra=None, only=None):
    """Run every probe. Returns (facts, statuses). User facts never overwrite measured ones; a conflict is noted."""
    facts, statuses = {}, []
    for name, fn in PROBES:
        if only and name not in only:
            continue
        try:
            got, note = fn(sysif, ctx)
        except Exception as e:  # a probe bug must not take the report down
            got, note = {}, "error: %s" % e
        for k, v in got.items():
            facts[k] = {"value": v, "source": ("file:" if name in ("scewin", "ledger") else "probe:") + name}
        statuses.append({"probe": name, "status": note, "facts": len(got)})
    for k, v in (extra or {}).items():
        facts.setdefault(k, {"value": v, "source": "file:facts"})
    for k, v in (user_facts or {}).items():
        if k in facts and str(facts[k]["value"]) != str(v):
            statuses.append({"probe": "user", "status": "your stated %s=%r differs from the measured %r; the "
                             "measured value is used" % (k, v, facts[k]["value"]), "facts": 0})
            continue
        facts.setdefault(k, {"value": v, "source": "user"})
    return facts, statuses
