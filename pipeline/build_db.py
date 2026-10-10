#!/usr/bin/env python3
"""Load scraped meeting JSON (data/<phase>/*.json) into an SQLite database (schema.sql).

Incremental: a meeting is (re)imported only if it is new or its JSON has a newer
scraped_at. Topic labels (speech_topics), topics and term stopword flags are kept
across re-imports.

Besides copying the raw data, the import builds "speeches" (continuous turns by one
speaker) from the transcript segments:
  1. Text repeated at segment/clip boundaries is trimmed (clips overlap a little, so the
     speech-to-text output repeats a few words to a sentence across the cut).
  2. Segments are split at inline minute markers such as
     "นายโสภณ ซารัมย์ (ประธานสภาผู้แทนราษฎร)  :  ต่อไปเชิญ..." — the site's speaker label
     often lags behind these, so the text after a marker is attributed to the named
     person (speaker_source = 'inferred').
  3. Each speech is tokenized with PyThaiNLP for word counts and full-text search.

Usage:
  .venv/bin/python pipeline/build_db.py                 # data/ -> mptracker.db
  .venv/bin/python pipeline/build_db.py --rebuild       # re-import every meeting
  .venv/bin/python pipeline/build_db.py --db other.db --data data
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import re
import sqlite3
import sys
from difflib import SequenceMatcher
from pathlib import Path

from pythainlp.corpus import thai_stopwords

from names import TITLES, clean_name, has_formal_title, person_key, pick_display, strip_titles
from thai_tokenizer import ThaiTokenizer

HERE = Path(__file__).resolve().parent  # pipeline/: schema.sql, party_colors.json
ROOT = HERE.parent  # project root: data/, mptracker.db

# Extra stopwords for parliamentary speech (formalities, fillers). Applied only to terms
# that are new to the DB; afterwards edit terms.is_stopword directly.
PARLIAMENT_STOPWORDS = {
    "ครับ", "ค่ะ", "คะ", "นะครับ", "นะคะ", "ท่าน", "ประธาน", "ท่านประธาน", "กราบเรียน", "เรียน",
    "เคารพ", "ที่เคารพ", "ขอบคุณ", "ขอบพระคุณ", "เชิญ", "กระผม", "ผม", "ดิฉัน", "สมาชิก",
    "ต่อไป", "นะ", "ก็", "อ่ะ", "เอ่อ", "อ่า", "อืม", "ฮะ", "จ้ะ", "ล่ะ", "หน่อย", "แล้วก็",
    "ซึ่ง", "นั้น", "นี้", "ตรงนี้", "อันนี้", "เรื่อง", "ครั้ง", "ด้วย", "ได้", "ไม่", "ว่า",
    "ที่จะ", "จริง ๆ", "แบบนี้", "แบบนั้น", "ดังนั้น", "ไหม", "มัน", "เนี่ย", "นู้น", "อย่างนี้",
    "อย่างนั้น", "ขออนุญาต", "ทั้งหมด", "ตรงนั้น", "จะต้อง", "เพราะฉะนั้น", "อะไร", "ยังไง",
    # formulaic phrases / function compounds that the phrase list turns into single tokens
    "เพื่อนสมาชิก", "สมาชิกสภา", "สมาชิกสภาผู้แทนราษฎร", "ขอเชิญ", "ไม่มี", "กราบ", "อนุญาต",
    "ผู้ทรงเกียรติ", "ความเคารพ", "ขอบพระคุณ", "สักครู่", "เมื่อสักครู่", "ตอนนี้", "เดี๋ยว",
}

TITLE_RE = "(?:" + "|".join(map(re.escape, TITLES)) + ")"
# "<anything>(<role or constituency>)  :  " — name is resolved from the text before "("
MARKER_PAREN_RE = re.compile(r"\(([^()\n]{1,120})\)\s*:\s")
COLON_RE = re.compile(r"\s:\s")


def norm_name(s: str | None) -> str | None:
    if not s:
        return None
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


# ───────────────────────────────────────────────────────────────── overlap trimming


def _despace(s: str) -> tuple[str, list[int]]:
    chars, idx = [], []
    for i, ch in enumerate(s):
        if not ch.isspace():
            chars.append(ch)
            idx.append(i)
    return "".join(chars), idx


def overlap_cut(prev: str, cur: str, window: int = 300) -> int:
    """Index in `cur` where its non-repeated text starts (0 = no overlap with `prev`).

    Matches whitespace-insensitively and tolerates a few differing characters at the very
    start of `cur` or the end of `prev` (the two clips transcribe the cut slightly
    differently)."""
    if not prev or not cur:
        return 0
    p, _ = _despace(prev[-window:])
    c, cidx = _despace(cur[:window])
    if not p or not c:
        return 0
    if p.endswith(c) or c in p[-len(c) - 15:]:
        return len(cur) if len(cur) <= window else 0  # cur entirely repeats prev's tail
    m = SequenceMatcher(None, p, c, autojunk=False).find_longest_match(0, len(p), 0, len(c))
    if m.size >= 10 and m.b <= 12 and len(p) - (m.a + m.size) <= 12:
        return cidx[m.b + m.size - 1] + 1
    return 0


# ───────────────────────────────────────────────────────────────── marker splitting


class MarkerFinder:
    def __init__(self, known_names: set[str]):
        self.names = sorted(known_names, key=len, reverse=True)
        self.by_last_char: dict[str, list[str]] = collections.defaultdict(list)
        for n in self.names:
            self.by_last_char[n[-1]].append(n)
        self.fallback_re = re.compile(TITLE_RE + r"\s?\S+\s\S+$")  # unknown names need first name + surname

    def _name_before(self, text: str, end: int, allow_fallback: bool) -> tuple[str, int] | None:
        window = text[max(0, end - 80):end].rstrip()
        base = max(0, end - 80)
        if not window:
            return None
        for n in self.by_last_char.get(window[-1], ()):
            if window.endswith(n):
                return n, base + len(window) - len(n)
        if allow_fallback:
            m = self.fallback_re.search(window)
            if m:
                # trim junk glued on the front (e.g. "รับ" from "ครับ"): start at the title
                t = re.search(TITLE_RE, m.group(0))
                start = m.start() + (t.start() if t else 0)
                return norm_name(window[start:]), base + start
        return None

    def find(self, text: str) -> list[dict]:
        """Markers in `text`: [{start, end, name, label}], start = where the name begins,
        end = where the new speaker's words begin."""
        found = []
        for m in MARKER_PAREN_RE.finditer(text):
            hit = self._name_before(text, m.start(), allow_fallback=True)
            if hit:
                found.append({"start": hit[1], "end": m.end(), "name": hit[0], "label": m.group(1).strip()})
        for m in COLON_RE.finditer(text):  # "ชื่อ นามสกุล  :  " without a role, known names only
            if any(f["start"] <= m.start() < f["end"] for f in found):
                continue
            hit = self._name_before(text, m.start(), allow_fallback=False)
            if hit:
                found.append({"start": hit[1], "end": m.end(), "name": hit[0], "label": None})
        return sorted(found, key=lambda f: f["start"])


# ───────────────────────────────────────────────────────────────── tokenization


class Tokenizer(ThaiTokenizer):
    """Thai word segmentation (thai_tokenizer.py) plus the stopword list that flags terms on first insert."""

    def __init__(self, extra_words: set[str]):
        super().__init__(extra_words)
        self.stopwords = set(thai_stopwords()) | PARLIAMENT_STOPWORDS


# ───────────────────────────────────────────────────────────────── DB helpers


class Lookup:
    """get-or-create cache for small name tables."""

    def __init__(self, db: sqlite3.Connection, table: str, col: str = "name"):
        self.db, self.table, self.col = db, table, col
        self.cache = {row[1]: row[0] for row in db.execute(f"SELECT id, {col} FROM {table}")}

    def __call__(self, value: str | None) -> int | None:
        if not value:
            return None
        if value not in self.cache:
            cur = self.db.execute(f"INSERT INTO {self.table} ({self.col}) VALUES (?)", (value,))
            self.cache[value] = cur.lastrowid
        return self.cache[value]


def clock_add(hms: str | None, secs: float | None) -> str | None:
    if not hms:
        return None
    try:
        t = dt.datetime.strptime(hms, "%H:%M:%S") + dt.timedelta(seconds=secs or 0)
        return t.strftime("%H:%M:%S")
    except ValueError:
        return hms


# ───────────────────────────────────────────────────────────────── import


def scan_people(files: list[Path]) -> dict:
    """One pass over all JSON: name forms for marker matching, a display name per person key
    (most common clean form), each person's usual party, and known provinces/parties."""
    raw_names: set[str] = set()
    forms: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    party_votes: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    provinces: set[str] = set()
    parties: set[str] = set()
    for f in files:
        m = json.loads(f.read_text(encoding="utf-8"))
        for clip in m["clips"]:
            for sp in clip["all_speakers"]:
                if (n := clean_name(sp["name"])) and (k := person_key(n)):
                    raw_names.add(n)
                    forms[k][n] += 0  # known, but no weight
            for seg in clip["segments"]:
                n = clean_name(seg["speaker"])
                if n:
                    k = person_key(n)
                    raw_names.add(n)
                    forms[k][n] += 1
                    if seg["speaker_party"]:
                        party_votes[k][seg["speaker_party"]] += 1
                if seg["speaker_province"]:
                    provinces.add(seg["speaker_province"])
                if seg["speaker_party"]:
                    parties.add(seg["speaker_party"])
    display = {k: pick_display(c) for k, c in forms.items()}
    usual_party = {k: c.most_common(1)[0][0] for k, c in party_votes.items()}
    return {"names": raw_names, "display": display, "usual_party": usual_party,
            "provinces": provinces, "parties": parties}


def lexicon_extras(people: dict) -> set[str]:
    """Words the tokenizer must keep whole: people's first names and surnames (title stripped),
    provinces and parties (also as "พรรค…")."""
    name_words = {w for n in people["display"].values() for w in strip_titles(n).split() if len(w) >= 2}
    return name_words | people["provinces"] | people["parties"] | {"พรรค" + p for p in people["parties"]}


def import_meeting(db: sqlite3.Connection, m: dict, source: Path, ctx: dict) -> tuple[int, int]:
    persons, parties, tags, terms = ctx["persons"], ctx["parties"], ctx["tags"], ctx["terms"]
    markers: MarkerFinder = ctx["markers"]
    tok: Tokenizer = ctx["tokenizer"]

    old = db.execute("SELECT id FROM meetings WHERE phase=? AND meeting_id=?", (m["phase"], m["meeting_id"])).fetchone()
    if old:
        db.execute("DELETE FROM speeches_fts WHERE rowid IN (SELECT id FROM speeches WHERE meeting_id=?)", old)
        db.execute("DELETE FROM meetings WHERE id=?", old)  # cascades to clips, segments, speeches

    mid = db.execute(
        """INSERT INTO meetings (phase, meeting_id, meeting_date, title, council, meeting_type, episode,
             house_number, house_year, session_number, scraped_at, source_file)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (m["phase"], m["meeting_id"], m["meeting_date"], m["meeting_title"], m["meeting_council"],
         m["meeting_type"], m["meeting_episode"], m["meeting_group"], m["meeting_year"],
         m["meeting_number"], m["scraped_at"], str(source.relative_to(ROOT) if source.is_relative_to(ROOT) else source)),
    ).lastrowid

    # Pass 1: insert clips + raw segments, and produce attributed text parts in order.
    parts = []  # dicts: segment_id, part, text, clip_seq, start, stop, clip_start, person, party, province, source, label
    prev_text = ""
    cur_site_speaker = object()
    inferred: dict | None = None
    for clip in m["clips"]:
        clip_id = db.execute(
            "INSERT INTO clips (meeting_id, seq, start_time, end_time, video_path, url) VALUES (?,?,?,?,?,?)",
            (mid, clip["seq"], clip["start_time"], clip["end_time"], clip["video_path"], clip["url"]),
        ).lastrowid
        for t in clip["tags"]:
            db.execute("INSERT OR IGNORE INTO clip_site_tags (clip_id, tag_id) VALUES (?,?)", (clip_id, tags(t)))

        for seg in clip["segments"]:
            site_key = person_key(seg["speaker"])
            if site_key:
                persons.alias(norm_name(seg["speaker"]), site_key)
            seg_id = db.execute(
                """INSERT INTO segments (site_id, clip_id, start_sec, stop_sec, person_id, party_id, province, text, text_raw)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (seg["id"], clip_id, seg["start"], seg["stop"], persons(site_key), parties(seg["speaker_party"]),
                 seg["speaker_province"], seg["text"], seg["text_raw"]),
            ).lastrowid

            text = seg["text"]
            if not text.strip():
                continue
            cut = overlap_cut(prev_text, text)
            prev_text = text

            if site_key != cur_site_speaker:  # site label changed: trust it again
                cur_site_speaker = site_key
                inferred = None

            # split points: [(start_of_marker, start_of_words, speaker_override)]
            bounds = [(0, 0, inferred)]
            for mk in markers.find(text):
                key = person_key(mk["name"])
                if not key:
                    continue
                persons.alias(mk["name"], key)
                label = mk["label"]
                province = label if label in ctx["provinces"] else None
                who = {"key": key, "label": label, "province": province}
                bounds.append((mk["start"], mk["end"], who))
            for i, (_, words_start, who) in enumerate(bounds):
                stop = bounds[i + 1][0] if i + 1 < len(bounds) else len(text)
                piece = text[max(words_start, cut):stop].strip() if stop > cut else ""
                inferred = who if i > 0 else inferred
                if who and who["key"] != site_key:
                    person, source_, label = who["key"], "inferred", who["label"]
                    party = ctx["usual_party"].get(person)
                    province = who["province"]
                else:
                    person, source_, label = site_key, "site" if site_key else "none", (who or {}).get("label")
                    party, province = seg["speaker_party"], seg["speaker_province"]
                if not piece:
                    continue
                parts.append({"segment_id": seg_id, "part": i, "text": piece, "clip_seq": clip["seq"],
                              "start": seg["start"], "stop": seg["stop"], "clip_start": clip["start_time"],
                              "person": person, "party": party, "province": province,
                              "source": source_, "label": label})

    # Pass 2: merge consecutive parts with the same speaker into speeches.
    speeches: list[list[dict]] = []
    for p in parts:
        if speeches and speeches[-1][-1]["person"] == p["person"] and p["person"] is not None:
            speeches[-1].append(p)
        else:
            speeches.append([p])

    n_words = 0
    texts = [" ".join(p["text"] for p in group) for group in speeches]
    all_tokens = tok.tokens_many(texts)  # in parallel; the database inserts below stay sequential
    for ordinal, (group, text, toks) in enumerate(zip(speeches, texts, all_tokens), 1):
        first, last = group[0], group[-1]
        sources = {p["source"] for p in group}
        source_ = "inferred" if "inferred" in sources else first["source"]
        label = next((p["label"] for p in group if p["label"]), None)
        party = next((p["party"] for p in group if p["party"]), None)
        province = next((p["province"] for p in group if p["province"]), None)
        n_words += len(toks)
        sid = db.execute(
            """INSERT INTO speeches (speech_key, meeting_id, ordinal, person_id, party_id, province, speaker_source,
                 speaker_label, first_clip_seq, first_start_sec, last_clip_seq, last_stop_sec, start_clock,
                 segment_count, char_count, word_count, text)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"{_site_id(db, first['segment_id'])}#{first['part']}", mid, ordinal, persons(first["person"]),
             parties(party), province, source_, label, first["clip_seq"], first["start"], last["clip_seq"],
             last["stop"], clock_add(first["clip_start"], first["start"]), len(group), len(text), len(toks), text),
        ).lastrowid
        db.executemany("INSERT INTO speech_segments (speech_id, segment_id, part, text) VALUES (?,?,?,?)",
                       [(sid, p["segment_id"], p["part"], p["text"]) for p in group])
        counts = collections.Counter(toks)
        db.executemany("INSERT INTO speech_terms (speech_id, term_id, count) VALUES (?,?,?)",
                       [(sid, terms(t), c) for t, c in counts.items()])
        db.execute("INSERT INTO speeches_fts (rowid, tokens) VALUES (?,?)", (sid, " ".join(toks)))
    return len(speeches), n_words


def _site_id(db: sqlite3.Connection, segment_id: int) -> str:
    return db.execute("SELECT site_id FROM segments WHERE id=?", (segment_id,)).fetchone()[0]


class TermLookup(Lookup):
    def __init__(self, db: sqlite3.Connection, stopwords: set[str]):
        super().__init__(db, "terms", "term")
        self.stopwords = stopwords

    def __call__(self, value: str) -> int:
        if value not in self.cache:
            cur = self.db.execute("INSERT INTO terms (term, is_stopword) VALUES (?,?)",
                                  (value, int(value in self.stopwords)))
            self.cache[value] = cur.lastrowid
        return self.cache[value]


class PersonLookup:
    """Person ids by normalised name key; also records every raw spelling as an alias."""

    def __init__(self, db: sqlite3.Connection, display: dict[str, str]):
        self.db, self.display = db, display
        self.cache = {k: i for i, k in db.execute("SELECT id, name_key FROM persons")}
        # display names may improve as more data arrives
        db.executemany("UPDATE persons SET name=? WHERE name_key=? AND name<>?",
                       [(display[k], k, display[k]) for k in self.cache if k in display])

    def __call__(self, key: str | None, raw: str | None = None) -> int | None:
        if not key:
            return None
        if key not in self.cache:
            name = self.display.get(key) or clean_name(raw) or key
            self.cache[key] = self.db.execute("INSERT INTO persons (name, name_key) VALUES (?,?)",
                                              (name, key)).lastrowid
        return self.cache[key]

    def alias(self, raw: str | None, key: str) -> None:
        if not raw:
            return
        pid = self(key, raw)
        if self.db.execute("INSERT OR IGNORE INTO person_aliases (alias, person_id) VALUES (?,?)",
                           (raw, pid)).rowcount:
            # a spelling with a rank/academic title (often only seen in minute markers) beats a plain one
            name = clean_name(raw)
            if name and has_formal_title(name) and not has_formal_title(self.display.get(key, "")):
                self.display[key] = name
                self.db.execute("UPDATE persons SET name=? WHERE id=?", (name, pid))


def migrate(db: sqlite3.Connection) -> None:
    """Add columns introduced after a DB was first created."""
    speech_cols = {r[1] for r in db.execute("PRAGMA table_info(speeches)")}
    if "party_source" not in speech_cols:
        db.execute("ALTER TABLE speeches ADD COLUMN party_source TEXT NOT NULL DEFAULT 'site'")
    if "party_inferred" in speech_cols:  # replaced by party_source
        db.execute("UPDATE speeches SET party_id = NULL WHERE party_inferred = 1")
        db.execute("ALTER TABLE speeches DROP COLUMN party_inferred")
    if "is_mp" not in {r[1] for r in db.execute("PRAGMA table_info(persons)")}:
        db.execute("ALTER TABLE persons ADD COLUMN is_mp INTEGER NOT NULL DEFAULT 0")
    person_cols = {r[1] for r in db.execute("PRAGMA table_info(persons)")}
    if "name_key" not in person_cols:
        sys.exit("This database predates person-name merging. Delete mptracker.db and run pipeline/build_db.py again.")
    if "is_presiding" not in person_cols:
        db.execute("ALTER TABLE persons ADD COLUMN is_presiding INTEGER NOT NULL DEFAULT 0")
    cols = {r[1] for r in db.execute("PRAGMA table_info(parties)")}
    for col in ("name_en", "color"):
        if col not in cols:
            db.execute(f"ALTER TABLE parties ADD COLUMN {col} TEXT")


def import_members(db: sqlite3.Connection, path: Path) -> tuple[int, list[str]]:
    """Load the official roster (data/members.json from scrape_members.py) into `members`, link each entry to the
    person who speaks under that name, and flag persons.is_mp. Returns (matched, names that matched nobody)."""
    db.execute("DELETE FROM members")
    db.execute("UPDATE persons SET is_mp = 0")
    if not path.exists():
        print(f"No {path.name} — run pipeline/scrape_members.py to add the official roster of MPs.")
        return 0, []
    parties = Lookup(db, "parties")
    person_by_key = dict(db.execute("SELECT name_key, id FROM persons"))
    unmatched, matched = [], 0
    for m in json.loads(path.read_text(encoding="utf-8"))["members"]:
        key = person_key(m["name"])
        person_id = person_by_key.get(key)
        if person_id is None:
            unmatched.append(m["name"])
        else:
            matched += 1
        db.execute("INSERT INTO members (member_no, name, name_key, party_id, province, district, person_id) VALUES (?,?,?,?,?,?,?)",
                   (m["member_no"], m["name"], key, parties(m["party"]), m["province"], m["district"], person_id))
    db.execute("UPDATE persons SET is_mp = 1 WHERE id IN (SELECT person_id FROM members WHERE person_id IS NOT NULL)")
    return matched, unmatched


def fill_labels_from_roster(db: sqlite3.Connection) -> tuple[int, int]:
    """Where the transcript site gave no party/province, use the roster. The party is filled only for people who have
    no site-given party at all (an MP who changed party may differ from the roster's current one); the province
    (constituency) never changes, so it is filled wherever missing. Returns (people given a party, speeches given a province)."""
    people = [r[0] for r in db.execute(
        """SELECT m.person_id FROM members m WHERE m.person_id IS NOT NULL AND m.party_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM speeches s WHERE s.person_id = m.person_id AND s.party_id IS NOT NULL)
           AND EXISTS (SELECT 1 FROM speeches s WHERE s.person_id = m.person_id)""")]
    for pid in people:
        db.execute("""UPDATE speeches SET party_id = (SELECT party_id FROM members WHERE person_id = ?), party_source = 'roster'
                      WHERE person_id = ? AND party_id IS NULL""", (pid, pid))
    provinces = db.execute(
        """UPDATE speeches SET province = (SELECT province FROM members WHERE person_id = speeches.person_id)
           WHERE province IS NULL AND person_id IN (SELECT person_id FROM members WHERE person_id IS NOT NULL)""").rowcount
    return len(people), provinces


def infer_missing_parties(db: sqlite3.Connection) -> int:
    """For people the roster does not cover either: MPs state their party when they introduce themselves
    ("กระผม <name> สมาชิกสภาผู้แทนราษฎร … พรรคเพื่อไทย"). For people with no party anywhere, take it from such
    introductions — only when exactly one party is named. Speeches are marked party_source = 'intro'.
    Returns the number of people filled in."""
    parties = dict(db.execute("SELECT name, id FROM parties"))
    if not parties:
        return 0
    intro = re.compile(r"(?:สมาชิก(?:สภา)?ผู้แทนราษฎร|ส\.ส\.).{0,60}?พรรค\s?("
                       + "|".join(sorted(map(re.escape, parties), key=len, reverse=True)) + ")")
    unlabelled = db.execute("""SELECT p.id, p.name FROM persons p JOIN speeches s ON s.person_id = p.id
                               GROUP BY p.id HAVING SUM(s.party_id IS NOT NULL) = 0""").fetchall()
    filled = 0
    for pid, name in unlabelled:
        first_name = strip_titles(name).split()[0]
        votes: collections.Counter = collections.Counter()
        for (text,) in db.execute("SELECT substr(text, 1, 400) FROM speeches WHERE person_id = ?", (pid,)):
            if first_name in text[:250] and (m := intro.search(text)):
                votes[m.group(1)] += 1
        if len(votes) == 1:
            party = next(iter(votes))
            db.execute("UPDATE speeches SET party_id = ?, party_source = 'intro' WHERE person_id = ? AND party_id IS NULL",
                       (parties[party], pid))
            filled += 1
    return filled


# Minute-marker roles that mean the person was chairing the sitting.
PRESIDING_LABELS = ("ประธานสภาผู้แทนราษฎร%", "รองประธานสภาผู้แทนราษฎร%", "ประธานรัฐสภา%", "รองประธานรัฐสภา%")


def refresh_aggregates(db: sqlite3.Connection) -> None:
    """Rebuild derived tables that span all meetings."""
    db.execute("DELETE FROM member_terms")
    db.execute("""INSERT INTO member_terms (party_id, person_id, term_id, count)
                  SELECT s.party_id, s.person_id, st.term_id, SUM(st.count)
                  FROM speeches s JOIN speech_terms st ON st.speech_id = s.id
                  WHERE s.person_id IS NOT NULL GROUP BY s.party_id, s.person_id, st.term_id""")
    like = " OR ".join("speaker_label LIKE ?" for _ in PRESIDING_LABELS)
    db.execute(f"""UPDATE persons SET is_presiding = id IN
                   (SELECT person_id FROM speeches WHERE person_id IS NOT NULL AND ({like}))""", PRESIDING_LABELS)


def apply_party_colors(db: sqlite3.Connection) -> None:
    path = HERE / "party_colors.json"
    if not path.exists():
        return
    parties = json.loads(path.read_text(encoding="utf-8"))["parties"]
    db.executemany("UPDATE parties SET name_en=?, color=? WHERE name=?",
                   [(p.get("name_en"), p.get("color"), name) for name, p in parties.items()])
    missing = [r[0] for r in db.execute("SELECT name FROM parties WHERE color IS NULL")]
    if missing:
        print(f"Parties without a colour (add them to party_colors.json): {', '.join(missing)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=ROOT / "mptracker.db")
    ap.add_argument("--data", type=Path, default=ROOT / "data")
    ap.add_argument("--rebuild", action="store_true", help="re-import all meetings even if unchanged")
    args = ap.parse_args()

    files = sorted(args.data.glob("*/*.json"))
    if not files:
        sys.exit(f"No meeting JSON files under {args.data}")

    db = sqlite3.connect(args.db)
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA journal_mode = WAL")
    db.executescript((HERE / "schema.sql").read_text(encoding="utf-8"))
    migrate(db)

    print(f"Scanning {len(files)} files for speaker names ...", flush=True)
    people = scan_people(files)
    tokenizer = Tokenizer(lexicon_extras(people))
    ctx = {
        "persons": PersonLookup(db, people["display"]), "parties": Lookup(db, "parties"),
        "tags": Lookup(db, "site_tags"), "terms": TermLookup(db, tokenizer.stopwords),
        "markers": MarkerFinder(people["names"] | set(people["display"].values())),
        "tokenizer": tokenizer, "usual_party": people["usual_party"], "provinces": people["provinces"],
    }

    existing = {(r[0], r[1]): r[2] for r in db.execute("SELECT phase, meeting_id, scraped_at FROM meetings")}
    done = skipped = 0
    for f in files:
        m = json.loads(f.read_text(encoding="utf-8"))
        key = (m["phase"], m["meeting_id"])
        if not args.rebuild and existing.get(key) == m["scraped_at"]:
            skipped += 1
            continue
        with db:
            n_speeches, n_words = import_meeting(db, m, f, ctx)
        done += 1
        print(f"  {m['phase']}/{m['meeting_id']} ({m['meeting_date']}): {n_speeches} speeches, {n_words} words", flush=True)

    with db:
        apply_party_colors(db)
        # parties the site leaves blank are filled from the official roster, then from self-introductions;
        # both are re-derived on every run, so first undo the previous run's fills
        db.execute("UPDATE speeches SET party_id = NULL, party_source = 'site' WHERE party_source <> 'site'")
        matched, unmatched = import_members(db, args.data / "members.json")
        if matched or unmatched:
            n_party, n_province = fill_labels_from_roster(db)
            print(f"Roster: {matched} of {matched + len(unmatched)} MPs matched to speakers; party filled for "
                  f"{n_party} people and province for {n_province} speeches the site left blank.")
            if unmatched:
                print(f"  not matched (never spoke, or spelled differently): {', '.join(unmatched)}")
        filled = infer_missing_parties(db)
        if filled:
            print(f"Party filled in from self-introductions for {filled} more people.")
        refresh_aggregates(db)
    db.execute("PRAGMA optimize")
    db.close()
    tokenizer.close()
    print(f"Imported {done} meeting(s), {skipped} unchanged -> {args.db}")


if __name__ == "__main__":
    main()
