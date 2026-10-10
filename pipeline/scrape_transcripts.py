#!/usr/bin/env python3
"""Scrape meeting transcripts from asrs.parliament.go.th (Thai House of Representatives).

Video page URLs look like https://asrs.parliament.go.th/video/<phase>/<meeting_id>/<seq>,
where each <seq> is a ~5-minute clip. The transcript panel on that page is loaded from
the site's JSON API, which this script calls directly:

  POST /api/opensearch/searchplaylist  {query: meeting_id, phase}      -> one doc per clip
  POST /api/opensearch/searchbyid      {meeting_id, seq, phase}        -> transcript segments
  POST /api/opensearch/advancesearch   {date: {start, stop}, ...}      -> used to find meeting IDs

Request bodies and responses are AES-CBC encrypted, and requests carry a short-lived
anonymous HS256 JWT. The frontend does the same thing in the browser, and the keys
ship in its JS bundle. The JWT secret is read from the live bundle on each run, so the
script keeps working if the site rotates it.

Output (default ./data):
  data/<phase>/<meeting_id>_<date>.json   full meeting: metadata, clips, segments
  data/<phase>/<meeting_id>_<date>.txt    readable transcript grouped by speaker

Examples:
  python pipeline/scrape_transcripts.py                          # 2026-09-01 .. today
  python pipeline/scrape_transcripts.py --since 2026-08-01 --until 2026-08-31
  python pipeline/scrape_transcripts.py --meeting 2026/108       # one meeting
  python pipeline/scrape_transcripts.py --force                  # re-download existing files
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import hmac
import html
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

BASE = "https://asrs.parliament.go.th"
API = BASE + "/api"
# Payload AES key, hardcoded in the site's frontend ("PLM@S3cretT0ken".padEnd(32, "0")).
AES_KEY = b"PLM@S3cretT0ken".ljust(32, b"0")
UA = "Mozilla/5.0 (X11; Linux x86_64) mptracker-transcript-scraper"

# Consecutive empty meeting IDs tolerated while walking IDs (the site has gaps).
MAX_ID_GAP = 15


# --------------------------------------------------------------------------- API client


class AsrsClient:
    def __init__(self, delay: float = 0.2, retries: int = 4):
        self.delay = delay
        self.retries = retries
        self._local = threading.local()
        self.jwt_secret = self._load_jwt_secret()

    @property
    def session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers["User-Agent"] = UA
            self._local.session = s
        return s

    # --- crypto helpers (mirror the frontend's crypto-js usage)

    @staticmethod
    def decrypt(blob: str):
        ct, iv = blob.split(".")
        d = Cipher(algorithms.AES(AES_KEY), modes.CBC(bytes.fromhex(iv))).decryptor()
        raw = d.update(base64.b64decode(ct)) + d.finalize()
        u = padding.PKCS7(128).unpadder()
        return json.loads(u.update(raw) + u.finalize())

    @staticmethod
    def encrypt(obj) -> list[str]:
        iv = os.urandom(16)
        p = padding.PKCS7(128).padder()
        data = p.update(json.dumps(obj).encode()) + p.finalize()
        e = Cipher(algorithms.AES(AES_KEY), modes.CBC(iv)).encryptor()
        return [base64.b64encode(e.update(data) + e.finalize()).decode() + "." + iv.hex()]

    def _load_jwt_secret(self) -> str:
        index = self.session.get(BASE + "/", timeout=60).text
        m = re.search(r'src="(/static/js/main\.[0-9a-f]+\.js)"', index)
        if not m:
            sys.exit("Could not find the JS bundle on the site's index page; the site layout may have changed.")
        bundle = self.session.get(BASE + m.group(1), timeout=120).text
        m = re.search(r"JSON\.parse\('(\{\"r\":\"[^']+\})'\)", bundle)
        if not m:
            sys.exit("Could not find the encrypted JWT secret in the JS bundle.")
        return self.decrypt(json.loads(m.group(1))["r"])

    def _jwt(self) -> str:
        def b64(b: bytes) -> str:
            return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

        now = int(time.time())
        header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
        payload = b64(json.dumps({"user_profile": {"role": "User"}, "iat": now, "exp": now + 300},
                                 separators=(",", ":")).encode())
        sig = hmac.new(self.jwt_secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
        return f"{header}.{payload}.{b64(sig)}"

    def call(self, path: str, data: dict) -> dict:
        last_err = None
        for attempt in range(self.retries):
            try:
                r = self.session.post(API + path, json=self.encrypt(data),
                                      headers={"authorization": self._jwt()}, timeout=(10, 60))
                r.raise_for_status()
                result = self.decrypt(r.json()[0])
                if self.delay:
                    time.sleep(self.delay)
                return result
            except (requests.RequestException, ValueError, KeyError) as e:
                last_err = e
                time.sleep(2 ** attempt)
        raise RuntimeError(f"{path} {data} failed after {self.retries} attempts: {last_err}")

    # --- endpoints

    def playlist(self, phase: int, meeting_id: int) -> list[dict]:
        r = self.call("/opensearch/searchplaylist", {"query": str(meeting_id), "phase": str(phase)})
        return r.get("data") or []

    def clip_segments(self, phase: int, meeting_id: int, seq: int) -> list[dict]:
        r = self.call("/opensearch/searchbyid",
                      {"meeting_id": str(meeting_id), "seq": str(seq), "phase": str(phase)})
        if r.get("status") != "success":
            raise RuntimeError(f"searchbyid {phase}/{meeting_id}/{seq}: {r}")
        return r.get("data") or []

    def search_by_date(self, start: dt.date, stop: dt.date) -> list[dict]:
        r = self.call("/opensearch/advancesearch", {
            "query": None, "speaker": None, "speaker_party": None, "tag": None,
            "date": {"start": start.isoformat(), "stop": stop.isoformat()}, "opt_date": "AND",
            "meeting_type": "all", "options": "1", "route": "/video-search", "params": "",
        })
        return r.get("data") or []


# --------------------------------------------------------------------- meeting discovery


def discover_meetings(client: AsrsClient, since: dt.date, until: dt.date) -> list[dict]:
    """Return [{phase, meeting_id, meeting_date, playlist}] for meetings in [since, until].

    Search results are capped, so search is only used to find seed IDs (one query per week).
    From the seeds, meeting IDs are walked up and down using the playlist endpoint, which
    gives each meeting's date, until the walk leaves the date range.
    """
    seeds: dict[int, set[int]] = {}
    day = since
    while day <= until:
        end = min(day + dt.timedelta(days=6), until)
        for doc in client.search_by_date(day, end):
            s = doc["_source"]
            seeds.setdefault(int(s["phase"]), set()).add(int(s["meeting_id"]))
        day = end + dt.timedelta(days=1)

    if not seeds:
        print("No meetings found by search in that date range.")
        return []

    found: dict[tuple[int, int], dict] = {}
    for phase, ids in seeds.items():
        cache: dict[int, list[dict]] = {}

        def get(mid: int) -> list[dict]:
            if mid not in cache:
                cache[mid] = client.playlist(phase, mid) if mid > 0 else []
            return cache[mid]

        for direction, start in ((+1, max(ids)), (-1, min(ids))):
            mid, gap = start, 0
            while gap <= MAX_ID_GAP and mid > 0:
                pl = get(mid)
                if not pl:
                    gap += 1
                else:
                    gap = 0
                    date = dt.date.fromisoformat(pl[0]["_source"]["meeting_date"])
                    if since <= date <= until:
                        found[(phase, mid)] = {"phase": phase, "meeting_id": mid,
                                               "meeting_date": date.isoformat(), "playlist": pl}
                    elif (direction < 0 and date < since) or (direction > 0 and date > until):
                        break
                mid += direction
        # Fill anything between the seeds that the two outward walks didn't cover.
        for mid in range(min(ids), max(ids) + 1):
            pl = get(mid)
            if pl:
                date = dt.date.fromisoformat(pl[0]["_source"]["meeting_date"])
                if since <= date <= until:
                    found[(phase, mid)] = {"phase": phase, "meeting_id": mid,
                                           "meeting_date": date.isoformat(), "playlist": pl}

    return sorted(found.values(), key=lambda m: (m["meeting_date"], m["meeting_id"]))


# ------------------------------------------------------------------------------ output


def clean_text(raw: str) -> str:
    text = html.unescape(raw or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


def first(xs):
    return xs[0] if xs else None


META_FIELDS = ["phase", "meeting_id", "meeting_date", "meeting_title", "meeting_council",
               "meeting_type", "meeting_episode", "meeting_group", "meeting_year",
               "meeting_number", "meeting_room", "volume"]


def build_meeting(phase: int, meeting_id: int, playlist: list[dict], clips_raw: dict[int, list[dict]]) -> dict:
    src0 = playlist[0]["_source"]
    meeting = {k: src0.get(k) for k in META_FIELDS}
    meeting["scraped_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    meeting["clips"] = []
    for seq in sorted(clips_raw):
        segs = clips_raw[seq]
        s0 = segs[0]["_source"] if segs else {}
        meeting["clips"].append({
            "seq": seq,
            "url": f"{BASE}/video/{phase}/{meeting_id}/{seq}",
            "start_time": s0.get("start_time"),
            "end_time": s0.get("end_time"),
            "video_path": s0.get("video_path"),
            "tags": s0.get("tag") or [],
            "all_speakers": [
                {"name": n, "party": p, "province": v}
                for n, p, v in zip(s0.get("all_speaker") or [], s0.get("all_speaker_party") or [],
                                   s0.get("all_speaker_province") or [])
            ],
            "segments": [
                {
                    "id": d["_id"],
                    "start": d["_source"].get("start"),
                    "stop": d["_source"].get("stop"),
                    "speaker": first(d["_source"].get("speaker")),
                    "speaker_party": first(d["_source"].get("speaker_party")),
                    "speaker_province": first(d["_source"].get("speaker_province")),
                    "text": clean_text(d["_source"].get("text")),
                    "text_raw": d["_source"].get("text"),
                }
                for d in segs
            ],
        })
    return meeting


def render_text(meeting: dict) -> str:
    """Plain-text transcript, grouped into speaker turns as in the site's transcript panel."""
    lines = [
        f"{meeting['meeting_title']} ({meeting['meeting_council']})",
        f"{meeting['meeting_episode']}  ชุดที่ {meeting['meeting_group']} ปีที่ {meeting['meeting_year']} "
        f"ครั้งที่ {meeting['meeting_number']}",
        f"Date: {meeting['meeting_date']}   Meeting ID: {meeting['phase']}/{meeting['meeting_id']}",
        "",
    ]
    current = object()
    for clip in meeting["clips"]:
        lines.append(f"===== Clip {clip['seq']}  {clip['start_time']}-{clip['end_time']}  {clip['url']}")
        current = object()  # always reprint the speaker at a clip boundary
        for seg in clip["segments"]:
            if not seg["text"]:
                continue
            if seg["speaker"] != current:
                current = seg["speaker"]
                who = current or "(ไม่ระบุผู้พูด)"
                extra = ", ".join(x for x in (seg["speaker_party"], seg["speaker_province"]) if x)
                lines.append("")
                lines.append(f"[{who}{' — ' + extra if extra else ''}]")
            lines.append(seg["text"])
        lines.append("")
    return "\n".join(lines) + "\n"


# -------------------------------------------------------------------------------- main


def scrape_meeting(client: AsrsClient, phase: int, meeting_id: int, playlist: list[dict] | None,
                   out_dir: Path, workers: int, force: bool) -> Path | None:
    playlist = playlist if playlist is not None else client.playlist(phase, meeting_id)
    if not playlist:
        print(f"  {phase}/{meeting_id}: no data")
        return None
    date = playlist[0]["_source"]["meeting_date"]
    stem = out_dir / str(phase) / f"{meeting_id}_{date}"
    json_path = stem.with_suffix(".json")
    if json_path.exists() and not force:
        print(f"  {phase}/{meeting_id} ({date}): already saved, skipping (use --force to refresh)")
        return json_path

    seqs = sorted({int(d["_source"]["seq"]) for d in playlist})
    print(f"  {phase}/{meeting_id} ({date}): {len(seqs)} clips", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda s: (s, client.clip_segments(phase, meeting_id, s)), seqs))
    meeting = build_meeting(phase, meeting_id, playlist, dict(results))

    json_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = json_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meeting, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(json_path)
    stem.with_suffix(".txt").write_text(render_text(meeting), encoding="utf-8")
    n_seg = sum(len(c["segments"]) for c in meeting["clips"])
    print(f"    saved {n_seg} segments -> {json_path}")
    return json_path


def main() -> None:
    today = dt.date.today()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", type=dt.date.fromisoformat, default=dt.date(today.year, 9, 1),
                    help="first meeting date to include, YYYY-MM-DD (default: Sep 1 this year)")
    ap.add_argument("--until", type=dt.date.fromisoformat, default=today,
                    help="last meeting date to include, YYYY-MM-DD (default: today)")
    ap.add_argument("--meeting", action="append", default=[], metavar="PHASE/ID",
                    help="scrape specific meeting(s) instead of a date range, e.g. 2026/108 (repeatable)")
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parent.parent / "data",
                    help="output directory (default: data/ in the project root)")
    ap.add_argument("--workers", type=int, default=4, help="parallel clip requests per meeting (default 4)")
    ap.add_argument("--delay", type=float, default=0.2, help="pause after each request, seconds (default 0.2)")
    ap.add_argument("--force", action="store_true", help="re-download meetings that are already saved")
    args = ap.parse_args()

    client = AsrsClient(delay=args.delay)

    if args.meeting:
        targets = []
        for m in args.meeting:
            phase, mid = m.strip("/").split("/")[-2:]
            targets.append({"phase": int(phase), "meeting_id": int(mid), "playlist": None})
    else:
        print(f"Finding meetings from {args.since} to {args.until} ...", flush=True)
        targets = discover_meetings(client, args.since, args.until)
        print(f"Found {len(targets)} meetings: "
              + ", ".join(f"{t['phase']}/{t['meeting_id']} ({t['meeting_date']})" for t in targets))

    failures = []
    for t in targets:
        try:
            scrape_meeting(client, t["phase"], t["meeting_id"], t["playlist"], args.out, args.workers, args.force)
        except Exception as e:  # keep going; report at the end
            print(f"  FAILED {t['phase']}/{t['meeting_id']}: {e}")
            failures.append(t)
    if failures:
        sys.exit(f"{len(failures)} meeting(s) failed; re-run to retry (saved meetings are skipped).")
    print("Done.")


if __name__ == "__main__":
    main()
