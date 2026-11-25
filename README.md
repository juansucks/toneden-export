# toneden_export

Builds a fuller archive of a ToneDen account than the built-in data export. The built-in export is a limited JSON file: it lists IDs, URLs and counts, but contains no files, no statistics and no gate/unlock details. This script reads that JSON, queries the ToneDen API for everything it references, and downloads the referenced files.

It produces:

- Raw API JSON for the account, its attachments (gates/contests) with unlock/entry statistics, smart links with insights, ad campaigns with insights, user lists and playbooks.
- Audience fan lists, overall and per attachment (unofficial endpoint).
- Gate metadata for every attachment (unofficial endpoint).
- Campaigns the account **unlocked** (other creators' gates): their post and gate metadata, and the downloadable file when the gate is open for the account.
- Every media file referenced anywhere (artwork, previews, backgrounds, logos, avatars, download files), sorted by owner.
- A manifest of what was downloaded and where each URL came from.

## Get your export JSON

While logged in to ToneDen, either:

1. [Open Advanced settings](https://www.toneden.io/settings/advanced), then click **View Your Data**.
2. Or open the [Privacy Portal](https://www.toneden.io/privacy-portal) and choose **Download your data**.

Both give the same JSON. Single-account and multi-account exports (`"Accounts": {...}`) are supported.

## Install

Python 3.8+ and `requests`:

```
pip install requests
```

## Authenticate

The script needs a credential for each account in the export. Pick one.

**Session cookie (unofficial, no approved app needed).** Log in at toneden.io, open the browser dev tools, go to Application (Chrome) or Storage (Firefox), Cookies, `https://www.toneden.io`, and copy the value of `connect.sid`. Keep it URL-encoded, as shown (`s%3A...`).

```
export TONEDEN_COOKIE='s%3A...'
```

The cookie is a full login for that account. Don't commit it or paste it into shared logs, and log out of that session when finished. It is only sent to the API host, never to asset hosts, and is not written to `run.log`.

**OAuth bearer token.** Requires an approved ToneDen API app.

```
export TONEDEN_TOKEN='<access token>'
# or let the script run the authorization-code flow:
export TONEDEN_CLIENT_ID=... TONEDEN_CLIENT_SECRET=...
python toneden_export.py export.json --oauth   # prints the token once; reuse via TONEDEN_TOKEN
```

The redirect URI (default `http://localhost:8765/callback`) must be registered on the app. The unofficial endpoints (`gateMetadata`, `audience/fans`) may not accept OAuth tokens; those calls then show up in `_errors.json` and the rest of the export continues.

**Several accounts in one export:** a credential only reaches its own account, so pass one per user ID:

```
python toneden_export.py export.json --token-map '{"2420976": "s%3A...", "123": "<token>"}'
```

`--token-map` also accepts a path to a JSON file. Values starting with `connect.sid=` are treated as cookies, anything else as a bearer token.

**Unlocked downloads need the right account.** Whether a gate counts as open is per viewer. Use the credential of the account that completed the unlock, otherwise `download_url` is absent.

## Usage

```
python toneden_export.py ToneDen_Privacy_Data.json -o out/ -v
```

The export file is optional. Without it the account is taken from the credential and only data the API can give is fetched (add `--saves` and/or `--downloads-tsv` to get unlocked/saved campaigns).

| Option | Description |
|---|---|
| `-o, --out DIR` | Output directory (default `toneden_export`). |
| `-v, --verbose` | Print every request, download and skip to stderr. `run.log` always contains the full log. |
| `--token`, `--cookie`, `--token-map`, `--oauth` | Credentials, see above. Env: `TONEDEN_TOKEN`, `TONEDEN_COOKIE`. |
| `--redirect-uri URI` | Redirect URI for `--oauth`. |
| `--only UID` | Process only this user ID (repeatable). |
| `--playbook-id ID` | Fetch a playbook campaign (repeatable). Playbook IDs cannot be derived from the export. |
| `--link-metrics a,b,c` | `metric` values to try on `/links/{id}/insights` (default `clicks,clickthroughs,email_clicks,rsvps`). |
| `--downloads-tsv FILE` | Campaigns from a `toneden_unlock.py --downloads-out` file (`post_url`, `download_url`, `filename`). The download URL and filename in the file are used directly; the posts API supplies the gate ID, poster and artwork. Filed like other unlocks. Rows go to the first account, or `--tsv-user UID`. |
| `--saves` | Also list the account's saved posts through the API and process them like unlocked campaigns. The listing endpoint is undocumented, see below. |
| `--saves-endpoint PATH` | Saved-posts listing to use instead of the built-in guesses: a path with `{uid}` and optional `?query`, repeatable. |
| `--delay SECONDS` | Pause between per-campaign API requests (default 0.25). |
| `--no-unlock-assets` | Don't download assets or write link shortcuts for campaigns you unlocked. Their post and gateMetadata JSON is still saved. |
| `--unlock-links MODE` | Link shortcut for each unlocked campaign's download target. `fallback` (default): only when it can't be saved as a file (it is an HTML page, or the request fails). `always`: also next to files that were saved. `never`: none. |
| `--link-format FMT` | Shortcut type: `url` (default, an Internet Shortcut file) or `txt` (the URL as text). |
| `--no-fans` | Skip the audience fan lists. |
| `--no-download` | API JSON only, no file downloads. |
| `--workers N` | Parallel downloads (default 8). |
| `--api-base URL` | API base URL (default `https://www.toneden.io/api/v1`). |

The script is safe to rerun: existing downloads are reused, and a changed layout or missing file extension is repaired in place.

## Output

```
out/
  export.json                       copy of the input
  run.log                           full log
  manifest.json                     url -> files, status, sha256, sources (origin, field)
  accounts/<uid>/api/
    me.json, user.json, userlists.json
    attachments_list_{unlock,contest}.json
    attachments/<id>/                attachment, gate_metadata, unlocks|entries (+ totals, platforms), fans
    audience_fans.json
    links_list.json, links/<id>/     link, insights_<metric>, insights_overview
    campaigns_list.json, campaigns/<id>/
    unlocked/<poster>/<link_path> [<id>]/   post.json, gate_metadata.json
    _insight_metrics.json            which link metrics were accepted
    _errors.json                     non-200 responses
  assets/
    attachments/<id>/<file>          your own attachments
    links/<id>/<file>
    campaigns/<id>/<file>
    account/<uid>/<file>
    unlocked/<poster>/<link_path> [<id>]/<file>   other creators' campaigns you unlocked
    unlocked/<poster>/<link_path> [<id>]/<name>.url|.txt   link shortcut, see below
```

Unlocked and saved campaigns:

- Campaign URLs come from the export's `Campaigns Interacted With (Gate Unlocks)` and `Saved Tracks & Presaves` sections, `--downloads-tsv` rows and `--saves`. Duplicates across sources are merged.
- Each is looked up with `/posts?author_username=&link_path=`. The gate's attachment ID is `post.id` (what the site's own unlock calls use), with `post.unlock.id` tried too if it differs. Posts without a `post.unlock` object are handled.
- Files go to `assets/unlocked/<poster>/<link_path> [<id>]/`. The poster is the post's `author.username` (not whatever appeared in the URL, so one author always lands in one folder), `link_path` is the post's slug, and `<id>` is the gate ID; the square brackets are literal, e.g. `assets/unlocked/korisound/gemi-kori-vesta [6085494]/`. Both names are URL-decoded and every character other than letters, digits, `_ - . ~ ( )` and spaces is replaced with `_`.
- Posts already fetched are cached (`api/unlocked/_index.json` plus `post.json`) and not requested again on reruns; `gateMetadata` is requested again unless a TSV row already has the download URL. Failures are listed in `api/unlocked/_failed.json`.
- Folders from earlier layouts (`unlocked/<id>/`, `unlocked/<poster>/<id>/`) are moved when the same campaign is processed again, and emptied folders are removed. Any that remain are reported at the end of the run (they weren't matched by this run, for example with `--no-download`).
- `-v` / `run.log` print `-> unlocked/<poster>/<link_path> [<id>]` for every campaign, with where it came from.

Saved posts (`--saves`): `/users/socialStats` reports a saves count, but the listing endpoint isn't known. The script tries `/users/{uid}/saves`, `/users/{uid}/saves?type=unlock`, `/users/{uid}/saved` and `/users/{uid}/savedPosts`, paging with the largest `limit` the server accepts, and uses the first that returns items. What each one returned (status, response shape, item count) is written to `api/_saves_probe.json`, and the listed count is compared with `socialStats`. If it comes back empty, open the site's saved-posts page with the browser dev tools Network tab open, copy the request path and query, and pass it with `--saves-endpoint`. Saved posts can only be downloaded if the gate is open for that account.

Unlocked campaign links:

- The download target is `metadata.download_url` from gateMetadata. For link-type unlocks whose gate is open and which have no `download_url`, the post's `direct_url` is used instead.
- An HTML page is never saved as the file, even if the name or URL claims an extension. If the target can't be saved as a file (HTML page such as a share link, HTTP error, page host), a shortcut is written where the file would be: `<download_display_url without extension>.url` (or `.txt`), falling back to the post's `link_path` when there is no display name.
- With `--unlock-links always` the shortcut is written even when the file was saved, so both are present (`Track.wav` and `Track.url`). A shortcut from an earlier run is removed when it is no longer wanted.
- The URL in the shortcut is whatever the API returned. Direct download URLs may be signed and expire, so don't rely on a saved one for long.
- `--no-download` and `--no-unlock-assets` also suppress shortcuts.

File naming:

- A file used by several owners is stored once per owner (hardlink, else copy).
- `download_url` files are named from the sibling `download_display_url`. For unlocked campaigns that name comes from the posts response, since gateMetadata doesn't include it.
- If the name has no known extension, one is taken from the file's magic bytes, then `Content-Type`, then the URL. If none can be determined the file keeps its name.
- Different URLs that end up with the same name in one folder get a short hash suffix.

## What counts as an asset

URLs are scanned in the export and in every API response. A URL is downloaded if its path has a media extension, or if its JSON key looks like media (`artwork`, `image`, `preview`, `avatar`, `cover`, `thumb`, `banner`, `logo`, `background`, `bg`, `audio`, `video`, `stream`, `download`, `file`). Keyed URLs on YouTube, Vimeo, SoundCloud, Spotify and similar page hosts, and responses that turn out to be HTML, are skipped. Both patterns are constants at the top of the script (`MEDIA_EXT`, `MEDIA_KEY`, `PAGE_HOSTS`).

Fan lists are saved as JSON only; fan avatars are not downloaded. For unlocked campaigns only the campaign's own artwork, preview and download file are fetched, not author or collaborator avatars.

## Endpoints

Documented: `/users/me`, `/users/{id}`, `/users/{id}/attachments`, `/attachments/{id}` with `/unlocks`, `/entries`, `/totals`, `/platforms`, `/users/{id}/links`, `/links/{id}` with `/insights`, `/insights/overview`, `/users/{id}/advertising/campaigns` and `userLists`, `/advertising/campaigns/{id}` with `/insights`, `/creativeInsights`, `/playbooks/campaigns/{id}`.

Unofficial (found via the site, may change or reject OAuth): `/attachments/{id}/gateMetadata`, `/users/{id}/audience/fans?filters=...`, `/posts?author_username=&link_path=`.

## Limitations

- Allowed `metric` values for `/links/{id}/insights` aren't in the public docs. The defaults are guesses; rejected metrics are dropped and results are summarised in `_insight_metrics.json`. Pass the real list with `--link-metrics`.
- The per-attachment fan filter for contests (`contest_entered`) and `offset`/`limit` pagination on the fans endpoint are unverified. If the server ignores pagination, only the first page is saved.
- Playbook IDs aren't in the export; pass them with `--playbook-id`.
- Private posts with a secret token in the URL aren't handled.
- The saved-posts listing endpoint is unknown; `--saves` only works if one of the guessed or supplied endpoints returns items.
- Download URLs from a TSV may be signed or expire; `api.soundcloud.com/.../download` and share-page links (Drive, Mediafire) usually can't be fetched as files and fall back to link shortcuts.
- Using `direct_url` for link-type unlocks is an assumption about the API's fields; it is only used when the gate is open and `download_url` is missing.
- Facebook ad video creatives only expose a `video_id`, so those videos can't be downloaded.
- Comments, submissions, follows and the other activity sections have no API endpoints; they remain only in `export.json`.
- Fan lists contain other people's personal data. Treat that output accordingly.
