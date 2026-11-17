"""MSI: NOT implemented.

Prototype notes for whoever adds it (see docs/VENDORS.md, nothing here has been verified by us):
- Most MSI Z690/Z790 boards are AMI Aptio, so SCEWIN may work. Whether runtime variable protection blocks imports,
  and whether a password is needed, is unknown.
- Question names differ from ASRock (for example the memory profile is "Extreme Memory Profile(XMP)").
- Start from AsrockBackend's parent, ScewinBackend, and override only what differs.
"""
NAME = "msi"
TITLE = "MSI"
STATUS = "not implemented"
NOTES = "AMI BIOS; SCEWIN untested by us"


def make_backend(cfg, state_dir, system, ui=None):
    raise NotImplementedError("MSI backend not implemented: see docs/VENDORS.md")
