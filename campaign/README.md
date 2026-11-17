# Campaign scripts

The scripts that ran the reference campaign (see
[raptor-lake-field-notes](https://github.com/Xipzer/raptor-lake-field-notes)), cleaned and made configurable. They
are the proven logic; the harness (`tuner.py`) is the general tool and is taking these features over one by one.
Use these if you want to reproduce the campaign's method closely, on an ASRock board, from WSL.

They write the BIOS and reboot the PC. Read [../docs/METHOD.md](../docs/METHOD.md) and the safety section of the main
README first.

| File | What |
|---|---|
| `ramsweep.py` | The step sweep: one step per boot, restore and confirm after any failure, step cards, presence gate for risky groups |
| `finalrun.py` | Validation ladder (at-rest test, y-cruncher 60, Cinebench 30 with GPU heat, hot reboots, TM5), step-back ladders, before/after benchmark, positive controls |
| `memtrack.py` | Memory tests with the CPU kept cool: load confined to cool cores, ramped up only under a temperature threshold, guard stop |
| `tm5run.py` | TestMem5 0.13.1 through the elevated helper, with the parser for its real output |
| `heat.py`, `heat-run.sh` | A CPU test with GPU heat through the core tool's `shakedown-cli` |
| `cpuconfirm.sh` | 30-minute confirmation of the kept CPU set |
| `nvram.py` | SCEWIN script reader/editor by token |
| `plan.py` | Every board- and campaign-specific value: tokens, groups, ladders, predictions, core masks. **Replace it with yours** |
| `settings.py`, `campaign.example.toml` | One config file for every path and command |

## Setup

1. Windows side, elevated: `tools\windows\install-tasks.ps1 -Scewin <path to SCEWIN_64.exe> -Stage <folder>
   -HelperDir <folder> -FailsafeDir <folder> -PasswordFile <BIOS admin password file>`. Read the script first.
   `-Remove` undoes it.
2. Put `dimmtemp.exe` (from `tools/dimmtemp.c`, needs PawnIO) in the helper folder.
3. WSL side: copy `campaign.example.toml` to `campaign.toml` and set every path to yours.
4. Export your BIOS once, untouched, to `golden.txt` in the state folder. Write your own `plan.py`.
5. Create `ram-state.json` in the state folder: `{"g": 0, "k": 0, "good": {}, "phase": "apply", "step": null, "results": []}`.
6. Make `ramsweep.py` (or `finalrun.py`, `memtrack.py --boot`) run at every boot, for example as a systemd user
   service in WSL with linger on, and test the failsafe once with no change.

Notifications, mirrors and step cards go to the shell commands under `[notify]`; nothing is sent anywhere unless you
set them.

## Known limits

- `ramsweep.py` treats "Password accepted" in SCEWIN's output as the sign of a successful import: ASRock wording.
- The A-Tuning voltage buttons in `helper-agent.ps1` are found by window position on one A-Tuning version.
- Group names and ladders in `plan.py` are the reference campaign's, in the order they ran: they are an example of a
  plan, not a plan for your PC.
