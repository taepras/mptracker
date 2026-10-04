-- SQLite schema for parliament transcripts scraped by scrape_transcripts.py.
-- Built/updated by build_db.py. Tables marked [preserved] are never wiped by a re-import.

PRAGMA foreign_keys = ON;

-- ───────────── Core structure: meeting → clip → segment (as published by the site) ─────────────
CREATE TABLE IF NOT EXISTS meetings (
  id              INTEGER PRIMARY KEY,
  phase           INTEGER NOT NULL,          -- 2026
  meeting_id      INTEGER NOT NULL,          -- site's id, e.g. 108
  meeting_date    TEXT    NOT NULL,          -- 'YYYY-MM-DD'
  title           TEXT,                      -- การประชุมสภาผู้แทนราษฎร เป็นพิเศษ
  council         TEXT,                      -- สภาผู้แทนราษฎร
  meeting_type    TEXT,
  episode         TEXT,                      -- สมัยสามัญประจำปีครั้งที่สอง
  house_number    INTEGER,                   -- ชุดที่ (meeting_group)
  house_year      INTEGER,                   -- ปีที่ (meeting_year)
  session_number  INTEGER,                   -- ครั้งที่ (meeting_number)
  scraped_at      TEXT,
  source_file     TEXT,
  UNIQUE (phase, meeting_id)
);

CREATE TABLE IF NOT EXISTS clips (
  id            INTEGER PRIMARY KEY,
  meeting_id    INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  seq           INTEGER NOT NULL,
  start_time    TEXT,                        -- wall clock 'HH:MM:SS'
  end_time      TEXT,
  video_path    TEXT,
  url           TEXT,
  UNIQUE (meeting_id, seq)
);

CREATE TABLE IF NOT EXISTS parties (
  id       INTEGER PRIMARY KEY,
  name     TEXT NOT NULL UNIQUE,
  name_en  TEXT,
  color    TEXT                              -- '#RRGGBB', from party_colors.json
);

CREATE TABLE IF NOT EXISTS persons (
  id           INTEGER PRIMARY KEY,
  name         TEXT NOT NULL,                -- display name: the most common spelling, title included
  name_key     TEXT NOT NULL UNIQUE,         -- title-less, space-less key used to merge spellings (names.py)
  is_presiding INTEGER NOT NULL DEFAULT 0    -- has chaired a sitting (Speaker / deputy / President of Parliament);
                                             -- derived from minute markers, refreshed by build_db.py
);

-- Every raw spelling seen for a person ("นายอนุสรณ์ ธรรมใจ", "๑๙๓. นาง…", doubled names, …)
CREATE TABLE IF NOT EXISTS person_aliases (
  alias      TEXT PRIMARY KEY,
  person_id  INTEGER NOT NULL REFERENCES persons(id)
);

-- Raw transcript segments, exactly as the site labels them.
CREATE TABLE IF NOT EXISTS segments (
  id              INTEGER PRIMARY KEY,
  site_id         TEXT NOT NULL UNIQUE,      -- '2026-108-0002-00020011-004'
  clip_id         INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
  start_sec       REAL,                      -- offset within the clip's video
  stop_sec        REAL,
  person_id       INTEGER REFERENCES persons(id),   -- site's label; NULL = unlabelled
  party_id        INTEGER REFERENCES parties(id),
  province        TEXT,
  text            TEXT NOT NULL,             -- cleaned, but NOT de-duplicated
  text_raw        TEXT                       -- original HTML
);
CREATE INDEX IF NOT EXISTS idx_segments_clip ON segments(clip_id, start_sec);

-- Site-assigned tags per clip
CREATE TABLE IF NOT EXISTS site_tags (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS clip_site_tags (
  clip_id  INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
  tag_id   INTEGER NOT NULL REFERENCES site_tags(id),
  PRIMARY KEY (clip_id, tag_id)
);

-- ───────────── Speeches: continuous turns by one speaker (derived) ─────────────
-- Built by merging consecutive segment text with the same speaker, after
--   * trimming text repeated at clip/segment boundaries, and
--   * splitting segments at inline minute markers ("นายก ข (จังหวัด)  :  ...")
--     where the site's speaker label lags behind the actual speaker.
CREATE TABLE IF NOT EXISTS speeches (
  id              INTEGER PRIMARY KEY,
  speech_key      TEXT NOT NULL UNIQUE,      -- stable across re-imports: '<first segment site_id>#<part>'
  meeting_id      INTEGER NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
  ordinal         INTEGER NOT NULL,          -- order within the meeting
  person_id       INTEGER REFERENCES persons(id),
  party_id        INTEGER REFERENCES parties(id),   -- party at the time
  province        TEXT,
  speaker_source  TEXT NOT NULL,             -- 'site' | 'inferred' (from an inline marker) | 'none'
  speaker_label   TEXT,                      -- role/constituency from marker, e.g. 'ประธานสภาผู้แทนราษฎร'
  first_clip_seq  INTEGER,
  first_start_sec REAL,                      -- seek offset in the first clip's video
  last_clip_seq   INTEGER,
  last_stop_sec   REAL,
  start_clock     TEXT,                      -- approx. wall-clock start 'HH:MM:SS'
  segment_count   INTEGER,
  char_count      INTEGER,
  word_count      INTEGER,
  text            TEXT NOT NULL,
  UNIQUE (meeting_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_speeches_person ON speeches(person_id);
CREATE INDEX IF NOT EXISTS idx_speeches_party  ON speeches(party_id);

-- Which (part of a) segment makes up which speech
CREATE TABLE IF NOT EXISTS speech_segments (
  speech_id   INTEGER NOT NULL REFERENCES speeches(id) ON DELETE CASCADE,
  segment_id  INTEGER NOT NULL REFERENCES segments(id) ON DELETE CASCADE,
  part        INTEGER NOT NULL,              -- 0 = text before first marker in the segment, 1 = after, …
  text        TEXT NOT NULL,                 -- de-duplicated text contributed to the speech
  PRIMARY KEY (segment_id, part)
);
CREATE INDEX IF NOT EXISTS idx_speech_segments_speech ON speech_segments(speech_id);

-- ───────────── Topics (per speech) ─────────────
CREATE TABLE IF NOT EXISTS topics (            -- [preserved]
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  parent_id   INTEGER REFERENCES topics(id),
  description TEXT
);

-- Keyed by speech_key (not speeches.id) so labels survive re-importing a meeting.
CREATE TABLE IF NOT EXISTS speech_topics (     -- [preserved]
  speech_key  TEXT NOT NULL,
  topic_id    INTEGER NOT NULL REFERENCES topics(id),
  source      TEXT NOT NULL,                 -- 'manual', 'llm:<model>', 'rules-v1', …
  confidence  REAL,
  created_at  TEXT DEFAULT (datetime('now')),
  PRIMARY KEY (speech_key, topic_id, source)
);
CREATE INDEX IF NOT EXISTS idx_speech_topics_topic ON speech_topics(topic_id);

-- ───────────── Words (word clouds, frequency analysis) ─────────────
CREATE TABLE IF NOT EXISTS terms (             -- [preserved]
  id          INTEGER PRIMARY KEY,
  term        TEXT NOT NULL UNIQUE,
  is_stopword INTEGER NOT NULL DEFAULT 0     -- edit freely; no re-import needed
);

CREATE TABLE IF NOT EXISTS speech_terms (
  speech_id  INTEGER NOT NULL REFERENCES speeches(id) ON DELETE CASCADE,
  term_id    INTEGER NOT NULL REFERENCES terms(id),
  count      INTEGER NOT NULL,
  PRIMARY KEY (speech_id, term_id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_speech_terms_term ON speech_terms(term_id);

-- Word counts per (party at the time, person, term) — rebuilt from speech_terms on every
-- build_db.py run (~1 s). Backs the MP and party word clouds.
CREATE TABLE IF NOT EXISTS member_terms (
  party_id   INTEGER,
  person_id  INTEGER NOT NULL,
  term_id    INTEGER NOT NULL,
  count      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_member_terms_person ON member_terms(person_id);
CREATE INDEX IF NOT EXISTS idx_member_terms_party  ON member_terms(party_id);

-- Full-text search over Thai-tokenized speech text (rowid = speeches.id).
-- Query with space-separated tokens, e.g. MATCH '"ค่า ไฟ"' (tokenize your query the same way).
CREATE VIRTUAL TABLE IF NOT EXISTS speeches_fts USING fts5(
  tokens, content='', contentless_delete=1, tokenize='unicode61'
);

-- ───────────── Convenience views ─────────────
CREATE VIEW IF NOT EXISTS v_speeches AS
SELECT s.id, s.speech_key, m.meeting_date, m.phase, m.meeting_id AS site_meeting_id, s.ordinal,
       p.name AS speaker, pa.name AS party, s.province, s.speaker_source, s.speaker_label,
       s.start_clock, s.word_count, s.text,
       'https://asrs.parliament.go.th/video/' || m.phase || '/' || m.meeting_id || '/' || s.first_clip_seq AS video_url,
       s.first_start_sec
FROM speeches s
JOIN meetings m ON m.id = s.meeting_id
LEFT JOIN persons p ON p.id = s.person_id
LEFT JOIN parties pa ON pa.id = s.party_id;

CREATE VIEW IF NOT EXISTS v_speech_topics AS
SELECT s.id AS speech_id, st.speech_key, t.name AS topic, st.source, st.confidence
FROM speech_topics st
JOIN topics t ON t.id = st.topic_id
LEFT JOIN speeches s ON s.speech_key = st.speech_key;
