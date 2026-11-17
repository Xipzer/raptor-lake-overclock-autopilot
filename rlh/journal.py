"""Append-only Markdown journal, atomic JSON state, and the optional mirror and notify hooks.

The journal is written BEFORE anything that can take the machine down, so if the PC dies the last line names what
was running ("If nothing follows, ...").
"""
import json
import os
import shlex
import subprocess
import time


class Clock:
    """Wall clock. The simulator swaps in a fake one so runs are deterministic."""

    def time(self):
        return time.time()

    def sleep(self, s):
        time.sleep(s)

    def stamp(self, fmt="%H:%M:%S"):
        return time.strftime(fmt, time.localtime(self.time()))


class FakeClock(Clock):
    def __init__(self, start=1_700_000_000.0):
        self.t = start

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def stamp(self, fmt="%H:%M:%S"):
        return time.strftime(fmt, time.gmtime(self.t))


def _hook(command, text, timeout=30):
    """Run a user-supplied hook command with the text on stdin. Failures never stop the sweep."""
    if not command:
        return False
    args = command if isinstance(command, list) else shlex.split(command, posix=os.name != "nt")
    try:
        subprocess.run(args, input=text, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=timeout)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


class Journal:
    def __init__(self, path, clock, mirror=None, notify=None, echo=True):
        self.path, self.clock, self.mirror, self.notify, self.echo = path, clock, mirror, notify, echo
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def write(self, text, important=False):
        line = "- %s %s" % (self.clock.stamp(), text)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        if self.echo:
            print(line)
        _hook(self.mirror, line + "\n")
        if important:
            _hook(self.notify, text)

    def heading(self, text):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("\n## %s %s\n\n" % (self.clock.stamp("%Y-%m-%d %H:%M"), text))
        if self.echo:
            print("## " + text)
        _hook(self.mirror, "\n## %s\n\n" % text)

    def tail(self, n=30):
        try:
            with open(self.path, encoding="utf-8") as f:
                return f.read().splitlines()[-n:]
        except OSError:
            return []


class StateStore:
    def __init__(self, path):
        self.path = path

    def exists(self):
        return os.path.exists(self.path)

    def load(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    def save(self, st):
        """Write to a temp file, fsync, then rename over the old file: a crash leaves the old or the new state."""
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
