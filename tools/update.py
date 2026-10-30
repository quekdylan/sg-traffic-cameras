#!/usr/bin/env python3
"""Keep the dataset in step with the SPF camera lists.

    python3 tools/update.py check   compare SPF with the data and update it
    python3 tools/update.py apply   publish paths submitted from the path editor
    python3 tools/update.py proposal FILE...   check files proposed in a pull request

check: cameras that SPF removed, or whose description changed, are updated in the data
straight away. New cameras, and cameras that moved, get best-guess approach paths and wait
in review/pending.json until their paths are checked in the editor (editor/index.html).

apply: reads review/submitted/*.json (written by the editor's Submit button), moves those
cameras from pending into the data or replaces the paths of published cameras, and deletes
the submitted files.

Both rebuild the data files and manifest, and write a short summary to review/summary.md.

Malaysian cameras (ids from 5001, kept in data/my_cameras.json) aren't on the SPF lists:
check leaves them alone, and apply publishes their reviewed paths like any other camera.
"""
import datetime
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build  # noqa: E402

ROOT = build.ROOT
PENDING = os.path.join(ROOT, "review", "pending.json")
SUBMITTED = os.path.join(ROOT, "review", "submitted")
SUMMARY = os.path.join(ROOT, "review", "summary.md")
FIELDS = ("id", "type", "category", "location", "lat", "lon")
OPTIONAL = ("source",)  # kept when present, e.g. the OpenStreetMap node of a Malaysian camera


def fields(c):
    return dict({k: c[k] for k in FIELDS}, **{k: c[k] for k in OPTIONAL if k in c})


def load_pending():
    if not os.path.exists(PENDING):
        return []
    with open(PENDING) as f:
        return json.load(f)["cameras"]


def save_pending(pending):
    if not pending:
        if os.path.exists(PENDING):
            os.remove(PENDING)
        return
    os.makedirs(os.path.dirname(PENDING), exist_ok=True)
    with open(PENDING, "w") as f:
        json.dump({"cameras": sorted(pending, key=lambda c: c["id"])}, f, separators=(",", ":"))


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


def label(c):
    return f"#{c['id']} {c['category']}: {c['location']} ({c['lat']}, {c['lon']})"


MAX_END_M = 150      # a path must end this close to its camera
MIN_LEN_M, MAX_LEN_M = 20, 1500


def path_problem(cam, pts):
    """Why a submitted path can't be used for this camera, or None if it's fine."""
    if not isinstance(pts, list) or not 2 <= len(pts) <= 255:
        return "needs 2 to 255 points"
    for p in pts:
        if not (isinstance(p, list) and len(p) == 2 and
                all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in p)):
            return "has a point that isn't a [lat, lon] pair"
        lat0, lat1, lon0, lon1 = build.bounds_for(cam)
        if not (lat0 < p[0] < lat1 and lon0 < p[1] < lon1):
            return "goes outside " + ("Malaysia" if build.is_my(cam) else "Singapore")
    end = build.metres(pts[-1], (cam["lat"], cam["lon"]))
    if end > MAX_END_M:
        return f"ends {end:.0f} m from the camera (it must end within {MAX_END_M} m)"
    length = sum(build.metres(a, b) for a, b in zip(pts, pts[1:]))
    if not MIN_LEN_M <= length <= MAX_LEN_M:
        return f"is {length:.0f} m long (it must be {MIN_LEN_M} to {MAX_LEN_M} m)"
    return None


def load_submissions(files):
    """Merge submitted files into {camera id: path}. Returns (submitted, problems)."""
    submitted, problems = {}, []
    for path in files:
        name = os.path.basename(path)
        try:
            with open(path) as f:
                body = json.load(f)
            if not isinstance(body, dict):
                raise ValueError("not a JSON object")
        except (OSError, ValueError) as ex:
            problems.append(f"- {name}: can't be read ({ex})")
            continue
        for key, e in body.items():
            if not (isinstance(key, str) and key.isdigit() and isinstance(e, dict)):
                problems.append(f"- {name}: entry {key!r} isn't a camera id with a path")
                continue
            submitted[int(key)] = e.get("path")
    return submitted, problems


def match(spf_rows, current):
    """Pair SPF rows with current cameras.

    Returns (kept [(spf, cam)], moved [(spf, cam)], new [spf], removed [cam]).
    """
    left = list(range(len(current)))
    pairs = {}
    keys = (
        lambda c: (c["type"], c["lat"], c["lon"], c["location"]),  # unchanged
        lambda c: (c["type"], c["lat"], c["lon"]),                  # description changed
    )
    for key in keys:
        for si, s in enumerate(spf_rows):
            if si in pairs:
                continue
            for k in left:
                if key(current[k]) == key(s):
                    pairs[si] = k
                    left.remove(k)
                    break
    kept = [(spf_rows[si], current[k]) for si, k in pairs.items()]
    # Same type and description but new coordinates: the camera moved. Take the nearest.
    moved = []
    for si, s in enumerate(spf_rows):
        if si in pairs:
            continue
        same = [k for k in left if (current[k]["type"], current[k]["location"]) == (s["type"], s["location"])]
        if same:
            k = min(same, key=lambda k: build.metres((current[k]["lat"], current[k]["lon"]), (s["lat"], s["lon"])))
            pairs[si] = k
            left.remove(k)
            moved.append((s, current[k]))
    new = [s for si, s in enumerate(spf_rows) if si not in pairs]
    removed = [current[k] for k in left]
    return kept, moved, new, removed


def check():
    import paths
    import spf

    data = build.load()
    pending, my_pending = [], []
    for c in load_pending():
        (my_pending if build.is_my(c) else pending).append(c)
    rows = spf.fetch_all()
    types = {r["type"] for r in rows}
    if types != {1, 2, 3, 4, 5, 6} or len(rows) < 0.8 * (len(data) + len(pending)):
        sys.exit(f"SPF pages look incomplete ({len(rows)} cameras, types {sorted(types)}); not changing anything")

    for c in data:
        c["_pending"] = False
    for c in pending:
        c["_pending"] = True
    kept, moved, new, removed = match(rows, data + pending)

    manifest = json.load(open(os.path.join(ROOT, "manifest.json")))
    next_id = max([manifest.get("max_id", 0)] + [c["id"] for c in data + pending]) + 1
    if next_id >= build.MY_ID_BASE:
        sys.exit(f"Singapore camera ids have reached {build.MY_ID_BASE}, where Malaysian ids start")
    out_data, out_pending, text_changes, to_review = [], [], [], []
    for s, c in kept:
        if (c["location"], c["category"]) != (s["location"], s["category"]):
            text_changes.append(f"- #{c['id']}: \"{c['location']}\" is now \"{s['location']}\"")
            c.update(location=s["location"], category=s["category"])
        (out_pending if c["_pending"] else out_data).append(c)
    for s, c in moved:
        to_review.append(dict(s, id=c["id"], _why=f"moved from ({c['lat']}, {c['lon']})"))
    for s in new:
        to_review.append(dict(s, id=next_id, _why="new"))
        next_id += 1

    for c in to_review:
        print(f"guessing a path for {label(c)}", file=sys.stderr)
        guess = paths.suggest(c)
        guess.pop("roads")  # the editor draws on an OpenStreetMap base map instead
        out_pending.append(dict(fields(c), **guess, why=c["_why"]))

    for c in out_data + out_pending:
        c.pop("_pending", None)
    manifest_max = max([manifest.get("max_id", 0)] + [c["id"] for c in data + pending + out_pending])
    changed = build.build(out_data, spf_checked=build.today())
    with open(os.path.join(ROOT, "manifest.json")) as f:
        manifest = json.load(f)
    manifest["max_id"] = manifest_max
    with open(os.path.join(ROOT, "manifest.json"), "w") as f:
        f.write(json.dumps(manifest, indent=2) + "\n")
    save_pending(out_pending + my_pending)

    lines = [f"SPF check {build.today().isoformat()}: {len(rows)} cameras on the SPF pages."]
    if removed:
        lines += ["", "Removed (no longer on the SPF pages):"] + [f"- {label(c)}" for c in removed]
    if text_changes:
        lines += ["", "Description changed:"] + text_changes
    if to_review:
        lines += ["", "Waiting for a path review in the editor:"]
        lines += [f"- {label(c)}, {c['_why']}" for c in to_review]
    if not (removed or text_changes or to_review):
        lines += ["", "No changes."]
    if out_pending:
        lines += ["", f"{len(out_pending)} camera(s) waiting for review in total."]
    if my_pending:
        lines += ["", f"{len(my_pending)} Malaysian camera(s) also waiting for review."]
    write_summary(lines)
    output(data_changed=str(changed).lower(), pending=len(out_pending), new_review=len(to_review))


def apply():
    pending = {c["id"]: c for c in load_pending()}
    files = sorted(glob.glob(os.path.join(SUBMITTED, "*.json")))
    submitted, skipped = load_submissions(files)
    data, my = build.load(), build.load_my()
    added, updated = [], []
    by_id = {c["id"]: c for c in data + my}
    for cid, pts in sorted(submitted.items()):
        cam = pending.get(cid) or by_id.get(cid)
        if cam is None:
            skipped.append(f"- #{cid}: no such camera")
            continue
        problem = path_problem(cam, pts)
        if problem:
            skipped.append(f"- #{cid}: path {problem}")
            continue
        path = [[round(a, 6), round(b, 6)] for a, b in pts]
        if cid in pending:
            c = pending.pop(cid)
            (my if build.is_my(c) else data).append(dict(fields(c), path=path))
            added.append(f"- {label(c)}")
        elif by_id[cid]["path"] != path:
            by_id[cid]["path"] = path
            updated.append(f"- {label(by_id[cid])}")
    changed = build.build(data, my=my)
    save_pending(list(pending.values()))
    for path in files:
        os.remove(path)
    lines = [f"Published {len(added)} new camera(s) and updated {len(updated)} path(s)."]
    if added:
        lines += ["", "New:"] + added
    if updated:
        lines += ["", "Path updated:"] + updated
    if skipped:
        lines += ["", "Ignored:"] + skipped
    lines += ["", f"{len(pending)} camera(s) still waiting for review."]
    write_summary(lines)
    output(data_changed=str(changed).lower(), pending=len(pending))


def proposal(files):
    """Report on submitted files in a pull request. Exits 1 if any of it can't be used."""
    submitted, problems = load_submissions(files)
    pending = {c["id"]: c for c in load_pending()}
    by_id = {c["id"]: c for c in build.load() + build.load_my()}
    rows = []
    for cid, pts in sorted(submitted.items()):
        cam = pending.get(cid) or by_id.get(cid)
        problem = "no such camera" if cam is None else path_problem(cam, pts)
        if problem:
            problems.append(f"- #{cid}: path {problem}" if cam else f"- #{cid}: {problem}")
            continue
        length = sum(build.metres(a, b) for a, b in zip(pts, pts[1:]))
        end = build.metres(pts[-1], (cam["lat"], cam["lon"]))
        if cid in pending:
            what = "new camera"
        else:
            old = cam["path"]
            moved = max(min(build.metres(p, q) for q in old) for p in pts)
            what = f"path changed, moves up to {moved:.0f} m from the current one"
        rows.append(f"- {label(cam)}: {what}. {length:.0f} m long, ends {end:.0f} m from the camera. "
                    f"[Current path](https://quekdylan.github.io/sg-traffic-cameras/editor/?id={cid})")
    lines = [f"**{len(rows)} camera(s) in this proposal.**", ""] + rows
    if problems:
        lines += ["", "**Can't be used as is:**"] + problems
    if not submitted and not problems:
        lines += ["No cameras in the submitted file."]
    print("\n".join(lines))
    sys.exit(1 if problems or not submitted else 0)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "proposal":
        proposal(sys.argv[2:])
    else:
        {"check": check, "apply": apply}[cmd]()
