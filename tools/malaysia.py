#!/usr/bin/env python3
"""Queue Malaysian cameras for a path review.

    python3 tools/malaysia.py add FILE

FILE is a JSON list of cameras, each {type, location, lat, lon} with optional source (for
example "OSM node 123") and bearing (direction of travel past the camera, 0 = north).
Malaysia has no official camera list to check against, so cameras are only added this
way, after someone has checked them by hand.

Each camera gets the next free id from 5001 and best-guess approach paths (800 m on
expressways, 400 m elsewhere), then waits in review/pending.json until its path is checked
in the editor, like a new SPF camera. Cameras already in the data or waiting (same type
within 20 m, covering the same direction) are skipped.
"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build  # noqa: E402
import update  # noqa: E402

CATEGORIES = {1: "Red light", 2: "Fixed speed"}
SAME_M = 20


def travel(cam):
    """Direction of travel a known camera covers, or None if it can't be told."""
    if cam.get("path"):
        p = cam["path"]
        return build.bearing(p[-2], p[-1])
    if cam.get("def") is not None:
        return cam["cands"][cam["def"]]["bearing"]
    return None


def same_camera(known, cam, bearing):
    if known["type"] != cam["type"]:
        return False
    if build.metres((known["lat"], known["lon"]), (cam["lat"], cam["lon"])) >= SAME_M:
        return False
    other = travel(known)
    # Opposite carriageways often share a gantry: both directions are separate cameras.
    return bearing is None or other is None or abs((bearing - other + 180) % 360 - 180) < 90


def add(path):
    with open(path) as f:
        wanted = json.load(f)
    import paths

    pending = update.load_pending()
    known = build.load_my() + [c for c in pending if build.is_my(c)]
    next_id = max([build.MY_ID_BASE - 1] + [c["id"] for c in known]) + 1
    added, skipped = [], []
    for w in wanted:
        cam = {"type": int(w["type"]), "lat": float(w["lat"]), "lon": float(w["lon"])}
        if cam["type"] not in CATEGORIES:
            sys.exit(f"{w.get('location')}: type {cam['type']} isn't a Malaysian camera type (1 or 2)")
        lat0, lat1, lon0, lon1 = build.MY_BOUNDS
        if not (lat0 < cam["lat"] < lat1 and lon0 < cam["lon"] < lon1):
            sys.exit(f"{w.get('location')}: ({cam['lat']}, {cam['lon']}) isn't in Peninsular Malaysia")
        if any(same_camera(k, cam, w.get("bearing")) for k in known):
            skipped.append(f"- {w['location']}: already in the data or waiting for review")
            continue
        cam.update(id=next_id, category=CATEGORIES[cam["type"]], location=w["location"])
        if w.get("source"):
            cam["source"] = w["source"]
        print(f"guessing a path for #{next_id} {cam['location']}", file=sys.stderr)
        guess = paths.suggest(cam, length="auto", bearing=w.get("bearing"))
        guess.pop("roads")
        if guess["def"] is None and w.get("bearing") is not None:
            # The checked direction may only match a road a little further away.
            near = [(abs((k["bearing"] - w["bearing"] + 180) % 360 - 180), k["dist"], i)
                    for i, k in enumerate(guess["cands"]) if len(k["pts"]) >= 2]
            near = [n for n in near if n[0] < 30]
            if near:
                guess["def"] = min(near, key=lambda n: (n[1], n[0]))[2]
        entry = dict(update.fields(cam), **guess, why="new-my")
        pending.append(entry)
        known.append(entry)
        added.append(f"- #{next_id} {cam['location']}" + ("" if guess["def"] is not None else " (no default path: pick one)"))
        next_id += 1
    update.save_pending(pending)
    lines = [f"Queued {len(added)} Malaysian camera(s) for a path review."]
    if added:
        lines += [""] + added
    if skipped:
        lines += ["", "Skipped:"] + skipped
    print("\n".join(lines))


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "add":
        sys.exit(__doc__)
    add(sys.argv[2])
