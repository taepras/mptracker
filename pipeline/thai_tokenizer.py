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
  * lexicon/phrases_*.txt list compounds that DO belong together (งดออกเสียง, แลนด์บริดจ์, ตัวชี้วัด …);
    each is forced to win over its own parts. mine_phrases.py finds them in the transcripts.

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
# ICU (which segmented OSCAR) leaves fragments: starting with a vowel/tone mark, ending in a leading vowel, mojibake
BAD_WORD_RE = re.compile(r"^[ะัาำิ-ฺ็-๎]|[เแโใไ]$|เธ.*เธ")
# leftovers of broken words/abbreviations: start with a vowel/tone mark, or one letter + tone/karan (ร์, ต์, น่)
LEFTOVER_RE = re.compile(r"^[ะัาำิ-ฺ็-๎]|^[ก-ฮ][่-๋์]$")
CLUSTER_RE = re.compile(r"[เแโใไ]?[ก-ฮ][ั-ฺ็-๎]*ะ?")  # one Thai character cluster
END = ""  # trie key holding a word's cost


def _read_list(path: Path) -> list[str]:
    """One entry per line; '#' starts a comment; spaces inside an entry are ignored."""
    if not path.exists():
        return []
    entries = (line.split("#", 1)[0].replace(" ", "").strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [e for e in entries if e]


def load_phrases(mined: bool = True) -> list[str]:
    """Approved multi-word expressions: mined + manual, minus blocked."""
    blocked = set(_read_list(PHRASES_BLOCKED))
    wanted = (_read_list(PHRASES_MINED) if mined else []) + _read_list(PHRASES_MANUAL)
    return [p for p in dict.fromkeys(wanted) if p not in blocked]


def trusted_compounds() -> set[str]:
    """Words from the dictionaries (a superset of what OSCAR/ICU treats as one word)."""
    return set(thai_words()) | set(thai_orst_words()) | set(thai_volubilis_words()) | set(thai_icu_words())


def valid_word(word: str) -> bool:
    return 1 < len(word) <= MAX_WORD_LEN and bool(THAI_WORD_RE.match(word)) and not BAD_WORD_RE.search(word)


class ThaiTokenizer:
    def __init__(self, extra_words: set[str] = frozenset(), phrases: list[str] | None = None,
                 workers: int | None = None):
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

        # approved phrases beat the best segmentation of their own text by a fixed margin
        # shortest first, so a longer phrase always beats a split into shorter ones
        for phrase in sorted(set(load_phrases() if phrases is None else phrases), key=len):
            if phrase not in counts:
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
        for m in TOKEN_RE.finditer(text.replace("ๆ", " ").replace("ฯ", " ")):
            piece = m.group()
            if "ก" <= piece[0] <= "๏":
                out.extend(self._segment(piece))
            elif piece[0].isalpha():
                out.append(piece.lower())
        return [t for t in out if len(t) >= 2 and LETTER_RE.search(t) and not LEFTOVER_RE.search(t)]

    def tokens_many(self, texts: list[str]) -> list[list[str]]:
        """tokens() over many texts, spread over worker processes (fork: the trie is shared, not pickled)."""
        if self.workers <= 1 or len(texts) < 20:
            return [self.tokens(t) for t in texts]
        global _ACTIVE
        _ACTIVE = self
        if self._pool is None:
            self._pool = multiprocessing.get_context("fork").Pool(self.workers)
        return self._pool.map(_tokens_in_worker, texts, chunksize=8)

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool.join()
            self._pool = None


_ACTIVE: ThaiTokenizer | None = None


def _tokens_in_worker(text: str) -> list[str]:
    return _ACTIVE.tokens(text)
