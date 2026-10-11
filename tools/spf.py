"""Fetch and parse the Singapore Police Force enforcement camera pages."""
import html
import re
import urllib.request

PAGES = (
    "https://www.police.gov.sg/knowledge-hub/traffic/traffic-matters/red-light-camera-locations",
    "https://www.police.gov.sg/Knowledge-Hub/Traffic/Traffic-Matters/Speed-Enforcement-Camera-Locations",
    "https://www.police.gov.sg/knowledge-hub/traffic/traffic-matters/traffic-violation-enforcement-camera-locations",
)
# Section heading on the SPF pages -> (type code, category)
SECTIONS = {
    "red light camera locations": (1, "Red light"),
    "fixed speed camera locations": (2, "Fixed speed"),
    "mobile speed camera locations": (3, "Mobile speed"),
    "average speed camera locations": (4, "Average speed"),
    "police speed laser camera locations": (5, "Police speed laser"),
}
TVEC = (6, "Traffic violation (TVEC)")
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

_TOKEN = re.compile(r"<p\b[^>]*>(.*?)</p>|<table\b.*?</table>", re.S | re.I)
_ROW = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.S | re.I)
_CELL = re.compile(r"<td\b[^>]*>(.*?)</td>", re.S | re.I)
_COORD = re.compile(r"(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)")


def text(s):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s)).split())


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def parse(page, default=None):
    """Yield dicts with type, category, location, lat, lon from one SPF page."""
    section = default
    for m in _TOKEN.finditer(page):
        if m.group(1) is not None:
            t = text(m.group(1)).lower()
            if t in SECTIONS:
                section = SECTIONS[t]
            continue
        if section is None:
            continue
        location = None
        for row in _ROW.findall(m.group(0)):
            cells = [text(c) for c in _CELL.findall(row)]
            if any(c.lower() == "location" for c in cells):
                location = None  # header row
                continue
            coords = [c for cell in cells for c in _COORD.findall(cell)]
            words = [c for c in cells if c and not c.rstrip(".").isdigit() and not _COORD.search(c)
                     and not c.lower().startswith("camera ")]
            if words:
                location = words[0]
            # Some locations span several rows ("Camera A | Camera B", or one row per camera with
            # the location cell merged), so rows with only coordinates reuse the last location.
            for lat, lon in coords:
                if location is None:
                    raise ValueError(f"coordinates without a location: {cells}")
                yield {"type": section[0], "category": section[1], "location": location,
                       "lat": float(lat), "lon": float(lon)}


def fetch_all(pages=None):
    """All cameras on the three SPF pages. pages: optional {url: html} to skip fetching."""
    out = []
    for url in PAGES:
        page = pages[url] if pages else fetch(url)
        out += list(parse(page, TVEC if "violation" in url else None))
    return out
