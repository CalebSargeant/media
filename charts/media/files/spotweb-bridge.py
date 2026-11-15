#!/usr/bin/env python3
"""Newznab proxy that makes Spotweb results usable by Radarr.

Spotnet titles are free text with no year and no scene naming, so Radarr's
searches ("Title Year") find nothing and its parser rejects what does come back.
This sits in front of Spotweb and:

  - search (q present): queries Spotweb without the trailing year and renames
    results that match the requested movie to "Title Year <quality> NL-Spotnet";
  - RSS (no q): renames results that match a monitored movie Radarr is missing,
    so a new Spotnet post is grabbed without anyone searching for it.

Everything else (caps, NZB downloads, unmatched items) passes through untouched.
Matching is deliberately strict: every title word must be present, a different
year or an extra sequel number rejects the result.

Run with --self-test to check the matching rules.
"""
import io
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = os.environ.get("SPOTWEB_URL", "http://127.0.0.1:80").rstrip("/")
# Host header sent upstream; Spotweb builds its NZB links from it.
UPSTREAM_HOST = os.environ.get("SPOTWEB_HOST", "")
RADARR_URL = os.environ.get("RADARR_URL", "").rstrip("/")
RADARR_API_KEY = os.environ.get("RADARR_API_KEY", "")
PORT = int(os.environ.get("PORT", "8080"))
WANTED_TTL = int(os.environ.get("WANTED_TTL", "900"))

NS = {"newznab": "http://www.newznab.com/DTD/2010/feeds/attributes/", "atom": "http://www.w3.org/2005/Atom"}
for prefix, uri in NS.items():
    ET.register_namespace(prefix, uri)

STOP = {"de", "het", "een", "the", "a", "an", "van", "of", "en", "and", "le", "la", "der", "die", "das"}
# Hard markers end the release name; soft ones can be part of a title ("Het eerste DVD boek").
HARD = {
    "480p", "576p", "720p", "1080p", "2160p", "4k", "uhd", "x264", "x265", "h264", "h265", "hevc", "avc",
    "xvid", "divx", "web", "dl", "webdl", "webrip", "bluray", "bdrip", "brrip", "hdtv", "aac", "ac3",
    "eac3", "dts", "ddp", "dd", "flac", "remux", "proper", "repack",
}
SOFT = {
    "dvd", "dvdrip", "dvd5", "dvd9", "dvdr", "nl", "nlsub", "nlsubs", "dutch", "gesproken", "nlgesproken",
    "subs", "sub", "complete", "mkv", "mp4", "avi", "iso", "pal", "ntsc", "multi",
}
QUALITY_PREFIX = re.compile(r"^(aac|ac3|eac3|ddp|dd|dts|x26|h26)\d")
YEAR = re.compile(r"^(19|20)\d\d$")
RESOLUTION = re.compile(r"\b(480|576|720|1080|2160)p\b", re.I)
CATEGORY_QUALITY = {"2030": "DVDRip", "2040": "720p WEB-DL", "2050": "1080p BluRay", "2060": "1080p BluRay"}


def tokens(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.findall(r"[a-z0-9]+", text)


def title_part(toks):
    """Tokens before the first year or hard quality marker: the name of the release."""
    out = []
    for t in toks:
        if YEAR.match(t) or t in HARD or QUALITY_PREFIX.match(t):
            break
        out.append(t)
    return out


def matches(movie_title, year, spot_title, min_coverage):
    want = [t for t in tokens(movie_title) if t not in STOP] or tokens(movie_title)
    spot = tokens(spot_title)
    name = title_part(spot)
    if not want or not set(want) <= set(name):
        return False
    years = {t for t in spot if YEAR.match(t)}
    if years and (not year or str(year) not in years):
        return False
    movie_numbers = {t for t in tokens(movie_title) if t.isdigit()}
    if any(t.isdigit() and t not in movie_numbers for t in name):
        return False
    significant = set(t for t in name if t not in STOP and (t not in SOFT or t in want))
    if len(set(want)) == 1:
        return significant == set(want)
    return len(set(want)) / max(len(significant), 1) >= min_coverage


def renamed(movie_title, year, spot_title, categories):
    spot = tokens(spot_title)
    tail = spot[len(title_part(spot)):]
    tail = [t for t in tail if not YEAR.match(t)]
    if RESOLUTION.search(spot_title) and tail:
        quality = " ".join(tail)
    else:
        quality = next((CATEGORY_QUALITY[c] for c in categories if c in CATEGORY_QUALITY), "DVDRip")
    clean = re.sub(r"[^\w\s'&-]", " ", movie_title)
    clean = re.sub(r"\s+", " ", clean).strip()
    return " ".join(p for p in (clean, str(year) if year else "", quality, "NL-Spotnet") if p)


def item_categories(item):
    return [a.get("value") for a in item.findall("newznab:attr", NS) if a.get("name") == "category"]


_wanted = {"at": 0.0, "movies": []}
WANTED_PAGE = 200


def wanted_movies():
    if time.time() - _wanted["at"] < WANTED_TTL:
        return _wanted["movies"]
    if not RADARR_URL or not RADARR_API_KEY:
        return []
    out, page = [], 1
    try:
        # Missing movies a page at a time: the whole library with its alternate titles
        # parses to more than the sidecar's memory limit.
        while True:
            req = urllib.request.Request(
                f"{RADARR_URL}/api/v3/wanted/missing?monitored=true&pageSize={WANTED_PAGE}&page={page}",
                headers={"X-Api-Key": RADARR_API_KEY})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.load(r)
            records = data.get("records") or []
            for m in records:
                if not m.get("monitored") or m.get("hasFile") or not m.get("isAvailable"):
                    continue
                titles = {m.get("title") or ""} | {m.get("originalTitle") or ""}
                titles |= {a.get("title") or "" for a in m.get("alternateTitles") or []}
                titles = {t for t in titles if len([x for x in tokens(t) if x not in STOP]) >= 2}
                if titles:
                    out.append((m.get("title"), m.get("year") or 0, sorted(titles)))
            if not records or page * WANTED_PAGE >= (data.get("totalRecords") or 0):
                break
            page += 1
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        print(f"radarr wanted list failed: {e}", file=sys.stderr)
        return _wanted["movies"]
    _wanted.update(at=time.time(), movies=out)
    return out


def parseable(title, year):
    """Radarr already parses a title carrying the movie's year; leave those alone."""
    return bool(year) and str(year) in tokens(title)


def rewrite_search(body, movie_title, year):
    root = ET.fromstring(body)
    for item in root.iter("item"):
        title = item.find("title")
        if title is None or not title.text or parseable(title.text, year):
            continue
        if matches(movie_title, year, title.text, 0.66):
            title.text = renamed(movie_title, year, title.text, item_categories(item))
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def rewrite_rss(body):
    movies = wanted_movies()
    if not movies:
        return body
    root = ET.fromstring(body)
    for item in root.iter("item"):
        title = item.find("title")
        if title is None or not title.text:
            continue
        hits = {(name, year) for name, year, titles in movies for t in titles if matches(t, year, title.text, 0.6)}
        if len(hits) == 1:
            name, year = hits.pop()
            if parseable(title.text, year):
                continue
            title.text = renamed(name, year, title.text, item_categories(item))
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def split_year(q):
    words = q.split()
    if len(words) > 1 and YEAR.match(words[-1]):
        return " ".join(words[:-1]), int(words[-1])
    return q, 0


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path == "/healthz":
            return self.reply(200, b"ok", "text/plain")
        path = url.path[len("/radarr"):] if url.path.startswith("/radarr") else url.path
        params = urllib.parse.parse_qs(url.query, keep_blank_values=True)
        t = (params.get("t") or [""])[0]
        q = (params.get("q") or [""])[0].strip()
        movie_title, year = split_year(q)
        if path == "/api" and t in ("search", "movie") and q:
            params["q"] = [movie_title]
        upstream = f"{UPSTREAM}{path}?{urllib.parse.urlencode(params, doseq=True)}"
        headers = {"User-Agent": self.headers.get("User-Agent", "spotweb-bridge")}
        if UPSTREAM_HOST:
            headers["Host"] = UPSTREAM_HOST
        try:
            with urllib.request.urlopen(urllib.request.Request(upstream, headers=headers), timeout=120) as r:
                body, ctype, status = r.read(), r.headers.get("Content-Type", "application/xml"), r.status
        except urllib.error.HTTPError as e:
            return self.reply(e.code, e.read(), e.headers.get("Content-Type", "text/plain"))
        except (urllib.error.URLError, TimeoutError) as e:
            return self.reply(502, f"spotweb unreachable: {e}".encode(), "text/plain")
        if path == "/api" and t in ("search", "movie") and b"<rss" in body[:500]:
            try:
                body = rewrite_search(body, movie_title, year) if q else rewrite_rss(body)
            except ET.ParseError as e:
                print(f"unparseable spotweb response, passed through: {e}", file=sys.stderr)
        self.reply(status, body, ctype)

    def reply(self, status, body, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s\n" % re.sub(r"apikey=[^&\s]+", "apikey=REDACTED", fmt % args))


def self_test():
    ok = [
        ("Dikkie Dik Logeren", 2008, "Dikkie Dik Logeren"),
        ("Dikkie Dik Het eerste DVD boek", 2009, "Het eerste DVD boek van Dikkie Dik"),
        ("Lekker Weertje Dikkie Dik", 0, "Dirk Scheele Lekker Weertje, Dikkie Dik"),
        ("Dikkie Dik en de verdwenen knuffel", 2024,
         "Dikkie Dik en de verdwenen knuffel 1080p WEB DL HEVC AAC2 0 x265 GP-M-NLgesproken"),
    ]
    bad = [
        ("Dikkie Dik Logeren", 2008, "Dikkie Dik Logeren 2012"),  # other year
        ("Dikkie Dik Twee poezen", 2008, "Dikkie Dik Logeren"),  # missing words
        ("Dikkie Dik en de verdwenen knuffel", 2024, "Dikkie Dik 2 Een Nieuwe Vriend Voor Dikkie Dik 2024"),
        ("Dikkie Dik Een nieuwe vriend", 2024, "Dikkie Dik 2 Een Nieuwe Vriend Voor Dikkie Dik 2024"),  # sequel number
        ("Up", 2009, "Grown Ups 2 2013"),
    ]
    for args in ok:
        assert matches(*args, 0.66), args
    for args in bad:
        assert not matches(*args, 0.66), args
    assert not matches("The Matrix", 1999, "The Matrix Reloaded", 0.66)
    assert matches("The Matrix", 1999, "The Matrix NL subs", 0.66)
    assert matches("Lekker Weertje Dikkie Dik", 0, "Dirk Scheele Lekker Weertje, Dikkie Dik", 0.6)
    assert not matches("Dikkie Dik Logeren", 2008, "Dikkie Dik Logeren en meer verhalen deel 2", 0.6)
    assert renamed("Dikkie Dik - Logeren", 2008, "Dikkie Dik Logeren", ["2000", "2030"]) == \
        "Dikkie Dik - Logeren 2008 DVDRip NL-Spotnet"
    assert renamed("Dikkie Dik en de verdwenen knuffel", 2024,
                   "Dikkie Dik en de verdwenen knuffel 1080p WEB DL HEVC AAC2 0 x265", ["2040"]) == \
        "Dikkie Dik en de verdwenen knuffel 2024 1080p web dl hevc aac2 0 x265 NL-Spotnet"
    assert parseable("The Matrix 1999 1080p BluRay", 1999) and not parseable("Dikkie Dik Logeren", 2008)
    rss = (b'<?xml version="1.0"?><rss xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/"><channel>'
           b'<item><title>Dikkie Dik Twee Poezen DVD</title><newznab:attr name="category" value="2030"/></item>'
           b'<item><title>Some Other Film</title></item></channel></rss>')
    _wanted.update(at=time.time(), movies=[("Dikkie Dik - Twee poezen", 2008, ["Dikkie Dik - Twee poezen"])])
    out = rewrite_rss(rss).decode()
    assert "Dikkie Dik - Twee poezen 2008 DVDRip NL-Spotnet" in out and "Some Other Film" in out, out
    assert split_year("Dikkie Dik Twee poezen 2008") == ("Dikkie Dik Twee poezen", 2008)
    assert split_year("Lekker Weertje Dikkie Dik") == ("Lekker Weertje Dikkie Dik", 0)
    pages = []

    def fake_urlopen(req, timeout):
        page = int(urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)["page"][0])
        pages.append(page)
        rec = {"monitored": True, "isAvailable": True, "title": "Dikkie Dik - Twee poezen", "year": 2008}
        return io.BytesIO(json.dumps({"totalRecords": WANTED_PAGE + 1,
                                      "records": [rec] * (WANTED_PAGE if page == 1 else 1)}).encode())
    globals().update(RADARR_URL="http://radarr", RADARR_API_KEY="x")
    _wanted["at"] = 0
    real, urllib.request.urlopen = urllib.request.urlopen, fake_urlopen
    try:
        assert len(wanted_movies()) == WANTED_PAGE + 1 and pages == [1, 2], pages
    finally:
        urllib.request.urlopen = real
    print("self-test passed")


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-test"]:
        self_test()
        sys.exit(0)
    print(f"spotweb-bridge on :{PORT} -> {UPSTREAM}", file=sys.stderr)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
