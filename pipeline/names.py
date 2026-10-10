"""Normalise Thai speaker names so one person maps to one key.

The site's speaker labels vary for the same person: different titles
("รองศาสตราจารย์อนุสรณ์ ธรรมใจ" / "นายอนุสรณ์ ธรรมใจ"), doubled names
("นายรังสิมันต์ โรม นายรังสิมันต์ โรม"), roll-call numbering ("๑๙๓. นางบุญยิ่ง …"),
role suffixes ("… สมาชิกสภาผู้แทนราษฎร"), and spacing differences.

    person_key(raw)   -> 'อนุสรณ์ธรรมใจ'   (title-less, space-less; None if not a name)
    clean_name(raw)   -> 'รองศาสตราจารย์อนุสรณ์ ธรรมใจ'   (display form, title kept)
    strip_titles(s)   -> 'อนุสรณ์ ธรรมใจ'
"""

from __future__ import annotations

import re

_LEVELS = ("เอก", "โท", "ตรี")
_RANK_STEMS = (
    "พล", "พัน", "ร้อย", "จ่าสิบ", "สิบ",                                  # army
    "พลตำรวจ", "พันตำรวจ", "ร้อยตำรวจ", "จ่าสิบตำรวจ", "สิบตำรวจ",         # police
    "พลเรือ", "นาวา", "เรือ", "พันจ่า", "จ่า",                              # navy
    "พลอากาศ", "นาวาอากาศ", "เรืออากาศ", "พันจ่าอากาศ", "จ่าอากาศ",        # air force
)
RANKS = [s + lv + f for s in _RANK_STEMS for lv in _LEVELS for f in ("หญิง", "")] + ["ดาบตำรวจ", "พลตำรวจ"]
HONORIFICS = [
    "ศาสตราจารย์พิเศษ", "ศาสตราจารย์", "รองศาสตราจารย์", "ผู้ช่วยศาสตราจารย์", "ดร.",
    "นายแพทย์", "แพทย์หญิง", "ทันตแพทย์หญิง", "ทันตแพทย์", "เภสัชกรหญิง", "เภสัชกร",
    "สัตวแพทย์หญิง", "สัตวแพทย์", "หม่อมราชวงศ์", "หม่อมหลวง", "หม่อมเจ้า", "ท่านผู้หญิง",
    "คุณหญิง", "ว่าที่", "ท่าน",
]
BASIC = ["นางสาว", "นาง", "นาย"]
TITLES = sorted(RANKS + HONORIFICS + BASIC, key=len, reverse=True)

_TITLE_RE = re.compile(r"(?:%s)\s?" % "|".join(map(re.escape, TITLES)))
_ROLE_TAIL_RE = re.compile(
    r"\s*\(?\s*(?:สมาชิกสภาผู้แทนราษฎร|สมาชิกวุฒิสภา|สมาชิกรัฐสภา|เลขาธิการ|รองเลขาธิการ|ประธาน|รองประธาน|"
    r"รัฐมนตรี|รองนายกรัฐมนตรี|นายกรัฐมนตรี|กรรมาธิการ|ผู้ชี้แจง|แบบบัญชีรายชื่อ)\S*.*$")
_NUMBERING_RE = re.compile(r"[\d๐-๙]+\.\s*")
_POLITE_TAIL_RE = re.compile(r"\s(?:ครับ|ค่ะ|คะ)(?:\s.*)?$")


def strip_titles(name: str) -> str:
    """Remove leading titles. After the first one, a plain นาย/นาง/นางสาว is only removed if
    it repeats the previous title (นายนาย…), so names that happen to start with นาง… survive."""
    s = name.strip()
    prev = None
    for _ in range(4):
        m = _TITLE_RE.match(s)
        if not m:
            break
        title = m.group(0).strip()
        if prev is not None and title in BASIC and title != prev:
            break
        rest = s[m.end():].strip()
        if len(rest.replace(" ", "")) < 2:
            break
        s, prev = rest, title
    return s


_STEM_ABBR = {
    "พล": "พล.", "พัน": "พ.", "ร้อย": "ร.", "จ่าสิบ": "จ.ส.", "สิบ": "ส.",
    "พลตำรวจ": "พล.ต.", "พันตำรวจ": "พ.ต.", "ร้อยตำรวจ": "ร.ต.", "จ่าสิบตำรวจ": "จ.ส.ต.", "สิบตำรวจ": "ส.ต.",
    "พลเรือ": "พล.ร.", "นาวา": "น.", "เรือ": "ร.", "พันจ่า": "พ.จ.", "จ่า": "จ.",
    "พลอากาศ": "พล.อ.", "นาวาอากาศ": "น.", "เรืออากาศ": "ร.", "พันจ่าอากาศ": "พ.จ.", "จ่าอากาศ": "จ.",
}
_LEVEL_ABBR = {"เอก": "อ.", "โท": "ท.", "ตรี": "ต."}
TITLE_ABBR = {
    **{s + lv + f: _STEM_ABBR[s] + _LEVEL_ABBR[lv] + f for s in _RANK_STEMS for lv in _LEVELS for f in ("หญิง", "")},
    "ดาบตำรวจ": "ด.ต.",
    "ศาสตราจารย์พิเศษ": "ศ.พิเศษ", "ศาสตราจารย์": "ศ.", "รองศาสตราจารย์": "รศ.", "ผู้ช่วยศาสตราจารย์": "ผศ.",
    "นายแพทย์": "นพ.", "แพทย์หญิง": "พญ.", "ทันตแพทย์": "ทพ.", "ทันตแพทย์หญิง": "ทพญ.",
    "เภสัชกร": "ภก.", "เภสัชกรหญิง": "ภกญ.", "สัตวแพทย์": "สพ.", "สัตวแพทย์หญิง": "สพญ.",
    "หม่อมราชวงศ์": "ม.ร.ว.", "หม่อมหลวง": "ม.ล.", "หม่อมเจ้า": "ม.จ.",
}
_DROP_TITLES = {*BASIC, "ท่าน"}


def display_name(name: str) -> str:
    """Short display form: นาย/นาง/นางสาว (and ท่าน) dropped, other titles abbreviated
    ("รองศาสตราจารย์อนุสรณ์ ธรรมใจ" -> "รศ. อนุสรณ์ ธรรมใจ", "พลตำรวจโท X Y" -> "พล.ต.ท. X Y").
    Follows strip_titles' rules for which leading words count as titles."""
    s, prev, kept, spaced = name.strip(), None, [], False
    for _ in range(4):
        m = _TITLE_RE.match(s)
        if not m:
            break
        title = m.group(0).strip()
        # "ว่าที่ร้อยตรี นายX": a plain title set off by a space still goes; glued on ("ดร.นายก") it is part of a name
        if prev is not None and title in BASIC and title != prev and not spaced:
            break
        spaced = m.group(0) != title
        rest = s[m.end():].strip()
        if len(rest.replace(" ", "")) < 2:
            break
        if title not in _DROP_TITLES:
            kept.append(TITLE_ABBR.get(title, title))
        s, prev = rest, title
    return " ".join([*kept, s])


def _basic_clean(raw: str) -> str:
    s = re.sub(r"\s+", " ", raw or "").strip()
    s = s.replace(" - ", " ")
    s = _NUMBERING_RE.sub("", s)
    s = _POLITE_TAIL_RE.sub("", s)
    s = _ROLE_TAIL_RE.sub("", s)
    s = re.sub(r"[():]+", " ", s)
    return re.sub(r"\s+", " ", s).strip().rstrip(".").strip()


def _collapse_repeat(s: str) -> str:
    """'X นายX' / 'XนายX' / 'X X' -> 'X' (one person written twice)."""
    for m in _TITLE_RE.finditer(s):
        if m.start() == 0:
            continue
        left, right = s[:m.start()].strip(), s[m.start():].strip()
        if left and _key(left) == _key(right):
            return left
    toks = s.split()
    half = len(toks) // 2
    if len(toks) % 2 == 0 and half and toks[:half] == toks[half:]:
        return " ".join(toks[:half])
    return s


def _key(s: str) -> str:
    return strip_titles(s).replace(" ", "")


def clean_name(raw: str | None) -> str | None:
    """Display form with title, or None if `raw` isn't a usable name."""
    if not raw:
        return None
    s = _collapse_repeat(_basic_clean(raw))
    core = strip_titles(s)
    if core in TITLES or len(re.sub(r"[^ก-๏A-Za-z]", "", core)) < 3:
        return None
    return s


def person_key(raw: str | None) -> str | None:
    s = clean_name(raw)
    return _key(s) if s else None


def has_formal_title(name: str) -> bool:
    """True if the name starts with a rank or academic/professional title (not นาย/นาง/ท่าน)."""
    m = _TITLE_RE.match(name)
    return bool(m) and m.group(0).strip() not in (*BASIC, "ท่าน")


def pick_display(forms: dict[str, int]) -> str:
    """Choose a display name among spellings {name: count}: prefer forms with a formal title
    (rank, ศาสตราจารย์ …), then the most frequent."""
    formal = {n: c for n, c in forms.items() if has_formal_title(n)}
    pool = formal or forms
    return max(pool.items(), key=lambda kv: (kv[1], -len(kv[0])))[0]
