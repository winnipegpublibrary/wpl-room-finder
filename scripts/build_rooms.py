#!/usr/bin/env python3
"""Fetch WPL bookable rooms from LibCal and write a static JSON file for the
room finder widget (winnipeg-ca-wpl-room-finder.js).

Usage:
    LIBCAL_CLIENT_ID=... LIBCAL_CLIENT_SECRET=... python3 build_rooms.py out/rooms.json

Exits non-zero if any request fails or no rooms are found, so a scheduled
job never publishes partial data (the previously published file stays live).

Stdlib only — no pip install needed.
"""
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://wpl.libcal.com/1.1"

# LibCal location IDs; branch display order follows this list.
LIDS = [
    372, 385, 376, 378, 379, 391, 380, 381, 382, 383, 384, 377, 386,
    387, 388, 389, 390, 392, 393, 394,
]

# Matched against LibCal category names. Keep in sync with ROOM_TYPES in
# winnipeg-ca-wpl-room-finder.js (only the keys matter to the widget).
ROOM_TYPES = {
    # "Program rooms" are the same spaces as meeting rooms, so they're folded in.
    "meeting": re.compile(r"meeting|mtg|program|r[ée]union", re.I),
    "tutorial": re.compile(r"tutorial|tutorat", re.I),
}

# Public LibCal hours feed (no API key needed). Used only for branch
# coordinates, for the widget's map view.
HOURS_FEED = "https://wpl.libcal.com/api_hours_today.php?iid=3164&lid=0&format=json"

# Spaces location ID -> Hours location ID. LibCal numbers them separately.
HOURS_LID = {
    372: 1262,  # Millennium
    385: 7343,  # Bill and Helen Norrie
    376: 1264,  # Charleswood
    378: 1265,  # Cornish
    379: 1266,  # Fort Garry
    391: 1280,  # Harvey Smith
    380: 1268,  # Henderson
    381: 1269,  # Louis Riel
    382: 1270,  # Munroe
    383: 1271,  # Osborne
    384: 1272,  # Pembina Trail
    377: 1274,  # St. Boniface
    386: 1278,  # Sir William Stephenson
    387: 1275,  # St. James-Assiniboia
    388: 1276,  # St. John's
    389: 1277,  # St. Vital
    390: 1279,  # Transcona
    392: 1281,  # West Kildonan
    393: 1282,  # Westwood
    394: 1283,  # Windsor Park
}

BATCH_SIZE = 20  # IDs per comma-separated request; 1 disables batching
TIMEOUT = 30


class LibCal:
    def __init__(self, client_id, client_secret):
        self.requests = 0
        self.failures = 0
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }).encode()
        self.token = self._request(f"{API}/oauth/token", data=body)["access_token"]

    def _request(self, url, data=None):
        headers = {"User-Agent": "wpl-room-finder-build/1.0"}
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif getattr(self, "token", None):
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=data, headers=headers)
        self.requests += 1
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.load(resp)

    def get(self, path):
        return self._request(f"{API}{path}")

    def fetch_many(self, endpoint, ids, id_key):
        """Return {str(id): record}. Tries comma-separated batches first,
        then fetches anything missing one at a time."""
        found = {}
        if BATCH_SIZE > 1 and len(ids) > 1:
            for i in range(0, len(ids), BATCH_SIZE):
                chunk = ids[i:i + BATCH_SIZE]
                try:
                    res = self.get(f"{endpoint}/{','.join(map(str, chunk))}")
                except (urllib.error.URLError, ValueError) as e:
                    print(f"  batch {endpoint} failed ({e}); will fetch singly", file=sys.stderr)
                    continue
                for rec in res if isinstance(res, list) else []:
                    if isinstance(rec, dict) and rec.get(id_key) is not None:
                        found[str(rec[id_key])] = rec

        missing = [i for i in ids if str(i) not in found]
        if missing and found:
            print(f"  {endpoint}: {len(missing)} not returned by batch, fetching singly", file=sys.stderr)
        for i in missing:
            try:
                res = self.get(f"{endpoint}/{i}")
            except (urllib.error.URLError, ValueError) as e:
                self.failures += 1
                print(f"  ERROR {endpoint}/{i}: {e}", file=sys.stderr)
                continue
            if res:
                found[str(i)] = res[0]
        return found


def room_type(category_name):
    for key, rx in ROOM_TYPES.items():
        if rx.search(category_name or ""):
            return key
    return None


def branch_coords():
    """{hours_lid: (lat, lng)} from the public hours feed. Non-fatal: if the
    feed is down, branches just have no coordinates (widget hides maps)."""
    req = urllib.request.Request(HOURS_FEED, headers={"User-Agent": "wpl-room-finder-build/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            locs = json.load(resp).get("locations") or []
    except (urllib.error.URLError, ValueError) as e:
        print(f"  WARNING hours feed unavailable, no coordinates: {e}", file=sys.stderr)
        return {}
    coords = {}
    for loc in locs:
        try:
            coords[int(loc["lid"])] = (float(loc["lat"]), float(loc["long"]))
        except (KeyError, TypeError, ValueError):
            pass  # no coordinates for this location
    return coords


def build(api):
    coords = branch_coords()
    # 1. Categories per branch.
    locs = api.fetch_many("/space/categories", LIDS, "lid")
    branches, categories = [], []
    for order, lid in enumerate(LIDS):
        loc = locs.get(str(lid))
        if not loc:
            continue
        branch = {"lid": lid, "name": loc.get("name", ""), "order": order}
        if HOURS_LID.get(lid) in coords:
            branch["lat"], branch["lng"] = coords[HOURS_LID[lid]]
        branches.append(branch)
        for cat in loc.get("categories") or []:
            t = room_type(cat.get("name"))
            if t:
                categories.append({"cid": cat["cid"], "lid": lid, "type": t,
                                   "public": bool(cat.get("public", 1))})

    # 2. Room IDs per matching category.
    cats = api.fetch_many("/space/category", [c["cid"] for c in categories], "cid")
    item_meta = {}
    for c in categories:
        for item_id in (cats.get(str(c["cid"])) or {}).get("items") or []:
            item_meta.setdefault(item_id, c)

    # 3. Room details.
    ids = list(item_meta)
    details = api.fetch_many("/space/item", ids, "id")
    rooms = []
    for item_id in ids:
        d = details.get(str(item_id))
        if not d:
            continue
        # Normalise to https so images aren't blocked as mixed content.
        image = re.sub(r"^(https?:)?//", "https://", str(d.get("image") or ""))
        try:
            capacity = int(d.get("capacity") or 0)
        except (TypeError, ValueError):
            capacity = 0
        rooms.append({
            "id": item_id,
            "lid": item_meta[item_id]["lid"],
            "type": item_meta[item_id]["type"],
            "name": d.get("name", ""),
            "description": d.get("description", ""),
            "image": image,
            "imageAlt": d.get("image_alt_text") or "",
            "capacity": capacity,
            "filterIds": [str(f) for f in d.get("filter_ids") or []],
            # From the LibCal category. The widget hides non-public rooms
            # unless CONFIG.showNonPublic is set (testing).
            "public": item_meta[item_id]["public"],
        })

    # 4. Feature filter names.
    try:
        filters = [{"id": f["id"], "name": f["name"]} for f in api.get("/space/search/filters")]
    except (urllib.error.URLError, ValueError, KeyError, TypeError) as e:
        api.failures += 1
        print(f"  ERROR filters: {e}", file=sys.stderr)
        filters = []

    return {
        "version": 2,
        "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "branches": branches,
        "rooms": rooms,
        "filters": filters,
    }


def main():
    if len(sys.argv) != 2:
        sys.exit(f"usage: {sys.argv[0]} OUTPUT.json")
    out_path = sys.argv[1]
    try:
        client_id = os.environ["LIBCAL_CLIENT_ID"]
        client_secret = os.environ["LIBCAL_CLIENT_SECRET"]
    except KeyError as e:
        sys.exit(f"missing environment variable {e}")

    api = LibCal(client_id, client_secret)
    data = build(api)

    by_type = {t: sum(r["type"] == t for r in data["rooms"]) for t in ROOM_TYPES}
    print(f"{len(data['branches'])} branches, {len(data['rooms'])} rooms {by_type}, "
          f"{len(data['filters'])} filters, "
          f"{sum('lat' in b for b in data['branches'])} branches with coordinates — "
          f"{api.requests} API requests, {api.failures} failures")

    if api.failures or not data["rooms"]:
        sys.exit("not writing output: incomplete data")

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
