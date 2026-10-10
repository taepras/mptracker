#!/usr/bin/env python3
"""Local web UI for browsing MPs' and parties' speeches in mptracker.db.

Run:  .venv/bin/python web/app.py   then open http://127.0.0.1:5000
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import sys
from pathlib import Path

from flask import Flask, abort, g, jsonify, render_template, url_for
from markupsafe import Markup, escape

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
from names import TITLES, display_name, strip_titles  # noqa: E402

DB_PATH = ROOT / "mptracker.db"
CLOUD_TERM_RE = re.compile(r"^[ก-๏A-Za-z][ก-๏A-Za-z0-9 .]*$")

app = Flask(__name__)
app.json.ensure_ascii = False  # Thai text as UTF-8, not \uXXXX (halves JSON size)
app.json.compact = True
CLOUD_MODES = ("distinctive", "frequent")


def db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    if (conn := g.pop("db", None)) is not None:
        conn.close()


def load_word_stats() -> dict:
    """Corpus-wide word statistics for the word clouds, computed once at startup (<1 s):
    how many speakers use each term, and each term's House-wide count excluding presiding officers."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    spread = dict(conn.execute("SELECT term_id, COUNT(DISTINCT person_id) FROM member_terms GROUP BY term_id"))
    # baseline for party clouds: everything party members said, minus presiding officers
    party_totals = dict(conn.execute(
        """SELECT mt.term_id, SUM(mt.count) FROM member_terms mt JOIN terms t ON t.id = mt.term_id
           WHERE t.is_stopword = 0 AND mt.party_id IS NOT NULL
             AND mt.person_id NOT IN (SELECT id FROM persons WHERE is_presiding)
           GROUP BY mt.term_id"""))  # stopwords excluded on both sides, like the party's own counts
    n_people = conn.execute("SELECT COUNT(DISTINCT person_id) FROM member_terms").fetchone()[0]
    # words to keep out of party clouds: people's names (self-introductions, calling on members),
    # province names (constituency mentions) and party names
    name_words = {w for (n,) in conn.execute("SELECT name FROM persons") for w in strip_titles(n).split()}
    places = {p for (p,) in conn.execute("SELECT DISTINCT province FROM speeches WHERE province IS NOT NULL")}
    parties = {p for (p,) in conn.execute("SELECT name FROM parties")}
    conn.close()
    return {"spread": spread, "party_totals": party_totals, "party_grand_total": sum(party_totals.values()),
            "n_people": n_people,
            "name_words": name_words, "party_skip": name_words | places | {"พรรค" + p for p in parties}}


# ───────────────────────────────────────────────────────────── template helpers


def text_on(color: str | None) -> str | None:
    """Black or white, whichever contrasts more with a #RRGGBB background (None if the colour is unusable)."""
    if not color or not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
        return None
    r, g_, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g_, b)]
    lum = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    return "#111111" if lum > 0.179 else "#ffffff"


def chip_style(color: str | None) -> str:
    """CSS vars for a party tag: party colour background, black/white text for contrast."""
    fg = text_on(color)
    return f"--pc: {color}; --pf: {fg}" if fg else ""


BUBBLE_TOP = 200


def bubble(label: str, words: int, color: str | None, url: str | None = None, **extra) -> dict:
    return {"label": label, "value": words or 0, "color": color if text_on(color) else None,
            "fg": text_on(color), "url": url, **extra}


@app.template_filter("num")
def fmt_num(n) -> str:
    return f"{n or 0:,}"


@app.template_filter("pct")
def fmt_pct(x) -> str:
    return f"{(x or 0) * 100:.1f}%"


@app.template_filter("mmss")
def fmt_mmss(sec) -> str:
    sec = int(sec or 0)
    return f"{sec // 60}:{sec % 60:02d}"


@app.template_filter("thaidate")
def fmt_thaidate(iso: str) -> str:
    months = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]
    y, m, d = (int(x) for x in iso.split("-"))
    return f"{d} {months[m - 1]} {y + 543}"


def highlight(text: str, q: str | None) -> Markup:
    out = str(escape(text)).replace("\n", "<br>")
    if q:
        out = re.sub(re.escape(str(escape(q))), lambda m: f"<mark>{m.group(0)}</mark>", out)
    return Markup(out)


def _load_logos() -> dict[str, str]:
    """party name → file under static/logos/ (the "logo" field in pipeline/party_colors.json)."""
    try:
        parties = json.loads((ROOT / "pipeline" / "party_colors.json").read_text(encoding="utf-8"))["parties"]
    except (OSError, ValueError, KeyError):
        return {}
    return {name: p["logo"] for name, p in parties.items() if p.get("logo") and (LOGO_DIR / p["logo"]).exists()}


LOGO_DIR = ROOT / "web" / "static" / "logos"
LOGOS = _load_logos()


def party_logo(name: str | None) -> str | None:
    f = LOGOS.get(name or "")
    return url_for("static", filename=f"logos/{f}") if f else None


app.jinja_env.globals.update(chip_style=chip_style, highlight=highlight, TITLES=TITLES, party_logo=party_logo)
app.add_template_filter(display_name, "dname")

# Each person's party/province from their most recent speech that has a party.
LATEST_PARTY_CTE = """
  latest AS (
    SELECT s.person_id, s.party_id, pa.name AS party, pa.color AS party_color, s.province,
           ROW_NUMBER() OVER (PARTITION BY s.person_id ORDER BY m.meeting_date DESC, s.ordinal DESC) AS rn
    FROM speeches s JOIN meetings m ON m.id = s.meeting_id JOIN parties pa ON pa.id = s.party_id)"""


# ───────────────────────────────────────────────────────────── pages


_person_totals: dict[int, tuple[int, int, int]] = {}


def person_ranks(conn: sqlite3.Connection, pid: int) -> dict[str, int]:
    """1-based rank of a person among all speakers by speeches / words / meetings (totals cached per process)."""
    if not _person_totals:
        _person_totals.update({r[0]: tuple(r[1:]) for r in conn.execute(
            """SELECT person_id, COUNT(*), SUM(word_count), COUNT(DISTINCT meeting_id)
               FROM speeches WHERE person_id IS NOT NULL GROUP BY person_id""")})
    mine = _person_totals.get(pid)
    if not mine:
        return {}
    return {k: 1 + sum(1 for t in _person_totals.values() if t[i] > mine[i])
            for i, k in enumerate(("speeches", "words", "meetings"))}


def person_roles(conn: sqlite3.Connection, pid: int) -> list[str]:
    """Roles a person was announced with in minute markers ("นาย… (รองนายกรัฐมนตรี…)  :"), most used first.
    Provinces/party-list, the generic "สมาชิกสภาผู้แทนราษฎร" and stage directions are left out;
    spellings that differ only in spacing are merged."""
    rows = conn.execute(
        """SELECT speaker_label, COUNT(*) AS n FROM speeches
           WHERE person_id = ? AND speaker_label IS NOT NULL
             AND speaker_label NOT IN (SELECT DISTINCT province FROM speeches WHERE province IS NOT NULL)
             AND speaker_label NOT IN ('แบบบัญชีรายชื่อ', 'สมาชิกสภาผู้แทนราษฎร')
             AND speaker_label NOT LIKE '%ได้ยืน%'
           GROUP BY speaker_label ORDER BY n DESC""", (pid,)).fetchall()
    roles: dict[str, str] = {}
    for label, _n in rows:
        roles.setdefault(re.sub(r"\s+", "", label), re.sub(r"\s+", " ", label).strip())
    return list(roles.values())


@app.route("/")
def index():
    rows = db().execute(
        f"""WITH stats AS (
              SELECT person_id, COUNT(*) AS speeches, SUM(word_count) AS words,
                     COUNT(DISTINCT meeting_id) AS meetings
              FROM speeches WHERE person_id IS NOT NULL GROUP BY person_id),
            {LATEST_PARTY_CTE}
            SELECT p.id, p.name, l.party_id, l.party, l.party_color, l.province, st.speeches, st.words, st.meetings
            FROM persons p JOIN stats st ON st.person_id = p.id
            LEFT JOIN latest l ON l.person_id = p.id AND l.rn = 1
            ORDER BY st.words DESC""").fetchall()
    parties = sorted({r["party"] for r in rows if r["party"]})
    meta = db().execute("SELECT MIN(meeting_date), MAX(meeting_date), COUNT(*) FROM meetings").fetchone()
    # Bubbles are grouped by (latest) party: the top people individually, everyone else as one "อื่นๆ" per party.
    def group(r) -> dict:
        return {"group": r["party"] or "ไม่ระบุพรรค", "group_color": r["party_color"] if text_on(r["party_color"]) else None,
                "group_url": url_for("party", party_id=r["party_id"]) if r["party_id"] else None}

    bubbles = [bubble(display_name(r["name"]), r["words"], r["party_color"], url_for("mp", pid=r["id"]), sub=f"{r['words']:,} คำ", **group(r))
               for r in rows[:BUBBLE_TOP]]
    rest: dict = {}
    for r in rows[BUBBLE_TOP:]:
        rest.setdefault(r["party_id"], []).append(r)
    for rs in rest.values():
        words = sum(r["words"] for r in rs)
        bubbles.append(bubble("อื่นๆ", words, None, sub=f"{words:,} คำ", **group(rs[0])))
    return render_template("index.html", people=rows, parties=parties, meta=meta, bubbles=bubbles,
                           bubble_top=min(BUBBLE_TOP, len(rows)))


@app.route("/mp/<int:pid>/")
def mp(pid: int):
    conn = db()
    person = conn.execute("SELECT id, name, is_presiding FROM persons WHERE id=?", (pid,)).fetchone()
    if not person:
        abort(404)
    roles = person_roles(conn, pid)
    stats = conn.execute(
        """SELECT COUNT(*) AS speeches, SUM(s.word_count) AS words, COUNT(DISTINCT s.meeting_id) AS meetings,
                  MIN(m.meeting_date) AS first, MAX(m.meeting_date) AS last
           FROM speeches s JOIN meetings m ON m.id = s.meeting_id WHERE s.person_id=?""", (pid,)).fetchone()
    parties = conn.execute(
        """SELECT pa.id, pa.name, pa.color, COUNT(*) AS n, MAX(m.meeting_date) AS last FROM speeches s
           JOIN parties pa ON pa.id = s.party_id JOIN meetings m ON m.id = s.meeting_id
           WHERE s.person_id=? GROUP BY pa.id ORDER BY last DESC""", (pid,)).fetchall()
    province = conn.execute(
        """SELECT province FROM speeches WHERE person_id=? AND province IS NOT NULL
           GROUP BY province ORDER BY COUNT(*) DESC LIMIT 1""", (pid,)).fetchone()

    total_meetings = conn.execute(
        "SELECT COUNT(DISTINCT meeting_id) FROM speeches WHERE person_id IS NOT NULL").fetchone()[0]
    return render_template("mp.html", person=person, roles=roles, stats=stats, parties=parties,
                           total_meetings=total_meetings,
                           ranks=person_ranks(conn, pid),
                           province=province[0] if province else None)


@app.route("/api/mp/<int:pid>/speeches.json")
def mp_speeches(pid: int):
    """All of a person's speeches, newest first — searched and paged in the browser (static/speeches.js)."""
    rows = db().execute(
        """SELECT m.meeting_date AS date, m.title, s.start_clock AS clock, s.word_count AS words,
                  s.first_clip_seq AS clip, s.first_start_sec AS start, m.phase, m.meeting_id AS meeting,
                  s.speaker_source AS source, s.speaker_label AS label, s.text
           FROM speeches s JOIN meetings m ON m.id = s.meeting_id
           WHERE s.person_id = ? ORDER BY m.meeting_date DESC, s.ordinal""", (pid,)).fetchall()
    if not rows and not db().execute("SELECT 1 FROM persons WHERE id=?", (pid,)).fetchone():
        abort(404)
    return jsonify([{k: r[k] for k in r.keys() if r[k] is not None} for r in rows])


@app.route("/parties/")
def parties():
    conn = db()
    total_words = conn.execute("SELECT SUM(word_count) FROM speeches WHERE person_id IS NOT NULL").fetchone()[0]
    rows = conn.execute(
        """SELECT pa.id, pa.name, pa.color, COUNT(DISTINCT s.person_id) AS members, COUNT(*) AS speeches,
                  SUM(s.word_count) AS words, COUNT(DISTINCT s.meeting_id) AS meetings
           FROM parties pa JOIN speeches s ON s.party_id = pa.id
           GROUP BY pa.id ORDER BY words DESC""").fetchall()
    bubbles = [bubble(r["name"], r["words"], r["color"], url_for("party", party_id=r["id"]),
                      sub=f"{r['words']:,} คำ") for r in rows]
    return render_template("parties.html", parties=rows, total_words=total_words, bubbles=bubbles)


def party_ranks(conn: sqlite3.Connection, party_id: int) -> dict[str, dict[str, int]]:
    """Where a party stands among all parties on each stat shown on its page (same definitions as the page):
    {stat: {"rank": 1-based, parties with the same value share it, "of": number of parties,
            "tied": parties with this very value, itself included}}."""
    rows = conn.execute(
        """SELECT party_id, COUNT(*) AS speeches, SUM(word_count) AS words,
                  COUNT(DISTINCT meeting_id) AS meetings, COUNT(DISTINCT person_id) AS members
           FROM speeches WHERE party_id IS NOT NULL GROUP BY party_id""").fetchall()
    mine = next((r for r in rows if r["party_id"] == party_id), None)
    if mine is None:
        return {}
    stats = {"speeches": lambda r: r["speeches"], "words": lambda r: r["words"], "meetings": lambda r: r["meetings"],
             "words_per_member": lambda r: round(r["words"] / r["members"])}  # rounded as displayed, so ties match
    ranks = {}
    for name, value in stats.items():
        values, own = [value(r) for r in rows], value(mine)
        ranks[name] = {"rank": 1 + sum(v > own for v in values), "of": len(values), "tied": sum(v == own for v in values)}
    return ranks


@app.route("/party/<int:party_id>/")
def party(party_id: int):
    conn = db()
    p = conn.execute("SELECT id, name, color FROM parties WHERE id=?", (party_id,)).fetchone()
    if not p:
        abort(404)
    stats = conn.execute(
        """SELECT COUNT(DISTINCT s.person_id) AS members, COUNT(*) AS speeches, SUM(s.word_count) AS words,
                  COUNT(DISTINCT s.meeting_id) AS meetings, MIN(m.meeting_date) AS first, MAX(m.meeting_date) AS last
           FROM speeches s JOIN meetings m ON m.id = s.meeting_id WHERE s.party_id=?""", (party_id,)).fetchone()
    house = conn.execute(
        "SELECT COUNT(*) AS speeches, SUM(word_count) AS words, COUNT(DISTINCT meeting_id) AS meetings "
        "FROM speeches WHERE person_id IS NOT NULL").fetchone()
    members = conn.execute(
        f"""WITH {LATEST_PARTY_CTE.strip()},
            here AS (
              SELECT s.person_id, COUNT(*) AS speeches, SUM(s.word_count) AS words,
                     COUNT(DISTINCT s.meeting_id) AS meetings,
                     (SELECT province FROM speeches x WHERE x.person_id = s.person_id AND x.party_id = s.party_id
                        AND province IS NOT NULL GROUP BY province ORDER BY COUNT(*) DESC LIMIT 1) AS province
              FROM speeches s WHERE s.party_id = ? GROUP BY s.person_id)
            SELECT p.id, p.name, p.is_presiding, h.province, h.speeches, h.words, h.meetings,
                   CASE WHEN l.party_id <> ? THEN l.party END AS now_party,
                   CASE WHEN l.party_id <> ? THEN l.party_id END AS now_party_id,
                   CASE WHEN l.party_id <> ? THEN l.party_color END AS now_party_color
            FROM here h JOIN persons p ON p.id = h.person_id
            LEFT JOIN latest l ON l.person_id = h.person_id AND l.rn = 1
            ORDER BY h.words DESC""", (party_id, party_id, party_id, party_id)).fetchall()
    # one bubble per speaker, area = words spoken for this party, in the party's colour
    bubbles = [bubble(m["name"], m["words"], p["color"], url_for("mp", pid=m["id"]), sub=f"{m['words']:,} คำ")
               for m in members]
    return render_template("party.html", party=p, stats=stats, house=house, ranks=party_ranks(conn, party_id),
                           members=members, bubbles=bubbles)


# ───────────────────────────────────────────────────────────── word-cloud APIs


def _cloud_rows(where: str, arg: int) -> list[sqlite3.Row]:
    return db().execute(
        f"""SELECT t.id, t.term, SUM(mt.count) AS n FROM member_terms mt JOIN terms t ON t.id = mt.term_id
            WHERE {where} AND t.is_stopword = 0 GROUP BY t.id""", (arg,)).fetchall()


@app.route("/api/mp/<int:pid>/cloud-<mode>.json")
def mp_cloud(pid: int, mode: str):
    """[[word, weight, count], …] — mode=distinctive (default): count × log(N speakers / speakers using it);
    mode=frequent: raw counts."""
    if mode not in CLOUD_MODES:
        abort(404)
    person = db().execute("SELECT name FROM persons WHERE id=?", (pid,)).fetchone()
    if not person:
        abort(404)
    own_name = set(strip_titles(person["name"]).split())
    rows = _cloud_rows("mt.person_id = ?", pid)
    ws = app.config["WORD_STATS"]
    min_count = 2 if len(rows) > 300 else 1
    scored = []
    for r in rows:
        term = r["term"]
        if r["n"] < min_count or term in own_name or not CLOUD_TERM_RE.match(term):
            continue
        weight = r["n"] * math.log(ws["n_people"] / ws["spread"].get(r["id"], 1)) if mode == "distinctive" else r["n"]
        if weight > 0:
            scored.append((term, round(weight, 2), r["n"]))
    scored.sort(key=lambda x: -x[1])
    return jsonify([list(x) for x in scored[:120]])


@app.route("/api/party/<int:party_id>/cloud-<mode>.json")
def party_cloud(party_id: int, mode: str):
    """mode=distinctive (default): words that set the party apart from other parties' MPs, scored by
    weighted log-odds with an informative Dirichlet prior (Monroe, Colaresi & Quinn 2008) — a z-score that
    balances how over-represented a word is against how much evidence there is; mode=frequent: raw counts.
    Presiding officers' speech (mostly procedure) is left out, as are names of people, provinces and parties."""
    if mode not in CLOUD_MODES:
        abort(404)
    if not db().execute("SELECT 1 FROM parties WHERE id=?", (party_id,)).fetchone():
        abort(404)
    rows = _cloud_rows("mt.party_id = ? AND mt.person_id NOT IN (SELECT id FROM persons WHERE is_presiding)",
                       party_id)
    ws = app.config["WORD_STATS"]
    totals, grand = ws["party_totals"], ws["party_grand_total"]
    n_party = sum(r["n"] for r in rows) or 1
    n_rest = max(grand - n_party, 1)
    a0 = 5000  # prior strength (pseudo-words)
    min_count = 5 if n_party > 50_000 else 2
    scored = []
    for r in rows:
        term, y_i = r["term"], r["n"]
        if y_i < min_count or term in ws["party_skip"] or not CLOUD_TERM_RE.match(term):
            continue
        if mode == "distinctive":
            total = totals.get(r["id"], y_i)
            y_j, a_w = total - y_i, a0 * total / grand
            delta = (math.log((y_i + a_w) / (n_party + a0 - y_i - a_w))
                     - math.log((y_j + a_w) / (n_rest + a0 - y_j - a_w)))
            weight = delta / math.sqrt(1 / (y_i + a_w) + 1 / (y_j + a_w))
        else:
            weight = y_i
        if weight > 0:
            scored.append((term, round(weight, 2), y_i))
    scored.sort(key=lambda x: -x[1])
    return jsonify([list(x) for x in scored[:120]])


RELOAD = "--no-reload" not in sys.argv  # hot reload: restarts on .py edits, templates re-read per request


def main() -> None:
    if not DB_PATH.exists():
        sys.exit(f"{DB_PATH} not found — run build_db.py first.")
    # The reloader re-runs this script in a child process; only the child serves, so only it loads the stats.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not RELOAD:
        print("Loading word statistics ...", flush=True)
        app.config["WORD_STATS"] = load_word_stats()
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=RELOAD)


if __name__ == "__main__":
    main()
