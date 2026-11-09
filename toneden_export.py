#!/usr/bin/env python3
"""Expand a ToneDen privacy export via the ToneDen API and download referenced assets.

  TONEDEN_TOKEN=<bearer> python toneden_export.py ToneDen_Privacy_Data.json -o out/ -v
  TONEDEN_COOKIE=<connect.sid value> python toneden_export.py export.json -o out/   # unofficial
  TONEDEN_CLIENT_ID=.. TONEDEN_CLIENT_SECRET=.. python toneden_export.py export.json --oauth

Accepts both export layouts: single account (root has "Personal Data") and multi-account
(root has "Accounts": {id: {...}}). A token/cookie only reaches the account that authorized it
(plus what it manages); use --token-map '{"<userID>": "<token or connect.sid=...>"}' for
per-account credentials. The cookie is a full session credential; it is sent only to the API host.

Output:
  out/export.json                              copy of the input export
  out/run.log                                  full log (every request/download), always written
  out/accounts/<uid>/api/...                   raw JSON per endpoint; _errors.json = non-200 responses
        incl. unofficial site endpoints: attachments/<id>/gate_metadata.json (gateMetadata),
        audience_fans.json and attachments/<id>/fans.json (audience/fans?filters=...)
        unlocked/<user>/<unlock id>/post.json + gate_metadata.json (campaigns this account unlocked)
  out/assets/<kind>/<id>/<file>                downloaded assets, e.g.
        assets/attachments/12345/art.png       assets/links/678/bg.jpg
        assets/account/<uid>/avatar.jpg        assets/unlocked/<user>/<unlock id>/<download_display_url>
      (unlocked = other creators' campaigns this account unlocked, kept apart from own attachments)
      kind = attachments | links | campaigns | unlocked | account. A URL used by several owners is stored once
      per owner (hardlink, else copy). Extension comes from the name, else magic bytes, else
      Content-Type, else the URL. Which API response/export section held each URL (origin) and
      the JSON field are recorded in manifest.json, not in folders.
  out/manifest.json                            url -> files / status / sha256 / sources (origin, field)

Requires: requests
"""
import argparse
import hashlib
import http.server
import json
import mimetypes
import os
import re
import shutil
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

import requests

API = "https://www.toneden.io/api/v1"
AUTH_URL = "https://www.toneden.io/auth/oauth2/authorize"
TOKEN_URL = "https://www.toneden.io/auth/oauth2/token"
UA = "toneden-export/1.2"

# /links/{id}/insights requires a `metric` param; its allowed values aren't in the public docs
# ("rsvps" is livestream-only). Candidates are probed; metrics that never succeed get dropped.
# Override with --link-metrics. Results: accounts/<uid>/api/_insight_metrics.json
LINK_METRICS = ["clicks", "clickthroughs", "email_clicks", "rsvps"]

# A URL is downloaded if its path has a MEDIA_EXT extension ("strong"), or if the JSON key it
# sits under matches MEDIA_KEY ("weak"; skipped if the host is a known page host or the
# response is HTML).
MEDIA_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".avif", ".mp3", ".wav",
             ".m4a", ".aac", ".ogg", ".flac", ".mp4", ".mov", ".webm", ".pdf", ".zip"}
MEDIA_KEY = re.compile(r"artwork|image|img|photo|avatar|cover|thumb|preview|banner|logo|"
                       r"background|(^|[_\s])bg([_\s]|$)|audio|video|stream|download|file", re.I)
PAGE_HOSTS = re.compile(r"(^|\.)(youtube\.com|youtu\.be|vimeo\.com|soundcloud\.com|spotify\.com|"
                        r"apple\.com|facebook\.com|instagram\.com|twitter\.com|x\.com)$", re.I)
URL_RE = re.compile(r"https?://\S+$")
ID_KEYS = ("id", "ID", "Id", "_id")

VERBOSE = False
LOGF = None
_log_lock = threading.Lock()


def _emit(msg, console):
    with _log_lock:
        if LOGF:
            LOGF.write(f"{datetime.now().isoformat(timespec='seconds')} {msg}\n")
            LOGF.flush()
        if console:
            print(msg, file=sys.stderr, flush=True)


def log(*a):
    _emit(" ".join(str(x) for x in a), True)


def vlog(*a):
    _emit(" ".join(str(x) for x in a), VERBOSE)


def unwrap(data):
    """API wraps single objects: {"attachment": {...}}, {"link": {...}}, {"campaign": {...}}."""
    if isinstance(data, dict) and len(data) == 1:
        v = next(iter(data.values()))
        if isinstance(v, dict):
            return v
    return data


def iid(item):
    item = unwrap(item)
    if isinstance(item, dict):
        for k in ID_KEYS:
            if item.get(k) is not None:
                return item[k]
    return None


def as_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("attachments", "links", "campaigns", "userLists", "fans", "data", "results", "items"):
            if isinstance(data.get(k), list):
                return data[k]
        for v in data.values():
            if isinstance(v, list):
                return v
    return []


def dump(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False))


def slug(s, n=60):
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")[:n] or "x"


def safe_name(s, n=150):
    """Keep the name readable (spaces, parens, unicode); drop only path/illegal characters."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", s).strip(" .")[:n]


class Api:
    def __init__(self, base, cred):
        self.base = base.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"Accept": "application/json", "User-Agent": UA})
        if cred.startswith("connect.sid="):  # unofficial: logged-in web session cookie
            self.s.headers["Cookie"] = cred
        else:
            self.s.headers["Authorization"] = f"Bearer {cred}"
        self.errors = []

    def get(self, path, params=None, probe=False):
        """probe=True: a 400 is an expected answer (not recorded in errors)."""
        last = None
        for n in range(6):
            t0 = time.time()
            try:
                r = self.s.get(self.base + path, params=params, timeout=30, allow_redirects=False)
            except requests.RequestException as e:
                last = {"path": path, "params": params, "status": 0, "body": str(e)}
                vlog(f"GET {path} {params or ''} -> error {e}; retry in {2 ** n}s")
                time.sleep(2 ** n)
                continue
            vlog(f"GET {path} {params or ''} -> {r.status_code} {len(r.content)}B "
                 f"{time.time() - t0:.2f}s")
            if r.status_code == 429 or r.status_code >= 500:
                try:
                    wait = float(r.headers.get("Retry-After", ""))
                except ValueError:
                    wait = 2 ** n
                last = {"path": path, "params": params, "status": r.status_code, "body": r.text[:500]}
                log(f"  {r.status_code} on {path}; waiting {min(wait, 120):.0f}s (try {n + 1}/6)")
                time.sleep(min(wait, 120))
                continue
            try:
                data = r.json()
            except ValueError:
                data = r.text[:500]
                if r.status_code == 200:  # e.g. HTML login page instead of JSON
                    self.errors.append({"path": path, "params": params, "status": "non-json", "body": data})
                    return -1, None
            if r.status_code != 200 and not (probe and r.status_code == 400):
                self.errors.append({"path": path, "params": params, "status": r.status_code, "body": data})
            return r.status_code, data
        self.errors.append(last)
        return 0, None

    def paginate(self, path, params=None, limits=(100, 25, None), max_pages=10000):
        # limit's max is undocumented: fall back to smaller/no limit if the first page 400s
        for limit in limits:
            out, seen, offset = [], set(), 0
            for _ in range(max_pages):
                q = {**(params or {}), "offset": offset}
                if limit:
                    q["limit"] = limit
                st, data = self.get(path, q)
                if st == 400 and offset == 0 and limit != limits[-1]:
                    self.errors.pop()
                    break
                if st != 200:
                    return out
                items = as_list(data)
                sig = hashlib.sha1(json.dumps(items, sort_keys=True, default=str).encode()).hexdigest()
                if not items or sig in seen:  # empty page, or server ignoring offset
                    return out
                seen.add(sig)
                out.extend(items)
                offset += len(items)
                vlog(f"  paginate {path}: {len(out)} items so far")
            else:
                return out
        return []


# ---------------------------------------------------------------- asset discovery

def norm(k):
    return re.sub(r"[\s\-]+", "_", str(k)).lower()


def name_from(hint):
    """download_display_url holds the filename (occasionally a URL); reduce to a safe basename."""
    if not isinstance(hint, str) or not hint.strip():
        return None
    h = hint.strip()
    if URL_RE.match(h):
        h = unquote(urlsplit(h).path)
    h = unquote(h).replace("\\", "/").rsplit("/", 1)[-1]
    return safe_name(h) or None


def _leaf(url, key, hint):
    name = name_from(hint)
    strong = (os.path.splitext(unquote(urlsplit(url).path))[1].lower() in MEDIA_EXT
              or (name is not None and os.path.splitext(name)[1].lower() in MEDIA_EXT))
    if strong or MEDIA_KEY.search(key):
        yield url, strong, key, name


def harvest(o, key=""):
    """Yield (url, strong, field, filename_hint). A `<x>_url` with sibling `<x>_display_url`
    (e.g. download_url / download_display_url) takes the sibling's value as its filename."""
    if isinstance(o, dict):
        nk = {norm(k): v for k, v in o.items()}
        for k, v in o.items():
            n = norm(k)
            if n.endswith("_display_url"):
                continue
            if isinstance(v, str) and URL_RE.match(v):
                hint = nk.get(n[:-4] + "_display_url") if n.endswith("_url") else None
                yield from _leaf(v, str(k), hint)
            else:
                yield from harvest(v, str(k))
    elif isinstance(o, list):
        for v in o:
            yield from harvest(v, key)
    elif isinstance(o, str) and URL_RE.match(o):
        yield from _leaf(o, key, None)


def _owner(item, uid):
    if isinstance(item, dict):
        if item.get("Attachment ID") is not None:
            return "attachments", item["Attachment ID"]
        if item.get("Link ID") is not None:
            return "links", item["Link ID"]
    return "account", uid


def _exp(origin, v, uid):
    if isinstance(v, list):
        for it in v:
            kind, oid = _owner(it, uid)
            yield kind, oid, origin, it
    elif isinstance(v, dict):
        if any(isinstance(x, list) for x in v.values()):  # e.g. Hosted Media Files: {name: [..]}
            for sk, sv in v.items():
                yield from _exp(f"export_{slug(sk, 50)}", sv, uid)
        else:
            yield "account", uid, origin, v


def export_sources(acct, uid):
    """(kind, id, origin, obj): export items attributed to the attachment/link they describe."""
    for sect, val in acct.items():
        if sect == "Interactions & Activity" and isinstance(val, dict):
            for sub, v in val.items():
                yield from _exp(f"export_{slug(sub, 50)}", v, uid)
        else:
            yield "account", uid, f"export_{slug(sect, 50)}", val


# ---------------------------------------------------------------- downloading

KNOWN_EXT = MEDIA_EXT | {".jpe", ".tif", ".tiff", ".heic", ".opus", ".aif", ".aiff", ".mid", ".midi",
                         ".m4v", ".mkv", ".avi", ".wma", ".txt", ".json", ".csv", ".xml", ".html"}
CT_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp",
          "image/svg+xml": ".svg", "image/avif": ".avif", "image/bmp": ".bmp", "image/tiff": ".tif",
          "image/heic": ".heic", "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/wav": ".wav",
          "audio/x-wav": ".wav", "audio/wave": ".wav", "audio/ogg": ".ogg", "audio/mp4": ".m4a",
          "audio/x-m4a": ".m4a", "audio/aac": ".aac", "audio/flac": ".flac", "audio/x-flac": ".flac",
          "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov",
          "application/pdf": ".pdf", "application/zip": ".zip"}


def sniff_ext(h):
    """Extension from magic bytes (first ~512 bytes); None if unrecognised."""
    if h.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if h.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if h[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if h[:4] == b"RIFF":
        return {b"WEBP": ".webp", b"WAVE": ".wav", b"AVI ": ".avi"}.get(h[8:12])
    if h.startswith(b"%PDF-"):
        return ".pdf"
    if h.startswith(b"ID3") or h[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xfa"):
        return ".mp3"
    if h.startswith(b"OggS"):
        return ".ogg"
    if h.startswith(b"fLaC"):
        return ".flac"
    if h[4:8] == b"ftyp":
        brand = h[8:12]
        if brand[:3] == b"M4A":
            return ".m4a"
        if brand == b"qt  ":
            return ".mov"
        if brand in (b"avif", b"avis"):
            return ".avif"
        if brand[:3] == b"hei" or brand[:3] == b"mif":
            return ".heic"
        return ".mp4"
    if h.startswith(b"\x1a\x45\xdf\xa3"):
        return ".webm"
    if h.startswith(b"PK\x03\x04"):
        return ".zip"
    if h[:4] in (b"II*\x00", b"MM\x00*"):
        return ".tif"
    if h[:4] == b"MThd":
        return ".mid"
    if h[:4] == b"FORM" and h[8:12] in (b"AIFF", b"AIFC"):
        return ".aiff"
    if h[:2] == b"BM" and len(h) >= 14 and h[6:10] == b"\x00\x00\x00\x00":
        return ".bmp"
    t = h.lstrip(b"\xef\xbb\xbf \t\r\n")[:512].lower()
    if t.startswith(b"<svg") or (t.startswith(b"<?xml") and b"<svg" in t):
        return ".svg"
    return None


def with_ext(name, head, ct, url):
    """Append an extension unless the name already ends in a known one. Order: magic bytes,
    content-type, then the URL path's own extension."""
    if Path(name).suffix.lower() in KNOWN_EXT:
        return name
    url_ext = Path(unquote(urlsplit(url).path)).suffix.lower()
    ext = sniff_ext(head) or CT_EXT.get(ct) or (url_ext if url_ext in KNOWN_EXT else "")
    if not ext:
        vlog(f"no extension determinable for {name!r} ({ct or 'no content-type'}) {url}")
    return name + ext


class Placer:
    """Chooses collision-free filenames; one URL's name never overwrites another URL's file."""

    def __init__(self, prior_claims):
        self.claimed = dict(prior_claims)  # path str -> url
        self.lock = threading.Lock()

    def claim(self, path, url):
        with self.lock:
            for p in (path, path.with_name(f"{path.stem}_{hashlib.sha1(url.encode()).hexdigest()[:8]}{path.suffix}")):
                owner = self.claimed.get(str(p))
                if owner == url or (owner is None and not p.exists()):
                    self.claimed[str(p)] = url
                    return p
            return None


def link_or_copy(src, dst):
    if dst.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def head_of(path, n=512):
    with open(path, "rb") as f:
        return f.read(n)


def src_str(sources, n=6):
    """Compact 'kind/id:origin[field]' list for log lines."""
    parts = [f"{x['kind']}/{x['id']}:{x['origin']}[{x['field']}]" for x in sources[:n]]
    return ", ".join(parts) + (f" (+{len(sources) - n} more)" if len(sources) > n else "")


def fetch_asset(url, info, out, placer, prior):
    """Files land in assets/<kind>/<id>/<name>. A URL shared by several owners is stored once
    per owner (hardlink, else copy). Where each URL was found is recorded in the manifest."""
    root = out / "assets"
    dirs = [root / kind / str(oid) for (kind, oid) in sorted(info["dests"])]
    base = {"url": url,
            "sources": [{"kind": k, "id": i, "origin": o, "field": f}
                        for (k, i), of in sorted(info["dests"].items()) for o, f in sorted(of)]}

    src = src_str(base["sources"])
    # reuse a previous run's download; fix its extension / layout if needed
    rec = prior.get(url)
    if rec and rec.get("status") in (200, "cached"):
        old = [out / f for f in rec.get("files", [])]
        prev = next((f for f in old if f.exists()), None)
        if prev:
            name = with_ext(prev.name, head_of(prev), rec.get("content_type") or "", url)
            files = []
            for d in dirs:
                p = placer.claim(d / name, url)
                link_or_copy(prev, p)
                files.append(p)
            for f in old:  # drop this tool's own superseded files (old layout / no extension)
                if f not in files and f.exists() and root in f.parents:
                    f.unlink()
                    vlog(f"removed superseded {f.relative_to(out)}")
                    for par in f.parents:
                        if par == root:
                            break
                        try:
                            par.rmdir()
                        except OSError:
                            break
            vlog(f"cached {url} -> {len(files)} file(s) <- {src}")
            return {**base, "status": "cached", "files": [str(p.relative_to(out)) for p in files],
                    "bytes": files[0].stat().st_size, "sha256": rec.get("sha256"),
                    "content_type": rec.get("content_type")}

    if not info["strong"] and PAGE_HOSTS.search(urlsplit(url).hostname or ""):
        vlog(f"skip (page host) {url} <- {src}")
        return {**base, "status": "skipped-page-host", "files": []}
    for n in range(4):
        try:
            # plain requests.get: credentials must never be sent to third-party hosts
            with requests.get(url, stream=True, timeout=60, headers={"User-Agent": UA}) as r:
                vlog(f"GET {url} -> {r.status_code} {r.headers.get('Content-Type', '')} "
                     f"{r.headers.get('Content-Length', '?')}B <- {src}")
                if r.status_code == 429 or r.status_code >= 500:
                    time.sleep(2 ** n)
                    continue
                if r.status_code != 200:
                    return {**base, "status": r.status_code, "files": []}
                ct = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if not info["strong"] and ct in ("text/html", "application/xhtml+xml"):
                    vlog(f"skip (html) {url} <- {src}")
                    return {**base, "status": "skipped-html", "files": []}
                dirs[0].mkdir(parents=True, exist_ok=True)
                tmp = dirs[0] / f".{hashlib.sha1(url.encode()).hexdigest()[:10]}.part"
                sha, size = hashlib.sha256(), 0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        f.write(chunk)
                        sha.update(chunk)
                        size += len(chunk)
            name = info["hint"] or safe_name(unquote(Path(urlsplit(url).path).name)) or "file"
            name = with_ext(name, head_of(tmp), ct, url)
            first = placer.claim(dirs[0] / name, url)
            os.replace(tmp, first)
            files = [first]
            for d in dirs[1:]:
                p = placer.claim(d / first.name, url)
                link_or_copy(first, p)
                files.append(p)
            vlog(f"saved {url} -> {', '.join(str(p.relative_to(out)) for p in files)} ({size}B) <- {src}")
            return {**base, "status": 200, "files": [str(p.relative_to(out)) for p in files],
                    "bytes": size, "sha256": sha.hexdigest(), "content_type": ct}
        except requests.RequestException as e:
            vlog(f"download error {url}: {e}; retry <- {src}")
            time.sleep(2 ** n)
    return {**base, "status": "error", "files": []}


# ---------------------------------------------------------------- auth

def oauth(client_id, client_secret, redirect):
    u, got = urlsplit(redirect), {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            got["code"] = (parse_qs(urlsplit(self.path).query).get("code") or [None])[0]
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Authorized. Return to the terminal.")

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer((u.hostname, u.port or 80), H)
    url = AUTH_URL + "?" + urlencode({"response_type": "code", "client_id": client_id,
                                      "redirect_uri": redirect})
    log("Authorize:", url)
    webbrowser.open(url)
    srv.handle_request()
    if not got.get("code"):
        sys.exit("OAuth: no code returned")
    r = requests.post(TOKEN_URL, json={"grant_type": "authorization_code", "code": got["code"],
                                       "redirect_uri": redirect, "client_id": client_id,
                                       "client_secret": client_secret}, timeout=30)
    r.raise_for_status()
    tok = r.json()["access_token"]
    print(f"Token (reuse via TONEDEN_TOKEN): {tok}", file=sys.stderr)  # not written to run.log
    return tok


# ---------------------------------------------------------------- per-account fetch

POST_RE = re.compile(r"^https?://(?:www\.)?toneden\.io/([^/?#]+)/post/([^/?#]+)", re.I)
UNLOCK_SECT = "Campaigns Interacted With (Gate Unlocks)"


def unlock_campaigns(api, ia, adir, sources, with_assets):
    """Campaigns this account unlocked (other people's attachments): campaign URL ->
    /posts?author_username&link_path -> post.unlock.id -> /attachments/{id}/gateMetadata.
    The viewer must be the account that completed the gate, else is_gate_open is false and there
    is no download_url. Assets go to assets/unlocked/<user>/<unlock id>/, apart from own attachments."""
    seen = {}
    for n, it in enumerate(ia.get(UNLOCK_SECT) or []):
        url = it.get("Campaign URL") if isinstance(it, dict) else None
        src = f"export['Interactions & Activity'][{UNLOCK_SECT!r}][{n}]['Campaign URL']"
        m = POST_RE.match(url or "")
        if not m:
            if url:
                vlog(f"  unlock campaign skipped (not a toneden.io/<user>/post/<path> URL): {url} <- {src}")
            continue
        seen.setdefault((unquote(m[1]), unquote(m[2])), (url, src))
    log(f"  {len(seen)} unlocked campaigns")
    for k, ((user, path), (url, src)) in enumerate(seen.items(), 1):
        vlog(f"  unlocked campaign {k}/{len(seen)}: {url} <- {src}")
        st, data = api.get("/posts", {"author_username": user, "link_path": path})
        if st != 200:
            log(f"  {st} /posts {user}/{path} <- {src}")
            continue
        post = (data.get("post") if isinstance(data, dict) else None) or {}
        unlock = post.get("unlock") or {}
        aid = unlock.get("id")
        if aid is None:
            log(f"  no post.unlock.id for {url} <- {src}")
            continue
        uname = safe_name(user) or "unknown"
        d = adir / "unlocked" / uname / str(aid)
        old_d = adir / "unlocked" / str(aid)  # earlier layout: unlocked/<id>
        if old_d.is_dir() and not d.exists():
            d.parent.mkdir(parents=True, exist_ok=True)
            old_d.rename(d)
        owner = f"{uname}/{aid}"  # assets/unlocked/<user>/<unlock id>/
        dump(d / "post.json", data)
        st, gm = api.get(f"/attachments/{aid}/gateMetadata")
        if st != 200:
            log(f"  {st} /attachments/{aid}/gateMetadata ({url}) <- {src}")
            continue
        dump(d / "gate_metadata.json", gm)
        md = (gm.get("metadata") if isinstance(gm, dict) else None) or {}
        dl = md.get("download_url")
        if not dl:
            log(f"  {url}: no download_url (is_gate_open={md.get('is_gate_open')}, "
                f"viewer_id={gm.get('viewer_id') if isinstance(gm, dict) else None})")
        if not with_assets:
            vlog(f"  unlock assets skipped (--no-unlock-assets) for {aid}")
            continue
        # only the campaign's own media; not author/collaborator avatars
        art = {"post.artwork_url": post.get("artwork_url"),
               "post.unlock.artwork_url": unlock.get("artwork_url"),
               "post.unlock.preview_url": unlock.get("preview_url")}
        sources.append(("unlocked", owner, "api_posts", art))
        if dl:
            # filename lives only in the posts response (download_display_url), not in gateMetadata
            name = unlock.get("download_display_url") or post.get("download_display_url")
            sources.append(("unlocked", owner, "api_gate_metadata+api_posts(name)",
                            {"download_url": dl, "download_display_url": name}))


def run_account(api, acct, uid, adir, playbook_ids, metrics, fans=True, unlock_assets=True):
    """Fetch everything for one account. Returns [(kind, id, origin, obj)] or None on auth failure."""
    ia = acct.get("Interactions & Activity", {})
    sources = list(export_sources(acct, uid))
    vlog(f"  {len(sources)} export sections/items scanned for assets")

    def grab(rel, path, params=None, owner=("account", uid), origin=None):
        st, data = api.get(path, params)
        if st == 200:
            dump(adir / (rel + ".json"), data)
            sources.append((*owner, origin or "api_" + slug(rel.rsplit("/", 1)[-1]), data))
            return data
        log(f"  {st} {path}")
        return None

    def add_list(kind, items, origin):
        for it in items:
            i = iid(it)
            sources.append((kind, i, origin, it) if i is not None else ("account", uid, origin, it))

    me = grab("me", "/users/me", origin="api_me")
    if me is None and api.errors and api.errors[-1]["status"] in (401, 403, 302, "non-json"):
        dump(adir / "_errors.json", api.errors)
        log(f"  auth failed on /users/me (token/cookie invalid or expired, or app not approved); skipping {uid}")
        return None
    if me and iid(me) is not None and str(iid(me)) != str(uid):
        log(f"  note: credential belongs to user {iid(me)}, not {uid}; user-scoped calls may 403")
    grab("user", f"/users/{uid}", origin="api_user")

    # attachments
    att = {}  # id -> "unlock" | "contest" | None
    for t in ("unlock", "contest"):
        items = api.paginate(f"/users/{uid}/attachments", {"type": t})
        log(f"  attachments ({t}): {len(items)}")
        dump(adir / f"attachments_list_{t}.json", items)
        add_list("attachments", items, "api_attachments_list")
        for it in items:
            if iid(it) is not None:
                att[iid(it)] = t
    for it in ia.get("Gate Attachments You Own (Creator Accounts)") or []:
        t = "contest" if "contest" in str(it.get("Type", "")).lower() else "unlock"
        att.setdefault(it.get("Attachment ID"), t)
    hosted = ia.get("Hosted Media Files (Stored in S3, Referenced by URL)") or {}
    for it in hosted.get("Attachment Media (artwork / previews)") or []:
        att.setdefault(it.get("Attachment ID"), None)
    att.pop(None, None)
    log(f"  {len(att)} attachments total")
    for n, (i, t) in enumerate(att.items(), 1):
        vlog(f"  attachment {i} ({n}/{len(att)})")
        o = ("attachments", i)
        d = grab(f"attachments/{i}/attachment", f"/attachments/{i}", owner=o, origin="api_attachment")
        if t is None:
            t = "contest" if "contest" in str((unwrap(d) or {}).get("type", "")).lower() else "unlock"
        # unofficial (site) endpoint
        grab(f"attachments/{i}/gate_metadata", f"/attachments/{i}/gateMetadata", owner=o,
             origin="api_gate_metadata")
        if t == "contest":
            grab(f"attachments/{i}/entries", f"/attachments/{i}/entries", {"aggregate_interval": "day"}, o)
            grab(f"attachments/{i}/entries_totals", f"/attachments/{i}/entries/totals", owner=o)
        else:
            grab(f"attachments/{i}/unlocks", f"/attachments/{i}/unlocks", {"aggregate_interval": "day"}, o)
            grab(f"attachments/{i}/unlocks_totals", f"/attachments/{i}/unlocks/totals", owner=o)
            grab(f"attachments/{i}/unlocks_platforms", f"/attachments/{i}/unlocks/platforms", owner=o)

    # campaigns this account unlocked
    unlock_campaigns(api, ia, adir, sources, unlock_assets)

    # audience fans (unofficial site endpoint; may not accept OAuth tokens). Contains third-party
    # personal data. Saved as JSON only: fan avatars etc. are not downloaded.
    if fans:
        def fan_list(rel, filters=None):
            params = {"filters": json.dumps(filters, separators=(",", ":"))} if filters else None
            items = api.paginate(f"/users/{uid}/audience/fans", params)
            dump(adir / f"{rel}.json", items)
            log(f"  fans {rel}: {len(items)}")
            return items
        fan_list("audience_fans")
        for i, t in att.items():
            if t is None:
                continue
            key = "contest_entered" if t == "contest" else "track_downloaded"
            fan_list(f"attachments/{i}/fans", {key: [i]})

    # links
    links = api.paginate(f"/users/{uid}/links")
    dump(adir / "links_list.json", links)
    add_list("links", links, "api_links_list")
    link_ids = {iid(x) for x in links} | {x.get("Link ID") for x in
                                          ia.get("Smart Links You Created (Creator Accounts)") or []}
    link_ids.discard(None)
    log(f"  {len(link_ids)} links")
    mstat = {}  # metric -> [ok, invalid]
    for n, i in enumerate(sorted(link_ids), 1):
        vlog(f"  link {i} ({n}/{len(link_ids)})")
        o = ("links", i)
        ld = unwrap(grab(f"links/{i}/link", f"/links/{i}", owner=o, origin="api_link")) or {}
        for m in metrics:
            s = mstat.setdefault(m, [0, 0])
            if m == "rsvps" and ld.get("target_type") != "livestream":
                continue
            if s[1] >= 3 and s[0] == 0:  # never valid so far: stop probing
                continue
            st, data = api.get(f"/links/{i}/insights", {"metric": m}, probe=True)
            if st == 200:
                s[0] += 1
                dump(adir / f"links/{i}/insights_{slug(m)}.json", data)
                sources.append(("links", i, f"api_insights_{slug(m)}", data))
            else:
                s[1] += 1
                vlog(f"    metric {m!r} -> {st}")
        grab(f"links/{i}/insights_overview", f"/links/{i}/insights/overview", owner=o)
    if link_ids:
        dump(adir / "_insight_metrics.json",
             {m: {"ok": s[0], "rejected": s[1]} for m, s in mstat.items()})
        log("  link insight metrics (ok/rejected): " +
            ", ".join(f"{m} {s[0]}/{s[1]}" for m, s in mstat.items()))

    # ad campaigns, user lists
    camps = api.paginate(f"/users/{uid}/advertising/campaigns")
    dump(adir / "campaigns_list.json", camps)
    add_list("campaigns", camps, "api_campaigns_list")
    camp_ids = {iid(x) for x in camps} | {iid(x) for x in
                                          ia.get("Ad Campaigns You Created (Creator Accounts)") or []}
    camp_ids.discard(None)
    log(f"  {len(camp_ids)} ad campaigns")
    for i in sorted(camp_ids):
        o = ("campaigns", i)
        grab(f"campaigns/{i}/campaign", f"/advertising/campaigns/{i}", owner=o, origin="api_campaign")
        grab(f"campaigns/{i}/insights", f"/advertising/campaigns/{i}/insights", owner=o)
        grab(f"campaigns/{i}/creative_insights", f"/advertising/campaigns/{i}/creativeInsights", owner=o)
    lists = api.paginate(f"/users/{uid}/advertising/userLists")
    dump(adir / "userlists.json", lists)
    sources.append(("account", uid, "api_userlists", lists))

    # playbooks (IDs only from the export's own entries or --playbook-id)
    pb = {iid(x) for x in ia.get("Playbooks You Created (Creator Accounts)") or []} | set(playbook_ids)
    pb.discard(None)
    for i in sorted(pb):
        grab(f"playbooks/{i}", f"/playbooks/campaigns/{i}", origin="api_playbook")

    dump(adir / "_errors.json", api.errors)
    return sources


def main():
    global VERBOSE, LOGF
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("export", help="ToneDen privacy export JSON (not the .schema)")
    ap.add_argument("-o", "--out", default="toneden_export")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="log every request/download to stderr (run.log always has them)")
    ap.add_argument("--token", default=os.environ.get("TONEDEN_TOKEN"))
    ap.add_argument("--cookie", default=os.environ.get("TONEDEN_COOKIE"),
                    help="connect.sid value (or 'connect.sid=...') of a logged-in session; unofficial, "
                         "needs no approved app")
    ap.add_argument("--token-map", help="JSON object or file path: {\"<userID>\": \"<token or "
                    "connect.sid=...>\"}")
    ap.add_argument("--oauth", action="store_true", help="run authorization-code flow to get a token")
    ap.add_argument("--redirect-uri", default="http://localhost:8765/callback",
                    help="must match a redirect URI registered on the app")
    ap.add_argument("--only", type=int, action="append", default=[], help="restrict to user ID (repeatable)")
    ap.add_argument("--playbook-id", type=int, action="append", default=[],
                    help="playbook campaign ID to fetch (repeatable; not derivable from the export)")
    ap.add_argument("--link-metrics", default=",".join(LINK_METRICS),
                    help="comma list of `metric` values to try on /links/{id}/insights")
    ap.add_argument("--no-unlock-assets", action="store_true",
                    help="don't download assets of campaigns this account unlocked (their posts/"
                         "gateMetadata JSON is still saved)")
    ap.add_argument("--no-fans", action="store_true",
                    help="skip /users/{id}/audience/fans (unofficial; third-party personal data)")
    ap.add_argument("--api-base", default=API)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--no-download", action="store_true", help="API JSON only")
    a = ap.parse_args()
    VERBOSE = a.verbose

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    LOGF = open(out / "run.log", "a", encoding="utf-8")
    vlog("start", " ".join(x for x in sys.argv[1:] if not x.startswith("connect.sid")))

    tmap = {}
    if a.token_map:
        tmap = json.loads(Path(a.token_map).read_text() if os.path.exists(a.token_map) else a.token_map)
        tmap = {str(k): v for k, v in tmap.items()}
    if a.cookie and not a.token:
        a.token = a.cookie if a.cookie.startswith("connect.sid=") else "connect.sid=" + a.cookie
    if not a.token and a.oauth:
        a.token = oauth(os.environ["TONEDEN_CLIENT_ID"], os.environ["TONEDEN_CLIENT_SECRET"],
                        a.redirect_uri)
    if not a.token and not tmap:
        sys.exit("No credential: set TONEDEN_TOKEN / TONEDEN_COOKIE, pass --token/--cookie/--token-map, or use --oauth")

    export = json.loads(Path(a.export).read_text())
    shutil.copyfile(a.export, out / "export.json")
    metrics = [m.strip() for m in a.link_metrics.split(",") if m.strip()]

    # single-account: root is the account; multi-account: root["Accounts"] = {key: account}
    if "Accounts" in export and isinstance(export["Accounts"], dict):
        accounts = list(export["Accounts"].items())
    else:
        accounts = [(None, export)]

    sources, done = [], 0
    for key, acct in accounts:
        uid = acct.get("Personal Data", {}).get("id") or key
        if not uid:
            log(f"skip account {key}: no user ID")
            continue
        if a.only and int(uid) not in a.only:
            continue
        cred = tmap.get(str(uid)) or a.token
        if not cred:
            log(f"skip account {uid}: no credential")
            continue
        log(f"account {uid}")
        res = run_account(Api(a.api_base, cred), acct, uid,
                          out / "accounts" / str(uid) / "api", a.playbook_id, metrics, not a.no_fans, not a.no_unlock_assets)
        if res is not None:
            sources += res
            done += 1
    if not done:
        sys.exit("No account could be fetched")

    # assets: url -> {strong, hint, dests{(kind,id): {(origin, field)}}}
    targets = {}
    for kind, oid, origin, obj in sources:
        for url, strong, field, hint in harvest(obj):
            t = targets.setdefault(url, {"strong": False, "hint": None, "dests": {}})
            t["strong"] = t["strong"] or strong
            t["hint"] = t["hint"] or hint
            t["dests"].setdefault((kind, str(oid)), set()).add((origin, field))
    log(f"{len(targets)} asset URLs")
    for u, t in sorted(targets.items()):
        vlog(f"  found {u} <- " + ", ".join(f"{k}/{i}:{o}[{f}]" for (k, i), of in sorted(t["dests"].items())
                                          for o, f in sorted(of)))

    manifest = []
    if not a.no_download and targets:
        prior, claims = {}, {}
        mp = out / "manifest.json"
        if mp.exists():
            try:
                for rec in json.loads(mp.read_text()):
                    prior[rec["url"]] = rec
                    for f in rec.get("files", []):
                        claims[str(out / f)] = rec["url"]
            except (ValueError, KeyError):
                pass
        placer = Placer(claims)
        with ThreadPoolExecutor(a.workers) as ex:
            items = sorted(targets.items())
            for n, rec in enumerate(ex.map(lambda kv: fetch_asset(kv[0], kv[1], out, placer, prior), items), 1):
                manifest.append(rec)
                if rec["status"] not in (200, "cached"):
                    log(f"  {rec['status']} {rec['url']} <- {src_str(rec['sources'])}")
                elif not VERBOSE and n % 25 == 0:
                    log(f"  downloaded {n}/{len(items)}")
        dump(mp, manifest)
    ok = sum(r["status"] in (200, "cached") for r in manifest)
    log(f"done: {done} accounts, {ok}/{len(targets)} assets -> {out} (see accounts/*/api/_errors.json, run.log)")


if __name__ == "__main__":
    main()
