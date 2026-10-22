#!/usr/bin/env python3
"""
Spyne Image Resequencer  -  local tool (python 3.8+, no third-party packages)

Run:   python3 app.py          then open http://127.0.0.1:8765

What it does
  1. You paste your console Bearer token (kept in memory only, never written to disk).
  2. You give it a list of VINs and the label order you want
     (e.g. Exterior Front Right, Exterior, Exterior Front Left, Exterior Left, ...).
  3. It reads every VIN's images, shows a before/after preview, and only on your
     click calls  POST /inventory/v2/media/image-sequencing  (the same call the
     console's  Images > Resequence > Save Sequence  button makes).
  4. Every apply writes a backup of the original order so you can undo it.
"""
import json
import os
import re
import sys
import time
import uuid
import threading
import ssl
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.request
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API = "https://api.spyne.ai"
HERE = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(HERE, "backups")
PORT = int(os.environ.get("PORT", "8765"))

def _ctx():
    try:
        import certifi  # optional, helps on some Mac python.org installs
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


SSL_CTX = _ctx()
SCAN = {"cancel": False, "running": False, "done": 0, "total": 0, "rows": [], "error": None, "sequence": []}

STATE = {  # in-memory only
    "token": None, "enterpriseId": None, "teamId": None, "userId": None,
    "vin_map": {},          # VIN -> dealerVinId
}
LOCK = threading.Lock()

# ---------------------------------------------------------------- labels
SECTORS = ["Exterior", "Exterior Front Left", "Exterior Left", "Exterior Rear Left",
           "Exterior Rear", "Exterior Rear Right", "Exterior Right", "Exterior Front Right"]
# classic 8 angles kept for numeric shortcuts in the sequence box (e.g. "320")
ANGLE_LABEL = {320: "Exterior Front Right", 0: "Exterior", 30: "Exterior Front Left", 90: "Exterior Left",
               140: "Exterior Rear Left", 180: "Exterior Rear", 220: "Exterior Rear Right", 260: "Exterior Right"}


def angle_to_label(angle):
    """Console-style label for any angle: nearest of 8 sectors (0 = straight front, clockwise 45 deg each)."""
    return SECTORS[int(round((int(angle) % 360) / 45.0)) % 8]


ALIASES = {  # user-typed text  ->  canonical label
    "front right": "Exterior Front Right", "frontright": "Exterior Front Right", "fr": "Exterior Front Right",
    "front": "Exterior", "exterior front": "Exterior", "straight front": "Exterior",
    "front left": "Exterior Front Left", "frontleft": "Exterior Front Left", "fl": "Exterior Front Left",
    "left": "Exterior Left", "driver side": "Exterior Left",
    "rear left": "Exterior Rear Left", "rearleft": "Exterior Rear Left", "rl": "Exterior Rear Left",
    "rear": "Exterior Rear", "back": "Exterior Rear",
    "rear right": "Exterior Rear Right", "rearright": "Exterior Rear Right", "rr": "Exterior Rear Right",
    "right": "Exterior Right", "passenger side": "Exterior Right",
    "misc": "Miscellaneous", "other": "Miscellaneous",
    "focus": "Focus Shoot", "focus shoot": "Focus Shoot", "focusshoot": "Focus Shoot",
    "interiors": "Interior",
}
CANON = SECTORS[7:] + SECTORS[:7] + ["Interior", "Focus Shoot", "Miscellaneous"]
CANON_LOWER = {c.lower(): c for c in CANON}


def canon_label(text):
    """Map a user-typed label (or a numeric angle) to a canonical label."""
    t = re.sub(r"\s+", " ", str(text).strip().strip('"').strip("'")).lower()
    if not t:
        return None
    if t in CANON_LOWER:
        return CANON_LOWER[t]
    if t in ALIASES:
        return ALIASES[t]
    if re.fullmatch(r"\d{1,3}", t) and int(t) in ANGLE_LABEL:   # "320" -> Front Right
        return ANGLE_LABEL[int(t)]
    return text.strip()  # unknown: keep as typed; will warn later


def image_label(img):
    cat = (img.get("image_category") or "").strip()
    angle = img.get("angle")
    if cat.lower() == "exterior":
        if angle is None:
            return "Exterior"
        try:
            return angle_to_label(angle)
        except (TypeError, ValueError):
            return "Exterior"
    if cat.lower().startswith("interior"):
        return "Interior"
    if cat.lower().startswith("focus"):
        return "Focus Shoot"
    return cat or "Unknown"


# ---------------------------------------------------------------- http helpers
class ApiError(Exception):
    pass


class AuthError(ApiError):
    pass


def api(method, path, body=None, query=None):
    if not STATE["token"]:
        raise ApiError("Not connected: paste your token first.")
    url = API + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json, text/plain, */*")
    req.add_header("Authorization", "Bearer " + STATE["token"])
    req.add_header("X-Request-Id", str(uuid.uuid4()))
    req.add_header("User-Agent", "Mozilla/5.0 spyne-resequencer")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    last = None
    for attempt in range(5 if method == "GET" else 1):   # retry reads only; never re-POST a write
        try:
            with urllib.request.urlopen(req, timeout=90, context=SSL_CTX) as r:
                raw = r.read().decode("utf-8", "replace")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "replace")[:300]
            last = ApiError("HTTP %s from %s: %s" % (e.code, path, txt))
            if e.code in (401, 403):
                raise AuthError("HTTP %s - token expired or not allowed. Paste a fresh token and click Connect again." % e.code)
            if e.code in (500, 502, 503, 504, 429) and attempt < 4:
                time.sleep(2 * (attempt + 1))
                continue
            raise last
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
            if attempt < 4 and method == "GET":
                time.sleep(2 * (attempt + 1))
                continue
            break
    e = last
    if isinstance(e, ApiError):
        raise e
    reason = getattr(e, "reason", e)
    hint = " (Mac: run 'Install Certificates.command' from your Python folder)" if "CERTIFICATE" in str(reason).upper() else ""
    raise ApiError("Network error: %s%s" % (reason, hint))


def find_vehicles(obj, out):
    """Recursively collect vehicle docs from the (unknown-shaped) list response."""
    if isinstance(obj, dict):
        did = obj.get("dealerVinId")
        vin = obj.get("vin") or obj.get("vinName")
        if isinstance(did, str) and isinstance(vin, str):
            out[vin.strip().upper()] = {
                "dealerVinId": did, "mediaId": obj.get("mediaId"),
                "sources": obj.get("mediaSources") or [],
                "imagesStatus": obj.get("imagesStatus"), "mediaStatus": obj.get("mediaStatus"),
                "outCount": obj.get("output_image_count"), "inCount": obj.get("input_image_count"),
                "thumb": obj.get("thumbnail_output_url") or obj.get("thumbnail_input_url"),
                "name": " ".join(str(x) for x in (obj.get("year"), obj.get("make"), obj.get("model")) if x),
                "stock": obj.get("stockNumber"),
            }
            return
        for v in obj.values():
            find_vehicles(v, out)
    elif isinstance(obj, list):
        for v in obj:
            find_vehicles(v, out)


def load_inventory(include_sold=False):
    found = {}
    page, per_page = 1, 100
    while page <= 50:
        body = {"enterpriseId": STATE["enterpriseId"], "teamId": STATE["teamId"], "page": page,
                "per_page": per_page, "sort_by": {"creationEpoch": "desc"}, "dealerLang": "en",
                "isSold": bool(include_sold)}
        res = api("POST", "/inventory/v2/list", body)
        before = len(found)
        find_vehicles(res, found)
        if len(found) == before:
            break
        page += 1
    STATE["vin_map"].update(found)
    return len(STATE["vin_map"])


def get_vehicle_images(dealer_vin_id):
    res = api("GET", "/inventory/v2", query={"dealerVinId": dealer_vin_id})
    d = (res or {}).get("data") or {}
    vm = d.get("vehicleMedia") or {}
    rec = vm.get("mediaRecord") or {}
    media_id = rec.get("_id") or vm.get("mediaId")
    try:
        imgs = rec["outputData"]["catalog"]["processedData"]["image_data"]
    except (KeyError, TypeError):
        imgs = []
    imgs = sorted(imgs, key=lambda i: int(i.get("frame_no") or 0))
    return d.get("vin"), media_id, imgs


# ---------------------------------------------------------------- planning
def build_target(imgs, seq):
    """Stable re-order: images grouped by the label order in `seq`; unlisted labels go last."""
    rank = {lab: i for i, lab in enumerate(seq)}
    big = len(seq) + 1
    keyed = [(rank.get(image_label(im), big), idx, im) for idx, im in enumerate(imgs)]
    keyed.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in keyed]


def is_app_sourced(meta):
    return any("APP" in str(x).upper() for x in (meta.get("sources") or []))


def plan_one(vin, seq, use_meta_shortcuts=True):
    vin = vin.strip().upper()
    meta = STATE["vin_map"].get(vin)
    row = {"vin": vin, "status": "ok", "warnings": []}
    if not meta:
        row.update(status="not_found", message="VIN not in loaded inventory")
        return row
    did = meta["dealerVinId"]
    row.update(dealerVinId=did, name=meta.get("name"), sources=meta.get("sources"), listThumb=meta.get("thumb"))
    if use_meta_shortcuts and (meta.get("outCount") in (0, None) and meta.get("imagesStatus") not in ("DONE", None)):
        row.update(status="no_images", message="Images not processed (%s) - nothing to sequence" % meta.get("imagesStatus"))
        return row
    try:
        _, media_id, imgs = get_vehicle_images(did)
    except AuthError:
        raise
    except ApiError as e:
        row.update(status="error", message=str(e))
        return row
    if not media_id or not imgs:
        row.update(status="no_images", message="No catalog images to sequence (Raw / Photo NA / unsupported?)")
        return row
    target = build_target(imgs, seq)
    ref_exact = (STATE.get("refs") or {}).get(vin)
    if ref_exact and sorted(ref_exact) == sorted(i["image_id"] for i in imgs):
        byid = {i["image_id"]: i for i in imgs}
        target = [byid[x] for x in ref_exact]  # exact order of the uploaded correct folder
    cur_ids = [i["image_id"] for i in imgs]
    new_ids = [i["image_id"] for i in target]
    labs = [image_label(i) for i in imgs]
    present = set(labs)
    extra = sorted(present - set(seq))
    if extra:
        row["warnings"].append("Not in your sequence (placed last): " + ", ".join(extra))
    row.update(
        mediaId=media_id, total=len(imgs), changed=(cur_ids != new_ids),
        heroNow=labs[0], heroNew=image_label(target[0]),
        current=[{"id": i["image_id"], "label": image_label(i), "angle": i.get("angle"), "thumb": i.get("lowres_output") or i.get("input_image")} for i in imgs],
        target=[{"id": i["image_id"], "label": image_label(i), "angle": i.get("angle"), "thumb": i.get("lowres_output") or i.get("input_image")} for i in target],
    )
    ref = (STATE.get("refs") or {}).get(vin)
    if ref:
        row["refVin"] = True
        row["matchesRefNow"] = (cur_ids == ref)
        row["targetMatchesRef"] = (new_ids == ref)
        if new_ids != ref:
            row["warnings"].append("Reference mismatch: computed order differs from the uploaded correct folder for this VIN")
    pat = STATE.get("ref_pattern")
    if pat:
        miss = [l for l in pat if l not in present]
        if miss:
            row["warnings"].append("Missing vs reference: " + ", ".join("%s" % l for l in miss))
        cnt = {}
        for l in labs:
            cnt[l] = cnt.get(l, 0) + 1
        if "Interior" in pat and cnt.get("Interior", 0) < 1:
            row["warnings"].append("No interior images (reference has them)")
    if not row["changed"]:
        row["status"] = "already_ok"
    return row


def apply_one(vin, seq, user_id):
    """Re-reads the vehicle, re-plans, backs up, posts, verifies."""
    p = plan_one(vin, seq)
    if p["status"] not in ("ok",):
        return {"vin": p["vin"], "result": p["status"], "message": p.get("message", "nothing to do")}
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bpath = os.path.join(BACKUP_DIR, "%s_%s.json" % (p["vin"], stamp))
    with open(bpath, "w") as f:
        json.dump({"vin": p["vin"], "dealerVinId": p["dealerVinId"], "mediaId": p["mediaId"],
                   "original_order": [c["id"] for c in p["current"]],
                   "applied_order": [c["id"] for c in p["target"]]}, f, indent=1)
    body = {
        "mediaId": p["mediaId"],
        "imageList": [{"imageId": t["id"], "fromSequenceNo": str(n + 1)} for n, t in enumerate(p["target"])],
        "enterpriseId": STATE["enterpriseId"],
        "userId": user_id or STATE["userId"],
    }
    api("POST", "/inventory/v2/media/image-sequencing", body)
    time.sleep(0.8)
    _, _, after = get_vehicle_images(p["dealerVinId"])
    ok = [i["image_id"] for i in after] == [t["id"] for t in p["target"]]
    return {"vin": p["vin"], "result": "applied" if ok else "applied_unverified", "backup": os.path.basename(bpath),
            "message": "verified" if ok else "POST succeeded but re-read order differs (may be eventual consistency) - check in console"}


def undo_backup(name):
    path = os.path.join(BACKUP_DIR, os.path.basename(name))
    with open(path) as f:
        b = json.load(f)
    body = {
        "mediaId": b["mediaId"],
        "imageList": [{"imageId": iid, "fromSequenceNo": str(n + 1)} for n, iid in enumerate(b["original_order"])],
        "enterpriseId": STATE["enterpriseId"], "userId": STATE["userId"],
    }
    api("POST", "/inventory/v2/media/image-sequencing", body)
    return {"vin": b["vin"], "result": "restored", "message": "original order re-applied"}


REF_RE = re.compile(r"^(?P<grp>.+?)_(?P<frame>\d+)_(?P<cat>.+)_(?P<id>[0-9a-fA-F]{32})\.[A-Za-z0-9]+$")
VIN_RE = re.compile(r"[A-HJ-NPR-Z0-9]{17}")


def parse_ref_paths(paths):
    """paths like 'KNDEUCAA3R7580285/1_7_Exterior_<id>.jpg' -> {VIN: [{frame, category, id}]}"""
    refs = {}
    for p in paths:
        parts = [x for x in str(p).replace("\\", "/").split("/") if x]
        if not parts:
            continue
        m = REF_RE.match(parts[-1])
        if not m:
            continue
        vin = None
        for comp in reversed(parts[:-1]):
            mv = VIN_RE.search(comp.upper())
            if mv:
                vin = mv.group(0)
                break
        if not vin:
            continue
        refs.setdefault(vin, []).append({"frame": int(m.group("frame")), "category": m.group("cat"), "id": m.group("id").lower()})
    return refs


def _img_has_id(img, rid):
    try:
        return rid.lower() in json.dumps(img).lower()
    except Exception:
        return False


def verify_reference(refs):
    """refs: {VIN: [{frame, category, id}]} parsed from the uploaded correct folders.
    Looks every reference image up in the VIN's live record (matching image id), derives the label
    order from the correct order, and cross-checks category + ids."""
    reports, derived = [], []
    STATE["refs"] = {}
    for vin, items in refs.items():
        vin = vin.strip().upper()
        items = sorted(items, key=lambda x: int(x.get("frame") or 0))
        rep = {"vin": vin, "files": len(items), "matched": 0, "unmatched": 0, "catMismatch": 0,
               "inInventory": False, "labels": [], "notes": []}
        meta = STATE["vin_map"].get(vin)
        if not meta:
            rep["notes"].append("VIN not in connected inventory - cannot look up image angles")
            reports.append(rep)
            continue
        rep["inInventory"] = True
        _, _, imgs = get_vehicle_images(meta["dealerVinId"])
        rep["liveImages"] = len(imgs)
        ordered, labs = [], []
        for it in items:
            hit = next((im for im in imgs if _img_has_id(im, it["id"])), None)
            if not hit:
                rep["unmatched"] += 1
                continue
            rep["matched"] += 1
            cat = (hit.get("image_category") or "").lower()
            fc = (it.get("category") or "").lower()
            if fc and cat and not (cat.startswith(fc[:5]) or fc.startswith(cat[:5])):
                rep["catMismatch"] += 1
            ordered.append(hit["image_id"])
            labs.append(image_label(hit))
        if rep["unmatched"]:
            rep["notes"].append("%d file(s) not found in the live VIN record (image ids differ)" % rep["unmatched"])
        if rep["matched"] and not rep["unmatched"] and len(imgs) != len(items):
            rep["notes"].append("Folder has %d images, console has %d" % (len(items), len(imgs)))
        seq = []
        for l in labs:
            if l not in seq:
                seq.append(l)
        rep["labels"] = labs
        rep["sequence"] = seq
        # self-consistency: does our sort of the live images with this sequence reproduce the folder order?
        if rep["matched"] == len(items) and items:
            rep["selfCheck"] = ([image_label(i) for i in build_target(imgs, seq)] == labs) if len(imgs) == len(items) else None
            if len(imgs) == len(items):
                STATE["refs"][vin] = ordered
            derived.append(seq)
        reports.append(rep)
    out = {"reports": reports, "sequence": None, "agree": None}
    if derived:
        keys = ["|".join(d) for d in derived]
        best = max(set(keys), key=keys.count)
        out["sequence"] = best.split("|")
        out["agree"] = keys.count(best)
        out["of"] = len(derived)
        STATE["ref_pattern"] = out["sequence"]
    else:
        STATE["ref_pattern"] = None
    return out


def parse_console_link(link):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(link.strip()).query)
    ent = (q.get("enterprise_id") or q.get("enterpriseId") or [""])[0]
    team = (q.get("team_id") or q.get("teamId") or [""])[0]
    return ent, team


def _scan_one(v, seq):
    if SCAN["cancel"]:
        return None
    try:
        return plan_one(v, seq)
    except AuthError as e:
        SCAN["cancel"] = True
        SCAN["error"] = str(e)
        return None
    except Exception as e:  # noqa - never let one VIN kill the scan
        return {"vin": v, "status": "error", "warnings": [], "message": "%s: %s" % (type(e).__name__, e)}


def scan_job(vins, seq, workers):
    try:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(_scan_one, v, seq) for v in vins]
            for f in as_completed(futs):
                row = f.result()
                SCAN["done"] += 1
                if row is not None:
                    SCAN["rows"].append(row)
    except Exception as e:  # noqa
        SCAN["error"] = "%s: %s" % (type(e).__name__, e)
    SCAN["running"] = False


# ---------------------------------------------------------------- web server
def normalise_seq(raw):
    if isinstance(raw, str):
        parts = re.split(r"[\n,;|>]+|\s+->\s+|\s*→\s*", raw)
    else:
        parts = raw
    seq, seen = [], set()
    for p in parts:
        c = canon_label(p)
        if c and c.lower() not in seen:
            seen.add(c.lower())
            seq.append(c)
    return seq


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj, ctype="application/json"):
        data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if self.path == "/api/backups":
            names = sorted(os.listdir(BACKUP_DIR), reverse=True) if os.path.isdir(BACKUP_DIR) else []
            return self._send(200, {"backups": names})
        if self.path == "/api/labels":
            return self._send(200, {"labels": CANON})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "bad json"})
        try:
            with LOCK:
                self._send(200, self.route(self.path, body))
        except ApiError as e:
            self._send(200, {"error": str(e)})
        except Exception as e:  # noqa
            self._send(500, {"error": "%s: %s" % (type(e).__name__, e)})

    def route(self, path, b):
        if path == "/api/connect":
            tok = (b.get("token") or "").strip()
            if tok.lower().startswith("bearer "):
                tok = tok[7:].strip()
            ent, team = parse_console_link(b.get("link") or "")
            ent = ent or (b.get("enterpriseId") or "").strip()
            team = team or (b.get("teamId") or "").strip()
            if not (ent and team and tok):
                return {"error": "Need a console link containing enterprise_id & team_id, and a token."}
            STATE.update(token=tok, enterpriseId=ent, teamId=team,
                         userId=(b.get("userId") or "").strip(), vin_map={})
            n = load_inventory(bool(b.get("includeSold")))
            counts = {}
            for m in STATE["vin_map"].values():
                for src in (m.get("sources") or ["(none)"]):
                    counts[str(src)] = counts.get(str(src), 0) + 1
            app_n = sum(1 for m in STATE["vin_map"].values() if is_app_sourced(m))
            return {"ok": True, "vehicles": n, "enterpriseId": ent, "teamId": team,
                    "sources": counts, "appVins": app_n}
        if path == "/api/scan":
            if SCAN["running"]:
                return {"error": "A scan is already running."}
            seq = normalise_seq(b.get("sequence") or "")
            if not seq:
                return {"error": "No sequence yet. In step 2 upload a correct VIN folder/zip or enter a reference VIN, then click Learn."}
            only = re.findall(r"\b[A-HJ-NPR-Z0-9]{17}\b", (b.get("onlyVins") or "").upper())
            if only:
                vins = only
            else:
                vins = sorted(STATE["vin_map"].keys())
                src = b.get("source", "all")
                if src == "app":
                    vins = [v for v in vins if is_app_sourced(STATE["vin_map"][v])]
                elif src != "all":
                    vins = [v for v in vins if src in [str(x) for x in (STATE["vin_map"][v].get("sources") or ["(none)"])]]
            if not vins:
                return {"error": "No matching vehicles. Connect first, or switch Source to 'All'."}
            workers = max(1, min(12, int(b.get("workers") or 4)))
            SCAN.update(running=True, cancel=False, done=0, total=len(vins), rows=[], error=None, sequence=seq)
            threading.Thread(target=scan_job, args=(vins, seq, workers), daemon=True).start()
            return {"started": len(vins)}
        if path == "/api/reference":
            if not STATE.get("token"):
                return {"error": "Connect first (step 1), then upload the correct folder."}
            import base64, io, zipfile
            paths = list(b.get("paths") or [])
            for z in (b.get("zips") or []):
                try:
                    zf = zipfile.ZipFile(io.BytesIO(base64.b64decode(z.get("b64") or "")))
                except Exception as e:  # noqa
                    return {"error": "Could not read zip %s: %s" % (z.get("name"), e)}
                zname = os.path.splitext(os.path.basename(z.get("name") or ""))[0]
                for n in zf.namelist():
                    if not n.endswith("/"):
                        paths.append((n if VIN_RE.search(n.upper()) else zname + "/" + n))
            refs = parse_ref_paths(paths)
            for v in set(VIN_RE.findall((b.get("vins") or "").upper())):
                meta = STATE["vin_map"].get(v)
                if not meta:
                    return {"error": "Reference VIN %s is not in the loaded inventory (check 'include sold' and reconnect)." % v}
                _, _, imgs = get_vehicle_images(meta["dealerVinId"])
                refs[v] = [{"frame": int(i.get("frame_no") or n), "category": i.get("image_category") or "", "id": i["image_id"]} for n, i in enumerate(imgs)]
            if not refs:
                return {"error": "No VIN folders with files like 1_7_Exterior_<imageid>.jpg were found (folder name must contain the 17-character VIN)."}
            return verify_reference(refs)
        if path == "/api/reference_clear":
            STATE["refs"] = {}
            STATE["ref_pattern"] = None
            return {"ok": True}
        if path == "/api/scan_cancel":
            SCAN["cancel"] = True
            return {"ok": True}
        if path == "/api/scan_status":
            since = int(b.get("since") or 0)
            st = {k: SCAN[k] for k in ("running", "done", "total", "error", "sequence")}
            st["rows"] = SCAN["rows"][since:]
            st["n"] = len(SCAN["rows"])
            errs = {}
            for r in SCAN["rows"]:
                if r.get("status") == "error":
                    k = re.sub(r"\s+", " ", (r.get("message") or "")[:90])
                    errs[k] = errs.get(k, 0) + 1
            st["errors"] = errs
            return st
        if path == "/api/plan":
            seq = normalise_seq(b.get("sequence") or "")
            if not seq:
                return {"error": "Sequence is empty."}
            vins = [v.strip().upper() for v in b.get("vins", []) if v.strip()]
            return {"sequence": seq, "rows": [plan_one(v, seq) for v in vins]}
        if path == "/api/apply":
            seq = normalise_seq(b.get("sequence") or "")
            out = []
            for v in b.get("vins", []):
                try:
                    out.append(apply_one(v, seq, b.get("userId")))
                except ApiError as e:
                    out.append({"vin": v, "result": "error", "message": str(e)})
                time.sleep(0.4)
            return {"results": out}
        if path == "/api/undo":
            return {"results": [undo_backup(n) for n in b.get("backups", [])]}
        return {"error": "unknown route"}


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    if not os.environ.get("NO_BROWSER"):
        threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:%d" % PORT)).start()
    print("Spyne Resequencer running at http://127.0.0.1:%d  (Ctrl+C to stop)" % PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)
