# MP Tracker

Browse what Thai MPs say in the House of Representatives (สภาผู้แทนราษฎร): every speech by every
speaker, word clouds of each MP's and each party's themes, and party-level stats.

Transcripts come from the parliament's AI speech-to-text archive at
[asrs.parliament.go.th](https://asrs.parliament.go.th). They are machine transcriptions and contain errors;
speaker labels are partly corrected by this project (see [Data processing](#data-processing)).

**Live site:** https://taepras.github.io/mptracker/

## How it works

```
asrs.parliament.go.th API ──pipeline/scrape_transcripts.py──▶ data/<phase>/<meeting>.json
                                                         │
                                         pipeline/build_db.py ◀──┘   (PyThaiNLP tokenizing, speaker fixes)
                                                         ▼
                                                   mptracker.db  (SQLite)
                                                         │
                     web/app.py (Flask, local) ◀────────┤
                                                         ▼
                                     pipeline/export_site.py ──▶ dist/  (static site → GitHub Pages)
```

| Step | Script | Output |
|---|---|---|
| Scrape | `pipeline/scrape_transcripts.py` | `data/<phase>/<meeting_id>_<date>.json` + readable `.txt` |
| Roster of MPs (occasionally) | `pipeline/scrape_members.py` | `data/members.json` |
| Mine phrases (occasionally) | `pipeline/mine_phrases.py` | `pipeline/lexicon/phrases_mined.txt` |
| Build DB | `pipeline/build_db.py` (schema in `pipeline/schema.sql`, tokenizer in `pipeline/thai_tokenizer.py`) | `mptracker.db` |
| Browse locally | `web/app.py` | http://127.0.0.1:5000 |
| Static export | `pipeline/export_site.py` | `dist/` |
| Publish | push to `main` + `pipeline/publish_db.sh` | GitHub Pages |

`data/`, `mptracker.db` and `dist/` are not in git (they're large and regenerable).

## Repository layout

```
pipeline/            data pipeline: scrape -> database -> static site
  scrape_transcripts.py   scrape_members.py   build_db.py   mine_phrases.py   export_site.py   publish_db.sh
  thai_tokenizer.py   names.py          (shared modules)
  schema.sql   party_colors.json        (database definition and party colours)
  lexicon/                              (multi-word expression lists for the tokenizer)
web/                 Flask app, templates and static assets (also compiled to a static site)
.github/workflows/   GitHub Pages deployment
data/  dist/  mptracker.db     generated, not in git
```

## Setup

Requires Python 3.12+.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

> On minimal Ubuntu/WSL without `ensurepip`: `python3 -m venv --without-pip .venv`, then
> `curl -sSL https://bootstrap.pypa.io/get-pip.py | .venv/bin/python`.

## Usage

```bash
# 1. Scrape transcripts (default: Sep 1 this year → today). Already-saved meetings are skipped.
.venv/bin/python pipeline/scrape_transcripts.py --since 2026-03-01
.venv/bin/python pipeline/scrape_transcripts.py --meeting 2026/108      # a single meeting
.venv/bin/python pipeline/scrape_transcripts.py --force                  # re-download

# 1b. Official roster of MPs (party, constituency) from hris.parliament.go.th — fills gaps the transcripts leave
.venv/bin/python pipeline/scrape_members.py

# 2. Build / update the database (incremental: only new or re-scraped meetings are imported)
.venv/bin/python pipeline/build_db.py
.venv/bin/python pipeline/build_db.py --rebuild                          # re-import everything

# 2b. Occasionally, after lots of new data: re-mine multi-word expressions, then re-import everything
.venv/bin/python pipeline/mine_phrases.py
.venv/bin/python pipeline/build_db.py --rebuild

# 3. Browse locally
.venv/bin/python web/app.py                                     # http://127.0.0.1:5000

# 4. Static site
.venv/bin/python pipeline/export_site.py                                 # dist/ for a domain root
.venv/bin/python pipeline/export_site.py --base /mptracker                # for a sub-path
python3 -m http.server -d dist 8000                             # preview
```

## Publishing (GitHub Pages)

`.github/workflows/pages.yml` rebuilds and deploys the site on every push to `main`. The workflow can't scrape
or build the database itself, so the database is stored as `mptracker.db.gz` on a GitHub Release tagged
`data`.

One-time setup:

1. Install and sign in to the [GitHub CLI](https://cli.github.com): `gh auth login`, then `gh auth setup-git`.
2. Repo **Settings → Pages → Source: GitHub Actions**.
3. `pipeline/publish_db.sh` — uploads the database and triggers a deploy.

After that:

- **Code change:** `git push` → site redeploys (~1 min).
- **New data:** scrape → `pipeline/build_db.py` → `pipeline/publish_db.sh`.

## Database

Main tables (full schema with comments in [`pipeline/schema.sql`](pipeline/schema.sql)):

| Table | What |
|---|---|
| `meetings`, `clips`, `segments` | Raw data as published: meeting → ~5-min video clip → transcript segment |
| `persons`, `person_aliases` | One row per person; every raw spelling of their name is an alias |
| `members` | Official roster of the House (hris.parliament.go.th): party, province, district for every sitting MP, linked to `persons` |
| `parties` | Party names, English names and colours (from `pipeline/party_colors.json`) |
| `speeches`, `speech_segments` | Continuous turns by one speaker, built from segments |
| `terms`, `speech_terms` | Thai-tokenized word counts per speech; `terms.is_stopword` is editable |
| `member_terms` | Word counts per (party, person) — backs the word clouds |
| `topics`, `speech_topics` | Topic labels per speech (manual or model-assigned); survive re-imports |
| `speeches_fts` | Full-text search over tokenized speech text |
| `v_speeches` | Convenience view: speech + date + speaker + party + video link |

Example:

```sql
SELECT meeting_date, party, substr(text, 1, 100)
FROM v_speeches WHERE speaker = 'นายรังสิมันต์ โรม' ORDER BY meeting_date DESC;
```

## Data processing

The raw transcript needs cleaning before it's useful per speaker:

- **Clip overlap** — consecutive ~5-minute clips repeat a few words to a sentence; repeated text is trimmed
  (whitespace-insensitive fuzzy match) when building speeches.
- **Speaker labels** — the site's labels often lag behind who is actually talking. Inline minute markers like
  `นายโสภณ ซารัมย์ (ประธานสภาผู้แทนราษฎร)  :  …` are used to split segments and reassign text
  (`speeches.speaker_source = 'inferred'`).
- **Names** — spellings are merged into one person by stripping titles and ranks, spacing, roll-call
  numbering and doubled names (`pipeline/names.py`). E.g. `รองศาสตราจารย์อนุสรณ์ ธรรมใจ` = `นายอนุสรณ์ ธรรมใจ`.
- **Party and province gaps** — the transcript site gives no party/province for some MPs. These are filled from the
  official roster (`members`), then, for people not on it, from the party they name when introducing themselves
  (`speeches.party_source` = `'roster'` / `'intro'`; `'site'` otherwise). `persons.is_mp` marks roster members.
- **Presiding officers** — people who chaired a sitting are detected from marker roles
  (`persons.is_presiding`) and left out of party word clouds, since their speech is mostly procedure.
- **Word segmentation** — Thai has no spaces, so `pipeline/thai_tokenizer.py` picks, for each run of text, the most
  probable segmentation using real word frequencies (PyThaiNLP's OSCAR corpus, downloaded on first use, ~100 MB).
  PyThaiNLP's default engine prefers the longest dictionary match, and the dictionaries contain junk entries, so
  "แล้วจะ" became แล้ + วจะ. The frequency model splits compounds into their parts (งบประมาณ → งบ + ประมาณ),
  which would make "ประมาณ" absorb every mention of the budget. `pipeline/mine_phrases.py` therefore restores the
  compounds the transcripts support: token sequences that spell a dictionary word, or whose parts nearly always
  occur together, are written to `pipeline/lexicon/phrases_mined.txt` and forced to stay together. Hand edits go in
  `pipeline/lexicon/phrases_manual.txt` (add) and `pipeline/lexicon/phrases_blocked.txt` (veto). Names, provinces and parties are
  kept whole.
- **Word clouds** — MP "distinctive words" weight counts by how few other speakers use the word; party
  "distinctive words" use weighted log-odds against other parties (Monroe, Colaresi & Quinn 2008).

## Known limitations

- Transcripts are AI-generated; names, numbers and technical terms are often mis-transcribed.
- When the chair calls the next speaker without a minute marker, that line stays with the previous speaker.
- Name spellings that differ by a typo (not just title/spacing) are not merged.
- Coverage starts 2026-03-15, the first sitting of the 27th House in the archive.
- The scraper relies on the site's internal API; if the site changes, `pipeline/scrape_transcripts.py` may need updating.
