# Method

How the autopilot tunes, and why. Summarised from the reference campaign
([raptor-lake-field-notes](https://github.com/Xipzer/raptor-lake-field-notes)) and the core roadmap
([overclock-autopilot](https://github.com/Xipzer/overclock-autopilot), `docs/ROADMAP.md`).

## FAFO, with safety rails

Fuck around: change one thing, test it hard, write down what happened.
Find out: believe the test, not the forum post, and not the first pass.

1. **One change at a time.** Declared linked pairs (tRFC/tRFC2) count as one.
2. **Know your way back before you start.** BIOS profile saved to a slot, Clear CMOS located, failsafe tested once
   with no change. A failsafe cannot fix a no-POST or a hard freeze: those need hands.
3. **Set hard limits before the run.** Voltage ceilings, core and DIMM temperature aborts, coolant limit.
4. **A single pass is not stability.** Two passes to keep a value. Long runs at checkpoints. Controls.
5. **Heat is part of the test.** RAM must pass at the DIMM temperature real use reaches. Keep the CPU cool while you
   test RAM, and test the CPU on hot coolant.
6. **Check your files after crashes.** `chkdsk /scan` and `sfc /scannow` after every unclean stop, before the next step.
7. **Distrust your own tooling.** A tool error is never a hardware result.
8. **Voltage is a one-way street.** Lower clocks are free to undo; lost silicon is not.
9. **A prediction is a guide.** Write down the expected outcome of every risky step before it runs; compare after.
10. **Short runs to search, long runs at checkpoints.**

## Phases

| # | Phase | Exit criterion |
|---|---|---|
| 0 | Doctor and plan | Report read; limits and cooling layout entered; no blocking warning (for example microcode older than 0x12B) |
| 1 | Baseline | BIOS export saved as golden; WHEA history map; the starting profile benchmarked |
| 2 | Safety setup | Restore list written; failsafe tested with no change; BIOS profile saved by the owner |
| 3 | CPU ratio | Highest all-core (or per-core mix) ratio that passes 30 minutes hot at the ceiling voltage |
| 4 | Interim voltage | 20 mV above a 10-minute floor, to cut heat during RAM work |
| 5 | RAM | Voltages first (down to daily ranges), then turnarounds, refresh, heat-sensitive timings, then risky ones |
| 6 | CPU extras | E-core and ring, each with the CPU test |
| 7 | Final voltage floor | Lowest Vcore with two clean 30-minute hot runs; margin to the 30-minute failure stated |
| 8 | Final validation | All long tests clean on the final set at real-use temperatures, plus hot reboots |
| 9 | After benchmark | Same suite as phase 1, same coolant start temperature |
| 10 | Hand-back | Restore list empty; owner saved the tuned and a safe BIOS profile |

Today the harness drives phase 5 (and CPU groups) end to end; `campaign/finalrun.py` does phases 8 and 9. The rest is
planned (see the README status table).

## A step

1. Pick the next step. Skip it if a rule, the temperature gate or the doctor says so. Wait for an opt-in if the
   group is risky (`tuner.py optin "<group>"`).
2. Build the BIOS script from a **fresh export** (SCEWIN matches questions by their full prompt, which includes the
   live value, so an old script is silently skipped). Copy back anything that drifted from the golden export, then
   set the kept values plus this step.
3. Import, re-export, verify. Arm the failsafe. Journal "SET ..." (if nothing follows, the PC did not come back).
   Reboot.
4. After the boot: crash records since the step was SET (never before), effective values, boot time. Classify.
5. Run the tests; two passes to keep.

## Outcome classes

| Outcome | How it is known | Who recovers |
|---|---|---|
| PASS | Values live as written, tests clean, no new WHEA records | |
| FAIL | Values live, a test found an error or new WHEA records, or the load died silently | tool |
| CRASH | Kernel-Power 41, 6008 or BugCheck 1001 after the step was set, or a restart during a test | tool or failsafe |
| NO TRAIN | Values the step did not touch changed, the board's fallback signature is live, or the memory profile dropped | tool |
| NOT APPLIED | Only our value changed, after the board's read-back offsets, with a normal boot | nothing |
| NO POST | Needed manual power cycles (`tuner.py mark no-post`) | person |
| SKIPPED | A rule, gate or doctor implication refused the step | |
| Not a result | Tool error, guard stop, test did not start. Never counted against the value | tool re-runs |

A slow boot proves nothing until the bugcheck records and the fallback signature are checked: on the reference
machine, six-minute "slow trains" were 0x9F shutdown hangs from a GPU process.

## Test ladder

| Rung | Test | Used for |
|---|---|---|
| 1 | memlat (latency, bandwidth) | Every RAM step: did it get faster, and is the value live |
| 2 | y-cruncher FFTv4 + N63 + VT3, 7 minutes, 30 GB | Every RAM step, twice |
| 3 | WHEA log | Every step; id 1 (training record) is excluded from CPU error counts |
| 4 | Cinebench with GPU heat, 10 minutes (core tool) | CPU steps far from the floor |
| 5 | Cinebench with GPU heat, 30 minutes | Near the voltage floor, per-core mixes; two passes to keep |
| 6 | memhold at-rest test, 45 minutes, with load beside it | Refresh settings (tREFI, tRFC): a flip in data nobody touched is a refresh fault |
| 7 | y-cruncher 60 minutes | RAM checkpoint and final validation |
| 8 | TestMem5 0.13.1 (anta777 DDR5 config), 60 minutes, at real-use DIMM temperature | The decisive RAM test; it found the heat-bound tREFI fault that every short run missed |
| 9 | Five hot reboots, each after a load | Does the set train reliably when hot |

On DDR5, on-die ECC never reaches WHEA: zero WHEA records is not proof of stable RAM. RAM verdicts come from tests.

## Step-back rules

1. A failed step: restore the last good set, reboot, confirm it by export, run integrity checks after any unclean
   stop, then go on. Never write the next step from a failing session.
2. A failed validation test gives back one rung of a ladder and repeats only that test. Passed tests are not re-run
   after loosening, except the CPU test after any CPU field change.
3. Two ladders. Memory: the newest refresh change first, then the value kept on the thinnest evidence, then earlier
   bases, down to plain XMP. CPU: E-cores back, then Vcore +10 mV to the owner's ceiling. A CPU fault never pulls
   back memory, and the reverse.
4. A 10-minute pass can be luck. Near the floor every step runs 30 minutes; keeping needs two.
5. Controls: after three failures in a row, or several late failures that look alike, run the kept set unchanged.
   If the control fails, the base is marginal: the failures since the last good control become unproven, the newest
   kept change is stepped back, and the control runs again. After a passing control, unproven steps are retried once.
6. Voltage that only delays the crash is not a fix: stop raising when the crash time grows but never passes.
7. A heat-bound fault is fixed at the heat-sensitive setting (tREFI), not by loosening tRFC.
8. Presence gate: steps that can stop the PC from showing a picture or freeze it (tRDRD_sg/tWRWR_sg, tRCD/tRP near a
   known failure, E-cores up, Command Rate, anything that crash-looped before) wait until the owner is at the PC.
9. Hard stops: never above the owner's voltage limits; never "Auto" for a CPU voltage (adaptive, far above the
   ceiling); never retry a value that corrupted data until cooling or voltage changes; stop when every rung is used.
10. Never edit a verdict. Downgrades only through `tuner.py mark`, with a note saying how you know.

## Where the rules live

- Limits and their defaults: `rlh/config.py` (`LIMITS`). Making one less safe needs `i_accept_the_risk = true`.
- Plan and step rules (tCL even in Gear 2, tRAS >= tRCD + read-back tRTP, tRTP minimum, SA step size, sg turnarounds
  left trained): `rlh/rules.py`.
- Platform and board quirks with sources and confidence: `quirks/*.toml`.
- The reference campaign's ladders and plan: `campaign/plan.py`.
