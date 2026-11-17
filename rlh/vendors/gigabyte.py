"""Gigabyte: NOT implemented.

Prototype notes for whoever adds it (see docs/VENDORS.md, nothing here has been verified by us):
- AMI Aptio on current boards. Whether SCEWIN imports work, and what blocks them, is unknown to us.
- Start from ScewinBackend in rlh/backends.py and override only what differs.
"""
NAME = "gigabyte"
TITLE = "Gigabyte"
STATUS = "not implemented"
NOTES = "AMI BIOS; SCEWIN untested by us"


def make_backend(cfg, state_dir, system, ui=None):
    raise NotImplementedError("Gigabyte backend not implemented: see docs/VENDORS.md")
