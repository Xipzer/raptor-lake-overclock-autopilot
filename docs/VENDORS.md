# Board vendors

| Vendor | Status | How it writes the BIOS |
|---|---|---|
| ASRock | **Supported**, tested end to end on a Z790 board | AMI SCEWIN, with the BIOS admin password passed as `/cpwd` |
| MSI | Not implemented | (prototype notes below) |
| ASUS | Not implemented | (prototype notes below) |
| Gigabyte | Not implemented | (prototype notes below) |
| Any board | Works in manual mode | You type each value in the BIOS; the harness does everything else |

`python tuner.py vendors` prints the same table. Choosing a vendor that is not implemented stops with a message
that points here; nothing is guessed.

## ASRock

Config:

```
[backend]
type = "scewin"
vendor = "asrock"
scewin = 'C:\Tools\scewin\SCEWIN_64.exe'
password_file = 'C:\Tools\scewin\bios-admin-password.txt'   # or set RLAP_BIOS_PASSWORD
```

What you need to know:

- On Z590 and newer, "Password protection of Runtime Variables" blocks SCEWIN imports. Set a BIOS Administrator
  password in the BIOS; the backend passes it with `/cpwd`. Exports work without it.
- Value 0 means Auto for most timing questions.
- On the tested BIOS an entered tRTP reads back one lower, and tWR set alone does not move tRTP.
- "VDD_IMC Voltage" is VDDQ_TX and "VDD_CPU Voltage" is VDD2.
- A failed memory training falls back to tRFC 395, tCL/tRCD/tRP 29, tWR 54 (use it as `fallback_signature`).
- "Fixed" Vcore is held at the VRM: the CPU's mailbox shows adaptive with target 0. Do not read voltage from there.
- Boot Failure Guard did not load defaults after a no-POST: it trained at DDR5-3600 with every setting still stored.
- More, with sources: `quirks/asrock.toml`.

## Adding your board

A vendor plugin is one file in `rlh/vendors/` that returns an object implementing `rlh/backend_api.py`:

| Method | Must do |
|---|---|
| `prepare()` | Check that every configured setting exists in this BIOS; save the pre-sweep BIOS (golden) once. Return `(ok, message)` |
| `apply(wanted, step)` | Write the FULL wanted set (all kept values plus the step), never a partial set. Read the BIOS back and return `(False, why)` if any value is not stored |
| `read()` | Read only. Return the live (effective) values, the stored values and the memory profile's value, or `None` |
| `arm_failsafe(good, step, tag)` | Leave the last good values where a startup task can restore them; return `True` if armed |
| `disarm()` | The step's test started: the failsafe must not act |
| `failsafe_restored()` | Text if the failsafe restored since the last call, else `None` |
| `reboot(why)` | Restart Windows |
| `automatic`, `name` | `True` if it writes and reboots by itself; a short name for the journal |

And these rules:

- Passwords come from the user's config or environment, never from code, and are masked in every output.
- A tool error is a failed `apply()` or a `None` from `read()`, never a hardware result.
- If the BIOS shows live (trained) values, return them as `effective`. Without them NO TRAIN cannot be told apart
  from PASS, so say so in your module docstring.

Steps:

1. **Find out how your BIOS can be written from Windows.** On AMI Aptio boards (most MSI, ASUS and Gigabyte Intel
   boards) try SCEWIN: export (`SCEWIN_64.exe /o /s export.txt`), change one harmless question
   (`/i /ms "<question>" /qv <value>`), export again, reboot, export again. Note what blocks it.
2. **Copy `rlh/vendors/msi.py`** (or the vendor's stub) and subclass `ScewinBackend` from `rlh/backends.py`. Override
   only what differs: password handling, how live values appear in prompts, anything extra the import needs.
3. **Write the quirks you found** into `quirks/<vendor>.toml` with sources and an honest `confidence`.
4. **Add tests** in `tests/test_engine.py` driven by a synthetic export (see `tests/fakes.py`).
5. **Run a manual-mode sweep with your backend used read-only** (manual backend with `scewin` set) before you let it
   write.
6. **Set `STATUS = "supported"` only after an end-to-end run on real hardware**, and say which board and BIOS.

### Prototype notes (not verified by us)

- **MSI**: AMI Aptio on current boards. Whether runtime variable protection blocks imports, and whether a password
  unlocks it, is unknown to us. Question names differ (for example "Extreme Memory Profile(XMP)").
- **ASUS**: community guides say "Publish HII Resources" must be enabled in the BIOS before SCEWIN can see the
  setup questions, and runtime variable protection may also apply.
- **Gigabyte**: AMI Aptio on current boards; SCEWIN behaviour unknown to us.

If your board cannot be written from Windows at all, the manual backend is the answer: the harness keeps the state,
the rules, the tests and the journal, and you are the write path.
