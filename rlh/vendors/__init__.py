"""Board vendor plugins.

A vendor plugin knows how to read and write BIOS settings from Windows on one vendor's boards. The sweep engine is
the same for every vendor: it only calls the methods listed in base.BiosBackend.

Status today:
- asrock: supported (AMI SCEWIN with /cpwd), tested end to end on a Z790 board.
- msi, asus, gigabyte: NOT implemented. Selecting one stops with a pointer to docs/VENDORS.md. Use the manual
  backend (you type the values in the BIOS) or write the plugin.

To add a vendor: copy msi.py, implement make_backend(), set STATUS = "supported" once it has been run end to end
on real hardware, and register the module below. See docs/VENDORS.md.
"""
from . import asrock, asus, gigabyte, msi

VENDORS = {m.NAME: m for m in (asrock, msi, asus, gigabyte)}


class VendorNotImplemented(Exception):
    pass


def get(name):
    key = (name or "").strip().lower()
    if key not in VENDORS:
        raise VendorNotImplemented("unknown board vendor %r (known: %s). See docs/VENDORS.md."
                                   % (name, ", ".join(VENDORS)))
    return VENDORS[key]


def make_backend(name, cfg, state_dir, system, ui=None):
    mod = get(name)
    if mod.STATUS != "supported":
        raise VendorNotImplemented(
            "%s: %s. Not implemented in this release. Use [backend] type = \"manual\" (you enter the values in the "
            "BIOS, the harness does the rest), or implement rlh/vendors/%s.py following docs/VENDORS.md."
            % (mod.TITLE, mod.NOTES, mod.NAME))
    return mod.make_backend(cfg, state_dir, system, ui)


def table():
    return [(m.NAME, m.TITLE, m.STATUS, m.NOTES) for m in VENDORS.values()]
