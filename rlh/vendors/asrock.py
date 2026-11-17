"""ASRock (AMI Aptio BIOS) through AMI SCEWIN. Supported and tested end to end on a Z790 board.

ASRock specifics handled here or in quirks/asrock.toml:
- Runtime variable protection blocks SCEWIN imports unless a BIOS Administrator password is set and passed with
  /cpwd. The password comes from [backend] password_file or the RLAP_BIOS_PASSWORD environment variable.
- A value of 0 means Auto for most timing questions.
- tRTP reads back one lower than entered on the tested BIOS (per-board read-back offset).
- The memory training fallback signature on the tested board is tRFC 395, tCL/tRCD/tRP 29, tWR 54.
- "VDD_IMC Voltage" is VDDQ_TX and "VDD_CPU Voltage" is VDD2.
"""
from ..backends import ScewinBackend

NAME = "asrock"
TITLE = "ASRock"
STATUS = "supported"
NOTES = "AMI SCEWIN with a BIOS admin password (/cpwd); tested on Z790"


class AsrockBackend(ScewinBackend):
    name = "scewin-asrock"
    needs_password = True


def make_backend(cfg, state_dir, system, ui=None):
    return AsrockBackend(cfg, state_dir, system, ui)
