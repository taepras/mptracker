# CLAUDE.md

Thai parliament transcript tracker: scrape → SQLite → Flask UI → static export → GitHub Pages.
See README.md for the user-facing overview.

## Commands

Always use the project venv (`.venv/bin/python`); the system Python has no pip and lacks pythainlp/flask.

```bash
.venv/bin/python pipeline/scrape_transcripts.py --since YYYY-MM-DD   # → data/<phase>/*.json (skips saved meetings)
.venv/bin/python pipeline/scrape_members.py                                # official roster of MPs → data/members.json (party, province)
.venv/bin/python pipeline/build_db.py                                # incremental import → mptracker.db (~4 min full)
.venv/bin/python pipeline/mine_phrases.py                            # re-mine pipeline/lexicon/phrases_mined.txt (~3 min),
                                                                     #   then build_db.py --rebuild
.venv/bin/python web/app.py                                          # dev server, http://127.0.0.1:5000
.venv/bin/python pipeline/export_site.py [--base /mptracker]         # → dist/ (~10 s)
pipeline/publish_db.sh                                               # upload DB to "data" release + trigger Pages deploy
```

There is no test suite. Verify changes by running the pipeline step and checking output, e.g. Flask's test
client (`app.test_client()` after setting `app.config["WORD_STATS"] = load_word_stats()`), `node --check` for JS,
and a link check over `dist/`.

## Layout

- `pipeline/scrape_transcripts.py` — client for the site's private API. Requests/responses are AES-CBC encrypted and
  carry an HS256 JWT; the JWT secret is decrypted from the live JS bundle at runtime (don't hardcode it).
  Endpoints: `/opensearch/searchplaylist` (clips per meeting), `/searchbyid` (segments per clip),
  `/advancesearch` (seed meeting IDs by date; capped at 100 results, so IDs are then walked ±).
- `pipeline/scrape_members.py` — official roster of House members from hris.parliament.go.th → `data/members.json`.
  The server omits its intermediate CA, so the script completes the chain from the certificate's AIA address and
  still verifies everything (never `verify=False`).
- `pipeline/build_db.py` — import + derivation. Key pieces: `overlap_cut` (clip-boundary dedup), `MarkerFinder`
  (inline "name (role)  :" markers → speaker reassignment), `PersonLookup` (name-key merging + aliases),
  `Tokenizer` (PyThaiNLP newmm with custom dict), `refresh_aggregates` (`member_terms`, `is_presiding`),
  `apply_party_colors`, `migrate`.
- `pipeline/thai_tokenizer.py` — Thai word segmentation: Viterbi over a vocabulary of dictionaries + Wikipedia titles + names,
  costs from OSCAR word frequencies (OSCAR's own token list is mostly ICU fragments/noise — used for frequencies
  only), plus forced phrases from `pipeline/lexicon/`. `tokens_many()` fans out to a fork pool. Replaces PyThaiNLP newmm.
- `pipeline/mine_phrases.py` — finds compounds to restore after segmentation (dictionary-backed / wikipedia-backed /
  corpus-only tiers, function-word edges rejected). Writes `pipeline/lexicon/phrases_mined.txt` (generated, don't edit);
  `phrases_manual.txt` and `phrases_blocked.txt` are hand-edited and survive re-mining.
- `pipeline/names.py` — Thai name normalisation (`person_key`, `clean_name`, `strip_titles`, `TITLES`). Shared by
  build_db and the web app; the title list is also passed to the browser as `window.TITLES`.
- `pipeline/schema.sql` — source of truth for tables; applied with `CREATE … IF NOT EXISTS` on every build.
- `web/app.py` — Flask app. All routes are file-shaped so the static export can crawl them:
  pages end in `/`, data is `/api/.../*.json` (e.g. `cloud-distinctive.json`, `speeches.json`).
- `web/static/` — `table-sort.js` (multi-key sortable tables), `cloud.js` (wordcloud2.js wrapper; URL template
  with `MODE`), `speeches.js` (client-side search/filter/paging of an MP's speeches).
- `pipeline/export_site.py` — crawls every URL via Flask's test client into `dist/`; `--base` sets the URL prefix.
- `.github/workflows/pages.yml` — on push: download `mptracker.db.gz` from release `data`, export, deploy.

## Conventions and gotchas

- **Static-export compatibility:** anything the UI needs must be reachable as a GET URL without query
  parameters. Do filtering/paging client-side, not via `request.args`. Use `url_for` everywhere (it carries
  the `--base` prefix). New routes must also be added to `urls()` in `pipeline/export_site.py`.
- **Schema changes:** add new columns in `migrate()` in `pipeline/build_db.py` (ALTER TABLE) as well as `pipeline/schema.sql`, so
  existing DBs keep working. A change to how persons are keyed requires deleting `mptracker.db` and rebuilding.
- **Preserved data:** `topics`, `speech_topics` (keyed by stable `speech_key`, not `speeches.id`) and
  `terms.is_stopword` must survive re-imports — never wipe them in import code.
- **Tokenization changes** (anything in `pipeline/thai_tokenizer.py`, `pipeline/lexicon/`, `PARLIAMENT_STOPWORDS`) need
  `pipeline/build_db.py --rebuild`; if no topic labels exist yet, delete `mptracker.db` instead so stale `terms` rows go.
  After changing the phrase list, check the top non-stopword terms for formulaic tokens (e.g. เพื่อนสมาชิก,
  ไม่มี) and add them to `PARLIAMENT_STOPWORDS`.
- **Tokenizer gotchas:** character ranges like `[ั-ฺ]` include the vowels า ำ ี ุ — a filter built on them
  silently deletes every consonant+vowel word (ปี, ทำ, ดี, มา). Never use PyThaiNLP's stopword list as a
  "function word" test (it contains ประมาณ, ผล, เปิด …); use `FUNCTION_WORDS` in `pipeline/mine_phrases.py`.
- **Word counts:** party/MP clouds read `member_terms`, not `speech_terms` (joins through `speech_terms` take
  tens of seconds). Keep stopword filtering consistent between numerator and baseline when scoring.
- **Party/province gaps:** the transcript site leaves them blank for some MPs. `build_db.py` resets and re-derives
  the fills every run: roster (`members`) first, then self-introductions; `speeches.party_source` records which.
  Roster party is only used for people with no site-given party (an MP who switched party keeps the historical one).
- **Presiding officers** (`persons.is_presiding`) are excluded from party clouds; their speech is procedure.
- **Thai text:** JSON responses use `ensure_ascii=False`. Sort Thai with `Intl.Collator('th')` in JS.
- **Party colours** live in `pipeline/party_colors.json` (sourced from Wikipedia's party-colour module); build_db
  reports parties missing a colour.
- **Dev server:** hot-reloads (Python edits restart it, which reloads word stats; templates re-read per request; refresh the browser for CSS/JS). `--no-reload` disables. When stopping it,
  use `kill $(pgrep -f '^\.venv/bin/python web/app\.py')`; a bare `pkill -f web/app.py` also kills the shell
  running it.
- **Scraping etiquette:** keep the default delay and worker count; the site has rate-limited/stalled
  connections before. Don't re-scrape everything with `--force` without reason.
- Comments, docstrings and UI copy: code comments in English; user-facing UI text in Thai.
