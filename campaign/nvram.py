"""Read and edit SCEWIN script files (nvram.txt) by question token.

A question block runs from "Setup Question" to the next blank line. Numeric questions carry Value=<n>,
option questions mark the chosen option with "*". The name shown in the BIOS can carry the effective value
(e.g. "tCL 40"), which label() returns.
"""
import re
import sys


class Script:
    def __init__(self, text):
        self.lines = text.split("\n")
        self.blocks = []
        cur = None
        for i, line in enumerate(self.lines):
            s = line.rstrip("\r")
            if s.startswith("Setup Question"):
                cur = {"start": i, "lines": [i]}
                self.blocks.append(cur)
            elif cur is not None:
                if s.strip() == "":
                    cur = None
                else:
                    cur["lines"].append(i)
        self.by_token = {}
        for b in self.blocks:
            for i in b["lines"]:
                if self.lines[i].startswith("Token"):
                    b["token"] = self.lines[i].split("=", 1)[1].split("//")[0].strip()
                    self.by_token.setdefault(b["token"], []).append(b)

    def text(self):
        return "\n".join(self.lines)

    def block(self, token):
        hits = self.by_token.get(token, [])
        if len(hits) != 1:
            raise KeyError("token %s: %d matches" % (token, len(hits)))
        return hits[0]

    def name(self, token):
        raw = self.lines[self.block(token)["start"]].split("=", 1)[1]
        raw = re.sub(r"\x1b\[[0-9;]*m|\[1;37;m|\[;;m|\{[a-z]\d+#", " ", raw)
        return re.sub(r"\s+", " ", raw).strip()

    def label(self, token):
        """The value the BIOS shows beside the name: the trained/effective value."""
        m = re.search(r"\[1;37;m\s*([^\[]+?)\s*\[;;m", self.lines[self.block(token)["start"]])
        return m.group(1).strip() if m else None

    def set_label(self, token, text):
        """Rewrite the value shown in a question's prompt (SCEWIN matches questions by the full prompt)."""
        i = self.block(token)["start"]
        self.lines[i] = re.sub(r"(\[1;37;m)\s*[^\[]*?(\s*\[;;m)", lambda m: m.group(1) + str(text) + m.group(2),
                               self.lines[i], count=1)

    def value(self, token):
        for i in self.block(token)["lines"]:
            if self.lines[i].startswith("Value"):
                m = re.search(r"<(-?\d+)>", self.lines[i])
                return int(m.group(1)) if m else None
        for i in self.block(token)["lines"]:
            m = re.search(r"\*\[([0-9A-Fa-f]+)\](.*?)(//|$)", self.lines[i])
            if m and not self.lines[i].startswith("BIOS Default"):
                return m.group(2).strip()
        return None

    def options(self, token):
        out = []
        for i in self.block(token)["lines"]:
            s = self.lines[i]
            if s.startswith("BIOS Default"):
                continue
            m = re.search(r"(\*?)\[([0-9A-Fa-f]+)\](.*?)(//|$)", s)
            if m:
                out.append((i, m.group(2), m.group(3).strip()))
        return out

    def set(self, token, value):
        opts = self.options(token)
        has_value = any(self.lines[i].startswith("Value") for i in self.block(token)["lines"])
        if has_value:
            for i in self.block(token)["lines"]:
                if self.lines[i].startswith("Value"):
                    self.lines[i] = re.sub(r"<-?\d+>", "<%d>" % int(value), self.lines[i], count=1)
            return
        labels = [o[2] for o in opts]
        codes = [o[1].upper() for o in opts]
        want = str(value)
        if want in labels:
            pick = labels.index(want)
        elif want.upper() in codes:
            pick = codes.index(want.upper())
        else:
            raise ValueError("token %s: no option %s (have %s)" % (token, value, labels))
        for n, (i, _, _) in enumerate(opts):
            s = self.lines[i].replace("*[", "[", 1)
            if n == pick:
                s = s.replace("[", "*[", 1)
            self.lines[i] = s


if __name__ == "__main__":
    sc = Script(open(sys.argv[1], encoding="utf-8", errors="replace").read())
    for tok in sys.argv[2:]:
        try:
            print(tok, "|", sc.name(tok), "| label", sc.label(tok), "| value", sc.value(tok))
        except Exception as e:
            print(tok, "->", e)
