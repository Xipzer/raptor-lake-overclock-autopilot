"""Configuration and shared plumbing for the campaign scripts: one TOML file (campaign.example.toml shows every key).

The file is campaign/campaign.toml, or the path in the RLAP_CAMPAIGN_CONFIG environment variable.
"""
import os
import subprocess
import sys

try:
    import tomllib
except ImportError:  # Python < 3.11
    sys.exit("The campaign scripts need Python 3.11 or newer (for tomllib).")

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.environ.get("RLAP_CAMPAIGN_CONFIG") or os.path.join(HERE, "campaign.toml")


def _load():
    if not os.path.exists(CONFIG):
        sys.exit("No campaign config at %s. Copy campaign/campaign.example.toml to campaign/campaign.toml and edit it, "
                 "or set RLAP_CAMPAIGN_CONFIG." % CONFIG)
    with open(CONFIG, "rb") as f:
        return tomllib.load(f)


C = _load()


def get(section, key, default=None):
    return C.get(section, {}).get(key, default)


def path(section, key, default=""):
    v = get(section, key, default)
    return os.path.expandvars(os.path.expanduser(v)) if v else v


HOME = path("paths", "state_dir", "~/rlap-campaign")
os.makedirs(HOME, exist_ok=True)
JOURNAL_COPIES = [os.path.expanduser(p) for p in get("paths", "journal_copies", [])]
STAGE = path("paths", "scewin_stage")
HELPER = path("paths", "helper_dir")
FS = path("paths", "failsafe_dir")
WINDIR = path("paths", "windows_dir", "/mnt/c/Windows")
SYS32 = WINDIR + "/System32"
PS = SYS32 + "/WindowsPowerShell/v1.0/powershell.exe"
SCHTASKS = SYS32 + "/schtasks.exe"
TASKLIST = SYS32 + "/tasklist.exe"
TASKKILL = SYS32 + "/taskkill.exe"
SHUTDOWN = SYS32 + "/shutdown.exe"

YC = path("tools", "ycruncher")
MEMLAT = path("tools", "memlat")
MEMHOLD = path("tools", "memhold")
MEMHOLD_LOG = path("tools", "memhold_log")
MEMHOLD_LOG_WIN = get("tools", "memhold_log_windows", "")
TM5_DIR = path("tools", "tm5_dir")
CB = path("tools", "cinebench")
SD = path("tools", "shakedown_dir")
SD_WIN = get("tools", "shakedown_dir_windows", "")
BENCH_TEMP_WIN = get("tools", "bench_temp_windows", "C:\\Windows\\Temp")

TASK_SCEWIN = get("tasks", "scewin_agent", "RlapScewinAgent")
TASK_HELPER = get("tasks", "helper", "RlapHelper")

SLOW_BOOT = int(get("campaign", "slow_boot_s", 120))
YC_ARGS = list(get("campaign", "ycruncher_args", ["stress", "-M:30G", "-D:60", "-TL:420", "FFTv4", "N63", "VT3"]))


def shell(name, text):
    """Run the [notify] command `name` with `text` on stdin. Best effort: never raises."""
    cmd = get("notify", name, "")
    if not cmd:
        return False
    try:
        subprocess.run(cmd, shell=True, input=text, text=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=60)
        return True
    except Exception:  # noqa: BLE001 - a notification must never stop a test
        return False


def coolant():
    """Coolant temperature in C from [sensors] coolant_command, or -1 if not configured or unreadable."""
    cmd = get("sensors", "coolant_command", "")
    if not cmd:
        return -1
    try:
        return float(subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30).stdout.strip())
    except (ValueError, subprocess.SubprocessError, OSError):
        return -1
