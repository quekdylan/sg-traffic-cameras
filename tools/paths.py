"""Best-guess approach paths for new cameras, from OpenStreetMap roads.

Snaps a camera to the nearest roads, traces each road back against the direction of
travel for about 400 m, and picks the likeliest one as the default. The guesses are
checked by hand in the path editor (editor/index.html) before they're published.
Standard library only.
"""
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict

OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
ROAD_CLASSES = (
    "motorway|trunk|primary|secondary|tertiary|unclassified|residential|"
    "motorway_link|trunk_link|primary_link|secondary_link|tertiary_link|living_street"
)
AREA_M = 800.0          # fetch roads this far around a camera

RED_LIGHT_TYPES = {"1"}
SNAP_MAX_M = 40.0         # no road within this -> flag
SNAP_GOOD_M = 15.0        # snapped closer than this -> trustworthy position
RIVAL_MARGIN_M = 8.0      # another road this much farther than the best -> ambiguous
SIDE_GAP_M = 2.5          # dual carriageway: nearer side must win by this much
JUNCTION_SEARCH_M = 120.0 # red light: look this far along the road for the junction
GRID_M = 100.0

APPROACH_M = 400.0      # length of each approach path
CAND_RADIUS_M = 30.0    # roads this close to a camera become candidates
EXTRA_RADIUS_M = 60.0   # further roads offered as extra options (e.g. the far carriageway)
MAX_EXTRA = 4
MAX_CANDS = 6
MAX_TURN_DEG = 60.0     # stop tracing where the road turns harder than this
SIMPLIFY_M = 3.0        # Douglas-Peucker tolerance for stored paths

LAT0, LON0 = 1.3521, 103.8198
M_PER_DEG_LAT = 110574.0
M_PER_DEG_LON = 111320.0 * math.cos(math.radians(LAT0))


def fetch_area(lat, lon, radius=AREA_M):
    dlat, dlon = radius / M_PER_DEG_LAT, radius / M_PER_DEG_LON
    bbox = f"{lat - dlat:.5f},{lon - dlon:.5f},{lat + dlat:.5f},{lon + dlon:.5f}"
    query = f'[out:json][timeout:90];way["highway"~"^({ROAD_CLASSES})$"]({bbox});out body geom;'
    data = urllib.parse.urlencode({"data": query}).encode()
    for attempt in range(3):
        for url in OVERPASS_URLS:
            req = urllib.request.Request(url, data=data, headers={"User-Agent": "sg-traffic-cameras/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.load(r)
            except (OSError, ValueError) as ex:
                print(f"  {url} failed: {ex}", file=sys.stderr)
        time.sleep(30 * (attempt + 1))
    raise RuntimeError("all Overpass endpoints failed")


def suggest(cam):
    """Candidate approach paths for one camera {type, lat, lon}.

    Returns {cands, def, conf, roads}: candidate paths (each with pts from the start of the
    approach to the camera), the index of the likeliest one or None, a confidence, and the
    nearby roads for drawing in the editor.
    """
    roads, junctions, grid = load_roads(fetch_area(cam["lat"], cam["lon"]))
    nidx = node_index(roads)
    snap, _ = classify({"type": str(cam["type"]), "lat": cam["lat"], "lon": cam["lon"],
                        "lat_raw": repr(cam["lat"]), "lon_raw": repr(cam["lon"])}, roads, junctions, grid)
    cands, base = build_candidates(to_xy(cam["lat"], cam["lon"]), roads, grid, nidx)
    choice = default_choice(cands[:base], snap)
    for c in cands:
        del c["_tb"]
    conf = "low" if choice is None or snap["flags"] else "medium"
    return {"cands": cands, "def": choice, "conf": conf, "roads": context_roads(roads)}


def to_xy(lat, lon):
    return ((lon - LON0) * M_PER_DEG_LON, (lat - LAT0) * M_PER_DEG_LAT)


def to_latlon(x, y):
    return (y / M_PER_DEG_LAT + LAT0, x / M_PER_DEG_LON + LON0)


def bearing_of(ax, ay, bx, by):
    return math.degrees(math.atan2(bx - ax, by - ay)) % 360.0


def angle_diff(a, b):
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def oneway_dir(tags):
    """+1 forward only, -1 reverse only, 0 two-way."""
    ow = tags.get("oneway", "").lower()
    if ow in ("yes", "true", "1"):
        return 1
    if ow == "-1":
        return -1
    if ow == "no":
        return 0
    if tags.get("highway") in ("motorway", "motorway_link") or tags.get("junction") in ("roundabout", "circular"):
        return 1
    return 0


class Road:
    __slots__ = ("id", "name", "highway", "oneway", "nodes", "pts")

    def __init__(self, el):
        tags = el.get("tags", {})
        self.id = el["id"]
        self.name = tags.get("name") or tags.get("ref") or ""
        if tags.get("ref") and tags.get("name"):
            self.name = f'{tags["name"]} ({tags["ref"]})'
        self.highway = tags.get("highway", "")
        self.oneway = oneway_dir(tags)
        self.nodes = el.get("nodes", [])
        self.pts = [to_xy(g["lat"], g["lon"]) for g in el["geometry"]]


def load_roads(payload):
    roads = [Road(el) for el in payload["elements"]
             if el.get("type") == "way" and el.get("geometry") and len(el["geometry"]) >= 2]
    # A junction is a node shared by two or more distinct roads.
    node_ways = defaultdict(set)
    for r in roads:
        for nid in r.nodes:
            node_ways[nid].add(r.id)
    junctions = {nid for nid, ws in node_ways.items() if len(ws) >= 2}
    grid = defaultdict(list)
    for ri, r in enumerate(roads):
        for si in range(len(r.pts) - 1):
            (ax, ay), (bx, by) = r.pts[si], r.pts[si + 1]
            for gx in range(int(min(ax, bx) // GRID_M), int(max(ax, bx) // GRID_M) + 1):
                for gy in range(int(min(ay, by) // GRID_M), int(max(ay, by) // GRID_M) + 1):
                    grid[(gx, gy)].append((ri, si))
    return roads, junctions, grid


def project(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
    qx, qy = ax + t * dx, ay + t * dy
    return math.hypot(px - qx, py - qy), t, qx, qy


def candidates(x, y, roads, grid, radius):
    """Nearest segment per road within radius, sorted by distance."""
    best = {}
    r_cells = int(radius // GRID_M) + 1
    gx0, gy0 = int(x // GRID_M), int(y // GRID_M)
    for gx in range(gx0 - r_cells, gx0 + r_cells + 1):
        for gy in range(gy0 - r_cells, gy0 + r_cells + 1):
            for ri, si in grid.get((gx, gy), ()):
                (ax, ay), (bx, by) = roads[ri].pts[si], roads[ri].pts[si + 1]
                d, t, qx, qy = project(x, y, ax, ay, bx, by)
                if d <= radius and (ri not in best or d < best[ri]["dist"]):
                    best[ri] = {"road": ri, "seg": si, "t": t, "dist": d, "qx": qx, "qy": qy,
                                "seg_bearing": bearing_of(ax, ay, bx, by)}
    return sorted(best.values(), key=lambda c: c["dist"])


def travel_bearing(road, c):
    """Bearing of legal travel, or None when the road is two-way."""
    if road.oneway == 0:
        return None
    return c["seg_bearing"] if road.oneway == 1 else (c["seg_bearing"] + 180.0) % 360.0


def distance_to_junction(road, c, junctions, forward):
    """Metres along the road from the snap point to the next junction node."""
    pts, nodes = road.pts, road.nodes
    si, (qx, qy) = c["seg"], (c["qx"], c["qy"])
    if forward:
        dist, prev = 0.0, (qx, qy)
        for k in range(si + 1, len(pts)):
            dist += math.hypot(pts[k][0] - prev[0], pts[k][1] - prev[1])
            prev = pts[k]
            if dist > JUNCTION_SEARCH_M:
                return None
            if nodes[k] in junctions:
                return dist
    else:
        dist, prev = 0.0, (qx, qy)
        for k in range(si, -1, -1):
            dist += math.hypot(pts[k][0] - prev[0], pts[k][1] - prev[1])
            prev = pts[k]
            if dist > JUNCTION_SEARCH_M:
                return None
            if nodes[k] in junctions:
                return dist
    return None


def classify(cam, roads, junctions, grid):
    x, y = to_xy(cam["lat"], cam["lon"])
    cands = candidates(x, y, roads, grid, SNAP_MAX_M)
    out = {"bearing_deg": "", "road": "", "highway": "", "snap_dist_m": "", "snap_lat": "",
           "snap_lon": "", "method": "", "confidence": "low", "flags": [], "opposite": None}
    if not cands:
        out["flags"].append("no_road_within_%dm" % SNAP_MAX_M)
        return out, None
    best = cands[0]
    road = roads[best["road"]]
    slat, slon = to_latlon(best["qx"], best["qy"])
    out.update(road=road.name, highway=road.highway, snap_dist_m=round(best["dist"], 1),
               snap_lat=round(slat, 6), snap_lon=round(slon, 6))
    if best["dist"] > SNAP_GOOD_M:
        out["flags"].append("far_from_road")
    lon_decimals = len(cam["lon_raw"].split(".")[-1]) if "." in cam["lon_raw"] else 0
    lat_decimals = len(cam["lat_raw"].split(".")[-1]) if "." in cam["lat_raw"] else 0
    if min(lat_decimals, lon_decimals) <= 3:
        out["flags"].append("low_precision_coords")

    # A different road nearly as close, unless it's just another piece of the same road
    # heading the same way.
    best_tb = travel_bearing(road, best)
    for c in cands[1:]:
        if c["dist"] - best["dist"] > RIVAL_MARGIN_M:
            break
        other = roads[c["road"]]
        other_tb = travel_bearing(other, c)
        same_dir = (best_tb is not None and other_tb is not None and angle_diff(best_tb, other_tb) < 30)
        if not same_dir:
            # Dual carriageways are mapped as two one-way ways a few metres apart, so the
            # other direction is almost always within the margin. Treat it as a side choice
            # rather than an unrelated rival road.
            if best_tb is not None and other_tb is not None and angle_diff(best_tb, other_tb) > 150:
                gap = c["dist"] - best["dist"]
                out["opposite"] = {"cand": c, "tb": other_tb, "road": other}
                out["flags"].append("opposite_carriageway_gap_%.1fm" % gap)
                if gap < SIDE_GAP_M:
                    out["flags"].append("side_unclear")
            else:
                out["flags"].append("rival_road:" + (other.name or other.highway))
            break

    if cam["type"] in RED_LIGHT_TYPES:
        fwd = distance_to_junction(road, best, junctions, True)
        back = distance_to_junction(road, best, junctions, False)
        if best_tb is not None:
            out["bearing_deg"], out["method"] = round(best_tb, 1), "oneway_road"
            toward_fwd = angle_diff(best_tb, best["seg_bearing"]) < 90
            if (fwd if toward_fwd else back) is None:
                out["flags"].append("no_junction_ahead")
        elif fwd is not None and (back is None or fwd < back * 0.5):
            out["bearing_deg"], out["method"] = round(best["seg_bearing"], 1), "toward_junction"
        elif back is not None and (fwd is None or back < fwd * 0.5):
            out["bearing_deg"], out["method"] = round((best["seg_bearing"] + 180) % 360, 1), "toward_junction"
        else:
            out["flags"].append("junction_direction_unclear")
            out["method"] = "two_way_unresolved"
    else:
        if best_tb is not None:
            out["bearing_deg"], out["method"] = round(best_tb, 1), "oneway_road"
        else:
            out["method"] = "two_way_unresolved"
            out["flags"].append("two_way_road")
    return out, best


def node_index(roads):
    idx = defaultdict(list)
    for ri, r in enumerate(roads):
        for k, nid in enumerate(r.nodes):
            idx[nid].append((ri, k))
    return idx


def trace_back(roads, nidx, ri, seg, q, d, length):
    """Walk backwards from q against travel direction d (+1 = along way order).

    Returns xy points ordered from the far end of the approach to the camera.
    """
    out = [q]
    remaining = length
    visited = {ri}
    # Direction of travel; used to pick the next way when the snap point sits exactly on a node.
    a, b = roads[ri].pts[seg], roads[ri].pts[seg + 1]
    heading = bearing_of(*a, *b) if d == 1 else bearing_of(*b, *a)
    for _ in range(50):
        r = roads[ri]
        ks = range(seg, -1, -1) if d == 1 else range(seg + 1, len(r.pts))
        last_k = None
        for k in ks:
            p = r.pts[k]
            step = math.hypot(p[0] - out[-1][0], p[1] - out[-1][1])
            if step >= remaining:
                f = remaining / step if step else 0.0
                out.append((out[-1][0] + (p[0] - out[-1][0]) * f, out[-1][1] + (p[1] - out[-1][1]) * f))
                return out[::-1]
            if step > 0:
                out.append(p)
            remaining -= step
            last_k = k
        if last_k is None:
            break
        # Reached the end of this way: continue onto the way traffic arrives from.
        here = r.nodes[last_k]
        if len(out) >= 2:
            heading = bearing_of(out[-1][0], out[-1][1], out[-2][0], out[-2][1])
        best = None
        for rj, k in nidx[here]:
            if rj in visited:
                continue
            o = roads[rj]
            opts = []
            if k > 0 and o.oneway in (0, 1):
                opts.append((1, k - 1, bearing_of(*o.pts[k - 1], *o.pts[k])))
            if k < len(o.pts) - 1 and o.oneway in (0, -1):
                opts.append((-1, k, bearing_of(*o.pts[k + 1], *o.pts[k])))
            for nd, nseg, tb in opts:
                turn = angle_diff(tb, heading)
                if turn > MAX_TURN_DEG:
                    continue
                score = (o.name != r.name or not o.name, turn)
                if best is None or score < best[0]:
                    best = (score, rj, nseg, nd)
        if best is None:
            break
        _, ri, seg, d = best
        visited.add(ri)
    return out[::-1]


def simplify(pts, tol):
    if len(pts) < 3:
        return pts
    (ax, ay), (bx, by) = pts[0], pts[-1]
    dmax, imax = -1.0, 0
    for i in range(1, len(pts) - 1):
        d, _, _, _ = project(pts[i][0], pts[i][1], ax, ay, bx, by)
        if d > dmax:
            dmax, imax = d, i
    if dmax <= tol:
        return [pts[0], pts[-1]]
    return simplify(pts[:imax + 1], tol)[:-1] + simplify(pts[imax:], tol)


def ll(p, nd=6):
    lat, lon = to_latlon(*p)
    return [round(lat, nd), round(lon, nd)]


def path_len(pts):
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:]))


def _cands_for(c, roads, nidx):
    r = roads[c["road"]]
    out = []
    for d in {1: [1], -1: [-1], 0: [1, -1]}[r.oneway]:
        tb = c["seg_bearing"] if d == 1 else (c["seg_bearing"] + 180.0) % 360.0
        pts = simplify(trace_back(roads, nidx, c["road"], c["seg"], (c["qx"], c["qy"]), d, APPROACH_M), SIMPLIFY_M)
        out.append({"road": r.name or r.highway, "highway": r.highway, "bearing": round(tb),
                    "dist": round(c["dist"], 1), "len": round(path_len(pts)),
                    "pts": [ll(p) for p in pts], "_tb": tb})
    return out


def build_candidates(cam_xy, roads, grid, nidx):
    """Returns (candidates, number of base candidates).

    The base list must keep its order: saved editor edits refer to candidates by index.
    Extra options from further away are appended after it.
    """
    first = candidates(cam_xy[0], cam_xy[1], roads, grid, CAND_RADIUS_M)[:4]
    cands = []
    for c in first:
        cands += _cands_for(c, roads, nidx)
    cands = cands[:MAX_CANDS]
    base = len(cands)
    have = {(k["road"], round(k["_tb"] / 15)) for k in cands}
    used = {c["road"] for c in first}
    near = candidates(cam_xy[0], cam_xy[1], roads, grid, EXTRA_RADIUS_M)
    extra = []
    for c in near:
        if c["road"] in used or len(extra) >= MAX_EXTRA:
            continue
        for k in _cands_for(c, roads, nidx):
            key = (k["road"], round(k["_tb"] / 15))
            if key not in have and len(k["pts"]) >= 2 and len(extra) < MAX_EXTRA:
                have.add(key)
                k["extra"] = True
                extra.append(k)
    return cands + extra, base


def default_choice(cands, row):
    if not row or row["bearing_deg"] == "":
        return None
    want = float(row["bearing_deg"])
    best = None
    for i, c in enumerate(cands):
        score = (c["road"] != row["road"], angle_diff(c["_tb"], want))
        if best is None or score < best[0]:
            best = (score, i)
    if best and best[0][1] < 45:
        return best[1]
    return None


def context_roads(roads):
    """Roads, simplified, for drawing in the editor.

    Points are 1e-5 degree integers (~1 m), delta-encoded: [lat0, lon0, dlat1, dlon1, ...].
    """
    out = []
    for r in roads:
        flat, plat, plon = [], 0, 0
        for p in simplify(r.pts, 1.5):
            lat, lon = to_latlon(*p)
            ilat, ilon = round(lat * 1e5), round(lon * 1e5)
            flat += [ilat - plat, ilon - plon]
            plat, plon = ilat, ilon
        out.append({"n": r.name, "h": r.highway, "o": r.oneway, "p": flat})
    return out
