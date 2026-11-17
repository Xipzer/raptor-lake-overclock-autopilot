"""Doctor report: Markdown for people, JSON for tools and the sweep engine."""
import json
import time

from . import kb

ORDER = {"WARN": 0, "LIMIT": 1, "NOTE": 2, "INFO": 3}
EVIDENCE = {"source": "source-backed (published sources)", "inference": "inference (reasoned from sources or "
            "measurements, not stated directly)", "thin": "thin (one anecdote, or a forum claim we could not verify)"}


def _fmt(v, name=""):
    if isinstance(v, int) and "microcode" in name and not isinstance(v, bool):
        return "%d (0x%X)" % (v, v)
    if isinstance(v, dict):
        return ", ".join("%s: %s" % kv for kv in list(v.items())[:8]) or "none"
    if isinstance(v, (list, tuple)):
        return ", ".join(map(str, v[:10])) or "none"
    return str(v)


def _evidence(r, holds=True):
    ev = [e for e in r.get("evidence", []) if e["holds"] is holds]
    if not ev and holds:
        ev = r.get("platform_evidence", [])
    return "; ".join("%s = %s (%s)" % (e["fact"], _fmt(e["value"], e["fact"]), e["source"]) for e in ev)


def build(results, statuses, facts, implications, meta=None):
    shown = [r for r in results if r["status"] in ("DETECTED", "APPLIES")]
    shown.sort(key=lambda r: (0 if r["status"] == "DETECTED" else 1, ORDER[r["entry"]["severity"]], r["id"]))
    return {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "meta": meta or {},
        "summary": {s: sum(1 for r in results if r["status"] == s)
                    for s in ("DETECTED", "APPLIES", "NOT PRESENT", "UNKNOWN", "N/A")},
        "items": [{
            "id": r["id"], "title": r["entry"]["title"], "status": r["status"], "severity": r["entry"]["severity"],
            "evidence_quality": r["entry"]["confidence"], "detection": r["detection"], "why_detected": _evidence(r),
            "symptom": r["entry"]["symptom"], "cause": r["entry"]["cause"],
            "remediation": r["entry"]["remediation"], "procedure": r["entry"].get("procedure", []),
            "tuning": r["entry"].get("tuning", ""), "sources": r["entry"]["sources"]} for r in shown],
        "not_present": [{"id": r["id"], "title": r["entry"]["title"], "why": _evidence(r, False)}
                        for r in results if r["status"] == "NOT PRESENT"],
        "could_not_check": [{"id": r["id"], "title": r["entry"]["title"], "missing": sorted(set(r["missing"]))}
                            for r in results if r["status"] == "UNKNOWN"],
        "probes": statuses,
        "facts": {k: v for k, v in sorted(facts.items())},
        "implications": implications,
    }


def to_json(report):
    return json.dumps(report, indent=1, default=str)


def to_markdown(rep):
    s = rep["summary"]
    out = ["# Quirk report", "",
           "Generated %s. Read-only: nothing on this machine was changed." % rep["generated"], "",
           "%d detected, %d apply to your platform in general, %d checked and not present, %d could not be "
           "checked, %d not applicable." % (s["DETECTED"], s["APPLIES"], s["NOT PRESENT"], s["UNKNOWN"], s["N/A"]),
           "",
           "Severity: WARN = act on it; LIMIT = no remedy, a constraint to know; NOTE = matters for tuning; "
           "INFO = background. Evidence quality says how good the evidence for the quirk itself is. Detection says "
           "how we know it applies to you (measured, stated by you in the config, or only by platform).", ""]
    for status, head in (("DETECTED", "Detected on this machine"), ("APPLIES", "Applies to your platform")):
        items = [i for i in rep["items"] if i["status"] == status]
        if not items:
            continue
        out += ["## %s" % head, ""]
        for sev in ("WARN", "LIMIT", "NOTE", "INFO"):
            for i in [x for x in items if x["severity"] == sev]:
                out += ["### [%s] %s" % (sev, i["title"]), "",
                        "- Id: `%s`. Evidence quality: %s. Detection: %s." % (i["id"], EVIDENCE[i["evidence_quality"]],
                                                                              i["detection"])]
                if i["why_detected"]:
                    out.append("- What we saw: %s" % i["why_detected"])
                out += ["- Symptom: %s" % i["symptom"], "- Why: %s" % i["cause"]]
                if i["remediation"]:
                    out.append("- What to do:")
                    out += ["  %d. %s" % (n + 1, step) for n, step in enumerate(i["remediation"])]
                else:
                    out.append("- What to do: nothing fixes this. Know it and plan around it.")
                if i["procedure"]:
                    out.append("- Guided check:")
                    out += ["  %d. %s" % (n + 1, step) for n, step in enumerate(i["procedure"])]
                if i["tuning"]:
                    out.append("- For your tuning: %s" % i["tuning"])
                out.append("- Sources: %s" % "; ".join(i["sources"]))
                out.append("")
    imp = rep["implications"]
    out += ["## Implications for your tuning plan", ""]
    lines = []
    for role, why in imp["skip"].items():
        lines.append("- Skip steps that change **%s**: %s" % (role, why))
    for role, c in imp["gate"].items():
        lines.append("- Tighten **%s** only when the last DIMM peak was below %s C" % (role, c))
    for lim, v in imp["cap"].items():
        lines.append("- Stricter limit **%s** = %s" % (lim, v))
    if imp["order_first"]:
        lines.append("- Tune first: %s" % ", ".join(imp["order_first"]))
    if imp["readback"]:
        lines.append("- Re-read after changes: %s" % ", ".join(imp["readback"]))
    lines += ["- %s" % n for n in imp["notes"]]
    out += (lines or ["- None."]) + ["", "The sweep engine reads these from implications.json (see README).", ""]
    if rep["not_present"]:
        out += ["## Checked and not present", ""]
        out += ["- %s (`%s`): %s" % (i["title"], i["id"], i["why"]) for i in rep["not_present"]] + [""]
    if rep["could_not_check"]:
        out += ["## Could not check", "", "A probe could not read these facts, so the entries below are neither "
                "confirmed nor ruled out. Run on the machine itself, elevated, with a SCEWIN export, or state the fact "
                "under [doctor.facts] in your config.", ""]
        out += ["- %s (`%s`): missing %s" % (i["title"], i["id"], ", ".join(i["missing"]))
                for i in rep["could_not_check"]] + [""]
    out += ["## Probes", "", "| Probe | Status | Facts |", "|---|---|---|"]
    out += ["| %s | %s | %d |" % (p["probe"], p["status"].replace("|", "/"), p["facts"]) for p in rep["probes"]]
    out += ["", "## Facts used", "", "| Fact | Value | Source |", "|---|---|---|"]
    out += ["| %s | %s | %s |" % (k, _fmt(v["value"], k).replace("|", "/"), v["source"]) for k, v in rep["facts"].items()]
    return "\n".join(out) + "\n"


def run(entries, facts, statuses, meta=None):
    results = kb.evaluate_all(entries, facts)
    imp = kb.implications(results)
    return build(results, statuses, facts, imp, meta)
