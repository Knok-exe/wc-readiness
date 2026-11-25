"""HL7 v2 parser, vendored from Knok-exe/hl7v2-to-fhir.

Source: https://github.com/Knok-exe/hl7v2-to-fhir, file src/hl7_fhir/parser.py at commit
dc60de6840d531607af02002bca97abe495889b1 (MIT licence, same author). Copied rather than
installed because that repo has no release tag yet, and depending on an unpinned git main
would let upstream changes break this project silently. The only edit: ParseError is defined
here instead of imported from hl7_fhir.errors. When hl7v2-to-fhir tags a release, this file can
be replaced with `from hl7_fhir.parser import parse_message` and a pinned dependency.

Original docstring:
A real HL7 v2 parser: honours MSH-1/MSH-2 delimiters, repetitions, components,
sub-components and escape sequences. Splitting is done on the RAW text first and
unescaping happens last, per leaf value (the order that avoids the classic bug where an
escaped delimiter such as \\F\\ is split on)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field


class ParseError(Exception):
    """The text is not a well-formed HL7 v2 message."""


SEGMENT_NAME = re.compile(r"^[A-Z][A-Z0-9]{2}$")


@dataclass(frozen=True)
class Encoding:
    field: str = "|"
    comp: str = "^"
    rep: str = "~"
    esc: str = "\\"
    sub: str = "&"

    def unescape(self, text: str) -> str:
        if self.esc not in text:
            return text
        e = re.escape(self.esc)
        pattern = re.compile(f"{e}([^{e}]*){e}")

        def replace(match: re.Match) -> str:
            seq = match.group(1)
            simple = {"F": self.field, "S": self.comp, "T": self.sub, "R": self.rep, "E": self.esc}
            if seq in simple:
                return simple[seq]
            if seq == ".br":
                return "\n"
            if seq in ("H", "N"):  # highlighting on/off: no meaning in plain text
                return ""
            if seq.startswith("X") and len(seq) > 1 and len(seq) % 2 == 1:
                try:
                    raw = bytes.fromhex(seq[1:])
                except ValueError:
                    return match.group(0)
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    return raw.decode("latin-1")
            return match.group(0)  # \Z..\ local escapes and anything unknown are kept as-is

        return pattern.sub(replace, text)


@dataclass
class Segment:
    name: str
    fields: list[str]  # raw (still escaped) text; fields[n-1] is field n
    enc: Encoding = field(default_factory=Encoding)

    def raw(self, n: int) -> str:
        return self.fields[n - 1] if 0 < n <= len(self.fields) else ""

    def _atomic(self, n: int) -> bool:
        """MSH-1 (the field separator) and MSH-2 (the encoding characters) are never split
        or unescaped: they define the delimiters rather than use them."""
        return self.name == "MSH" and n in (1, 2)

    def repetitions(self, n: int) -> list[str]:
        raw = self.raw(n)
        if self._atomic(n):
            return [raw] if raw else []
        return raw.split(self.enc.rep) if raw != "" else []

    def components(self, n: int, rep: int = 1) -> list[str]:
        """Raw components of repetition `rep` (1-based) of field n."""
        reps = self.repetitions(n)
        if not 0 < rep <= len(reps):
            return []
        if self._atomic(n):
            return [reps[0]]
        return reps[rep - 1].split(self.enc.comp)

    def get(self, n: int, rep: int = 1, comp: int = 1, sub: int | None = 1) -> str:
        """Unescaped text. sub=None returns the whole component (sub-components joined)."""
        comps = self.components(n, rep)
        if not 0 < comp <= len(comps):
            return ""
        text = comps[comp - 1]
        if self._atomic(n):
            return text
        if sub is None:
            return self.enc.unescape(text)
        parts = text.split(self.enc.sub)
        return self.enc.unescape(parts[sub - 1]) if 0 < sub <= len(parts) else ""

    def count(self, n: int) -> int:
        return len(self.repetitions(n))


@dataclass
class Message:
    segments: list[Segment]
    enc: Encoding
    raw: str = ""

    @property
    def msh(self) -> Segment:
        return self.segments[0]

    @property
    def control_id(self) -> str:
        return self.msh.get(10)

    @property
    def version(self) -> str:
        return self.msh.get(12)

    @property
    def type_code(self) -> str:
        return self.msh.get(9, comp=1)

    @property
    def event(self) -> str:
        return self.msh.get(9, comp=2)

    @property
    def type_label(self) -> str:
        return f"{self.type_code}^{self.event}" if self.event else self.type_code

    def all(self, name: str) -> list[Segment]:
        return [s for s in self.segments if s.name == name]

    def first(self, name: str) -> Segment | None:
        for s in self.segments:
            if s.name == name:
                return s
        return None


def normalise(text: str) -> str:
    """Strip MLLP bytes and make segment terminators a single CR."""
    text = text.replace("\x0b", "").replace("\x1c", "")
    return text.replace("\r\n", "\r").replace("\n", "\r").strip("\r \t")


def parse_message(text: str) -> Message:
    original = text
    text = normalise(text)
    if not text.startswith("MSH"):
        raise ParseError("message does not start with an MSH segment")
    if len(text) < 8:
        raise ParseError("MSH segment is too short to contain the encoding characters")
    field_sep = text[3]
    first_line = text.split("\r", 1)[0]
    parts = first_line.split(field_sep)
    msh2 = parts[1] if len(parts) > 1 else ""
    if len(msh2) < 4:
        raise ParseError("MSH-2 must contain at least four encoding characters")
    comp, rep, esc, sub = msh2[0], msh2[1], msh2[2], msh2[3]
    if len({field_sep, comp, rep, esc, sub}) != 5:
        raise ParseError(f"MSH-1/MSH-2 delimiters are not distinct: {field_sep}{msh2}")
    enc = Encoding(field_sep, comp, rep, esc, sub)

    segments: list[Segment] = []
    for number, line in enumerate(text.split("\r"), start=1):
        if line == "":
            continue
        name = line[:3]
        if not SEGMENT_NAME.match(name) or (len(line) > 3 and line[3] != field_sep):
            raise ParseError(f"segment {number} is malformed: {line[:20]!r}")
        pieces = line.split(field_sep)
        if name == "MSH":
            fields = [field_sep, *pieces[1:]]  # MSH-1 is the separator itself
        else:
            fields = pieces[1:]
        segments.append(Segment(name, fields, enc))
    msg = Message(segments, enc, original)
    if not msg.type_code:
        raise ParseError("MSH-9 (message type) is missing")
    if not msg.control_id:
        raise ParseError("MSH-10 (message control id) is missing")
    return msg


def split_messages(text: str) -> list[str]:
    """Split a file that contains several messages (each starts with an MSH line)."""
    lines = normalise(text).split("\r")
    messages: list[list[str]] = []
    for line in lines:
        if line.startswith("MSH"):
            messages.append([line])
        elif messages:
            messages[-1].append(line)
    return ["\r".join(m) for m in messages]
