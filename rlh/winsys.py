"""What the harness asks Windows: event log, registry, CIM, reboots. Read-only except reboot() and the installers.

Every query tolerates a missing tool or missing rights and returns None for "could not check", never a guess.
The event log is read with wevtutil (XML output, so it does not depend on the display language).
"""
import json
import os
import re
import subprocess
import xml.etree.ElementTree as ET

NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"
CRASH_IDS = (41, 1001, 6008)  # Kernel-Power 41, BugCheck 1001, EventLog 6008 (unexpected shutdown)


def run(args, timeout=120, input=None):
    """(exit code, output) or (None, error text) when the program cannot start."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, input=input, errors="replace")
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.SubprocessError) as e:
        return None, str(e)


def parse_events(xml_text):
    """wevtutil /f:xml prints <Event> elements back to back; wrap them so they parse as one document."""
    if not xml_text or "<Event" not in xml_text:
        return []
    xml_text = re.sub(r"<\?xml[^>]*\?>", "", xml_text)
    try:
        root = ET.fromstring("<Events>%s</Events>" % xml_text)
    except ET.ParseError:
        return []
    out = []
    for ev in root.iter(NS + "Event"):
        sysel = ev.find(NS + "System")
        if sysel is None:
            continue
        prov = sysel.find(NS + "Provider")
        tc = sysel.find(NS + "TimeCreated")
        data = {}
        for d in ev.iter(NS + "Data"):
            if d.get("Name"):
                data[d.get("Name")] = (d.text or "").strip()
        out.append({
            "id": int(sysel.findtext(NS + "EventID", "0")),
            "provider": prov.get("Name") if prov is not None else "",
            "time": tc.get("SystemTime") if tc is not None else "",
            "data": data,
        })
    return out


def iso_to_epoch(s):
    import datetime
    s = s.rstrip("Z")
    if "." in s:
        head, frac = s.split(".", 1)
        s = head + "." + frac[:6]
    try:
        dt = datetime.datetime.fromisoformat(s).replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None
    return dt.timestamp()


class WindowsSystem:
    def __init__(self, clock):
        self.clock = clock

    def events(self, log, ids=None, provider=None, since=None, count=200):
        conds = []
        if ids:
            conds.append("(" + " or ".join("EventID=%d" % i for i in ids) + ")")
        if provider:
            conds.append("Provider[@Name='%s']" % provider)
        if since:
            ms = max(1, int((self.clock.time() - since) * 1000))
            conds.append("TimeCreated[timediff(@SystemTime) <= %d]" % ms)
        q = "*[System[%s]]" % " and ".join(conds) if conds else "*"
        code, out = run(["wevtutil", "qe", log, "/q:" + q, "/f:xml", "/rd:true", "/c:%d" % count])
        if code is None or code != 0:
            return None
        return parse_events(out)

    def crashes_since(self, since):
        ev = self.events("System", CRASH_IDS, since=since)
        if ev is None:
            return None
        out = []
        for e in ev:
            if e["id"] == 1001 and e["provider"] not in ("Microsoft-Windows-WER-SystemErrorReporting", "BugCheck"):
                continue
            code = e["data"].get("BugcheckCode") or e["data"].get("param1") or ""
            out.append("%d%s at %s" % (e["id"], " code %s" % code if code else "", e["time"][:19]))
        return out

    def whea_since(self, since):
        ev = self.events("System", provider="Microsoft-Windows-WHEA-Logger", since=since, count=2000)
        if ev is None:
            return None
        return [{"id": e["id"], "time": e["time"], "apic": e["data"].get("ApicId"), "bank": e["data"].get("MCABank"),
                 "mcistat": e["data"].get("MciStat"), "source": e["data"].get("ErrorSource")} for e in ev]

    def boot_gap(self):
        """Seconds from the last clean shutdown (Kernel-General 13) to this kernel start (12): POST plus training."""
        ev = self.events("System", (12, 13), provider="Microsoft-Windows-Kernel-General", count=6)
        if not ev:
            return None
        ev.sort(key=lambda e: e["time"])
        starts = [e for e in ev if e["id"] == 12]
        if not starts:
            return None
        boot = iso_to_epoch(starts[-1]["time"])
        stops = [iso_to_epoch(e["time"]) for e in ev if e["id"] == 13 and iso_to_epoch(e["time"]) < boot]
        return int(boot - stops[-1]) if stops and boot else None

    def reg(self, path, name):
        try:
            import winreg
        except ImportError:
            return None
        root, _, sub = path.partition("\\")
        hive = {"HKLM": winreg.HKEY_LOCAL_MACHINE, "HKCU": winreg.HKEY_CURRENT_USER}[root]
        try:
            with winreg.OpenKey(hive, sub) as k:
                return winreg.QueryValueEx(k, name)[0]
        except OSError:
            return None

    def reg_set_dword(self, path, name, value):
        import winreg
        root, _, sub = path.partition("\\")
        hive = {"HKLM": winreg.HKEY_LOCAL_MACHINE}[root]
        with winreg.OpenKey(hive, sub, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, int(value))

    def cim(self, cls, namespace=None, props=None):
        """Instances of a CIM class as dicts, through PowerShell (no third-party WMI module needed)."""
        sel = " | Select-Object %s" % ",".join(props) if props else ""
        ns = " -Namespace %s" % namespace if namespace else ""
        code, out = run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                         "Get-CimInstance -ClassName %s%s -ErrorAction Stop%s | ConvertTo-Json -Depth 3" % (cls, ns, sel)])
        if code != 0 or not out.strip():
            return None
        try:
            data = json.loads(out)
        except ValueError:
            return None
        return data if isinstance(data, list) else [data]

    def bcd(self):
        code, out = run(["bcdedit", "/enum", "{current}"])
        if code != 0:
            return None
        vals = {}
        for line in out.splitlines():
            m = re.match(r"(\w+)\s{2,}(.*)$", line.strip())
            if m:
                vals[m.group(1).lower()] = m.group(2).strip()
        return vals

    def reboot(self, why):
        run(["shutdown", "/r", "/t", "10", "/c", ("Tuning harness: " + why)[:500]])


class NullSystem:
    """Not on Windows (or a test): every query is 'could not check'."""

    def __init__(self, clock=None):
        self.clock = clock

    def events(self, *a, **k):
        return None

    def crashes_since(self, since):
        return None

    def whea_since(self, since):
        return None

    def boot_gap(self):
        return None

    def reg(self, path, name):
        return None

    def cim(self, *a, **k):
        return None

    def bcd(self):
        return None

    def reboot(self, why):
        raise RuntimeError("cannot reboot from here")


def make(clock):
    return WindowsSystem(clock) if os.name == "nt" else NullSystem(clock)
