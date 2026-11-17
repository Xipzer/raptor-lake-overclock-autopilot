"""Synthetic SCEWIN exports and fake Windows interfaces for the unit tests. Nothing here comes from a real export:
the layout follows AMI's documented script format (header, then question blocks separated by blank lines)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HEADER = ("// Script File Name : export.txt\r\n// Created on 01/01/26 at 00:00:00\r\n"
          "// AMISCE Utility. Ver 5.05.01.0002\r\nHIICrc32= %s\r\n\r\n")


def numeric(name, token, value=0, live=None, markup="{a1#", default=0):
    prompt = "  %s%s" % (markup, name) if markup else " " + name
    if live is not None:
        prompt += " {p200# [1;37;m%s [;;m" % live
    return ("Setup Question\t=%s\r\nHelp String\t= Synthetic help.\r\nToken\t=%s\t// Do NOT change this line\r\n"
            "Offset\t=10\r\nWidth\t=02\r\nBIOS Default\t=<%d> \r\nValue\t=<%d>\r\n\r\n" % (prompt, token, default, value))


def option(name, token, options, chosen, live=None):
    prompt = " " + name + (" {p200# [1;37;m%s [;;m" % live if live is not None else "")
    rows = []
    for i, label in enumerate(options):
        star = "*" if label == chosen else ""
        rows.append("%s[%02X]%s" % (star, i, label))
    body = "Options\t=%s\t// Move \"*\" to the desired Option\r\n" % rows[0]
    body += "".join("         %s\r\n" % r for r in rows[1:])
    return ("Setup Question\t=%s\r\nHelp String\t= Synthetic.\r\nToken\t=%s\t// Do NOT change this line\r\n"
            "Offset\t=20\r\nWidth\t=01\r\nBIOS Default\t=[00]%s \r\n%s\r\n" % (prompt, token, options[0], body))


def inactive(name, token):
    return "// Setup Question\t= %s\r\n// Token\t=%s\t// Do NOT change this line\r\n// Value\t=<0>\r\n\r\n" % (name, token)


def export(live=None, values=None, crc="0000BEEF", profile="XMP 3.0 Profile 1", extra=""):
    """A small ASRock-like export. `live` overrides the live values shown in prompts, `values` the stored ones."""
    live = dict({"tCL": "40", "tRCD": "50", "tRP": "50", "tRAS": "96", "tWR": "24", "tRTP": "23", "tRFC": "680",
                 "tRFC2": "680", "tREFI": "32767", "TX": "1.450V", "VDD2": "1.450V", "tRDRD_sg": "16"}, **(live or {}))
    values = dict({k: 0 for k in live}, **(values or {}))
    q = [
        numeric("CAS# Latency (tCL)", "2CA5", values["tCL"], live["tCL"]),
        numeric("RAS# to CAS# Delay (tRCD)", "2CA7", values["tRCD"], live["tRCD"]),
        numeric("Row Precharge (tRP)", "2CA8", values["tRP"], live["tRP"]),
        numeric("RAS# Active Time (tRAS)", "2CA9", values["tRAS"], live["tRAS"]),
        numeric("Write Recovery Time (tWR)", "2CAB", values["tWR"], live["tWR"]),
        numeric("Read to Precharge (tRTP)", "2CB3", values["tRTP"], live["tRTP"]),
        numeric("Refresh Cycle Time (tRFC)", "2CAC", values["tRFC"], live["tRFC"]),
        numeric("Refresh Cycle Time 2 (tRFC2)", "2CAE", values["tRFC2"], live["tRFC2"]),
        numeric("tREFI", "2CB6", values["tREFI"], live["tREFI"]),
        numeric("tRDRD_sg", "2C10", 0),                     # duplicate name: menu copy without a live value
        numeric("tRDRD_sg", "2CC9", values["tRDRD_sg"], live["tRDRD_sg"]),
        numeric("VDD_IMC Voltage", "2CDB", values["TX"], live["TX"], markup="{a1# {f3#"),
        numeric("VDD_CPU Voltage", "2CFD", values["VDD2"], live["VDD2"], markup="{a1# {f3# {w1401#"),
        option("Load XMP Setting", "2C9F", ["Auto", "XMP 3.0 Profile 1", "XMP 3.0 Profile 2"], profile),
        option("CPU CORE/Cache Voltage", "2CEE", ["Auto", "Offset Mode", "Fixed Mode"], "Fixed Mode"),
        numeric("Fixed Voltage(V)", "2CEF", 1340, None, markup="{a1# {f3# {w1601#  "),
        option("CPU Core/Cache Load-Line Calibration", "71", ["Auto", "Level 1", "Level 2"], "Level 1", live="Level 1"),
        option("MRC Fast Boot", "70", ["Auto", "Disabled", "Enabled"], "Auto"),
        option("Boot Failure Guard Message", "D2", ["Disabled", "Enabled"], "Enabled"),
        numeric("Boot Failure Guard Count", "D3", 4, None, markup=""),
        option("ASRock Timing Optimization", "6A", ["Disabled", "Enabled"], "Enabled"),
        option("DRAM Gear Mode", "2B", ["Auto", "Gear2", "Gear4"], "Auto"),
        numeric("AC Loadline", "353", 50, None, markup=""),
        numeric("DC Loadline", "354", 50, None, markup=""),
        inactive("Boot Failure Guard Message", "1370"),
        "Setup Question\t= Reset NVRAM\r\nToken\t=01\t// Do NOT change this line\r\nValue\t=1\t// Enabled = 1, Disabled = 0\r\n\r\n",
        'Setup Question\t= Some Password\r\nToken\t=CA\t// Do NOT change this line\r\nValue\t=""\r\n\r\n',
    ]
    return HEADER % crc + "".join(q) + extra


class FakeSys:
    """Fake Windows for doctor probes: canned CIM rows, registry values, events."""

    def __init__(self, cim=None, reg=None, events=None, whea=None, bcd=None):
        self._cim, self._reg, self._events, self._whea, self._bcd = cim or {}, reg or {}, events, whea, bcd

    def cim(self, cls, namespace=None, props=None):
        return self._cim.get(cls)

    def reg(self, path, name):
        return self._reg.get((path, name))

    def events(self, log, ids=None, provider=None, since=None, count=200):
        return self._events

    def whea_since(self, since):
        return self._whea

    def bcd(self):
        return self._bcd

    def crashes_since(self, since):
        return None

    def boot_gap(self):
        return None
