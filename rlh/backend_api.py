"""The interface every BIOS backend implements. The engine calls nothing else.

A snapshot (what read() returns):
    {"effective": {setting: live value the BIOS shows, or None},
     "stored": {setting: value stored in NVRAM, or None},
     "profile": the memory profile question's value (None if not configured),
     "manual": None, or the user's answers (manual mode without a reader)}

Rules a backend must keep:
- apply() writes the FULL wanted set (every kept value plus the step), never a partial set, and verifies it by
  reading the BIOS back before it returns True.
- Passwords come from the user's config or environment, never from code. Mask them in every log line.
- read() never writes anything.
- A tool error is reported as a failure of apply() or read() (False / None), never as a hardware result.
"""


class BiosBackend:
    automatic = False   # True: the backend writes the BIOS and reboots by itself
    name = "base"

    def prepare(self):
        """Check that every configured setting exists and save the pre-sweep BIOS. Returns (ok, message)."""
        raise NotImplementedError

    def apply(self, wanted, step=None):
        """Write all of `wanted` ({setting: value}); `step` is the subset this step changes. Returns (ok, message)."""
        raise NotImplementedError

    def read(self):
        """A snapshot (see the module docstring), or None if the BIOS could not be read."""
        raise NotImplementedError

    def arm_failsafe(self, good, step, tag):
        """Leave the last good values for a startup failsafe before booting into `step`. Returns True if armed."""
        return False

    def disarm(self):
        """The step booted and its test started: the failsafe must not act."""

    def failsafe_restored(self):
        """Text if the startup failsafe restored the last good values since the last call, else None."""
        return None

    def reboot(self, why):
        raise NotImplementedError
