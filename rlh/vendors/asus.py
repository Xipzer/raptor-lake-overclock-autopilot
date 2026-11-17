"""ASUS: NOT implemented.

Prototype notes for whoever adds it (see docs/VENDORS.md, nothing here has been verified by us):
- AMI Aptio. Community guides say "Publish HII Resources" must be enabled before SCEWIN can see the setup
  questions, and runtime variable protection may also apply.
- Start from ScewinBackend in rlh/backends.py and override only what differs.
"""
NAME = "asus"
TITLE = "ASUS"
STATUS = "not implemented"
NOTES = "AMI BIOS; needs Publish HII Resources (community reports)"


def make_backend(cfg, state_dir, system, ui=None):
    raise NotImplementedError("ASUS backend not implemented: see docs/VENDORS.md")
