#!/usr/bin/env python3
"""Fetch the official roster of House members from hris.parliament.go.th and save it to data/members.json.

The transcript site gives no party/province for some MPs (e.g. จักรภพ เพ็ญแข). The House's HR portal lists every
sitting member with their constituency and party, so build_db.py uses it to fill those gaps and to tell MPs
apart from ministers, senators and staff who also speak in the chamber.

  .venv/bin/python pipeline/scrape_members.py
  .venv/bin/python pipeline/scrape_members.py --out other.json

Each entry:  {member_no, name, party, province, district, party_list, constituency}
  province   'แบบบัญชีรายชื่อ' for party-list members (the same label the transcript site uses)
  district   constituency number, null for party-list members

TLS: the portal's server sends only its own certificate and omits the intermediate CA, so a plain request fails
verification (browsers fetch the missing certificate themselves). This script does the same — it downloads the
intermediate from the certificate's "CA Issuers" address — and still verifies the whole chain and the host name
against the standard trusted roots. Verification is never switched off.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import ssl
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import certifi
import requests
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import AuthorityInformationAccessOID

URL = "https://hris.parliament.go.th/ss_th.php"
ROOT = Path(__file__).resolve().parent.parent
PARTY_LIST = "แบบบัญชีรายชื่อ"
MIN_EXPECTED = 400  # the House has 500 seats; fewer parsed entries means the page layout changed

ITEM_RE = re.compile(r'<li class="tb_[^"]*">(.*?)</li>', re.S)
NUMBER_RE = re.compile(r"เลขประจำตัวสมาชิก\s*:\s*(\d+)")
NAME_RE = re.compile(r'<h4 style="margin-top:8px;">(.*?)</h4>', re.S)
H4_RE = re.compile(r"<h4>(.*?)</h4>", re.S)
SEAT_RE = re.compile(r"สมาชิกสภาผู้แทนราษฎร\s*(?:จังหวัด)?\s*(\S+?)\s*เขตเลือกตั้งที่\s*(\d+)")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text).replace("\xa0", " ")).strip()


def verified_ca_bundle(url: str) -> str:
    """CA bundle = standard roots + the intermediate certificate(s) the server failed to send."""
    host = urlparse(url).hostname
    leaf = x509.load_pem_x509_certificate(ssl.get_server_certificate((host, 443)).encode())  # only to find the issuer
    access = leaf.extensions.get_extension_for_class(x509.AuthorityInformationAccess).value
    issuers = [d.access_location.value for d in access if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS]
    if not issuers:
        sys.exit(f"{host} sent an incomplete certificate chain and gives no CA Issuers address to complete it.")
    pems = []
    for issuer_url in issuers:
        data = requests.get(issuer_url, timeout=30).content
        cert = (x509.load_der_x509_certificate(data) if not data.startswith(b"-----")
                else x509.load_pem_x509_certificate(data))
        pems.append(cert.public_bytes(Encoding.PEM).decode())
    bundle = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)
    bundle.write(Path(certifi.where()).read_text() + "\n" + "\n".join(pems))
    bundle.close()
    return bundle.name


def fetch(url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) mptracker-roster-scraper"}
    try:
        response = requests.get(url, headers=headers, timeout=60)
    except requests.exceptions.SSLError:
        response = requests.get(url, headers=headers, timeout=60, verify=verified_ca_bundle(url))
    response.raise_for_status()
    response.encoding = "utf-8"
    return response.text


def parse(page: str) -> list[dict]:
    members = []
    for item in ITEM_RE.findall(page):
        number, name = NUMBER_RE.search(item), NAME_RE.search(item)
        if not (number and name):
            continue
        h4 = [_clean(x) for x in H4_RE.findall(item)]
        seat = next((x for x in h4 if x.startswith("สมาชิกสภาผู้แทนราษฎร")), "")
        party = next((x for x in h4 if x.startswith("พรรค")), "")
        entry = {"member_no": int(number.group(1)), "name": _clean(name.group(1)),
                 "party": party.removeprefix("พรรค").strip() or None, "constituency": seat,
                 "party_list": PARTY_LIST in seat, "province": None, "district": None}
        if entry["party_list"]:
            entry["province"] = PARTY_LIST
        elif m := SEAT_RE.search(seat):
            entry["province"], entry["district"] = m.group(1), int(m.group(2))
        members.append(entry)
    return members


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=URL)
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "members.json")
    args = ap.parse_args()

    members = parse(fetch(args.url))
    if len(members) < MIN_EXPECTED:
        sys.exit(f"Only {len(members)} members parsed from {args.url}; the page layout has probably changed.")
    unparsed = [m["name"] for m in members if not m["party_list"] and m["province"] is None]
    if unparsed:
        print(f"warning: constituency not understood for {len(unparsed)} members, e.g. {unparsed[:3]}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(
        {"source": args.url, "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
         "members": members}, ensure_ascii=False, indent=1), encoding="utf-8")
    parties = {m["party"] for m in members}
    print(f"{len(members)} members ({sum(m['party_list'] for m in members)} party-list), "
          f"{len(parties)} parties -> {args.out}")


if __name__ == "__main__":
    main()
