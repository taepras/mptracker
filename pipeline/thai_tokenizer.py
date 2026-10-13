"""Thai word segmentation tuned for parliamentary transcripts.

PyThaiNLP's default engine (newmm) maximises word length over a dictionary that contains junk entries
("วจะ", "คาที่", "ร่วมใน", …), so "แล้วจะ" becomes แล้ + วจะ and "ราคาที่" becomes รา + คาที่. This module
instead picks, for every run of Thai text, the segmentation with the highest unigram probability (Viterbi):

  * the vocabulary is the Thai dictionaries + Wikipedia titles + names. OSCAR's own token list is mostly
    fragments and web noise (ทลาส, ป็นบ, เลสเบ …), so it is used only for FREQUENCIES (1.9 B web tokens):
    frequent words (แล้ว, จะ, ราคา, ที่) beat accidental dictionary strings (แล้ + วจะ, รา + คาที่);
  * dictionary words OSCAR has hardly seen stay available at a low floor frequency only if they are
    "atomic" (cannot be built from known words); compounds such as ตัวชี้วัด or ร่วมใน are left out,
    because the dictionaries mix real compounds with accidental ones;
  * names, provinces and party names (passed in by the caller) are kept whole;
  * so are district and subdistrict names (lexicon/places_th.txt) whose usual split goes through a rare part
    (ชะเมา, not ชะ + เมา); those made only of everyday words (แม่คือ, บางเขา, ท่าอยู่) stay split, or they would
    swallow ordinary speech;
  * lexicon/phrases_*.txt list compounds that DO belong together (งดออกเสียง, แลนด์บริดจ์, ตัวชี้วัด …);
    each is forced to win over its own parts. mine_phrases.py finds them in the transcripts;
  * lexicon/abbreviations_mined.txt lists dotted abbreviations the dictionaries lack (ครม., กทม., อว. …), found
    where they stand alone in the transcripts; each wins over its parts (else ครม. -> คร + ม.), also inside a run
    (มติครม. -> มติ + ครม.);
  * lexicon/words_manual.txt lists single words to keep whole (ฮุจญาจ); Latin entries there may span spaces
    ("Thai PBS" and "ThaiPBS" both become the one token "thai pbs").

    tok = ThaiTokenizer(extra_words={"ภัณฑิล", ...})
    tok.tokens("ผมขอเสนอแลนด์บริดจ์")  -> ['ผม', 'ขอ', 'เสนอ', 'แลนด์บริดจ์']
"""

from __future__ import annotations

import collections
import math
import multiprocessing
import re
from pathlib import Path

from pythainlp.corpus import (oscar, thai_icu_words, thai_orst_words, thai_volubilis_words,
                              thai_wikipedia_titles, thai_words)

LEXICON_DIR = Path(__file__).resolve().parent / "lexicon"
PHRASES_MINED = LEXICON_DIR / "phrases_mined.txt"      # written by mine_phrases.py — don't edit
PHRASES_MANUAL = LEXICON_DIR / "phrases_manual.txt"    # hand-picked additions
PHRASES_BLOCKED = LEXICON_DIR / "phrases_blocked.txt"  # hand-picked vetoes (win over the other two)
ABBREVIATIONS_MINED = LEXICON_DIR / "abbreviations_mined.txt"  # written by mine_phrases.py — don't edit
WORDS_MANUAL = LEXICON_DIR / "words_manual.txt"  # hand-picked single words (Thai, or Latin with spaces)
PLACES = LEXICON_DIR / "places_th.txt"  # district + subdistrict names (third-party list, see its header)
PLACE_MAX_PART_FREQ = 100_000  # a place name is kept whole only if a part of its split is rarer than this in OSCAR
ABBREVIATION_MIN_COUNT = 3  # times an abbreviation must stand alone in the transcripts to be listed

OSCAR_MIN_FREQ = 50     # OSCAR words below this are web noise
DICT_FLOOR = 2          # frequency given to atomic dictionary words OSCAR has never seen
EXTRA_FREQ = 2000       # names/provinces/parties: keep whole unless their parts are very frequent words
PHRASE_MARGIN = 3.0     # nats by which an approved phrase beats the best segmentation of its own text
UNKNOWN_COST = 25.0     # nats per unknown character cluster (a floor-frequency word costs ~21)
MAX_WORD_LEN = 25

THAI_WORD_RE = re.compile(r"^[ก-๏]+\.?$|^(?:[ก-ฮ]{1,4}\.){2,6}$")  # words, or abbreviations like ก.ล.ต.
# Thai letters (dots allowed inside, for abbreviations such as ก.ล.ต.), Latin words, digit runs
TOKEN_RE = re.compile(r"[ก-๏][ก-๏.]*|[A-Za-z][A-Za-z0-9]*(?:[-'][A-Za-z0-9]+)*|[0-9๐-๙]+")
LETTER_RE = re.compile(r"[ก-๏A-Za-z]")
# dotted abbreviations standing at the start of a Thai run (after a space, digit, bracket …), e.g. ครม. / รพ.สต.;
# split into groups: a run of single letters (ก.ค.ศ., ส.ส.) is one abbreviation, each longer group is its own
# (รพ.สต. -> รพ. + สต., ส.อบจ. -> ส. + อบจ.)
ABBREVIATION_RUN_RE = re.compile(r"(?<![ก-๏.])(?:[ก-ฮ]{1,5}\.)+")
ABBREVIATION_GROUP_RE = re.compile(r"(?:[ก-ฮ]\.)+|[ก-ฮ]{2,5}\.")
# ICU (which segmented OSCAR) leaves fragments: starting with a vowel/tone mark, ending in a leading vowel, mojibake
BAD_WORD_RE = re.compile(r"^[ะัาำิ-ฺ็-๎]|[เแโใไ]$|เธ.*เธ")
# leftovers of broken words/abbreviations: start with a vowel/tone mark, or one letter + tone/karan (ร์, ต์, น่)
LEFTOVER_RE = re.compile(r"^[ะัาำิ-ฺ็-๎]|^[ก-ฮ][่-๋์]$")
CLUSTER_RE = re.compile(r"[เแโใไ]?[ก-ฮ][ั-ฺ็-๎]*ะ?")  # one Thai character cluster
END = ""  # trie key holding a word's cost


def _read_list(path: Path, keep_spaces: bool = False) -> list[str]:
    """One entry per line; '#' starts a comment; spaces inside an entry are ignored unless `keep_spaces`."""
    if not path.exists():
        return []
    entries = (line.split("#", 1)[0] for line in path.read_text(encoding="utf-8").splitlines())
    entries = (" ".join(e.split()) if keep_spaces else e.replace(" ", "").strip() for e in entries)
    return [e for e in entries if e]


def load_phrases(mined: bool = True) -> list[str]:
    """Approved multi-word expressions: mined + manual, minus blocked."""
    blocked = set(_read_list(PHRASES_BLOCKED))
    wanted = (_read_list(PHRASES_MINED) if mined else []) + _read_list(PHRASES_MANUAL)
    return [p for p in dict.fromkeys(wanted) if p not in blocked]


def load_abbreviations() -> list[str]:
    """Mined dotted abbreviations, minus anything in phrases_blocked.txt."""
    blocked = set(_read_list(PHRASES_BLOCKED))
    return [a for a in _read_list(ABBREVIATIONS_MINED) if a not in blocked]


def load_words() -> tuple[list[str], list[str]]:
    """Hand-picked single words: (Thai words, Latin words lower-cased, their parts joined by one space)."""
    words = _read_list(WORDS_MANUAL, keep_spaces=True)
    return [w.replace(" ", "") for w in words if "ก" <= w[0] <= "๏"], [w.lower() for w in words if w[0].isascii()]


def find_abbreviations(text: str):
    """Dotted abbreviations of two or more letters standing at the start of a Thai run in `text`."""
    for m in ABBREVIATION_RUN_RE.finditer(text):
        yield from (g for g in ABBREVIATION_GROUP_RE.findall(m.group()) if len(g) > 2)


def trusted_compounds() -> set[str]:
    """Words from the dictionaries (a superset of what OSCAR/ICU treats as one word)."""
    return set(thai_words()) | set(thai_orst_words()) | set(thai_volubilis_words()) | set(thai_icu_words())


def valid_word(word: str) -> bool:
    return 1 < len(word) <= MAX_WORD_LEN and bool(THAI_WORD_RE.match(word)) and not BAD_WORD_RE.search(word)


class ThaiTokenizer:
    def __init__(self, extra_words: set[str] = frozenset(), phrases: list[str] | None = None,
                 workers: int | None = None, abbreviations: list[str] | None = None):
        freq = collections.Counter()
        for word, count in oscar.word_freqs():  # the list has duplicate entries; they add up
            freq[word] += count
        self.log_total = math.log(sum(freq.values()))

        # vocabulary: dictionaries + Wikipedia titles + names; OSCAR only supplies the frequencies
        vocabulary = {w for w in trusted_compounds() | set(thai_wikipedia_titles()) if valid_word(w)}
        vocabulary |= {w for w in extra_words if len(w) > 1}
        counts: dict[str, float] = {w: freq[w] for w in vocabulary if freq.get(w, 0) >= OSCAR_MIN_FREQ}
        for word in extra_words:
            counts[word] = max(counts.get(word, 0), EXTRA_FREQ)
        if "ณ" in freq:  # the one real single-letter word
            counts["ณ"] = freq["ณ"]
        self._build_trie(counts)

        # words OSCAR has hardly seen: keep the atomic ones, drop those made of known words
        for word in vocabulary - counts.keys():
            if not self._covered(word):
                counts[word] = DICT_FLOOR
        del freq
        self._build_trie(counts)
        self.lexicon_size = len(counts)

        # place names whose split goes through a rare part are kept whole, like names (see PLACE_MAX_PART_FREQ)
        place_cost = self.log_total - math.log(EXTRA_FREQ)
        rare_cost = self.log_total - math.log(PLACE_MAX_PART_FREQ)
        for place in _read_list(PLACES):
            if place in counts or not valid_word(place):
                continue
            parts, unknown, cost = self._viterbi(place)
            if cost > place_cost and (unknown or max(self._best_cost(p) for p in parts) > rare_cost):
                self._insert(place, place_cost)

        # dotted abbreviations the dictionaries lack beat their parts the same way (ครม. over คร + ม.); they are
        # atomic, not phrases: no parts in breaks()
        for abbr in sorted(set(load_abbreviations() if abbreviations is None else abbreviations), key=len):
            if abbr not in counts:
                self._insert(abbr, self._best_cost(abbr) - PHRASE_MARGIN)

        # hand-picked words beat their parts the same way, and are atomic too
        thai_words_manual, latin_words = load_words()
        for word in thai_words_manual:
            if word not in counts:
                self._insert(word, self._best_cost(word) - PHRASE_MARGIN)
        # Latin words spanning spaces: matched before TOKEN_RE splits them, spaces optional (Thai PBS, ThaiPBS)
        self.latin_words = {w.replace(" ", ""): w for w in latin_words if " " in w}
        alternatives = "|".join(r"\s*".join(map(re.escape, w.split())) for w in
                                sorted(self.latin_words.values(), key=len, reverse=True))
        self.token_re = (re.compile(rf"(?i:{alternatives})(?![A-Za-z0-9])|{TOKEN_RE.pattern}")
                         if alternatives else TOKEN_RE)

        # approved phrases beat the best segmentation of their own text by a fixed margin
        # shortest first, so a longer phrase always beats a split into shorter ones
        self.phrase_parts: dict[str, list[str]] = {}  # phrase -> its segmentation without itself, for breaks()
        for phrase in sorted(set(load_phrases() if phrases is None else phrases), key=len):
            parts = self._viterbi(phrase)[0]
            if len(parts) > 1:  # not already one word (a place name or abbreviation can make it one)
                self.phrase_parts[phrase] = parts
                self._insert(phrase, self._best_cost(phrase) - PHRASE_MARGIN)
        self.workers = workers if workers is not None else min(8, multiprocessing.cpu_count())
        self._pool = None

    def _build_trie(self, counts: dict[str, float]) -> None:
        self.trie: dict = {}
        for word, count in counts.items():
            self._insert(word, self.log_total - math.log(count))

    def _insert(self, word: str, cost: float) -> None:
        node = self.trie
        for ch in word:
            node = node.setdefault(ch, {})
        node[END] = cost

    def _covered(self, word: str) -> bool:
        """True if `word` can be written entirely with (two or more) words already in the trie."""
        pieces, unknown, _ = self._viterbi(word)
        return len(pieces) > 1 and not unknown

    def _best_cost(self, text: str) -> float:
        return self._viterbi(text)[2]

    # --- segmentation -------------------------------------------------------------------------

    @staticmethod
    def _cluster_end(s: str, i: int) -> int:
        m = CLUSTER_RE.match(s, i)
        return m.end() if m and m.end() > i else i + 1

    def _viterbi(self, s: str) -> tuple[list[str], bool, float]:
        """Best segmentation of one Thai run: (tokens, used_unknown_clusters, total_cost)."""
        n = len(s)
        inf = float("inf")
        best = [inf] * (n + 1)
        back = [0] * (n + 1)
        unknown = [False] * (n + 1)
        best[0] = 0.0
        root = self.trie
        for i in range(n):
            base = best[i]
            if base == inf:
                continue
            node, j, found = root, i, False
            while j < n:
                node = node.get(s[j])
                if node is None:
                    break
                j += 1
                cost = node.get(END)
                if cost is not None:
                    found = True
                    if base + cost < best[j]:
                        best[j], back[j], unknown[j] = base + cost, i, False
            if not found:
                j = self._cluster_end(s, i)
                if base + UNKNOWN_COST < best[j]:
                    best[j], back[j], unknown[j] = base + UNKNOWN_COST, i, True
        spans = []  # (start, end, is_unknown), walking back from the end
        j = n
        while j > 0:
            spans.append((back[j], j, unknown[j]))
            j = back[j]
        spans.reverse()
        out: list[str] = []
        run = None  # [start, end] of a run of unknown clusters, emitted as one token
        for i, j, is_unknown in spans:
            if is_unknown:
                run = [i, j] if run is None else [run[0], j]
                continue
            if run:
                out.append(s[run[0]:run[1]])
                run = None
            out.append(s[i:j])
        if run:
            out.append(s[run[0]:run[1]])
        return out, any(u for _, _, u in spans), best[n]

    def _segment(self, s: str) -> list[str]:
        return self._viterbi(s)[0]

    def tokens(self, text: str) -> list[str]:
        """Content tokens of `text`: Thai words and Latin words (lower-cased); numbers and stray
        punctuation are dropped. 'ๆ' and 'ฯ' only mark repetition/abbreviation, so they act as spaces."""
        out = []
        for m in self.token_re.finditer(text.replace("ๆ", " ").replace("ฯ", " ")):
            piece = m.group()
            if "ก" <= piece[0] <= "๏":
                out.extend(self._segment(piece))
            elif piece[0].isalpha():
                key = "".join(piece.lower().split())
                out.append(self.latin_words.get(key, key))
        return [t for t in out if len(t) >= 2 and LETTER_RE.search(t) and not LEFTOVER_RE.search(t)]

    def _parts(self, token: str) -> list[str]:
        """A phrase token split back into its words (recursively: phrases can be built from shorter phrases)."""
        parts = self.phrase_parts.get(token)
        return [p for part in parts for p in self._parts(part)] if parts else [token]

    def breaks(self, text: str) -> list[int]:
        """Word-boundary offsets in `text`: every place a word starts or ends, with phrases split into their words.
        Offsets count UTF-16 units, as JavaScript does, for the transcript search in static/speeches.js: a match must
        start and end on a boundary, so คดี is not found inside โชคดี, but งบ is still found in งบประมาณ.
        A word's final dot is also a boundary, so ครม finds ครม."""
        out: set[int] = set()
        for m in TOKEN_RE.finditer(text.replace("ๆ", " ").replace("ฯ", " ")):  # same length: offsets stay valid
            out.add(m.start())
            out.add(m.end())
            if "ก" <= m.group()[0] <= "๏":
                pos = m.start()
                for token in self._viterbi(m.group())[0]:
                    for part in self._parts(token):
                        pos += len(part)
                        out.add(pos)
                        if len(part) > 2 and part.endswith("."):
                            out.add(pos - 1)
        result = sorted(out)
        if any(ord(ch) > 0xFFFF for ch in text):  # characters outside the BMP take two units in JavaScript
            units, n = [], 0
            for ch in text:
                units.append(n)
                n += 2 if ord(ch) > 0xFFFF else 1
            units.append(n)
            result = [units[i] for i in result]
        return result

    def tokens_many(self, texts: list[str]) -> list[list[str]]:
        """tokens() over many texts, spread over worker processes (fork: the trie is shared, not pickled)."""
        if self.workers <= 1 or len(texts) < 20:
            return [self.tokens(t) for t in texts]
        global _ACTIVE
        _ACTIVE = self
        if self._pool is None:
            self._pool = multiprocessing.get_context("fork").Pool(self.workers)
        return self._pool.map(_tokens_in_worker, texts, chunksize=8)

    def breaks_many(self, texts: list[str]) -> list[list[int]]:
        """breaks() over many texts, in the same worker processes as tokens_many()."""
        if self.workers <= 1 or len(texts) < 20:
            return [self.breaks(t) for t in texts]
        global _ACTIVE
        _ACTIVE = self
        if self._pool is None:
            self._pool = multiprocessing.get_context("fork").Pool(self.workers)
        return self._pool.map(_breaks_in_worker, texts, chunksize=8)

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool.join()
            self._pool = None


_ACTIVE: ThaiTokenizer | None = None


def _tokens_in_worker(text: str) -> list[str]:
    return _ACTIVE.tokens(text)


def _breaks_in_worker(text: str) -> list[int]:
    return _ACTIVE.breaks(text)


# Word boundaries are stored and shipped as a compact string: the gaps between consecutive offsets, one character
# each from BREAK_DIGITS when the gap is under 62, otherwise "_<base 10>_". Decoded by static/speeches.js.
BREAK_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def encode_breaks(offsets: list[int]) -> str:
    out, prev = [], 0
    for o in offsets:
        gap, prev = o - prev, o
        out.append(BREAK_DIGITS[gap] if gap < len(BREAK_DIGITS) else f"_{gap}_")
    return "".join(out)


def decode_breaks(code: str) -> list[int]:
    out, pos, i = [], 0, 0
    while i < len(code):
        if code[i] == "_":
            j = code.index("_", i + 1)
            gap, i = int(code[i + 1:j]), j + 1
        else:
            gap, i = BREAK_DIGITS.index(code[i]), i + 1
        pos += gap
        out.append(pos)
    return out
