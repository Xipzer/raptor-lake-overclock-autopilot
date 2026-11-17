"""The plan: every board-specific and campaign-specific value the campaign scripts use.

This is the reference campaign's plan (i9-14900KS, ASRock Z790I Lightning WiFi BIOS 10.01, 2x24 GB DDR5-8400),
in the order it ran. Results: github.com/Xipzer/raptor-lake-field-notes.
Do NOT run it as it is. Tokens differ between boards and BIOS versions: export your BIOS with SCEWIN, find your
tokens (`python tuner.py names export.txt --grep tRFC`), and write your own groups. Copy the method, not the numbers.

Group name prefixes:
  risky:    waits until the file "owner-awake" exists in the state folder (the owner is at the PC)
  alt:      try alternatives until one passes (a normal group stops at its first failure)
  cpu:      add the CPU test (Cinebench with GPU heat through the core tool)
  cb30:     the CPU test runs 30 minutes instead of 10
  control:  run the kept set unchanged (a failure in every alternative stops the sweep)
"""

# The memory profile question: if it stops showing this value after a boot, the BIOS loaded defaults.
PROFILE_TOKEN = "2C9F"
PROFILE_VALUE = "XMP 3.0 Profile 1"

PERCORE = ["2C6E", "2C6F", "2C70", "2C71", "2C72", "2C73", "2C74", "2C75"]  # Core 0..7 Max Ratio

NAMES = {"2CAC": "tRFC", "2CAE": "tRFC2", "2CAB": "tWR", "2CB3": "tRTP", "2CAF": "tRRD_L", "2CB1": "tWTR_L",
         "2CB2": "tWTR_S", "2CA7": "tRCD", "2CA8": "tRP", "2CA5": "tCL", "2CA9": "tRAS", "2CB6": "tREFI",
         "2CB5": "tCWL", "2CDB": "TX", "2CFD": "VDD2", "2CD2": "tWRRD_dg", "2CD1": "tWRRD_sg",
         "2C5D": "E-core ratio", "2CEF": "Vcore", "2C77": "ring ratio", "2D01": "VDD", "2D02": "VDDQ",
         "2CCE": "tRDWR_dg", "2CCD": "tRDWR_sg", "2CC9": "tRDRD_sg", "2CD5": "tWRWR_sg", "2CAD": "tRFCpb",
         "6AA": "tRCDW", "2CAA": "Command Rate", "2C4A": "CPU P-Core Ratio mode", "2C4B": "P-core ratio",
         "2C6E": "P0", "2C6F": "P1", "2C70": "P2", "2C71": "P3", "2C72": "P4", "2C73": "P5", "2C74": "P6", "2C75": "P7"}
VOLT = {"2CDB", "2CFD", "2D01", "2D02", "2CEF"}  # values in mV; the BIOS prompt shows e.g. "1.450V"
READS_LOWER = {"2CB3": 1}   # this BIOS shows (and trains) an entered tRTP one lower than entered
COMMAND_RATE = "2CAA"       # shown as "2N"
CPU_TOKENS = {"2C77", "2CEF", "2C5D", "2C4B"}
VCORE, RING, ECORE, PCORE_ALL, PCORE_MODE = "2CEF", "2C77", "2C5D", "2C4B", "2C4A"

GROUPS = [
    # 1. Voltages first, down to daily-safe values (TX <= 1.35 V, VDD2 1.40-1.45 V are common daily ranges).
    ("TX (VDD_IMC)", [{"2CDB": v} for v in (1400, 1350, 1300)]),
    ("VDD2 (VDD_CPU)", [{"2CFD": v} for v in (1420, 1400)]),             # 1420 failed: VDD2 stayed 1.45 V
    # 2. tWTR is set through tWRRD on Intel; then the other turnarounds, one group at a time.
    ("tWRRD_dg", [{"2CD2": v} for v in (56, 52, 48)]),
    ("tWRRD_sg", [{"2CD1": v} for v in (80, 70, 64)]),                   # 64 no train on the first base
    ("tRDWR_dg", [{"2CCE": v} for v in (20, 19)]),
    ("tRDWR_sg", [{"2CCD": v} for v in (20, 19)]),
    ("tRDRD_sg", [{"2CC9": v} for v in (15, 14, 12)]),                   # 14 gave no POST: needs the owner
    ("tRFCpb", [{"2CAD": v} for v in (560, 460)]),
    ("tRAS", [{"2CA9": v} for v in (84, 76)]),                           # floor tRCD + read-back tRTP
    ("tCWL", [{"2CB5": 38}]),
    ("tREFI", [{"2CB6": v} for v in (40960, 49152)]),                    # heat-bound: see the field notes
    ("cpu:ring", [{"2C77": 51}]),
    ("cpu:E-cores", [{"2C5D": 46}]),
    ("tRFCpb fine", [{"2CAD": v} for v in (440, 420)]),
    ("tREFI more", [{"2CB6": v} for v in (57344, 65535)]),
    ("alt:tCL 38", [{"2CA5": 38, "2CB5": 36}, {"2CA5": 38, "2CB5": 36, "2D01": 1500, "2D02": 1480}]),
    ("tRFC fine", [{"2CAC": v, "2CAE": v} for v in (670, 660)]),
    ("cpu:E-cores 47", [{"2C5D": 47}]),                                  # hard freeze
    ("risky:cpu:alt:ring 51", [{"2C77": 51, "2CEF": 1350}]),
    ("risky:tRCD/tRP 49", [{"2CA7": 49, "2CA8": 49}]),
    ("risky:tRRD_L 11", [{"2CAF": 11}]),
    ("tWR fine", [{"2CAB": v} for v in (22, 20)]),
    ("tRTP entered", [{"2CB3": v} for v in (20, 16, 13)]),               # entered one higher: targets 19, 15, 12
    ("tRAS after tRTP", [{"2CA9": v} for v in (68, 64)]),
    ("risky:tRCD/tRP 48", [{"2CA7": 48, "2CA8": 48}]),                   # crash loop twice: 49 is the floor
    ("risky:tRRD_L 10", [{"2CAF": 10}]),                                 # crash loop
    # Write-path timings: a bad one can corrupt data at once and loop Windows at startup.
    ("risky:tWR 18", [{"2CAB": v} for v in (18, 16)]),
    ("risky:tWRRD_dg 46", [{"2CD2": v} for v in (46, 44)]),
    ("risky:tWRRD_sg 68", [{"2CD1": v} for v in (68, 66, 64, 62)]),
    ("risky:tRP 48 alone", [{"2CA8": 48}]),
    ("risky:CR 1N", [{"2CAA": 1}]),                                      # no train at 8400
    ("risky:tRCDW", [{"6AA": v} for v in (44, 40)]),
    ("risky:tRDRD_sg 15", [{"2CC9": 15}]),
    ("tREFI up", [{"2CB6": v} for v in (98304, 131071, 262143)]),
    ("tRFC low", [{"2CAC": v, "2CAE": v} for v in (650, 640)]),
    # Control: two failures in a row on heat-soaked sticks. Test the kept base unchanged, then with tREFI lower.
    ("control:alt:base", [{"2CB6": 98304}, {"2CB6": 65535}]),
    ("tRFCpb low", [{"2CAD": v} for v in (400, 380)]),
    ("risky:tRDWR_dg 18", [{"2CCE": 18}]),
    ("risky:tRDWR_sg 18", [{"2CCD": 18}]),
    ("risky:tRP 47", [{"2CA8": v} for v in (47, 46)]),
    ("risky:tRCDW 36", [{"6AA": v} for v in (36, 32)]),
    ("risky:tWR 14", [{"2CAB": v} for v in (14, 12)]),
    ("risky:tCWL 34", [{"2CB5": 34}]),
    ("risky:tWRWR_sg 15", [{"2CD5": 15}]),
    # CPU after a liquid-metal repaste. Vcore up to the owner's 1.360 V ceiling only when the lower step fails.
    ("risky:cpu:alt:58 all-core", [{"2C4B": 58}, {"2C4B": 58, "2CEF": 1350}, {"2C4B": 58, "2CEF": 1360}]),
    ("risky:cpu:alt:58 mix", [
        dict({"2C4A": "Specific Per Core"}, **{t: (57 if i in (2, 4, 5, 6) else 58) for i, t in enumerate(PERCORE)}, **extra)
        for extra in ({}, {"2CEF": 1350}, {"2CEF": 1360})]),
    ("risky:cpu:alt:ring 51 at more Vcore", [{"2C77": 51}, {"2C77": 51, "2CEF": 1350}, {"2C77": 51, "2CEF": 1360}]),
    # 10-minute passes were luck twice: near the floor every step runs 30 minutes. Lowest passing value is kept.
    ("risky:cpu:cb30:57 all-core, Vcore down at ring 50", [
        dict({"2C4A": "All Core", "2C4B": 57, "2C77": 50, "2CEF": v}, **{t: 57 for t in PERCORE}) for v in (1340, 1330, 1320)]),
    ("risky:cpu:cb30:57 all-core, Vcore floor", [
        dict({"2C4A": "All Core", "2C4B": 57, "2C77": 50, "2CEF": v}, **{t: 57 for t in PERCORE}) for v in (1310, 1300, 1290, 1280)]),
]

FAST_FROM = "risky:tRP 47"   # from this group on: one test run per step (the long validation confirms)
SWEEP_FROM = "risky:tRP 47"  # memtrack hands the remaining probes to the sweep from here

# Predictions shown on the step card for risky groups (write yours BEFORE the step runs).
EXPECT = {
    "risky:tRDWR_dg 18": "The pair at 18 did not train and recovered by itself in about 2.5 minutes; the same is likely here.",
    "risky:tRDWR_sg 18": "The pair at 18 did not train and recovered by itself in about 2.5 minutes; the same is likely here.",
    "risky:tRP 47": "tRP 48 alone passes. Below it a startup crash loop is possible (tRCD/tRP 48 as a pair looped twice).",
    "risky:tRCDW 36": "44 and 40 passed with normal boots. A bad value corrupts writes: startup crash loop possible.",
    "risky:tWR 14": "24 down to 16 all passed. A bad value corrupts writes: startup crash loop possible.",
    "risky:tCWL 34": "Never tried on its own. Startup crash loop possible.",
    "risky:tWRWR_sg 15": "Never moved. Same family as tRDRD_sg, where 14 gave no picture and needed five power cycles.",
    "risky:tRP 48 alone": "Unknown. tRCD/tRP 48 as a pair crash-looped Windows at startup twice.",
    "risky:CR 1N": "Most likely no picture or a fallback train; 1N at 8400 is rare on this platform.",
    "risky:tRCDW": "Unknown, never tuned. A bad value corrupts writes, so a startup crash loop is possible.",
    "risky:tRDRD_sg 15": "Passed twice on an older base. 14 gave no picture and needed five power cycles.",
}

# finalrun.py: the profile the owner started from, for the before/after benchmark.
ORIGINAL = {"2C4B": 59, "2CEF": 1360, "2C5D": 45, "2C77": 50}

# finalrun.py: what to give back, in order, when a validation test fails. Refresh first, then the value kept on the
# thinnest evidence, then whole earlier bases, then plain XMP; only then CPU voltage, to the owner's ceiling.
STEPBACK = [
    ("tREFI 98304 -> 65535", {"2CB6": 65535}),
    ("tRFC/tRFC2 660 -> 688 and tRFCpb 420 -> 460", {"2CAC": 688, "2CAE": 688, "2CAD": 460}),
    ("tWRRD_sg 64 -> 70", {"2CD1": 70}),
    ("E-cores 46 -> 45", {"2C5D": 45}),
    ("an earlier base: tWR 20, tWRRD_dg/sg 48/70, tRP 49, tRCDW Auto, tRDRD_sg Auto",
     {"2CAB": 20, "2CD2": 48, "2CD1": 70, "2CA8": 49, "6AA": 0, "2CC9": 0}),
    ("the first stable base: tCL 40, tCWL 38, tRCD/tRP 50, tRAS 76, tWR 24, tRFC 680, tRFCpb 560, tRRD_L, tRTP and "
     "tREFI on Auto",
     {"2CA5": 40, "2CB5": 38, "2CA7": 50, "2CA8": 50, "2CA9": 76, "2CAB": 24, "2CAC": 680, "2CAE": 680, "2CAD": 560,
      "2CAF": 0, "2CB3": 0, "2CB6": 0}),
    ("plain XMP memory (every timing and memory voltage on Auto) with the tuned CPU",
     {"2CA5": 0, "2CB5": 0, "2CA7": 0, "2CA8": 0, "2CA9": 0, "2CAB": 0, "2CAC": 0, "2CAE": 0, "2CAD": 0, "2CAF": 0,
      "2CB3": 0, "2CB6": 0, "2CD2": 0, "2CD1": 0, "2CCE": 0, "2CCD": 0, "6AA": 0, "2CC9": 0, "2CDB": 0, "2D01": 0, "2D02": 0}),
    ("Vcore 1.340 -> 1.350 V (fixed)", {"2CEF": 1350}),
    ("Vcore 1.350 -> 1.360 V (fixed, the owner's ceiling)", {"2CEF": 1360}),
]
CPU_FIELDS = {"2C5D", "2CEF", "2C77", "2C4B"}
# A failure of the CPU test itself is answered with CPU fields only.
CPU_STEPBACK = [
    ("E-cores 46 -> 45", {"2C5D": 45}),
    ("Vcore 1.340 -> 1.350 V (fixed)", {"2CEF": 1350}),
    ("Vcore 1.350 -> 1.360 V (fixed, the owner's ceiling)", {"2CEF": 1360}),
]
# finalrun.py: positive controls for the at-rest test (only with the "allow-controls" file in the state folder).
DEFAULT_CONTROLS = [
    {"name": "tREFI 131071 (crashed Windows before)", "set": {"2CB6": 131071}, "minutes": 30},
    {"name": "tREFI 98304 (given back after a reset)", "set": {"2CB6": 98304}, "minutes": 45},
    {"name": "tREFI 98304 with tRFC/tRFC2 688 and tRFCpb 460", "set": {"2CB6": 98304, "2CAC": 688, "2CAE": 688, "2CAD": 460},
     "minutes": 45},
]

# memtrack.py: keep the CPU cool during memory tests.
STOP_AT = 95      # C on the hottest core: end the load (two readings in a row, or STOP_AT + 4 once)
STEP_DOWN = 90    # take the last core step away again at this
RAMP_BELOW = 84   # add cores only while the hottest core is under this
# Logical CPU sets, coolest first. Reference machine: P0 = 0-1, ..., P7 = 14-15, E-cores = 16-31. The cool P-cores
# came from per-core temperature logs; yours will differ.
MASKS = [("E-cores", 0xFFFF0000), ("E-cores + P1", 0xFFFF000C), ("E-cores + P0 P1", 0xFFFF000F),
         ("E-cores + P0 P1 P5", 0xFFFF0C0F), ("E-cores + P0 P1 P3 P5", 0xFFFF0CCF),
         ("all cores", 0xFFFFFFFF)]
