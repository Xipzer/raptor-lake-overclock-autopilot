# AGENTS.md

Guidance for AI agents (local LLM agents included) that drive Raptor Lake OC Autopilot for a machine's owner, or
change its code. Humans: see README.md and docs/METHOD.md.

Scope: Intel Raptor Lake (13th/14th gen) with DDR5 only. For other platforms point the owner at the core repo,
github.com/Xipzer/overclock-autopilot.

## Driving a sweep

1. **Run `doctor` first**, on the machine, elevated, with a SCEWIN export if the board allows it:
   `python tuner.py doctor -c <config> --scewin-export`. Read the whole report, including "Could not check".
   Re-run it after every BIOS update, microcode change, CMOS clear or hardware change, and before resuming a sweep
   that was paused for a long time.
2. **Research the platform's quirks before planning.** Read the detected and applicable entries and their sources.
   If the owner's hardware is not covered (another board vendor, another memory die), search for that platform's
   known issues first and add what you find to `quirks/*.toml` with sources and an honest `confidence`.
3. **Never raise a limit silently.** Limits are raised only by the owner, in the config, with
   `i_accept_the_risk = true` and a reason. Do not write that for them. If a plan needs it, explain why, what the
   research says about the risk, and let the owner decide. Never edit `LIMITS` defaults in `rlh/config.py` to get a
   step through.
4. **Predict risky steps before running them.** For every step that can stop training (primaries, tRFC near its
   floor, tRCD/tRP, tRRD_L, turnarounds, anything near a known failure), write down the expected outcome (PASS, FAIL,
   NO TRAIN, CRASH, NO POST), the floor or rule behind it, and whether the gain is worth the risk. Put those groups
   under `risky = true` with a `note`. Skip steps whose gain is in the per-mille range unless the owner wants them.
5. **Check, plan, simulate.** `check` must show 0 errors. Read `plan`. Run `simulate --owner` with the outcomes you
   predicted and read the journal it produces.
6. **Classify outcomes honestly.** A tool failure is not a hardware failure. A pass you corrected by fixing a parser
   is re-run, not marked. A failure on a base that later fails its control is unproven, not a limit. Use
   `mark` only to downgrade (no-post, crash, fail, no-train) with a note saying how you know. Never edit
   `state.json` or the journal by hand to change an outcome.
7. **Run controls when failures cluster.** The harness does it after `cluster_fails` failures in a row; do it
   yourself (`tuner.py control`) whenever several late
   failures look alike. One failing control means the base is marginal: step back the newest additions first.
8. **Keep the owner informed.** Tell them before any step that may need their hands (a no-POST risk), whenever the
   sweep pauses or stops, after every crash (with the chkdsk/sfc result), and when it finishes. Say plainly what is
   proven, what is unproven, and what the next decision is. Point them at the journal, not a summary of it.
9. **Undo the safety machinery.** When the sweep is finished or abandoned: `restore --yes`, then confirm the
   restore list is empty. Remind the owner about anything that is not in the list (autologon, paused services).

## Hard rules

- Do not run anything that writes BIOS settings, reboots, or starts a stress test unless the owner asked for that
  specific action on that specific machine. `check`, `plan`, `simulate`, `names`, `status` and `doctor` are safe.
- `doctor` is read-only and must stay that way: probes read, they never change a setting or install anything.
- One change per step. Two passing runs before a value is kept. Re-read coupled timings after a change (tCWL moves
  tWTR and tWR; tRAS moves tRC). tWR set alone does not move tRTP on this board; still read tRTP back, and use the
  read-back value for the tRAS floor (tRCD + tRTP).
- Do not tune tREFI up or tCL down above the temperature gate. Do not combine a VDD increase with a higher tREFI.
- Leave tRDRD_sg / tWRWR_sg trained unless the owner explicitly accepts the no-POST risk.
- On the SA rail: 10 to 20 mV steps, a short VT3 at each, stop at the first freeze (SA bug).
- Privacy: this repository is public. No user names, paths with user names, IP addresses, host names, chat IDs,
  passwords, serial numbers or wallet addresses in code, configs, examples or docs. Probes never collect serials.
- Docs: plain and short. Never use em dashes or en dashes in any file.

## Code map

| Path | Purpose |
|---|---|
| `tuner.py` | CLI; builds the engine from a config |
| `rlh/engine.py` | State machine: phases, classification, group logic, controls, backoff, mark/optin/resume |
| `rlh/scewin.py` | SCEWIN parser/editor; find by name with index/token for duplicates; live-value labels |
| `rlh/backend_api.py` | The interface every BIOS backend implements |
| `rlh/backends.py` | `ScewinBackend` (export, build from fresh export, import with /cpwd, verify by re-export, failsafe files), `ManualBackend` |
| `rlh/vendors/` | Vendor plugins: `asrock` supported; `msi`, `asus`, `gigabyte` not implemented (docs/VENDORS.md) |
| `rlh/rules.py` | Plan and step rules, voltage limits, temperature gate, doctor skips |
| `rlh/config.py` | Config loading, limits with acceptance |
| `rlh/testsuite.py` | Test runners, parsers, temperature watchdog |
| `rlh/winsys.py` | Event log (wevtutil XML), registry, CIM via PowerShell, bcdedit, reboot |
| `rlh/installers.py` | crashdumps, bcdedit, failsafe, autostart; restore list |
| `rlh/sim.py` | Simulated PC (backend + system + tester) and `drive()` |
| `rlh/doctor/` | `probes.py` (facts), `kb.py` (schema, evaluation, implications), `report.py` |
| `quirks/*.toml` | Knowledge base |
| `tools/failsafe.ps1` | Startup failsafe v2 (fresh export + values by token) |
| `campaign/` | The reference campaign scripts (WSL); board data in `campaign/plan.py`, paths in `campaign/campaign.toml` |

## Changing the code

- Standard library only. Python 3.11+. Keep it runnable natively on Windows.
- The engine must stay deterministic: no randomness, time only through the clock object, outcomes only from
  observations. Every change to the engine needs a test in `tests/test_engine.py` driven by the simulator.
- New quirk entries need: a source for every claim, `confidence` that matches the evidence, a `remediation` list
  (empty only for LIMIT), and a test if they add a new fact or probe.
- Run `cd tests && python -m unittest discover -s .` before handing anything back, and `python -m py_compile` on
  every changed `.py` file (including `campaign/`).
- A new vendor plugin stays `STATUS = "not implemented"` until it has run end to end on real hardware.
