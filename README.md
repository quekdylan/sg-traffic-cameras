# Singapore Traffic Enforcement Cameras

[![Last checked](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fquekdylan%2Fsg-traffic-cameras%2Fmain%2Fmanifest.json&query=%24.spf_checked&label=SPF%20checked&color=brightgreen)](https://github.com/quekdylan/sg-traffic-cameras/actions/workflows/spf-check.yml) [![Last updated](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fquekdylan%2Fsg-traffic-cameras%2Fmain%2Fmanifest.json&query=%24.updated&label=data%20updated&color=blue)](https://github.com/quekdylan/sg-traffic-cameras/releases/latest) [![Cameras](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fquekdylan%2Fsg-traffic-cameras%2Fmain%2Fmanifest.json&query=%24.cameras&label=cameras&color=blue)](https://quekdylan.github.io/sg-traffic-cameras/map/) [![Latest release](https://img.shields.io/github/v/release/quekdylan/sg-traffic-cameras?label=release)](https://github.com/quekdylan/sg-traffic-cameras/releases/latest) [![Monthly check](https://github.com/quekdylan/sg-traffic-cameras/actions/workflows/spf-check.yml/badge.svg)](https://github.com/quekdylan/sg-traffic-cameras/actions/workflows/spf-check.yml)

This repo contains locations of all 350 traffic enforcement cameras published by the Singapore Police Force (SPF). It also contains manually reviewed approach paths and bearings for each camera.

The data is meant for GPS camera-warning devices. 

Data is updated monthly. A GitHub Actions workflow checks the SPF pages once a month and updates the data when cameras are added, moved or removed, and is published after a manual review.

## Malaysian cameras

`data/my_cameras.json` has fixed speed and red light cameras in Peninsular Malaysia. Malaysia doesn't publish camera coordinates, so these come from OpenStreetMap: each one was checked by hand against satellite imagery, Street View and the AES/AwAS camera lists in the news before it was added, and only checked cameras are included. Expressway cameras have 800 m approach paths (about 25 s of warning at 110 km/h); the rest have 400 m, like Singapore's.

Malaysian cameras are numbered from 5001, use the same fields and type codes (1 red light, 2 fixed speed), and are included in `cameras.csv`, `camera_data.h` and `cameras.bin` next to the Singapore ones. The monthly SPF check never touches them. New ones are added with `tools/malaysia.py add FILE` and reviewed in the editor like new SPF cameras.

## Bus lanes

The repo also has Singapore's bus lanes with their hours, for devices that warn when the car is on a bus lane that's in operation. They're built from LTA's own road markings: every painted bus lane line is matched to its OpenStreetMap carriageway to get the direction of travel, and lines on the same road are joined into stretches that follow the road's centre line, which is where a car's GPS fix sits.

| Type | In operation |
|---|---|
| Normal bus lane | Mon–Fri 7:30–9:30am and 5–8pm, not on public holidays |
| Full-day bus lane | Mon–Sat 7:30am–11pm, not on public holidays |
| Bus-only lane | At all times (stretches next to LTA's "Bus Only Lane" signs) |

A second monthly workflow (`buslanes-check.yml`) rebuilds the bus lanes whenever LTA updates its lane markings or traffic signs (about every six months) or MOM publishes a new year's public holidays, and publishes them without a review. If a rebuild would suddenly lose more than 10% of the bus lane length or change the number of stretches by more than 20%, it doesn't publish: it opens an issue and the run fails, which sends an email. Paint that couldn't be tied to one carriageway and direction (mostly paint on roads OpenStreetMap doesn't have) is listed in `review/buslanes_left_out.md`.

## Map

[![Map of all cameras, coloured by type](map/preview.jpg)](https://quekdylan.github.io/sg-traffic-cameras/map/)

[Open the interactive map](https://quekdylan.github.io/sg-traffic-cameras/map/) to zoom in, see each camera's approach path, and filter by type. The bus lanes are there too: tick them in the list on the right.

## Camera types

| Code | Type | Count |
|---|---|---|
| 1 | Red light camera | 241 |
| 2 | Fixed speed camera | 33 |
| 3 | Mobile speed camera | 5 |
| 4 | Average speed camera (Tanah Merah Coast Road) | 12 |
| 5 | Police speed laser camera | 50 |
| 6 | Traffic violation enforcement camera (TVEC) | 9 |

## Files

| File | What it is |
|---|---|
| `data/cameras.csv` | One row per camera, for spreadsheets and scripts |
| `data/cameras.json` | The Singapore cameras as JSON |
| `data/my_cameras.json` | The Malaysian cameras as JSON, same fields plus `source` (the OpenStreetMap node) |
| `data/camera_data.h` | C/C++ header for microcontrollers (ESP32 etc.), about 12 KB of flash |
| `data/cameras.bin` | The same data as a small binary file, for devices that download updates (format below) |
| `data/buslanes.json` | Bus lane stretches (id, type, road, length, path in the direction of travel), their schedules and public holidays |
| `data/bus_lane_data.h` | The bus lanes as a C/C++ header, about 25 KB of flash |
| `data/buslanes.bin` | The bus lanes as a binary file for devices that download updates (format below) |
| `manifest.json` | Data versions, counts and SHA-256 of each file, for devices that check for updates. Bus lanes have their own `buslanes.version` |
| `cache/osm_roads.json.gz` | The OpenStreetMap roads near bus lanes, used if OpenStreetMap can't be reached |
| `review/` | Cameras waiting for their paths to be checked, and the result of the last SPF check |
| `tools/`, `editor/`, `map/` | The update scripts, the path editor and the map page |

## Fields

| Field | Meaning |
|---|---|
| `id` | Camera id, stable across updates. Singapore cameras are numbered from 1, Malaysian ones from 5001 |
| `type` | Camera type code (see below) |
| `category` | Camera type name |
| `location` | SPF's own description, e.g. `Adam Rd by Dunearn Rd twds Farrer Rd`. For Malaysian cameras, the road, KM marker and direction, e.g. `North-South Expressway (E2) KM 184.2, Jasin (northbound)` |
| `lat`, `lon` | Camera position as published by SPF, or as checked on OpenStreetMap for Malaysia (WGS84) |
| `bearing_deg` | (CSV only) Direction of travel at the camera, 0 = north, 90 = east |
| `path_length_m` | (CSV only) Length of the approach path in metres |
| `path_points` | (CSV only) Number of points in the path |
| `country` | (CSV only) `SG` or `MY` |
| `source` | (Malaysian cameras only) The OpenStreetMap node the position came from |
| `path` | The approach path. Points run from the start of the approach to the camera, so the last point is at the camera. In the CSV the points are `lat lon` pairs separated by `;`. In the JSON they're `[lat, lon]` arrays. |

In `camera_data.h`, coordinates are integers in micro-degrees (degrees × 1,000,000). `kCameras[i].firstPoint` and `pointCount` index into `kCameraPoints`. `kCameraDataVersion` is the manifest version the header was built from.

`cameras.bin` holds the same arrays, little-endian: a 12-byte header (`SGCB`, format `1` as uint16, camera count uint16, point count uint16, 0 uint16), then the points (int32 latE6, int32 lonE6, 8 bytes each), then the cameras (uint16 id, uint8 type, uint8 point count, uint16 first point, 6 bytes each).


`buslanes.bin` is little-endian: a 16-byte header (`SGBL`, format `1` as uint16, then uint16 counts of schedules, holidays and stretches, and a uint32 point count), the schedules (uint8 days with bit 0 = Monday, uint8 flags with bit 0 = not on public holidays, uint8 window count, uint8 0, then uint16 start minutes ×2 and uint16 end minutes ×2, minutes from midnight Singapore time), the public holidays (uint32 yyyymmdd), the points (int32 latE6, int32 lonE6) and the stretches (uint32 first point, uint16 point count, uint8 schedule, uint8 0).

## Using it on a device

A simple approach that works well:

1. Shortlist cameras within about 600 m of the GPS fix.
2. For each one, find the closest segment of its path. It's a match if you're within about 20m of it sideways and your heading is within about 35° of the segment's direction.
3. Distance to the camera = the distance along the path from your projected position to the last point.
4. Warn when the distance is under `max(150 m, speed × 12 s)` and two fixes in a row match. Clear the warning once you pass the end of the path.

To keep a device up to date, download `manifest.json` from this repo, compare its `version` with the one stored on the device, and if it's newer, download the data files and check them against the SHA-256 values listed. The bus lanes work the same way with `buslanes.version`.

For bus lanes: a fix within about 20 m of a stretch, heading within about 40° of its direction, on two fixes in a row, while its schedule is in operation, means the car is on that bus lane's road. GPS can't tell which lane you're in.

## Suggesting a fix

Notice an issue with the data?

- **Fix a path yourself:** open the [path editor](https://quekdylan.github.io/sg-traffic-cameras/editor/), go to **Fix a camera** and find it by number or road name (or tap **Suggest a fix** on the [map](https://quekdylan.github.io/sg-traffic-cameras/map/)). Correct the path, then press **Review and submit** and **Submit on GitHub**. GitHub opens with your fix filled in: choose **Propose changes**, then **Create pull request**. It goes live once it's merged upon manual review.
- **Or just report it:** [open an issue](https://github.com/quekdylan/sg-traffic-cameras/issues/new) with the camera number and what's wrong.

## Sources

- SPF, [Red Light Camera Locations](https://www.police.gov.sg/knowledge-hub/traffic/traffic-matters/red-light-camera-locations)
- SPF, [Speed Enforcement Camera Locations](https://www.police.gov.sg/Knowledge-Hub/Traffic/Traffic-Matters/Speed-Enforcement-Camera-Locations)
- SPF, [Traffic Violation Enforcement Camera Locations](https://www.police.gov.sg/knowledge-hub/traffic/traffic-matters/traffic-violation-enforcement-camera-locations)
- LTA, [Lane Marking](https://data.gov.sg/datasets/d_fa71cc0c433275f4d2b0133358cc4fbf/view) and [Traffic Sign](https://data.gov.sg/datasets/d_bbf0132c7290d6838f82003972d933d5/view) (bus lanes)
- MOM, [Singapore Public Holidays](https://data.gov.sg/datasets/d_8ef23381f9417e4d4254ee8b4dcdb176/view)
- Malaysian camera positions © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), checked by hand
- Road geometry © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright), via the [BBBike](https://download.bbbike.org/osm/bbbike/Singapore/) extract for the bus lanes

## License

The approach paths are derived from OpenStreetMap, so this dataset is released under the [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/). You're free to use, share and adapt it, including commercially, as long as you:

- credit "© OpenStreetMap contributors" and the Singapore Police Force as the source of the camera locations (OpenStreetMap alone for the Malaysian ones), and for the bus lanes: "Contains information from LTA Lane Marking, LTA Traffic Sign and Singapore Public Holidays accessed from data.gov.sg, made available under the terms of the Singapore Open Data Licence version 1.0", and
- share any database you build from it under the ODbL as well.

This project isn't affiliated with or endorsed by the Singapore Police Force. The data is provided as-is, with no guarantee that it's complete or accurate. Always obey road signs and speed limits.
