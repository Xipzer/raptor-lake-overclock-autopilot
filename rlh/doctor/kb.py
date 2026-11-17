"""The quirk knowledge base (quirks/*.toml) and its evaluation against facts.

Entry fields (all strings unless noted):
  id, title, category
  severity     INFO | NOTE | WARN | LIMIT   (LIMIT = no remedy: a constraint to know and live with)
  confidence   source | inference | thin   (how good the evidence for the quirk itself is)
  platform     table of fact -> expected: a list means "one of", a string is a case-insensitive regex,
               a bool or number must be equal. All must hold for the entry to apply.
  detect       optional list of {fact, op, value}; detect_mode = "all" (default) or "any".
               No detect list: the entry applies to the whole platform ("APPLIES": nothing to measure).
  symptom, cause, tuning
  remediation  list of steps; empty list = no remedy
  procedure    optional list of steps for a guided test (for example the SA bug check)
  implications optional list of directives for the sweep engine (see implications())
  sources      list of URLs or short references
Facts are a flat dict: name -> {"value": ..., "source": "probe:<name>" | "user" | "file:<name>"}.

Status of an entry for a machine:
  DETECTED      platform matches and the detect conditions hold
  APPLIES       platform matches; the entry has nothing to detect (general knowledge for this platform)
  NOT PRESENT   platform matches, a detect condition is false
  UNKNOWN       a needed fact is missing (a probe could not check), and nothing seen rules it out
  N/A           the platform does not match
"""
import glob
import os
import re

try:
    import tomllib
except ImportError:  # pragma: no cover
    tomllib = None

SEVERITIES = ("LIMIT", "WARN", "NOTE", "INFO")
CONFIDENCES = ("source", "inference", "thin")
OPS = ("eq", "ne", "lt", "le", "gt", "ge", "in", "not_in", "regex", "not_regex", "exists", "contains", "true", "false")
DIRECTIVES = ("skip", "gate", "cap", "order_first", "readback", "note")
MISSING = object()


class KBError(Exception):
    pass


def load(path):
    files = sorted(glob.glob(os.path.join(path, "*.toml"))) if os.path.isdir(path) else [path]
    entries, ids = [], set()
    for f in files:
        with open(f, "rb") as fh:
            data = tomllib.load(fh)
        for e in data.get("quirk", []):
            e["file"] = os.path.basename(f)
            validate(e)
            if e["id"] in ids:
                raise KBError("duplicate quirk id %s" % e["id"])
            ids.add(e["id"])
            entries.append(e)
    return entries


def validate(e):
    where = "quirk %s (%s)" % (e.get("id", "?"), e.get("file", "?"))
    for k in ("id", "title", "severity", "confidence", "symptom", "cause", "sources"):
        if k not in e:
            raise KBError("%s: missing %s" % (where, k))
    if e["severity"] not in SEVERITIES:
        raise KBError("%s: severity must be one of %s" % (where, SEVERITIES))
    if e["confidence"] not in CONFIDENCES:
        raise KBError("%s: confidence must be one of %s" % (where, CONFIDENCES))
    if "remediation" not in e:
        raise KBError("%s: remediation is required (use [] for none)" % where)
    if e["severity"] == "LIMIT" and e["remediation"]:
        raise KBError("%s: a LIMIT has no remedy; put workarounds under tuning" % where)
    if e["severity"] != "LIMIT" and not e["remediation"]:
        raise KBError("%s: no remediation given; use severity LIMIT if there is none" % where)
    for c in e.get("detect", []):
        if c.get("op") not in OPS or "fact" not in c:
            raise KBError("%s: bad detect condition %r" % (where, c))
    if e.get("detect_mode", "all") not in ("all", "any"):
        raise KBError("%s: detect_mode must be all or any" % where)
    for d in e.get("implications", []):
        if d.get("action") not in DIRECTIVES:
            raise KBError("%s: unknown implication action %r" % (where, d.get("action")))
    if not e["sources"]:
        raise KBError("%s: at least one source (or 'own measurement: ...')" % where)


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        s = v.strip().lower()
        try:
            return int(s, 16) if s.startswith("0x") else float(s)
        except ValueError:
            return None
    return None


def check(op, got, want):
    """True/False, or None when the fact is missing."""
    if got is MISSING:
        return None
    if op == "exists":
        return got is not None
    if got is None:
        return None
    if op == "true":
        return got is True
    if op == "false":
        return got is False
    if op in ("lt", "le", "gt", "ge"):
        a, b = _num(got), _num(want)
        if a is None or b is None:
            return None
        return {"lt": a < b, "le": a <= b, "gt": a > b, "ge": a >= b}[op]
    if op == "eq":
        a, b = _num(got), _num(want)
        if a is not None and b is not None:
            return a == b
        return str(got).casefold() == str(want).casefold()
    if op == "ne":
        return not check("eq", got, want)
    if op in ("in", "not_in"):
        hit = str(got).casefold() in [str(w).casefold() for w in want]
        return hit if op == "in" else not hit
    if op in ("regex", "not_regex"):
        hit = re.search(want, str(got), re.I) is not None
        return hit if op == "regex" else not hit
    if op == "contains":
        if isinstance(got, (list, tuple, set)):
            return any(str(want).casefold() == str(x).casefold() for x in got)
        if isinstance(got, dict):
            return str(want) in got
        return str(want).casefold() in str(got).casefold()
    raise KBError("unknown op %s" % op)


def _platform_conds(platform):
    out = []
    for fact, want in (platform or {}).items():
        if isinstance(want, list):
            out.append({"fact": fact, "op": "in", "value": want})
        elif isinstance(want, str):
            out.append({"fact": fact, "op": "regex", "value": want})
        else:
            out.append({"fact": fact, "op": "eq", "value": want})
    return out


def _eval(conds, facts, mode="all"):
    """(result True/False/None, evidence list, missing facts list)."""
    results, evidence, missing = [], [], []
    for c in conds:
        f = facts.get(c["fact"])
        got = f["value"] if f else MISSING
        r = check(c["op"], got, c.get("value"))
        results.append(r)
        if r is None:
            missing.append(c["fact"])
        else:
            evidence.append({"fact": c["fact"], "value": got, "op": c["op"], "want": c.get("value"), "holds": r,
                             "source": f["source"] if f else None})
    if not conds:
        return True, evidence, missing
    if mode == "any":
        if any(r is True for r in results):
            return True, evidence, missing
        return (None if None in results else False), evidence, missing
    if any(r is False for r in results):
        return False, evidence, missing
    return (None if None in results else True), evidence, missing


def evaluate(entry, facts):
    plat, pev, pmiss = _eval(_platform_conds(entry.get("platform")), facts)
    out = {"id": entry["id"], "entry": entry, "platform_evidence": pev, "missing": list(pmiss)}
    if plat is False:
        out["status"] = "N/A"
        return out
    det, dev, dmiss = _eval(entry.get("detect", []), facts, entry.get("detect_mode", "all"))
    out["evidence"] = dev
    out["missing"] += dmiss
    if not entry.get("detect"):
        out["status"] = "UNKNOWN" if plat is None else "APPLIES"
    elif det is False:
        out["status"] = "NOT PRESENT"
    elif det is True and plat is True:
        out["status"] = "DETECTED"
    else:
        out["status"] = "UNKNOWN"
    sources = {e["source"] for e in dev if e["holds"]} | {e["source"] for e in pev}
    sources.discard(None)
    if out["status"] == "DETECTED":
        out["detection"] = describe_sources(sources)
    elif out["status"] == "APPLIES":
        out["detection"] = "platform match (%s)" % describe_sources(sources) if sources else "platform match"
    else:
        out["detection"] = "-"
    return out


SOURCE_LABELS = {"probe": "measured on this machine", "file:scewin": "read from your BIOS export",
                 "file:ledger": "read from your run ledger", "file:facts": "from the facts file", "user": "stated by you"}


def describe_sources(sources):
    labels = []
    for s in sorted(sources):
        key = "probe" if s.startswith("probe:") else s
        label = SOURCE_LABELS.get(key, s)
        if label not in labels:
            labels.append(label)
    return ", ".join(labels) or "-"


def evaluate_all(entries, facts):
    return [evaluate(e, facts) for e in entries]


def implications(results):
    """Merge the directives of DETECTED and APPLIES entries into what the sweep engine consumes:
      skip  {role: reason}   steps that change these settings are skipped (a group may set ignore_doctor)
      gate  {role: below_c}  tighten only when the last DIMM peak was below this
      cap   {limit: value}   a stricter limit (never a looser one)
      order_first [roles]    settings that should be tuned before others (warned about, not reordered)
      readback [roles]       values to re-read after the listed changes
      notes [text]"""
    out = {"skip": {}, "gate": {}, "cap": {}, "order_first": [], "readback": [], "notes": [], "from": []}
    for r in results:
        if r["status"] not in ("DETECTED", "APPLIES"):
            continue
        for d in r["entry"].get("implications", []):
            a = d["action"]
            if a == "skip":
                for s in d["settings"]:
                    out["skip"].setdefault(s, "%s (%s)" % (d.get("reason", r["entry"]["title"]), r["id"]))
            elif a == "gate":
                for s in d["settings"]:
                    out["gate"][s] = min(d["below_c"], out["gate"].get(s, d["below_c"]))
            elif a == "cap":
                out["cap"][d["limit"]] = min(d["value"], out["cap"].get(d["limit"], d["value"]))
            elif a == "order_first":
                out["order_first"] += [s for s in d["settings"] if s not in out["order_first"]]
            elif a == "readback":
                out["readback"] += [s for s in d["settings"] if s not in out["readback"]]
            elif a == "note":
                out["notes"].append("%s (%s)" % (d["text"], r["id"]))
            if r["id"] not in out["from"]:
                out["from"].append(r["id"])
    return out
