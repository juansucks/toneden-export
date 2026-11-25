#!/usr/bin/env python3
"""Unlock ToneDen download gates for the account behind a connect.sid cookie.

Usage:
    export TONEDEN_SID='s%3A...'          # value of the connect.sid cookie
    python toneden_unlock.py URL [URL ...]
    python toneden_unlock.py -f urls.txt -m manual.txt -v
    python toneden_unlock.py --user disciplerecs --user zomboy
    python toneden_unlock.py --following --urls-out all_posts.txt --collect-only
    python toneden_unlock.py -f all_posts.txt                    # unlock a saved list

Collecting posts (--user NAME, repeatable; --following):
  A post is collected when its attachment has type == "unlock" and a non-empty
  download_source. --user takes a username or profile URL. --following walks every
  user the logged-in account follows. Collected URLs are added to any URLs given
  directly, de-duplicated, then unlocked. --collect-only stops after collecting;
  --urls-out FILE saves the list (one URL per line, readable again with -f).

Step choice per gate:
  - AND gate: every listed step is sent, in the order the post lists them.
  - OR gate:  one step is sent, ranked link > spotify > other > apple music.
  - Apple Music steps are never sent. Posts that need one, and posts that
    failed, are printed at the end and written to --manual-out with their log.
  - SoundCloud steps are sent the way the site sends them (your viewer record, read from
    the post page, goes in the request body). If the post has soundcloud_repost_enabled,
    ToneDen reposts the track on your SoundCloud, so the repost is undone afterwards with
    DELETE /attachments/{id}/reposts (after --undo-delay seconds, default 5).
  - --downloads-out FILE writes a tab-separated list (post_url, download_url, filename) of every
    gate that is open at the end: unlocked now, or already open. Rows are written as they come.
  - --save also adds each open gate to the account's saved posts (POST /attachments/{id}/saves),
    which is the only account-side request the site makes for a gate that needs no steps.

Requires: pip install requests
"""
import argparse
import json
import os
import re
import sys
import time
from urllib.parse import quote, unquote, urlparse

import requests

BASE = "https://www.toneden.io"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36")
UI_VERSION = "1.286"  # sent by the web client as the ui-version header

# Steps where the site only reports that a link was opened.
LINK_STEPS = {"custom-url-visit", "youtube"}
RANK = {"link": 0, "spotify": 1, "other": 2, "apple": 3}


class AuthError(Exception):
    pass


def kind(platform):
    if platform in LINK_STEPS:
        return "link"
    if platform.startswith("spotify"):
        return "spotify"
    if platform.startswith("apple-music"):
        return "apple"
    return "other"


def is_soundcloud(platform):
    return platform.startswith("soundcloud")


def parse_preload_viewer(html):
    """The logged-in viewer's record, which the site embeds as window.preloadViewer."""
    m = re.search(r"window\.preloadViewer\s*=\s*(\{.*?\});\s*</script>", html, re.S)
    if m:
        try:
            v = json.loads(m.group(1))
            return v if isinstance(v, dict) else None
        except ValueError:
            pass
    return None


def parse_post_url(url):
    path = urlparse(url if "//" in url else "https://" + url).path
    m = re.match(r"^/([^/]+)/post/([^/?#]+)", path)
    if not m:
        raise ValueError(f"not a ToneDen post URL: {url}")
    # Decode %XX escapes (emoji, accents, '*', '[' ...): the API wants the plain link_path, and
    # requests would otherwise encode the '%' a second time.
    return unquote(m.group(1)), unquote(m.group(2))


def plan_steps(post, done):
    """Return the platform steps to send, given the ones already completed."""
    platforms = post.get("platforms") or ([post["platform"]] if post.get("platform") else [])
    todo = [p for p in platforms if p not in done]
    if not todo:
        return []
    if post.get("is_or_condition"):
        if any(p in done for p in platforms):
            return []
        return [min(todo, key=lambda p: RANK[kind(p)])]
    return todo


class Toneden:
    def __init__(self, sid, delay=1.0, session=None):
        self.s = session or requests.Session()
        self.s.headers.update({"user-agent": UA, "accept-language": "en-US,en;q=0.9"})
        if sid:
            self.s.cookies.set("connect.sid", sid, domain="www.toneden.io", path="/")
        self.delay = delay
        self.csrf = None
        self.referer = BASE + "/"
        self.preload_viewer = None
        self.undo_delay = 5.0  # seconds to wait after a SoundCloud step before undoing its repost
        self.save_posts = False  # --save: POST /saves for every open gate
        self.downloads_fh = None  # --downloads-out: open file object for the download list
        # list page sizes to try, largest first; the server accepted 600 in testing
        self.limits = (1000, 600, 100, 25, 12)

    def _headers(self):
        h = {
            "accept": "application/json",
            "ui-version": UI_VERSION,
            "referer": self.referer,
            "origin": BASE,
        }
        if self.csrf:
            h["csrf-token"] = self.csrf
        return h

    def open_post_page(self, url):
        r = self.s.get(url, headers={"accept": "text/html"}, timeout=30)
        r.raise_for_status()
        m = re.search(r"csrfToken\s*=\s*'([^']+)'", r.text)
        if not m:
            raise RuntimeError("csrf token not found on page")
        self.csrf = m.group(1)
        self.referer = url
        self.preload_viewer = parse_preload_viewer(r.text)

    def api(self, method, path, **kw):
        for n in range(5):
            r = self.s.request(method, BASE + path, headers=self._headers(), timeout=30, **kw)
            if method == "GET" and n < 4 and (r.status_code == 429 or r.status_code >= 500):
                try:
                    wait = float(r.headers.get("Retry-After", ""))
                except ValueError:
                    wait = 2 ** n
                time.sleep(min(wait, 60))
                continue
            break
        if r.status_code in (401, 403):
            raise AuthError(f"{method} {path} -> {r.status_code}")
        r.raise_for_status()
        return r.json()

    def post_info(self, user, slug):
        return self.api("GET", "/api/v1/posts",
                        params={"author_username": user, "link_path": slug}).get("post")

    def gate_metadata(self, gate_id):
        """Returns (metadata, viewer_id); viewer_id is null when not signed in."""
        data = self.api("GET", f"/api/v1/attachments/{gate_id}/gateMetadata")
        return data["metadata"], data.get("viewer_id")

    def unlock(self, gate_id, viewer_id, platform):
        body = {"is_opted_in": False, "platform": platform, "optedOutFanID": viewer_id}
        if is_soundcloud(platform) and self.preload_viewer:
            # the site merges the viewer record into the SoundCloud step (observed in HARs)
            body = {**self.preload_viewer, **body}
        return self.api("POST", f"/api/v1/attachments/{gate_id}/unlock", json=body)["gateStatus"]

    def record_download(self, post_url, download_url, name):
        """Append one row to the --downloads-out file (only when a download_url is known)."""
        if self.downloads_fh and download_url:
            clean = lambda x: str(x or "").replace("\t", " ").replace("\n", " ")
            self.downloads_fh.write("\t".join(clean(x) for x in (post_url, download_url, name)) + "\n")
            self.downloads_fh.flush()

    def save(self, gate_id):
        """POST /attachments/{id}/saves: the site's "save" action (empty body)."""
        r = self.s.request("POST", f"{BASE}/api/v1/attachments/{gate_id}/saves",
                           headers=self._headers(), timeout=30)
        if r.status_code in (401, 403):
            raise AuthError(f"POST saves -> {r.status_code}")
        r.raise_for_status()
        return r

    def undo_repost(self, gate_id):
        """DELETE /attachments/{id}/reposts: remove the SoundCloud repost made by the gate."""
        for n in range(4):  # a failed undo leaves a repost on SoundCloud, so retry
            try:
                r = self.s.request("DELETE", f"{BASE}/api/v1/attachments/{gate_id}/reposts",
                                   headers=self._headers(), timeout=30)
            except (requests.ConnectionError, requests.Timeout):
                if n == 3:
                    raise
                time.sleep(2 * (n + 1))
                continue
            if r.status_code >= 500 and n < 3:
                time.sleep(2 * (n + 1))
                continue
            break
        if r.status_code in (401, 403):
            raise AuthError(f"DELETE reposts -> {r.status_code}")
        r.raise_for_status()
        return r

    # ------------------------------------------------------------ collecting

    def paginate(self, path, key, params=None, limits=None):
        """All items of a limit/offset list endpoint whose items sit under `key`.

        The max `limit` is undocumented, so a rejected first page (400/413/422, a 5xx or a
        timeout) retries with the next smaller size in self.limits. The offset advances by
        the number of items received (the server may cap the limit). Stops on an empty page
        or a page with nothing new.
        """
        limits = limits or self.limits
        for limit in limits:
            out, seen, offset = [], set(), 0
            while True:
                q = {**(params or {}), "limit": limit}
                if offset:
                    q["offset"] = offset
                try:
                    data = self.api("GET", path, params=q)
                except requests.HTTPError as e:
                    code = e.response.status_code
                    if offset == 0 and limit != limits[-1] and (code in (400, 413, 422) or code >= 500):
                        break  # try the next, smaller limit
                    raise
                except requests.Timeout:
                    if offset == 0 and limit != limits[-1]:
                        break
                    raise
                items = (data.get(key) if isinstance(data, dict) else None) or []
                new = [x for x in items if x.get("id") is None or x["id"] not in seen]
                if not new:
                    return out
                seen.update(x["id"] for x in new if x.get("id") is not None)
                out.extend(new)
                offset += len(items)
            # fell out of the loop on a 400: next limit
        return []

    def viewer(self):
        """(id, username) of the logged-in account. The site embeds it in its pages as
        window.preloadViewer; /api/v1/users/me is the fallback."""
        r = self.s.get(BASE + "/", headers={"accept": "text/html"}, timeout=30)
        m = re.search(r"window\.preloadViewer\s*=\s*(\{.*?\});\s*</script>", r.text, re.S)
        if m:
            try:
                v = json.loads(m.group(1))
                if v.get("id"):
                    return v["id"], v.get("username")
            except ValueError:
                pass
        try:
            data = self.api("GET", "/api/v1/users/me")
            v = data.get("user", data) if isinstance(data, dict) else {}
            if v.get("id"):
                return v["id"], v.get("username")
        except (requests.RequestException, ValueError):
            pass
        raise AuthError("could not determine the logged-in user (cookie expired?); "
                        "pass --self-id with your numeric user id")

    def user_by_username(self, username):
        return self.api("GET", "/api/v1/users", params={"username": username})["user"]


def qualifies(att):
    """Unlock attachment that has a download (download_source set and non-empty)."""
    src = att.get("download_source")
    return att.get("type") == "unlock" and src is not None and str(src).strip() != ""


def post_url(att, fallback_username):
    name = (att.get("owner") or {}).get("username") or fallback_username
    if not name or not att.get("link_path"):
        return None
    url = f"{BASE}/{quote(name, safe='')}/post/{quote(att['link_path'], safe='-_.~')}"
    if att.get("is_private") and att.get("secret_token"):
        url += "/" + att["secret_token"]
    return url


def username_arg(value):
    """Accept 'name', '@name' or a toneden.io profile/post URL."""
    v = value.strip().lstrip("@")
    if "//" in v or v.lower().startswith(("www.toneden.io", "toneden.io")):
        parts = [p for p in urlparse(v if "//" in v else "https://" + v).path.split("/") if p]
        return parts[0] if parts else ""
    return v


def gather(client, usernames, following, self_id, log, vlog):
    """Collect post URLs. Returns [(username, [url, ...])] in discovery order."""
    targets = []  # (user id, username or None)
    for name in usernames:
        name = username_arg(name)
        try:
            u = client.user_by_username(name)
        except (requests.RequestException, KeyError, ValueError) as e:
            log(f"  ! user {name!r}: lookup failed ({type(e).__name__}: {e})")
            continue
        targets.append((u["id"], u.get("username") or name))
    if following:
        me, me_name = (self_id, None) if self_id else client.viewer()
        followed = client.paginate(f"/api/v1/users/{me}/following", "following")
        log(f"following {len(followed)} users" + (f" (account {me_name or me})" if followed else ""))
        targets += [(u["id"], u.get("username")) for u in followed if u.get("id")]

    groups, seen_users, seen_atts = [], set(), set()
    for uid, uname in targets:
        if uid in seen_users:
            continue
        seen_users.add(uid)
        try:
            atts = client.paginate(f"/api/v1/users/{uid}/attachments", "attachments")
        except (AuthError, requests.RequestException) as e:
            log(f"  ! {uname or uid}: attachments failed ({type(e).__name__}: {e})")
            continue
        urls, skipped = [], {"other type": 0, "no download_source": 0, "no username": 0, "duplicate": 0}
        for a in atts:
            if a.get("type") != "unlock":
                skipped["other type"] += 1
            elif not qualifies(a):
                skipped["no download_source"] += 1
            elif a.get("id") in seen_atts:
                skipped["duplicate"] += 1
            else:
                url = post_url(a, uname)
                if url is None:
                    skipped["no username"] += 1
                    continue
                seen_atts.add(a.get("id"))
                urls.append(url)
        shown = (uname or str(uid))
        log(f"  {shown}: {len(atts)} attachments, {len(urls)} with a download")
        vlog("    skipped: " + ", ".join(f"{k}={v}" for k, v in skipped.items() if v))
        if urls:
            groups.append((shown, urls))
        time.sleep(client.delay)
    return groups


def undo_repost(client, gate_id, since, log, vlog):
    """DELETE the repost the gate made on SoundCloud, once client.undo_delay seconds have
    passed since `since` (time.monotonic()). Returns None on success, else an error string."""
    remaining = client.undo_delay - (time.monotonic() - since)
    if remaining > 0:
        time.sleep(remaining)
    try:
        r = client.undo_repost(gate_id)
    except requests.HTTPError as e:
        log(f"  ! undo repost: HTTP {e.response.status_code}")
        vlog(f"    body: {getattr(e.response, 'text', '')[:500]}")
        return f"HTTP {e.response.status_code}"
    except (AuthError, requests.RequestException) as e:
        log(f"  ! undo repost: {type(e).__name__}: {e}")
        return type(e).__name__
    log(f"  undo repost: HTTP {r.status_code}")
    vlog(f"    body: {r.text[:200]}")
    return None


def save_post(client, post, gate_id, log, vlog):
    """With --save: add the post to the account's saved posts unless it already is."""
    if not client.save_posts:
        return
    if (post.get("unlock") or {}).get("saved", post.get("saved")):
        vlog("  already in saved posts")
        return
    try:
        client.save(gate_id)
    except requests.HTTPError as e:
        log(f"  ! save: HTTP {e.response.status_code}")
        vlog(f"    body: {getattr(e.response, 'text', '')[:500]}")
    except (AuthError, requests.RequestException) as e:
        log(f"  ! save: {type(e).__name__}: {e}")
    else:
        log("  saved to account")


def process(client, url, log, vlog):
    """Returns (status, detail). status: unlocked | already | manual | failed | skipped.

    "already" covers every gate that is open before any step is sent: unlocked earlier by this
    account, or a gate that needs no steps (those look the same in the API).

    log() always prints and is kept in the per-post record; vlog() only does so with -v.
    """
    user, slug = parse_post_url(url)
    client.open_post_page(url)
    post = client.post_info(user, slug)
    if not post:
        return "failed", f"no post returned by the API (author_username={user!r}, link_path={slug!r})"
    if post.get("type") != "unlock":
        return "skipped", f"post type is {post.get('type')!r}, not an unlock gate"

    gate_id = post["id"]
    filename = (post.get("unlock") or {}).get("download_display_url") or post.get("download_display_url")
    meta, viewer_id = client.gate_metadata(gate_id)
    if not viewer_id:
        raise AuthError("not signed in (connect.sid missing, expired or invalid)")
    done = set(meta.get("platforms") or [])
    vlog(f"  gate {gate_id}: viewer={viewer_id} or_condition={bool(post.get('is_or_condition'))} "
         f"required={post.get('platforms') or post.get('platform')} done={sorted(done)} "
         f"open={meta.get('is_gate_open')} download_url={'yes' if meta.get('download_url') else 'no'} "
         f"saved={(post.get('unlock') or {}).get('saved')}")
    if meta.get("is_gate_open"):
        save_post(client, post, gate_id, log, vlog)
        client.record_download(url, meta.get("download_url"), filename)
        if client.downloads_fh and not meta.get("download_url"):
            vlog("  open, but gateMetadata has no download_url (not listed)")
        return "already", "gate already open for this account"

    steps = plan_steps(post, done)
    auto = [p for p in steps if kind(p) != "apple"]
    needs_apple = [p for p in steps if kind(p) == "apple"]
    log(f"  steps: {', '.join(auto) or '-'}" + (f"  | manual: {', '.join(needs_apple)}" if needs_apple else ""))

    status = {}
    failed = []
    reposted_at = None  # set when a SoundCloud step succeeded on a gate that reposts the track
    undo_err = None
    try:
        for p in auto:
            try:
                status = client.unlock(gate_id, viewer_id, p)
            except requests.HTTPError as e:
                failed.append(f"{p} ({e.response.status_code})")
                log(f"  ! {p}: HTTP {e.response.status_code}")
                vlog(f"    body: {getattr(e.response, 'text', '')[:500]}")
                continue
            log(f"  sent {p}: gate_open={status.get('is_gate_open')}")
            vlog(f"    response: {json.dumps(status)}")
            if is_soundcloud(p) and post.get("soundcloud_repost_enabled"):
                reposted_at = time.monotonic()
            if status.get("is_gate_open"):
                break
            time.sleep(client.delay)
    finally:
        # also runs if a later step raised, so a repost is never left behind
        if reposted_at is not None:
            undo_err = undo_repost(client, gate_id, reposted_at, log, vlog)

    if status.get("is_gate_open"):
        result = ("unlocked", status.get("download_url", ""))
        save_post(client, post, gate_id, log, vlog)
        client.record_download(url, status.get("download_url"), filename)
        if client.downloads_fh and not status.get("download_url"):
            vlog("  unlocked, but the response has no download_url (not listed)")
    elif needs_apple:
        result = ("manual", "Apple Music step required: " + ", ".join(needs_apple))
    elif failed:
        result = ("failed", "steps rejected: " + ", ".join(failed))
    else:
        result = ("failed", "all steps sent but the gate is still closed (a step may need a linked account)")
    if undo_err:
        first = f"{result[0]}: {result[1]}".strip()
        result = ("failed", f"{first} | undo repost FAILED ({undo_err}): "
                            "remove the repost on SoundCloud by hand")
    return result


def write_url_list(path, direct, groups):
    with open(path, "w") as fh:
        if direct:
            fh.write("# given on the command line / -f\n" + "\n".join(direct) + "\n")
        for name, urls in groups:
            fh.write(f"# {name} ({len(urls)})\n" + "\n".join(urls) + "\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("urls", nargs="*", help="ToneDen post URLs")
    ap.add_argument("-f", "--file", help="text file with one post URL per line")
    ap.add_argument("-u", "--user", action="append", default=[], metavar="NAME",
                    help="collect all posts of this user (username or profile URL; repeatable)")
    ap.add_argument("--following", action="store_true",
                    help="collect all posts of every user the logged-in account follows")
    ap.add_argument("--self-id", type=int,
                    help="your numeric user id, if it can't be read from the site (--following)")
    ap.add_argument("--collect-only", action="store_true",
                    help="only collect post URLs (print them, or write --urls-out); don't unlock")
    ap.add_argument("--urls-out", help="write the full URL list (given + collected) to this file")
    ap.add_argument("--page-size", type=int, metavar="N",
                    help="first `limit` to try when listing following/attachments (default 1000, "
                         "falls back to smaller sizes if rejected)")
    ap.add_argument("-c", "--cookie", default=os.environ.get("TONEDEN_SID"),
                    help="connect.sid cookie value (or set TONEDEN_SID)")
    ap.add_argument("-m", "--manual-out",
                    help="write posts that need Apple Music or failed, with their log, to this file")
    ap.add_argument("-d", "--delay", type=float, default=1.0, help="seconds between steps (default 1)")
    ap.add_argument("--undo-delay", type=float, default=5.0, metavar="SEC",
                    help="wait this long after a SoundCloud repost step before undoing the repost "
                         "(default 5)")
    ap.add_argument("--downloads-out", metavar="FILE",
                    help="write post_url, download_url and filename (tab-separated) of every open gate")
    ap.add_argument("--save", action="store_true",
                    help="also add every open gate to the account's saved posts "
                         "(POST /attachments/{id}/saves)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="also print gate state, full unlock responses and HTTP error bodies")
    args = ap.parse_args()

    direct = list(args.urls)
    if args.file:
        with open(args.file) as fh:
            direct += [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
    collecting = bool(args.user or args.following)
    if not direct and not collecting:
        ap.error("no URLs given (pass URLs, -f FILE, --user NAME or --following)")
    if not args.cookie and (args.following or not args.collect_only):
        ap.error("no cookie: pass --cookie or set TONEDEN_SID")

    sid = (args.cookie or "").strip()
    if sid.lower().startswith("connect.sid="):
        sid = sid.split("=", 1)[1]
    client = Toneden(sid, delay=args.delay)
    client.undo_delay = args.undo_delay
    client.save_posts = args.save
    if args.downloads_out and not args.collect_only:
        client.downloads_fh = open(args.downloads_out, "w", encoding="utf-8")
        client.downloads_fh.write("post_url\tdownload_url\tfilename\n")
    if args.page_size:
        client.limits = (args.page_size,) + tuple(n for n in client.limits if n < args.page_size)

    def say(msg):
        print(msg)

    def vsay(msg):
        if args.verbose and msg.strip():
            print(msg)

    groups = []
    if collecting:
        try:
            groups = gather(client, args.user, args.following, args.self_id, say, vsay)
        except AuthError as e:
            print(f"auth error: {e}")
            sys.exit(2)

    # one list: direct URLs first, then collected ones; de-duplicated by (user, post path)
    seen, urls, direct_kept = set(), [], []
    for is_direct, url in [(True, u) for u in direct] + [(False, u) for g in groups for u in g[1]]:
        try:
            u, s = parse_post_url(url)
        except ValueError as e:
            print(f"skipping: {e}")
            continue
        if (u.lower(), s) in seen:
            continue
        seen.add((u.lower(), s))
        urls.append(url)
        if is_direct:
            direct_kept.append(url)
    if collecting:
        print(f"\n{len(urls)} posts to process ({sum(len(g[1]) for g in groups)} collected from "
              f"{len(groups)} users)")
    if args.urls_out:
        write_url_list(args.urls_out, direct_kept, groups)
        print(f"URL list written to {args.urls_out}")
    if args.collect_only:
        if not args.urls_out:
            print("\n".join(urls))
        sys.exit(0)

    results = []  # (url, status, detail, log lines)
    for url in urls:
        lines = [url]

        def log(msg):
            print(msg)
            lines.append(msg)

        def vlog(msg):
            if args.verbose:
                log(msg)

        print(url)
        try:
            status, detail = process(client, url, log, vlog)
        except AuthError as e:
            print(f"  auth error: {e}")
            sys.exit(2)
        except Exception as e:  # keep going on per-post errors
            status, detail = "failed", f"{type(e).__name__}: {e}"
        log(f"  => {status}: {detail}")
        results.append((url, status, detail, lines))
        time.sleep(client.delay)

    print("\nSummary")
    for label in ("unlocked", "already", "manual", "skipped", "failed"):
        print(f"  {label}: {sum(1 for r in results if r[1] == label)}")
    if client.downloads_fh:
        client.downloads_fh.close()
        print(f"\nDownload list written to {args.downloads_out}")

    todo = [r for r in results if r[1] in ("manual", "failed")]
    if todo:
        print("\nNeeds attention:")
        for url, status, detail, _ in todo:
            print(f"  [{status}] {url}")
        if args.manual_out:
            with open(args.manual_out, "w") as fh:
                fh.write("\n\n".join("\n".join(r[3]) for r in todo) + "\n")
            print(f"\nLog written to {args.manual_out}")
    sys.exit(1 if any(r[1] == "failed" for r in results) else 0)


if __name__ == "__main__":
    main()
