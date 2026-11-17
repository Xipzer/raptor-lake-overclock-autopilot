"""Read and edit AMI SCEWIN script files (the text that `SCEWIN_64.exe /o /s file` exports).

A question block starts at a line "Setup Question = <prompt>" and ends at the next blank line. Numeric questions
carry "Value =<n>", option questions mark the chosen option with "*", string questions carry Value ="...".
Blocks whose lines start with "//" are inactive (the BIOS hides them) and are ignored.

Two quirks this module exists for:
- The prompt carries markup ({a1#, {p200#, ...) and, on many boards, the live value the BIOS shows beside the name,
  between "[1;37;m" and "[;;m" (for example "tCL [1;37;m40 [;;m"). That live value is the trained or effective
  value, which label() returns.
- SCEWIN matches a question on import by its full prompt, live value included. A script built from an old export
  is silently skipped ("unmatched question"). So edits are always made on a fresh export, and set_label() exists
  for the one case where a script must match a prompt the BIOS will only show after the next boot (the failsafe).

Questions are found by name, not token: tokens differ between BIOS versions and boards.
"""
import re

LABEL_RE = re.compile(r"\[1;37;m\s*([^\[]*?)\s*\[;;m")
MARKUP_RE = re.compile(r"\x1b\[[0-9;]*m|\[1;37;m[^\[]*\[;;m|\[;;m|\[1;37;m|\{[a-z]\d+#")


class QuestionError(KeyError):
    pass


def normalise(name):
    return re.sub(r"\s+", " ", MARKUP_RE.sub(" ", name)).strip()


class Question:
    def __init__(self, script, start):
        self.script = script
        self.start = start
        self.lines = [start]
        self.token = None

    @property
    def prompt(self):
        return self.script.lines[self.start].split("=", 1)[1]

    @property
    def name(self):
        return normalise(self.prompt)

    def _field(self, key):
        for i in self.lines:
            if self.script.lines[i].startswith(key):
                return i
        return None


class Script:
    def __init__(self, text):
        self.lines = text.split("\n")
        self.questions = []
        cur = None
        for i, line in enumerate(self.lines):
            s = line.rstrip("\r")
            if s.startswith("Setup Question"):
                cur = Question(self, i)
                self.questions.append(cur)
            elif cur is not None:
                if s.strip() == "":
                    cur = None
                else:
                    cur.lines.append(i)
                    if s.startswith("Token"):
                        cur.token = s.split("=", 1)[1].split("//")[0].strip().upper()
        self.by_token = {}
        for q in self.questions:
            self.by_token.setdefault(q.token, []).append(q)

    @classmethod
    def load(cls, path):
        with open(path, encoding="utf-8", errors="replace", newline="") as f:
            return cls(f.read())

    def text(self):
        return "\n".join(self.lines)

    def save(self, path):
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(self.text())

    def header(self, key):
        """A header field such as HIICrc32 (changes when the BIOS setup layout changes)."""
        for line in self.lines[:20]:
            if line.startswith(key):
                return line.split("=", 1)[1].strip()
        return None

    def matches(self, name):
        want = normalise(name).casefold()
        return [q for q in self.questions if q.name.casefold() == want]

    def search(self, pattern):
        rx = re.compile(pattern, re.I)
        return [q for q in self.questions if rx.search(q.name)]

    def find(self, name, index=None, token=None):
        """One question by its BIOS name. Names can repeat (per channel, per menu): then `index` (0-based among the
        matches, in file order) or `token` must pick one, and an ambiguous request raises instead of guessing."""
        hits = self.matches(name)
        if token is not None:
            hits = [q for q in hits if q.token == str(token).upper()]
        if not hits:
            raise QuestionError("no question named %r%s" % (name, " with token %s" % token if token else ""))
        if index is not None:
            if index >= len(hits):
                raise QuestionError("question %r: index %d but only %d matches" % (name, index, len(hits)))
            return hits[index]
        if len(hits) > 1:
            raise QuestionError("question %r is ambiguous: %d matches (tokens %s, live values %s); set index or token"
                                % (name, len(hits), [q.token for q in hits], [self.label(q) for q in hits]))
        return hits[0]

    # Reading
    def label(self, q):
        m = LABEL_RE.search(self.lines[q.start])
        return m.group(1).strip() if m else None

    def kind(self, q):
        i = q._field("Value")
        if i is not None:
            return "string" if '"' in self.lines[i].split("=", 1)[1].split("//")[0] else "value"
        return "options" if self.options(q) else "none"

    def value(self, q):
        """Int for numeric questions, the option label for option questions, a str for string questions."""
        i = q._field("Value")
        if i is not None:
            raw = self.lines[i].split("=", 1)[1].split("//")[0].strip()
            m = re.search(r"<(-?\d+)>", raw)
            if m:
                return int(m.group(1))
            m = re.search(r'"(.*)"', raw)
            if m:
                return m.group(1)
            m = re.match(r"-?\d+$", raw)
            return int(raw) if m else None
        for _, _, label, chosen in self._option_rows(q):
            if chosen:
                return label
        return None

    def default(self, q):
        i = q._field("BIOS Default")
        if i is None:
            return None
        raw = self.lines[i].split("=", 1)[1].strip()
        m = re.match(r"<(-?\d+)>", raw)
        if m:
            return int(m.group(1))
        m = re.match(r"\[([0-9A-Fa-f]+)\](.*)", raw)
        return m.group(2).strip() if m else raw

    def _option_rows(self, q):
        rows = []
        for i in q.lines:
            s = self.lines[i]
            if s.startswith("BIOS Default"):
                continue
            m = re.search(r"(\*?)\[([0-9A-Fa-f]+)\](.*?)(//|\r?$)", s)
            if m and (s.startswith("Options") or s[:1] in (" ", "\t")):
                rows.append((i, m.group(2).upper(), m.group(3).strip(), m.group(1) == "*"))
        return rows

    def options(self, q):
        return [label for _, _, label, _ in self._option_rows(q)]

    # Editing
    def set(self, q, value):
        i = q._field("Value")
        if i is not None and self.kind(q) == "value":
            key, rest = self.lines[i].split("=", 1)
            if re.search(r"<-?\d+>", rest):
                rest = re.sub(r"<-?\d+>", "<%d>" % int(value), rest, count=1)
            else:
                rest = re.sub(r"-?\d+", "%d" % int(value), rest, count=1)
            self.lines[i] = key + "=" + rest
            return
        rows = self._option_rows(q)
        if not rows:
            raise ValueError("question %r cannot be set (kind %s)" % (q.name, self.kind(q)))
        labels = [r[2].casefold() for r in rows]
        codes = [r[1] for r in rows]
        want = str(value)
        if want.casefold() in labels:
            pick = labels.index(want.casefold())
        elif want.upper().lstrip("0") in [c.lstrip("0") for c in codes]:
            pick = [c.lstrip("0") for c in codes].index(want.upper().lstrip("0"))
        else:
            raise ValueError("question %r has no option %r (options: %s)" % (q.name, value, [r[2] for r in rows]))
        for n, (line, _, _, _) in enumerate(rows):
            s = self.lines[line].replace("*[", "[", 1)
            if n == pick:
                s = s.replace("[", "*[", 1)
            self.lines[line] = s

    def set_label(self, q, text):
        """Rewrite the live value in a prompt so the script matches what the BIOS will show after a boot."""
        if self.label(q) is None:
            raise ValueError("question %r shows no live value to rewrite" % q.name)
        self.lines[q.start] = re.sub(r"(\[1;37;m)\s*[^\[]*?(\s*\[;;m)", lambda m: m.group(1) + str(text) + m.group(2),
                                     self.lines[q.start], count=1)

    def single(self):
        """Questions whose token is unique (safe to copy between two exports of the same BIOS)."""
        return [q for q in self.questions if len(self.by_token.get(q.token, [])) == 1]


def unmatched(output):
    """Lines in SCEWIN's import output that report a skipped question."""
    return [l.strip() for l in output.splitlines() if re.search(r"(?i)unmatched|not found|mismatch", l)]
