#!/usr/bin/env python3
"""Bus lane data: LTA's painted bus lane lines turned into directed stretches a device can match.

    python3 tools/buslanes.py build [--sources DIR] [--cached-roads]   rebuild from the latest data
    python3 tools/buslanes.py check                                    monthly: rebuild if a source changed

Sources:
  LTA Lane Marking (data.gov.sg)    the bus lane lines, normal and full-day
  LTA Traffic Sign (data.gov.sg)    "Bus Only Lane" signs, for lanes that are buses-only at all times
  MOM Public Holidays (data.gov.sg) days when normal and full-day lanes are not in operation
  OpenStreetMap roads (BBBike extract) which carriageway each line belongs to, and its direction

Each painted piece is snapped to the nearest parallel OSM road. Bus lanes run along the left
kerb, so paint left of a road's direction belongs to traffic going that way, and on a two-way
road paint right of it belongs to the other direction. Paint right of a one-way road (a
contraflow bus lane) or away from any road is left out and listed in review/buslanes_left_out.md.
The covered parts of each road are joined across gaps of up to GAP_M (junctions, bus bays) and
across road splits, and stored along the road's centre line, which is where a car's GPS fix is.

Writes data/buslanes.json, data/buslanes.bin, data/bus_lane_data.h and the "buslanes" block of
manifest.json. Standard library only.
"""
import argparse
import csv
import datetime
import gzip
import hashlib
import json
import math
import os
import struct
import sys
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build  # noqa: E402
import paths  # noqa: E402
from paths import to_latlon, to_xy  # noqa: E402

ROOT = build.ROOT
DATA = build.DATA
MANIFEST = os.path.join(ROOT, "manifest.json")
ROADS_CACHE = os.path.join(ROOT, "cache", "osm_roads.json.gz")
LEFT_OUT = os.path.join(ROOT, "review", "buslanes_left_out.md")
SUMMARY = os.path.join(ROOT, "review", "buslanes_summary.md")
FILES = ("data/buslanes.json", "data/buslanes.bin", "data/bus_lane_data.h")
VERSIONED = ("data/buslanes.json",)

DATASETS = {  # data.gov.sg dataset ids
    "lane_marking": "d_fa71cc0c433275f4d2b0133358cc4fbf",
    "traffic_sign": "d_bbf0132c7290d6838f82003972d933d5",
    "holidays": "d_8ef23381f9417e4d4254ee8b4dcdb176",
}
SOURCE_FILES = {"lane_marking": "lane_marking.geojson", "traffic_sign": "traffic_sign.geojson",
                "holidays": "holidays.csv"}
DATAGOV_META = "https://api-production.data.gov.sg/v2/public/api/datasets/{}/metadata"
DATAGOV_DOWNLOAD = "https://api-open.data.gov.sg/v1/public/api/datasets/{}/initiate-download"
UA = {"User-Agent": "sg-traffic-cameras/1.0"}
OSM_EXTRACT = "https://download.bbbike.org/osm/bbbike/Singapore/Singapore.osm.gz"  # weekly

# Lane marking codes (TYP_CD): L yellow bus lane line, A1 its dashed breaks; Q, Q1, Q2, A6, A7
# the red full-day lines and their breaks. A3 guide lines and S bus stop boxes aren't lanes.
NORMAL_CODES = {"L", "A1"}
FULL_DAY_CODES = {"Q", "Q1", "Q2", "A6", "A7"}
BUS_ONLY_SIGN = "[3505]"          # "Bus Only Lane (with graphics)"
BUS_LANE_SIGNS = ("[3001]", "[3301]", "[3233]", "[3504]")  # bus lane / operating time / full day

# Schedules, in the order of the type codes in the data. days: bit 0 = Monday .. bit 6 = Sunday.
SCHEDULES = [
    {"type": "normal", "name": "Normal bus lane", "days": 0b0011111, "not_on_holidays": True,
     "windows": [["07:30", "09:30"], ["17:00", "20:00"]]},
    {"type": "full_day", "name": "Full-day bus lane", "days": 0b0111111, "not_on_holidays": True,
     "windows": [["07:30", "23:00"]]},
    {"type": "bus_only", "name": "Bus-only lane", "days": 0b1111111, "not_on_holidays": False,
     "windows": [["00:00", "24:00"]]},
]
TYPE_INDEX = {s["type"]: i for i, s in enumerate(SCHEDULES)}

SAMPLE_M = 5.0        # paint is sampled this often
SNAP_MAX_M = 20.0     # a sample further than this from every parallel road is left out
PARALLEL_DEG = 30.0   # paint and road must run within this angle of each other
MIN_SIDE_M = 1.0      # closer to the centre line than this: can't tell which side
GAP_M = 60.0          # covered parts this close along a road are one stretch
TURN_MAX_DEG = 50.0   # stretches continue onto the next road only if it turns less than this
MIN_STRETCH_M = 50.0  # shorter stretches are dropped
SIMPLIFY_M = 2.0
MIN_REPORTED_M = 5.0  # shorter unplaced paint (dashes) isn't listed in the report
SIGN_NEAR_M = 30.0    # a sign belongs to a stretch passing this close...
SIGN_HEADING_DEG = 45.0  # ...in the direction the sign faces
CACHE_NEAR_M = 100.0  # roads this close to bus lane paint are kept in the cache

GUARD_LENGTH_DROP = 0.10  # monthly check: refuse to publish if total length drops more than this
GUARD_COUNT_CHANGE = 0.20  # ...or the stretch count changes more than this

BIN_MAGIC = b"SGBL"
BIN_FORMAT = 1


# --- Sources ---------------------------------------------------------------------------------------

def http_get(url, timeout=120):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def dataset_updated(key):
    meta = json.loads(http_get(DATAGOV_META.format(DATASETS[key]), 60))
    return meta["data"]["lastUpdatedAt"]


def download_sources(folder):
    """Download all three data.gov.sg files into folder. Returns {key: lastUpdatedAt}."""
    updated = {}
    for key, dsid in DATASETS.items():
        updated[key] = dataset_updated(key)
        for attempt in range(4):
            try:
                link = json.loads(http_get(DATAGOV_DOWNLOAD.format(dsid), 60))["data"]["url"]
                with open(os.path.join(folder, SOURCE_FILES[key]), "wb") as f:
                    f.write(http_get(link, 600))
                break
            except (OSError, ValueError, KeyError) as ex:
                print(f"  {key}: download failed ({ex})", file=sys.stderr)
                if attempt == 3:
                    raise
                time.sleep(10 * (attempt + 1))
    return updated


def load_paint(path):
    """[(cls, [(x, y), ...])] for every bus lane line; cls is "normal" or "full_day"."""
    with open(path) as f:
        features = json.load(f)["features"]
    out = []
    for feat in features:
        code = (feat.get("properties") or {}).get("TYP_CD")
        if code in NORMAL_CODES:
            cls = "normal"
        elif code in FULL_DAY_CODES:
            cls = "full_day"
        else:
            continue
        geom = feat.get("geometry") or {}
        parts = [geom.get("coordinates") or []] if geom.get("type") == "LineString" else geom.get("coordinates") or []
        for part in parts:
            pts = [to_xy(lat, lon) for lon, lat, *_ in part]
            if len(pts) >= 2:
                out.append((cls, pts))
    return out


def load_signs(path):
    """(bus-only signs, bus lane signs), each [(x, y, bearing)]."""
    with open(path) as f:
        features = json.load(f)["features"]
    only, lane = [], []
    for feat in features:
        p = feat.get("properties") or {}
        code = str(p.get("TYP_CD") or "")
        if BUS_ONLY_SIGN in code:
            target = only
        elif any(c in code for c in BUS_LANE_SIGNS):
            target = lane
        else:
            continue
        lon, lat = feat["geometry"]["coordinates"][:2]
        target.append((*to_xy(lat, lon), float(p.get("BEARG_NUM") or 0.0)))
    return only, lane


def load_holidays(path, since_year):
    with open(path, newline="", encoding="utf-8") as f:
        days = sorted({row["date"] for row in csv.DictReader(f) if row.get("date", "")[:4].isdigit()})
    return [d for d in days if int(d[:4]) >= since_year]


# --- OSM roads ------------------------------------------------------------------------------------

def slim_way(el):
    """The parts of an OSM way this script uses."""
    tags = el.get("tags", {})
    keep = {k: v for k, v in tags.items()
            if k in ("highway", "name", "ref", "oneway", "junction") or "bus" in k}
    return {"type": "way", "id": el["id"], "tags": keep, "nodes": el["nodes"],
            "geometry": [{"lat": round(g["lat"], 7), "lon": round(g["lon"], 7)} for g in el["geometry"]]}


def road_classes():
    return set(paths.ROAD_CLASSES.split("|"))


def fetch_roads(path=None):
    """Drivable roads from BBBike's weekly Singapore extract of OpenStreetMap (OSM XML), in the
    same shape Overpass gives (id, tags, nodes, geometry)."""
    if path is None:
        with tempfile.NamedTemporaryFile(suffix=".osm.gz", delete=False) as f:
            path = f.name
        try:
            for attempt in range(4):
                try:
                    with open(path, "wb") as out:
                        out.write(http_get(OSM_EXTRACT, 600))
                    break
                except OSError as ex:
                    print(f"  OSM extract: download failed ({ex})", file=sys.stderr)
                    if attempt == 3:
                        raise RuntimeError(f"OSM extract unavailable: {ex}")
                    time.sleep(30 * (attempt + 1))
            return fetch_roads(path)
        finally:
            os.remove(path)
    classes = road_classes()
    nodes, ways = {}, []
    with gzip.open(path) as f:
        for _event, el in ET.iterparse(f):
            if el.tag == "node":
                nodes[int(el.get("id"))] = (float(el.get("lat")), float(el.get("lon")))
            elif el.tag == "way":
                tags = {t.get("k"): t.get("v") for t in el.iter("tag")}
                if tags.get("highway") in classes:
                    refs = [int(n.get("ref")) for n in el.iter("nd")]
                    if len(refs) >= 2 and all(r in nodes for r in refs):
                        ways.append(slim_way({"id": int(el.get("id")), "tags": tags, "nodes": refs,
                                              "geometry": [{"lat": nodes[r][0], "lon": nodes[r][1]} for r in refs]}))
            elif el.tag == "relation":
                break  # ways are done
            if el.tag in ("node", "way"):
                el.clear()
    return ways


def cache_roads(ways, paint):
    """Save the roads near bus lane paint, so a later build can run without OSM."""
    cells = set()
    for _cls, pts in paint:
        for x, y in resample(pts, 25.0):
            cells.add((int(x // CACHE_NEAR_M), int(y // CACHE_NEAR_M)))
    near = []
    for w in ways:
        for g in w["geometry"]:
            x, y = to_xy(g["lat"], g["lon"])
            cx, cy = int(x // CACHE_NEAR_M), int(y // CACHE_NEAR_M)
            if any((cx + dx, cy + dy) in cells for dx in (-1, 0, 1) for dy in (-1, 0, 1)):
                near.append(w)
                break
    os.makedirs(os.path.dirname(ROADS_CACHE), exist_ok=True)
    raw = json.dumps({"elements": sorted(near, key=lambda w: w["id"])}, separators=(",", ":")).encode()
    with gzip.GzipFile(ROADS_CACHE, "wb", mtime=0) as f:
        f.write(raw)


def cached_roads():
    with gzip.open(ROADS_CACHE) as f:
        return json.load(f)["elements"]


class Road:
    """An OSM road, with one-way roads that run against their nodes turned around."""

    def __init__(self, el):
        r = paths.Road(el)
        self.id, self.name, self.highway = r.id, r.name, r.highway
        self.pts, self.nodes, self.oneway = r.pts, r.nodes, r.oneway
        if self.oneway == -1:
            self.pts, self.nodes, self.oneway = self.pts[::-1], self.nodes[::-1], 1
        tags = el.get("tags", {})
        self.bus_tagged = any("bus" in k for k in tags)
        # One-way roads that OSM says have a bus lane against the traffic.
        self.contraflow = self.oneway == 1 and (
            tags.get("oneway:bus") == "no" or any("opposite" in v for k, v in tags.items() if "busway" in k) or
            any(k.endswith(":backward") and "bus" in k for k in tags))
        self.cum = [0.0]
        for a, b in zip(self.pts, self.pts[1:]):
            self.cum.append(self.cum[-1] + math.hypot(b[0] - a[0], b[1] - a[1]))
        self.length = self.cum[-1]

    def point_at(self, s):
        s = max(0.0, min(self.length, s))
        for i in range(len(self.pts) - 1):
            if s <= self.cum[i + 1] or i == len(self.pts) - 2:
                seg = self.cum[i + 1] - self.cum[i]
                t = 0.0 if seg == 0 else (s - self.cum[i]) / seg
                (ax, ay), (bx, by) = self.pts[i], self.pts[i + 1]
                return (ax + (bx - ax) * t, ay + (by - ay) * t)
        return self.pts[-1]

    def between(self, s0, s1):
        """Points from s0 to s1 along the road (s1 < s0 runs backwards)."""
        lo, hi = min(s0, s1), max(s0, s1)
        pts = [self.point_at(lo)] + [p for p, c in zip(self.pts, self.cum) if lo < c < hi] + [self.point_at(hi)]
        return pts if s1 >= s0 else pts[::-1]


def road_grid(roads, cell=50.0):
    grid = defaultdict(list)
    for ri, r in enumerate(roads):
        for si in range(len(r.pts) - 1):
            (ax, ay), (bx, by) = r.pts[si], r.pts[si + 1]
            for gx in range(int(min(ax, bx) // cell), int(max(ax, bx) // cell) + 1):
                for gy in range(int(min(ay, by) // cell), int(max(ay, by) // cell) + 1):
                    grid[(gx, gy)].append((ri, si))
    return grid, cell


# --- Geometry -------------------------------------------------------------------------------------

def resample(pts, step):
    """Points every `step` metres along a line (the middle of each step)."""
    out = []
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        seg = math.hypot(bx - ax, by - ay)
        n = max(1, int(round(seg / step)))
        out += [(ax + (bx - ax) * (k + 0.5) / n, ay + (by - ay) * (k + 0.5) / n) for k in range(n)]
    return out


def samples_with_direction(pts):
    out = []
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        seg = math.hypot(bx - ax, by - ay)
        if seg < 0.2:
            continue
        n = max(1, int(round(seg / SAMPLE_M)))
        ux, uy = (bx - ax) / seg, (by - ay) / seg
        out += [((ax + (bx - ax) * (k + 0.5) / n, ay + (by - ay) * (k + 0.5) / n), (ux, uy), seg / n)
                for k in range(n)]
    return out


def heading(a, b):
    return paths.bearing_of(a[0], a[1], b[0], b[1])


def length_of(pts):
    return paths.path_len(pts)


# --- Building stretches ---------------------------------------------------------------------------

def snap_sample(p, u, roads, grid):
    """(road index, direction, position along the road) or a reason the sample can't be placed."""
    gridmap, cell = grid
    gx, gy = int(p[0] // cell), int(p[1] // cell)
    reach = int(SNAP_MAX_M // cell) + 1
    best = None
    for cx in range(gx - reach, gx + reach + 1):
        for cy in range(gy - reach, gy + reach + 1):
            for ri, si in gridmap.get((cx, cy), ()):
                (ax, ay), (bx, by) = roads[ri].pts[si], roads[ri].pts[si + 1]
                seg = math.hypot(bx - ax, by - ay)
                if seg < 0.1:
                    continue
                cos = abs(u[0] * (bx - ax) + u[1] * (by - ay)) / seg
                if cos < math.cos(math.radians(PARALLEL_DEG)):
                    continue
                d, t, _qx, _qy = paths.project(p[0], p[1], ax, ay, bx, by)
                if d <= SNAP_MAX_M and (best is None or d < best[0]):
                    side = ((bx - ax) * (p[1] - ay) - (by - ay) * (p[0] - ax)) / seg  # > 0: left
                    best = (d, ri, si, t, side)
    if best is None:
        return "no road"
    _d, ri, si, t, side = best
    road = roads[ri]
    s = road.cum[si] + t * (road.cum[si + 1] - road.cum[si])
    if road.oneway == 1:
        # A one-way carriageway's own lane lines can sit either side of its centre line
        # (on a two-lane one the bus lane line is the centre); only a road OSM tags with a
        # contraflow bus lane has one on the right.
        if road.contraflow and side < -MIN_SIDE_M:
            return "contraflow"
        return (ri, 1, s)
    if abs(side) < MIN_SIDE_M:
        return "on the centre line"
    return (ri, 1 if side > 0 else -1, s)


def intervals_of(positions, length):
    """Sorted positions along a road -> [(s0, s1)] covered, joining gaps up to GAP_M."""
    out = []
    for s in sorted(positions):
        if out and s - out[-1][1] <= GAP_M:
            out[-1][1] = s
        else:
            out.append([s, s])
    half = SAMPLE_M / 2
    return [(max(0.0, a - half), min(length, b + half)) for a, b in out]


def subtract(intervals, minus):
    out = []
    for a, b in intervals:
        pieces = [(a, b)]
        for c, d in minus:
            pieces = [q for (x, y) in pieces for q in ((x, min(y, c)), (max(x, d), y)) if q[1] - q[0] > 1.0]
        out += pieces
    return out


def build_stretches(paint, roads):
    grid = road_grid(roads)
    covered = defaultdict(list)       # (road, dir, cls) -> positions
    left_out = []                     # (reason, length, (x, y))
    snapped_pieces = 0
    for cls, pts in paint:
        samples = samples_with_direction(pts)
        if not samples:
            continue
        results = [(snap_sample(p, u, roads, grid), w, p) for p, u, w in samples]
        good = [r for r, _, _ in results if not isinstance(r, str)]
        if good:
            snapped_pieces += 1
        for ri, direction, s in good:
            covered[(ri, direction, cls)].append(s)
        # Paint that couldn't be placed, for the report (not single dashes of a broken line).
        if length_of(pts) >= MIN_REPORTED_M:
            left_out += [(r, w, p) for r, w, p in results if isinstance(r, str)]

    # Covered intervals per road and direction; full-day takes precedence where both are painted.
    ivals = {}
    for (ri, direction, cls), positions in covered.items():
        ivals[(ri, direction, cls)] = intervals_of(positions, roads[ri].length)
    for (ri, direction, cls) in list(ivals):
        if cls == "normal" and (ri, direction, "full_day") in ivals:
            ivals[(ri, direction, cls)] = subtract(ivals[(ri, direction, cls)], ivals[(ri, direction, "full_day")])

    # Each interval in travel order: (road, dir, cls, start s, end s).
    parts = [(ri, d, cls, a, b) if d > 0 else (ri, d, cls, b, a)
             for (ri, d, cls), lst in ivals.items() for a, b in lst]
    by_road = defaultdict(list)
    for k, part in enumerate(parts):
        by_road[(part[0], part[1], part[2])].append(k)
    node_roads = defaultdict(list)
    for ri, r in enumerate(roads):
        for idx, nid in enumerate(r.nodes):
            node_roads[nid].append((ri, idx))

    def to_end(part):
        ri, d, _cls, _s, e = part
        r = roads[ri]
        return (r.length - e, r.nodes[-1], r.length) if d > 0 else (e, r.nodes[0], 0.0)

    def end_heading(part):
        ri, _d, _cls, s, e = part
        r = roads[ri]
        return heading(r.point_at(e - math.copysign(min(10.0, abs(e - s)), e - s)), r.point_at(e))

    def start_heading(part):
        ri, _d, _cls, s, e = part
        r = roads[ri]
        return heading(r.point_at(s), r.point_at(s + math.copysign(min(10.0, abs(e - s)), e - s)))

    successor = {}
    for k, part in enumerate(parts):
        remaining, node, _ = to_end(part)
        if remaining > GAP_M:
            continue
        best = None
        for ri2, idx in node_roads[node]:
            if ri2 == part[0]:
                continue
            r2 = roads[ri2]
            s_node = r2.cum[idx]
            for d2 in (1, -1):
                if r2.oneway == 1 and d2 < 0:
                    continue
                for k2 in by_road.get((ri2, d2, part[2]), ()):
                    _ri, _d, _c, s2, e2 = parts[k2]
                    ahead = (s2 - s_node) * d2
                    if ahead < -1.0 or remaining + ahead > GAP_M:
                        continue
                    turn = paths.angle_diff(end_heading(part), start_heading((ri2, d2, part[2], s_node, e2)))
                    if turn > TURN_MAX_DEG:
                        continue
                    gap = remaining + max(0.0, ahead)
                    if best is None or gap < best[0]:
                        best = (gap, k2, s_node)
        if best:
            successor[k] = best
    # Two roads joining into one: keep the closer predecessor.
    chosen = {}
    for k, (gap, k2, s_node) in successor.items():
        if k2 not in chosen or gap < chosen[k2][0]:
            chosen[k2] = (gap, k, s_node)
    nxt = {k: (k2, s_node) for k2, (_gap, k, s_node) in chosen.items()}
    has_prev = {k2 for k2 in chosen}

    stretches, used = [], set()
    starts = [k for k in range(len(parts)) if k not in has_prev] + list(range(len(parts)))
    for start in starts:
        if start in used:
            continue
        chain, k, s_from = [], start, None
        while k is not None and k not in used:
            used.add(k)
            ri, d, cls, s, e = parts[k]
            if s_from is not None:
                s = s_from          # continue from the shared node
            follow = nxt.get(k)
            if follow:
                e = to_end(parts[k])[2]  # run on to the end of this road
            chain.append((ri, s, e))
            k, s_from = (follow if follow else (None, None))
        pts = []
        for ri, s, e in chain:
            seg = roads[ri].between(s, e)
            pts += seg[1:] if pts else seg
        if length_of(pts) < MIN_STRETCH_M:
            continue
        names = Counter(roads[ri].name for ri, _, _ in chain if roads[ri].name)
        tagged = sum(abs(e - s) for ri, s, e in chain if roads[ri].bus_tagged)
        stretches.append({"type": parts[start][2], "road": names.most_common(1)[0][0] if names else "",
                          "xy": paths.simplify(pts, SIMPLIFY_M), "osm_bus_tagged_m": tagged})
    return stretches, left_out, snapped_pieces


def nearest_on(stretch, x, y):
    """(distance, travel heading there) from a point to a stretch."""
    best = None
    pts = stretch["xy"]
    for a, b in zip(pts, pts[1:]):
        d, _t, _qx, _qy = paths.project(x, y, a[0], a[1], b[0], b[1])
        if best is None or d < best[0]:
            best = (d, heading(a, b))
    return best


def match_signs(stretches, signs):
    """Per sign: (stretch index, heading difference) of the closest stretch, or None."""
    out = []
    for x, y, bearing in signs:
        best = None
        for i, st in enumerate(stretches):
            xs = [p[0] for p in st["xy"]]
            ys = [p[1] for p in st["xy"]]
            if not (min(xs) - SIGN_NEAR_M <= x <= max(xs) + SIGN_NEAR_M and
                    min(ys) - SIGN_NEAR_M <= y <= max(ys) + SIGN_NEAR_M):
                continue
            d, h = nearest_on(st, x, y)
            if d <= SIGN_NEAR_M:
                diff = paths.angle_diff(h, bearing)
                if best is None or diff < best[1]:
                    best = (i, diff)
        out.append(best)
    return out


# --- Outputs --------------------------------------------------------------------------------------

def minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def finished(stretches):
    """Stretches in a stable order with ids, lat/lon paths and lengths."""
    out = []
    for st in stretches:
        path = [paths.ll(p) for p in st["xy"]]
        out.append({"type": st["type"], "road": st["road"], "length_m": round(length_of(st["xy"])), "path": path})
    out.sort(key=lambda s: (s["path"][0][0], s["path"][0][1]))
    for i, s in enumerate(out, 1):
        s["id"] = i
    return [{"id": s["id"], "type": s["type"], "road": s["road"], "length_m": s["length_m"], "path": s["path"]}
            for s in out]


def buslanes_json(data):
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def packed(data):
    points, rows = [], []
    for s in data["stretches"]:
        rows.append((len(points), len(s["path"]), TYPE_INDEX[s["type"]]))
        points += [(round(lat * 1e6), round(lon * 1e6)) for lat, lon in s["path"]]
    if len(data["stretches"]) > 65535 or any(n > 65535 for _f, n, _t in rows):
        raise ValueError("too many stretches or points")
    schedules = []
    for s in data["schedules"]:
        w = [(minutes(a), minutes(b)) for a, b in s["windows"]] + [(0, 0)] * (2 - len(s["windows"]))
        schedules.append((s["days"], 1 if s["not_on_holidays"] else 0, len(s["windows"]), w))
    holidays = [int(d.replace("-", "")) for d in data["holidays"]]
    return schedules, holidays, points, rows


def buslanes_bin(data):
    """Little-endian: 16-byte header ("SGBL", uint16 format, uint16 schedule count, uint16 holiday
    count, uint16 stretch count, uint32 point count), schedules (uint8 days, uint8 flags, uint8 window
    count, uint8 0, uint16 start minute x2, uint16 end minute x2), holidays (uint32 yyyymmdd), points
    (int32 latE6, int32 lonE6), stretches (uint32 first point, uint16 point count, uint8 schedule, 0)."""
    schedules, holidays, points, rows = packed(data)
    out = struct.pack("<4sHHHHI", BIN_MAGIC, BIN_FORMAT, len(schedules), len(holidays), len(rows), len(points))
    for days, flags, n, w in schedules:
        out += struct.pack("<BBBBHHHH", days, flags, n, 0, w[0][0], w[1][0], w[0][1], w[1][1])
    out += b"".join(struct.pack("<I", d) for d in holidays)
    out += b"".join(struct.pack("<ii", a, b) for a, b in points)
    out += b"".join(struct.pack("<IHBB", first, n, t, 0) for first, n, t in rows)
    return out


def bus_lane_header(data, version):
    schedules, holidays, points, rows = packed(data)
    out = [
        "#pragma once",
        "#include <stdint.h>",
        "",
        "// Generated by tools/buslanes.py. Do not edit by hand.",
        "// Source: LTA Lane Marking and Traffic Sign, MOM public holidays (data.gov.sg), OpenStreetMap roads.",
        "",
        "// Micro-degrees. A stretch's points run in the direction of travel.",
        "struct BusLanePoint {",
        "    int32_t latE6;",
        "    int32_t lonE6;",
        "};",
        "",
        "// When a lane is in operation. days: bit 0 = Monday .. bit 6 = Sunday. Minutes from midnight SGT.",
        "struct BusLaneSchedule {",
        "    uint8_t days;",
        "    uint8_t flags;        // bit 0: not in operation on public holidays",
        "    uint8_t windowCount;  // 1 or 2",
        "    uint8_t reserved;",
        "    uint16_t startMin[2];",
        "    uint16_t endMin[2];",
        "};",
        "",
        "struct BusLaneStretch {",
        "    uint32_t firstPoint;  // index of its first point in kBusLanePoints",
        "    uint16_t pointCount;",
        "    uint8_t schedule;     // index into kBusLaneSchedules: 0 normal, 1 full-day, 2 bus-only",
        "    uint8_t reserved;",
        "};",
        "",
        f"static constexpr uint32_t kBusLaneDataVersion = {version};  // manifest.json buslanes.version",
        f"static constexpr uint16_t kBusLaneScheduleCount = {len(schedules)};",
        f"static constexpr uint16_t kBusLaneHolidayCount = {len(holidays)};",
        f"static constexpr uint16_t kBusLaneStretchCount = {len(rows)};",
        f"static constexpr uint32_t kBusLanePointCount = {len(points)};",
        "",
        "static const BusLaneSchedule kBusLaneSchedules[kBusLaneScheduleCount] = {",
    ]
    for (days, flags, n, w), s in zip(schedules, data["schedules"]):
        out.append(f"    {{0x{days:02X}, {flags}, {n}, 0, {{{w[0][0]}, {w[1][0]}}}, {{{w[0][1]}, {w[1][1]}}}}},"
                   f"  // {s['name']}")
    out += ["};", "", "// yyyymmdd", "static const uint32_t kBusLaneHolidays[kBusLaneHolidayCount] = {"]
    for i in range(0, len(holidays), 8):
        out.append("    " + ", ".join(str(d) for d in holidays[i:i + 8]) + ",")
    out += ["};", "", "static const BusLanePoint kBusLanePoints[kBusLanePointCount] = {"]
    for s, (first, n, _t) in zip(data["stretches"], rows):
        pts = ", ".join(f"{{{a}, {b}}}" for a, b in points[first:first + n])
        out.append(f"    {pts},  // #{s['id']}")
    out += ["};", "", "static const BusLaneStretch kBusLaneStretches[kBusLaneStretchCount] = {"]
    for s, (first, n, t) in zip(data["stretches"], rows):
        out.append(f"    {{{first}, {n}, {t}, 0}},  // #{s['id']} {s['road']}")
    out += ["};", ""]
    return "\n".join(out)


def write_left_out(left_out):
    """Group left-out paint into places about 100 m across, biggest first."""
    places = []
    for reason, length, (x, y) in left_out:
        for p in places:
            if math.hypot(p["x"] - x, p["y"] - y) <= 100.0:
                p["length"] += length
                p["reasons"][reason] += length
                break
        else:
            places.append({"x": x, "y": y, "length": length, "reasons": Counter({reason: length})})
    places.sort(key=lambda p: -p["length"])
    total = sum(p["length"] for p in places)
    lines = [
        "# Bus lane paint left out",
        "",
        f"{len(places)} places, {total / 1000:.1f} km of paint in all. These lines couldn't be tied to one "
        "carriageway and direction, so devices don't warn for them. Most are contraflow bus lanes, lanes inside "
        "bus interchanges and paint on roads OpenStreetMap doesn't have. Nothing here needs fixing.",
        "",
        "| Place | Paint | Why |",
        "|---|---|---|",
    ]
    why = {"no road": "no road in OpenStreetMap", "contraflow": "against a one-way road (contraflow)",
           "on the centre line": "too close to the road's centre line to tell the side"}
    for p in places:
        lat, lon = to_latlon(p["x"], p["y"])
        link = f"https://www.openstreetmap.org/?mlat={lat:.5f}&mlon={lon:.5f}#map=19/{lat:.5f}/{lon:.5f}"
        lines.append(f"| [{lat:.5f}, {lon:.5f}]({link}) | {round(p['length'])} m | "
                     f"{why.get(p['reasons'].most_common(1)[0][0], p['reasons'].most_common(1)[0][0])} |")
    os.makedirs(os.path.dirname(LEFT_OUT), exist_ok=True)
    with open(LEFT_OUT, "w") as f:
        f.write("\n".join(lines) + "\n")
    return len(places), total


def totals(data):
    return len(data["stretches"]), sum(s["length_m"] for s in data["stretches"])


def load_published():
    path = os.path.join(DATA, "buslanes.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def write_outputs(data, sources):
    """Write the data files and the manifest's buslanes block. Returns True if the data changed."""
    contents = {"data/buslanes.json": buslanes_json(data).encode(), "data/buslanes.bin": buslanes_bin(data)}
    with open(MANIFEST) as f:
        manifest = json.load(f)
    old = manifest.get("files", {})
    block = manifest.get("buslanes", {})
    changed = any(old.get(n, {}).get("sha256") != hashlib.sha256(contents[n]).hexdigest() for n in VERSIONED)
    d = build.today()
    if changed or "version" not in block:
        block["version"] = max(int(d.strftime("%Y%m%d")), block.get("version", 0) + 1)
        block["updated"] = d.isoformat()
    contents["data/bus_lane_data.h"] = bus_lane_header(data, block["version"]).encode()
    files = dict(old)
    for name in FILES:
        raw = contents[name]
        with open(os.path.join(ROOT, name), "wb") as f:
            f.write(raw)
        files[name] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    count, length = totals(data)
    block.update({"stretches": count, "points": sum(len(s["path"]) for s in data["stretches"]),
                  "length_km": round(length / 1000, 1), "sources": sources})
    manifest["files"] = files
    manifest["buslanes"] = block
    with open(MANIFEST, "w") as f:
        f.write(json.dumps(manifest, indent=2) + "\n")
    return changed


def make(folder, sources, roads_from_cache, osm_file=None):
    print("Reading LTA data", file=sys.stderr)
    paint = load_paint(os.path.join(folder, SOURCE_FILES["lane_marking"]))
    only_signs, lane_signs = load_signs(os.path.join(folder, SOURCE_FILES["traffic_sign"]))
    holidays = load_holidays(os.path.join(folder, SOURCE_FILES["holidays"]), build.today().year - 1)
    if roads_from_cache:
        ways = cached_roads()
    else:
        try:
            ways = fetch_roads(osm_file)
            cache_roads(ways, paint)
        except RuntimeError as ex:
            if not os.path.exists(ROADS_CACHE):
                raise
            print(f"OSM unavailable ({ex}); using the cached roads", file=sys.stderr)
            ways = cached_roads()
    roads = [Road(w) for w in ways if len(w.get("geometry", [])) >= 2]
    print(f"Building stretches from {len(paint)} paint lines and {len(roads)} roads", file=sys.stderr)
    stretches, left_out, snapped = build_stretches(paint, roads)

    # Bus-only lanes: the stretch a "Bus Only Lane" sign faces down.
    bus_only = 0
    for m in match_signs(stretches, only_signs):
        if m and m[1] <= SIGN_HEADING_DEG and stretches[m[0]]["type"] != "bus_only":
            stretches[m[0]]["type"] = "bus_only"
            bus_only += 1
    # Bus lane signs as a check on the directions worked out from the paint.
    lane_matches = [m for m in match_signs(stretches, lane_signs) if m]
    agree = sum(1 for m in lane_matches if m[1] <= SIGN_HEADING_DEG)
    opposite = sum(1 for m in lane_matches if m[1] >= 180 - SIGN_HEADING_DEG)

    tagged = sum(st["osm_bus_tagged_m"] for st in stretches)
    data = {"schedules": SCHEDULES, "holidays": holidays, "stretches": finished(stretches)}
    places, left_m = write_left_out(left_out)
    count, length = totals(data)
    by_type = Counter()
    for s in data["stretches"]:
        by_type[s["type"]] += s["length_m"]
    stats = [
        f"- {count} stretches, {length / 1000:.1f} km: " +
        ", ".join(f"{SCHEDULES[TYPE_INDEX[t]]['name'].lower()} {by_type[t] / 1000:.1f} km" for t in TYPE_INDEX),
        f"- {bus_only} bus-only stretches from {len(only_signs)} \"Bus Only Lane\" signs",
        f"- Direction check: {agree} of {len(lane_matches)} bus lane signs face along their stretch "
        f"({opposite} face against it)",
        f"- OpenStreetMap tags a bus lane on {100 * tagged / max(1, length):.0f}% of the stretch length",
        f"- {snapped} of {len(paint)} paint lines placed; {left_m / 1000:.1f} km of paint left out in {places} "
        "places (see review/buslanes_left_out.md)",
        f"- Public holidays from {holidays[0] if holidays else '-'} to {holidays[-1] if holidays else '-'}",
    ]
    return data, stats


def output(**kv):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            for k, v in kv.items():
                f.write(f"{k}={v}\n")


def write_summary(lines):
    os.makedirs(os.path.dirname(SUMMARY), exist_ok=True)
    with open(SUMMARY, "w") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))


def guard_problem(old, new):
    """Why the new data looks broken compared with the published data, or None."""
    if not old or not old.get("stretches"):
        return None
    old_count, old_len = totals(old)
    new_count, new_len = totals(new)
    if new_len < old_len * (1 - GUARD_LENGTH_DROP):
        return f"total length fell from {old_len / 1000:.1f} km to {new_len / 1000:.1f} km"
    if abs(new_count - old_count) > old_count * GUARD_COUNT_CHANGE:
        return f"stretch count changed from {old_count} to {new_count}"
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=("build", "check"))
    ap.add_argument("--sources", help="folder with already downloaded LTA/MOM files (skips the download)")
    ap.add_argument("--cached-roads", action="store_true", help="use cache/osm_roads.json.gz instead of OSM")
    ap.add_argument("--osm", help="an already downloaded Singapore.osm.gz (skips the download)")
    ap.add_argument("--force", action="store_true", help="check: publish even if the safety check fails")
    args = ap.parse_args()

    with open(MANIFEST) as f:
        published_sources = json.load(f).get("buslanes", {}).get("sources", {})
    if args.command == "check" and not args.sources:
        latest = {key: dataset_updated(key) for key in DATASETS}
        if latest == published_sources:
            write_summary([f"No changes: LTA and MOM data unchanged since the last build ({build.today()})."])
            output(data_changed="false", blocked="false")
            return 0

    with tempfile.TemporaryDirectory() as tmp:
        folder = args.sources or tmp
        if args.sources:  # files downloaded just before, so their datasets' current dates apply
            try:
                sources = {key: dataset_updated(key) for key in DATASETS}
            except (OSError, ValueError, KeyError):
                sources = published_sources
        else:
            sources = download_sources(folder)
        data, stats = make(folder, sources, args.cached_roads, args.osm)
    problem = guard_problem(load_published(), data) if args.command == "check" and not args.force else None
    if problem:
        write_summary(["Bus lane data NOT published: " + problem + ".", "",
                       "This usually means a broken LTA file. Check the source, then run the workflow "
                       "again with force if the change is real.", "", *stats])
        output(data_changed="false", blocked="true")
        return 1
    changed = write_outputs(data, sources)
    write_summary([("Bus lane data updated" if changed else "Bus lane data unchanged") + f" ({build.today()}).",
                   "", *stats])
    output(data_changed=str(changed).lower(), blocked="false")
    return 0


if __name__ == "__main__":
    sys.exit(main())
