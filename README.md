# Raptor Lake OC Autopilot

Automatic Intel 13th/14th gen CPU and DDR5 RAM overclocking, undervolting and stability testing, with ASRock BIOS
automation built in.

**This is the specialised edition, for Intel Raptor Lake and Raptor Lake Refresh (Core i5/i7/i9 13th and 14th gen,
for example the 13900K, 14900K and 14900KS) with DDR5 only.** It knows that platform's quirks and safety limits.
For any other platform, or to build your own extension, use the generic core:
[overclock-autopilot](https://github.com/Xipzer/overclock-autopilot).

It changes one BIOS setting per step, reboots, reads back what the BIOS really applied, runs the right stability
test, and keeps a value only after it passes. It recovers by itself from crash loops and failed memory training,
and tells you plainly when only your hands can help.

What came out of the campaign this tool is built from (results, failures and mistakes):
[raptor-lake-field-notes](https://github.com/Xipzer/raptor-lake-field-notes).

## Status

| Part | Today | Notes |
|---|---|---|
| `tuner.py doctor`: read-only quirk check of your PC | Works | Microcode, VBS, crash dumps, WHEA history per core, SCEWIN export, DIMM temperatures |
| `tuner.py check / plan / simulate`: validate a plan, dry-run it on a simulated PC | Works | No hardware touched. 86 unit tests |
| Sweep engine (`start`, `run`, `mark`, `optin`, `control`, `resume`) | Works in simulation; manual backend usable | One change per step, two passes to keep, controls, step-back |
| ASRock backend (AMI SCEWIN with `/cpwd`) | Supported | The same write path ran the reference campaign on a Z790 board |
| MSI, ASUS, Gigabyte backends | Not implemented | Use the manual backend, or add a plugin: [docs/VENDORS.md](docs/VENDORS.md) |
| Startup failsafe v2 (`tools/failsafe.ps1`) | Works | Restores the last good values after a failed boot; cannot fix a no-POST or a hard freeze |
| Tests: y-cruncher, memlat, WHEA, Shakedown (CPU), generic commands | Works | |
| Tests: memhold (at-rest refresh test), TestMem5 | Ported, parsers tested | Not yet run through the harness on hardware (they ran in the campaign scripts) |
| Campaign scripts (`campaign/`) | Ran the full reference campaign | Reference implementation: RAM sweep, validation ladder, memory track with a CPU temperature guard. Needs your own plan |
| CPU-cool memory testing (core-set ramp, temperature guard) in the harness | Planned | Exists in `campaign/memtrack.py` today |
| Real-use temperature capture, full-page drift diff, report generator | Planned | See [docs/METHOD.md](docs/METHOD.md) |
| AMD, DDR4, non-AMI firmware, GPU tuning | Out of scope | Use the core repo |

## Quirks it knows about

`tuner.py doctor` checks your PC against a knowledge base in `quirks/*.toml`: what each quirk looks like, the cause, the remedy, what it means for a sweep, and its sources.
Every entry says whether a public source backs it or it comes from one machine.

| File | Covers | Examples |
|---|---|---|
| `quirks/cpu.toml` | Raptor Lake CPU | microcode, Vmin shift, fixed Vcore at idle, SA freeze bug, short tests overstating floors |
| `quirks/ddr5.toml` | DDR5 and the IMC | on-die ECC invisible to WHEA, 24 Gb M-die heat, tREFI and temperature, Gear 2 even tCL, tRAS floor |
| `quirks/asrock.toml` | ASRock Z790 boards | Fixed Vcore at the VRM, socket-sense Vcore, LLC levels, rail names, tWTR via tWRRD, tRTP read-back, Boot Failure Guard, the tRFC 395 fallback |
| `quirks/scewin.toml` | AMI SCEWIN | prompts embed the live value, partial writes, harmless TcgStorage error, fields with no live value, failsafe on fallback timings, "Auto" Vcore |
| `quirks/testing.toml` | Stability testing | y-cruncher launcher and worker, TestMem5 coverage and wording, CPU heat inside a RAM test, run counts, WHEA windows |
| `quirks/windows.toml` | Windows | crash dumps during sweeps, Windows Recovery, 0x9F shutdown hangs, WHEA history, WSL swap in Temp, the old iGPU driver and Windows Update, mixed-adapter displays |
| `quirks/thermal.toml` | Cooling | loop sensor position, core-to-water drift |

The same quirks in plain prose, with the measurements behind them, are indexed in the field notes:
[raptor-lake-field-notes/quirks.md](https://github.com/Xipzer/raptor-lake-field-notes/blob/master/quirks.md).
The SCEWIN details (token map, failsafe design and limits) are in
[scewin.md](https://github.com/Xipzer/raptor-lake-field-notes/blob/master/scewin.md), and the test method in
[validation.md](https://github.com/Xipzer/raptor-lake-field-notes/blob/master/validation.md).

## Results it was built from

On the reference machine (i9-14900KS, ASRock Z790I Lightning WiFi, 2x24 GB SK hynix M-die):

| Part | Final | Found by |
|---|---|---|
| CPU | 57x all P-cores, E-cores 46x, ring 50x, Fixed 1.320 V, LLC 1 | 10-minute sweeps, then 30-minute Cinebench runs with GPU heat; 1.310 V failed, 1.320 V passed twice |
| RAM | DDR5-8400 Gear 2, 38-49-46-64, tCWL 34, tRFC 660, tRFCpb 420, tREFI 32767, TX 1.35 V, VDD2 1.45 V, DIMM 1.50 / 1.48 V | One field per boot, two passes to keep, controls, then at-rest 45 min + y-cruncher 60 min + TestMem5 60 min |

Full tables, every failure and the mistakes: [raptor-lake-field-notes](https://github.com/Xipzer/raptor-lake-field-notes).

## Safety, in plain words

Read this before you run anything that writes.

- **Writing BIOS settings can stop your PC from starting.** A memory timing that is too tight can hang the board
  before it shows a picture. Know where your Clear CMOS jumper is. Save your working profile to a BIOS slot or USB.
- **Unstable memory corrupts data silently.** Back up first. Run `chkdsk /scan C:` and `sfc /scannow` after any crash.
- **Voltage damages Raptor Lake over time.** Intel says 13th and 14th gen CPUs age faster with voltage and heat.
  Update to microcode 0x12B or newer first. The tool caps voltages by default; raising a cap is your decision, written
  in the config with a reason.
- **The safety machinery weakens Windows on purpose while a sweep runs** (no crash dumps, no Windows Recovery after
  failed boots, a SYSTEM task that can rewrite the BIOS at startup). `tuner.py restore --yes` undoes it.
- **The automatic mode needs your BIOS administrator password** (ASRock blocks SCEWIN writes without it). It is read
  from a file you name in the config, or from the `RLAP_BIOS_PASSWORD` environment variable, and masked in all
  output. Make that file readable by administrators only. It is never stored in code.
- **Nothing here makes a setting safe for daily use.** A sweep finds candidates. Long validation at your real-use
  temperature makes a daily profile. You own the outcome.

## Requirements

- Windows 10 or 11 on a Raptor Lake CPU with DDR5.
- Python 3.11 or newer, standard library only. Runs natively on Windows; the campaign scripts run in WSL.
- For automatic BIOS writes: an ASRock board with AMI firmware, and **SCEWIN** (`SCEWIN_64.exe` and its driver
  files), which you get yourself from your board vendor's or AMI's utility package. It is not included here. Check
  that it is AMI-signed.
- Optional: [PawnIO](https://pawnio.eu) with its signed SmbusI801 module for DIMM temperatures (`tools/dimmtemp.c`),
  HWiNFO (it initialises DDR5 sensors after a cold boot), y-cruncher, TestMem5 0.13.1, Cinebench, and the core
  tool's `shakedown-cli` for CPU tests with per-core error mapping.
- Build the C tools with `zig cc -O2 -o memlat.exe memlat.c -target x86_64-windows-gnu -ladvapi32` (same for
  `dimmtemp.c` and `memhold.c`).

## Quick start

Start read-only, on the machine, in an elevated prompt:

```
python tuner.py doctor                              # which known quirks apply to this PC
python tuner.py vendors                             # which board vendors are supported
python tuner.py names export.txt --grep tRFC        # find your BIOS question names in a SCEWIN export
```

Then make a plan and dry-run it:

```
copy examples\reference-14900ks-z790i-lightning.toml my.toml     (edit settings, groups, tests, paths)
python tuner.py check -c my.toml
python tuner.py plan -c my.toml
python tuner.py simulate -c my.toml --owner
```

Then, only when you have read [docs/METHOD.md](docs/METHOD.md):

```
python tuner.py install failsafe --yes              # optional safety machinery; also crashdumps, bcdedit, autostart
python tuner.py start -c my.toml                    # saves the pre-sweep BIOS as golden.txt
python tuner.py run -c my.toml                      # one boot's worth of work; runs again after each reboot
python tuner.py status -c my.toml
python tuner.py restore --yes                       # when finished: undo the safety machinery
```

With `[backend] type = "manual"` it prints the values for you to enter in the BIOS and asks what happened after the
boot. That works on any board.

Dry run of the doctor on fake data, on any OS:

```
python tuner.py doctor --no-probes --scewin-file examples/fake-scewin-export.txt --facts examples/doctor-facts-reference.json --out doctor-demo
```

## What is in the repo

| Path | What |
|---|---|
| `tuner.py` | The command line |
| `rlh/engine.py` | Sweep state machine: phases, outcome classes, groups, controls, step-back |
| `rlh/backends.py`, `rlh/backend_api.py` | SCEWIN and manual backends; the interface every backend implements |
| `rlh/vendors/` | Board vendor plugins: `asrock` supported; `msi`, `asus`, `gigabyte` stubs that say so |
| `rlh/scewin.py` | SCEWIN script parser and editor (handles the live value inside prompts) |
| `rlh/rules.py`, `rlh/config.py` | Safety limits and tuning rules, checked before every step |
| `rlh/testsuite.py` | Tests and their parsers, temperature watchdogs |
| `rlh/installers.py` | Optional Windows safety machinery and the restore list |
| `rlh/doctor/`, `quirks/*.toml` | Quirk detection and the knowledge base, with sources |
| `rlh/sim.py` | A simulated PC for dry runs and tests |
| `tools/failsafe.ps1` | Startup failsafe v2 |
| `tools/memlat.c`, `tools/dimmtemp.c`, `tools/memhold.c` | Latency/bandwidth meter, DDR5 DIMM temperature reader, at-rest memory test |
| `tools/windows/` | Elevated helpers for the campaign scripts and their one-time installer |
| `campaign/` | The scripts that ran the reference campaign, cleaned and made configurable ([campaign/README.md](campaign/README.md)) |
| `examples/` | The reference RAM plan as a config, a synthetic SCEWIN export, doctor facts |
| `tests/` | Unit tests on synthetic data: `cd tests && python -m unittest discover -s .` |
| `docs/METHOD.md` | The FAFO workflow, test ladder and step-back rules |
| `docs/VENDORS.md` | ASRock support and how to add your board |

## Licence

[PolyForm Noncommercial 1.0.0](LICENSE.md). Free for personal and other noncommercial use.
