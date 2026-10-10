#!/usr/bin/env python3
"""Find multi-word expressions in the transcripts and write them to pipeline/lexicon/phrases_mined.txt.

The base segmenter (thai_tokenizer.py) splits compounds into their parts: งบประมาณ -> งบ + ประมาณ,
ตรวจสอบ -> ตรวจ + สอบ. Splitting is wrong for counting, because "ประมาณ" would then absorb every mention of the
budget. This script restores the compounds the transcripts themselves support. Token sequences are accepted when:

  dictionary  they spell a word in one of the dictionaries (seen >= 3 times, parts not almost always elsewhere);
  wikipedia   they spell a Wikipedia title (seen >= 8 times, parts mostly inside it);
  corpus      they are in no dictionary (แลนด์บริดจ์, เสียบบัตร …) but their parts nearly always occur together
              (seen >= 10 times, every part >= 30% inside it).

Sequences starting or ending with a function word (ใน, ที่, จะ, และ …) are rejected: they are accidental
("ร่วมใน", "ประกาศว่า"). A person's full name is never merged. The pass repeats until nothing new appears, so
longer phrases can build on shorter ones.

Edit pipeline/lexicon/phrases_manual.txt to force a phrase and phrases_blocked.txt to veto one; both survive
re-mining. Re-run pipeline/build_db.py --rebuild afterwards so the database uses the new list.

  .venv/bin/python pipeline/mine_phrases.py              # mine and write pipeline/lexicon/phrases_mined.txt
  .venv/bin/python pipeline/mine_phrases.py --dry-run    # only print what would be written
"""

from __future__ import annotations

import argparse
import collections
import re
import sqlite3
import sys
import time
from pathlib import Path

from pythainlp.corpus import thai_wikipedia_titles

import build_db
from names import TITLES
from thai_tokenizer import PHRASES_MINED, ThaiTokenizer, load_phrases, trusted_compounds, _read_list, PHRASES_BLOCKED

ROOT = Path(__file__).resolve().parent.parent  # project root: data/, mptracker.db
THAI_ONLY = re.compile(r"^[ก-๏]+$")
MAX_PARTS = 5
MAX_LEN = 25
TIERS = {  # name: (min count, min coverage)
    "dictionary": (3, 0.02),
    "wikipedia": (8, 0.10),
    "corpus": (10, 0.30),
}
PREFIX_COVERAGE = 0.4
PRUNE_RATIO = 0.97  # a phrase is a fragment if a longer phrase containing it was seen almost as often (การท่อง ⊂ การท่องเที่ยว)
# Prefix morphemes that may begin a phrase (การเมือง, ความคิด, ผู้แทน) but never end one.
PREFIXES = {"การ", "ความ", "ผู้"}
TITLE_TOKENS = set(TITLES) | {"ดร"}


# Grammatical words only (particles, prepositions, conjunctions, pronouns, auxiliaries). A phrase that starts or ends
# with one is accidental ("ร่วมใน", "ประกาศว่า", "เลยไป"). PyThaiNLP's stopword list is too loose for this test:
# it includes content words such as ประมาณ, ผล, เปิด.
FUNCTION_WORDS = set("""
ที่ ซึ่ง อัน ใน ของ และ หรือ แต่ จะ ได้ ไม่ มี เป็น ให้ ก็ ว่า แล้ว ไป มา กับ นี้ นั้น นี่ นั่น โดย ตาม จาก แก่ ถึง
เพื่อ ด้วย ยัง อีก เมื่อ ถ้า หาก แม้ จน ต่อ แห่ง ณ จึง นะ ครับ ค่ะ คะ น่ะ ผม ดิฉัน เรา ท่าน เขา มัน คุณ ฉัน เธอ ใคร
อะไร ไหน ทำไม อย่างไร เท่า ทั้ง ทุก บาง หลาย ต้อง ควร อาจ คง กำลัง เคย ย่อม เอง เลย อยู่ กัน คือ เพราะ ถูก มาก
ทำให้ ทำการ
""".split())


def prune_fragments(mined: dict[str, tuple[str, int, float]]) -> dict[str, tuple[str, int, float]]:
    """Drop phrases that are almost always part of a longer phrase (การท่อง inside การท่องเที่ยว): the shorter one
    is a fragment found in an earlier pass, before the longer one was merged."""
    kept: dict[str, tuple[str, int, float]] = {}
    for word in sorted(mined, key=len, reverse=True):
        n = mined[word][1]
        if not any(len(q) > len(word) and word in q and mined[q][1] >= PRUNE_RATIO * n for q in kept):
            kept[word] = mined[word]
    return kept


def mine_pass(tokens: list[list[str]], trusted: set[str], wiki: set[str], names: set[str],
              blocked: set[str], known: set[str], places: set[str]) -> dict[str, tuple[str, int, float]]:
    real = trusted | wiki | known  # strings that count as real words for the corpus-only tier
    unigrams = collections.Counter(t for s in tokens for t in s)
    func = FUNCTION_WORDS
    dict_ngrams, wiki_ngrams = collections.Counter(), collections.Counter()
    bigrams = collections.Counter()
    for s in tokens:
        n = len(s)
        for i in range(n - 1):
            bigrams[(s[i], s[i + 1])] += 1
            w = s[i]
            for k in range(1, min(MAX_PARTS, n - i)):
                w += s[i + k]
                if len(w) > MAX_LEN:
                    break
                if w in trusted:
                    dict_ngrams[tuple(s[i:i + k + 1])] += 1
                elif w in wiki:
                    wiki_ngrams[tuple(s[i:i + k + 1])] += 1

    def ok_edges(g: tuple[str, ...]) -> bool:
        return g[0] not in func and g[-1] not in func and g[-1] not in PREFIXES

    def is_name(g: tuple[str, ...]) -> bool:
        parts = [p for p in g if p not in TITLE_TOKENS]
        return len(parts) >= 1 and all(p in names for p in parts) and len(g) <= 3 and any(p in names for p in g)

    def coverage(g: tuple[str, ...], c: int) -> float:
        parts = g[1:] if g[0] in PREFIXES else g  # a prefix is free to occur everywhere
        return min(c / unigrams[p] for p in parts)

    def accepts(tier: str, g: tuple[str, ...], c: int) -> bool:
        min_count, min_cov = TIERS[tier]
        word = "".join(g)
        if (c < min_count or word in blocked or word in known or word in found or not THAI_ONLY.match(word)
                or not ok_edges(g) or is_name(g)):
            return False
        if any(p in places for p in g):  # keep provinces separate from their label: เชียงใหม่, not จังหวัดเชียงใหม่
            return False
        if tier == "corpus":
            # parts must be real words, never prefixes/titles/people's names (that is where fragments come from)
            if any(p in PREFIXES or p in TITLE_TOKENS or p in names or p not in real for p in g):
                return False
        elif g[0] in PREFIXES and len(g) > 4:
            return False
        # a prefix (การ, ความ, ผู้) joins its word only when that word hardly ever appears without it
        # (การเมือง yes; การคอร์รัปชัน no — otherwise one concept would be counted under two tokens)
        return coverage(g, c) >= (PREFIX_COVERAGE if g[0] in PREFIXES else min_cov)

    found: dict[str, tuple[str, int, float]] = {}
    inside: dict[tuple[str, ...], int] = {}  # sub-sequences of accepted dictionary/wikipedia phrases -> their count
    for tier, ngrams in (("dictionary", dict_ngrams), ("wikipedia", wiki_ngrams)):
        for g, c in ngrams.items():
            if accepts(tier, g, c):
                found["".join(g)] = (tier, c, coverage(g, c))
                for size in range(2, len(g)):
                    for i in range(len(g) - size + 1):
                        sub = g[i:i + size]
                        inside[sub] = max(inside.get(sub, 0), c)
    for g, c in bigrams.items():
        word = "".join(g)
        if word in trusted or word in wiki or inside.get(g, 0) >= 0.5 * c:
            continue  # already a dictionary word, or just a piece of a bigger accepted phrase
        if accepts("corpus", g, c):
            found[word] = ("corpus", c, coverage(g, c))
    return found


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=ROOT / "mptracker.db")
    ap.add_argument("--passes", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = sqlite3.connect(args.db)
    texts = [r[0] for r in db.execute("SELECT text FROM speeches WHERE person_id IS NOT NULL")]
    print(f"{len(texts):,} speeches", flush=True)
    people = build_db.scan_people(sorted((ROOT / "data").glob("*/*.json")))
    extras = build_db.lexicon_extras(people)
    names = {w for n in people["display"].values() for w in build_db.strip_titles(n).split() if len(w) >= 2}
    trusted, wiki = trusted_compounds(), set(thai_wikipedia_titles()) - trusted_compounds()
    blocked = set(_read_list(PHRASES_BLOCKED))
    manual = set(load_phrases(mined=False))

    mined: dict[str, tuple[str, int, float]] = {}
    for p in range(1, args.passes + 1):
        t = time.time()
        tok = ThaiTokenizer(extras, phrases=list(manual | mined.keys()))
        tokens = tok.tokens_many(texts)
        tok.close()
        new = mine_pass(tokens, trusted, wiki, names, blocked, manual | mined.keys(), people["provinces"])
        print(f"pass {p}: {len(new):,} new phrases ({time.time() - t:.0f}s)", flush=True)
        if not new:
            break
        mined.update(new)

    before = len(mined)
    mined = prune_fragments(mined)
    print(f"pruned {before - len(mined)} fragments of longer phrases")
    by_tier = collections.Counter(t for t, _, _ in mined.values())
    print("total", len(mined), dict(by_tier))
    if args.dry_run:
        return
    lines = ["# Generated by mine_phrases.py — do not edit; use phrases_manual.txt / phrases_blocked.txt.",
             "# word  # tier, times seen, share of its rarest part that occurs inside it"]
    order = {"dictionary": 0, "wikipedia": 1, "corpus": 2}
    for word, (tier, c, cov) in sorted(mined.items(), key=lambda kv: (order[kv[1][0]], -kv[1][1])):
        lines.append(f"{word}  # {tier} n={c} cov={cov:.2f}")
    PHRASES_MINED.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {PHRASES_MINED}")


if __name__ == "__main__":
    main()
