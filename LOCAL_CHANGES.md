# Local changes

What this fork does differently from `Twarner491/AvianVisitors`, and why.

The point of this file is to make an upstream merge mechanical instead of
archaeological. When a merge conflicts, look the file up here first — the
answer is usually "keep ours, it is deliberate" or "take theirs and
re-apply the one line below".

Station: a Raspberry Pi 4B in Singapore, served at `ghlyms.com` (public,
Cloudflare tunnel + Access) and `ghlyms.local` (LAN).

---

## Merging upstream

```bash
git fetch upstream
git checkout -b merge-upstream-$(date +%Y-%m) avian-visitors
git merge upstream/avian-visitors
# resolve, test, then merge back into avian-visitors
```

Never merge straight into `avian-visitors` — that is the branch the Pi
pulls, and a half-finished merge there is a broken site.

After any merge that touched illustrations:

```bash
python3 avian/scripts/build_masks.py     # dims.json/masks.json are merge=ours
./avian/scripts/bump_version.sh
sudo ./scripts/link_webroot.sh
```

One-time setup per clone, or `.gitattributes` cannot do its job:

```bash
git config merge.ours.driver true
```

---

## Files upstream does not have

These cannot conflict. Prefer adding local code here over editing an
upstream file.

| file | what |
|---|---|
| `avian/frontend/local-config.js` | station settings — see below |
| `avian/scripts/auto_illustrate.py` | illustrate newly detected species automatically |
| `avian/scripts/chroma_cut.py` | cut a render without rembg (the Pi has no rembg) |
| `avian/scripts/bump_version.sh` | bump the frontend cache token; ratchets past a high-water mark in the untracked `avian/frontend/.cache-token-high`, because a branch switch rewrites `index.html` and can otherwise reissue a token caches already hold |
| `avian/scripts/optimize_illustrations.py` | shrink the illustration library |
| `avian/scripts/fetch_fork_illustrations.py`, `fork_species.tsv` | pull illustrations from community forks |
| `scripts/noise_profile.sh` | diagnose what the background noise actually is |
| `scripts/filter_audition.sh` | compare audio filters by ear on one recording |
| `scripts/filter_test.sh`, `scripts/score_file.py` | A/B filters against the real model |
| `avian/api/submissions.php` | field recordings: submit, poll, audio, reject, list |
| `avian/api/places.php` | turns a coordinate into a planning-area name. The one place a fix becomes a name; nothing downstream sees the fix |
| `avian/api/access-auth.php` | verifies the Cloudflare Access JWT (RS256, audience, issuer, expiry) so a whitelisted visitor can record without the admin password |
| `avian/api/sg-areas.php` | GENERATED. The same boundaries as `sg-map.js`, for naming a fix server-side. Deliberately **not** in the Caddy allowlist, so it is never reachable as a URL |
| `avian/frontend/sg-map.js` | GENERATED. Singapore's 55 planning areas, ~50 KB. Lazy-loaded by `field.js` on first sight of the Map view, so a visit that never opens it pays nothing |
| `avian/scripts/build_sg_map.py` | rebuilds both of the above from one geoBoundaries download |
| `avian/scripts/preview.sh`, `preview-router.php`, `preview-seed.php` | run the whole site against throwaway data — see below |
| `avian/scripts/preview-expose.sh` | publish a running preview at `ghlyms.com/preview/` and take it down again |
| `avian/scripts/access-setup.sh` | find, install and check the two Cloudflare Access settings |
| `avian/frontend/field.js`, `field.css` | the Map view: the district map, the recorder and the list of what has been caught. Self-mounting into an empty `#v3`, so the feature costs `apt.js` two lines and nothing else |
| `scripts/submission_worker.py` | scores submitted clips with the station's own model |
| `tests/test_field_recordings.py`, `tests/test_field_access_auth.php` | the view wiring, the coordinate promise, and every JWT forgery worth naming |
| `scripts/install_submission_worker.sh` | installs the worker as a service |
| `frame/serve_frame.py` | serve the collage to a networked e-paper panel |
| `.gitattributes` | stop generated mask tables conflicting |

**Adding a new frontend file?** It must also be registered in the manifest
in `scripts/link_webroot.sh` (both parallel arrays). A file missing there
is not served — it 404s on the site while working perfectly from a repo
checkout, so it looks fine locally and is absent in production.

---

## Settings, not code

`avian/frontend/local-config.js` holds the values that are choices about
this installation. They used to be literals inside `apt.js`, which made
the file this fork edits most also the file upstream edits most.

| setting | value | why |
|---|---|---|
| `countExp` | `0.35` | upstream ships 0.65; flatter keeps rare birds legible |
| `liveAudioHosts` | `['ghlyms.com']` | the public host is behind Access, so the stream may be offered there |
| `atlasDefault` | `'classic'` | upstream defaults to stamps |
| `themeDefault` | `'light'` | upstream follows the OS |

`apt.js` reads each through a fallback to upstream's own value, so
deleting this file returns stock upstream behaviour. That is the safe
landing if a merge ever mangles something.

---

## The conflict surface

Nine modified lines in `apt.js`. All are structurally stable — none needs
editing during normal use, so they only conflict if upstream edits the
same line.

| what | resolution if it conflicts |
|---|---|
| `SKETCH_VERSION` / `IMG_VERSION` = `ASSET_VERSION` | keep ours; the token comes from the script tag now |
| theme default → `localCfg('themeDefault', 'auto')` | keep ours |
| `countExp` → `localCfg('countExp', 0.65)` | keep ours |
| live audio host test gains the allowlist check | keep ours |
| `lan_policy` forcing live audio off is skipped for hosts in `liveAudioHosts` | keep ours — without it the stream dies on ghlyms.com, since the tunnel sets `AVIAN_FORCE_AUTH` and `menu.php` then reports `lan_policy: true` |
| settings `.seg` selector gains `:not([data-atlas-seg])` | keep ours — without it the atlas toggle is claimed as a Pi config field and goes inert |
| `VIEW_TITLES` gains a fourth entry, `'On the Map'` | keep ours, re-adding any title upstream added |
| `go()` clamps to `VIEW_TITLES.length - 1` instead of `2` | keep ours — with upstream's literal `2` the map tab is unreachable and the slide stops at the atlas |

Plus insertions, which merge cleanly unless upstream edits the same
region: `ASSET_VERSION` and `localCfg()`.

The card-wall Atlas used to be ~250 lines of local code here. Upstream
shipped the same feature in v1.1.0 as "classic" (`atlasStyle()` returning
`'classic'`/`'stamps'`, key `bird:atlasStyle:v1`), with the same function
name and different values — two definitions in one file, where JavaScript
silently lets the last one win and git reports no conflict because ours was
an insertion. Ours was deleted and `atlasDefault: 'classic'` now drives
upstream's implementation.

### Other upstream files

| file | change |
|---|---|
| `avian/frontend/index.html` | loads `local-config.js`; single `?v=` token; theme resolver reads `themeDefault` |
| `avian/frontend/styles.css` | dual-source font path |
| `avian/scripts/cutout.py` | falls back to `u2net` under ~6 GB RAM — birefnet is ~1 GB and OOM-kills a 4 GB Pi |
| `avian/scripts/species-notes.json` | per-species prompt notes (e.g. Pygmy Cupwing) |
| `scripts/utils/reporting.py` | `maybe_auto_illustrate()` hook; `EXTRACTION_FILTER` support |
| `scripts/birdnet_analysis.py` | calls `maybe_auto_illustrate()` after `write_to_db` |
| `scripts/livestream.sh` | `LIVESTREAM_FILTER` — stream-only audio filter |
| `scripts/link_webroot.sh` | publishes `local-config.js` |
| `avian/scripts/pregen.py` | bare scientific-name label files, standalone eBird-region mode, NVIDIA backend (+278/-46 — the largest local change to an upstream file) |
| `.gitignore` | `birdnet.conf.save`, `*.whl`, `batch_requests.jsonl` |
| `scripts/update_caddyfile.sh` | publishes `/avian/api/submissions.php` — the API path is an allowlist, and an unlisted endpoint gets `respond 404` |
| `avian/api/admin-auth.php` | one line: `AV_REQUIRE_AUTH` is honoured under `cli-server` as well as `cli`, so the preview can run the site. FPM is neither |
| `avian/api/birdnet-api.php` | one line: `AV_DB_FILE` under the same two SAPIs, so a preview reads a copy of the database |
| `avian/frontend/index.html` | a fourth `<section class="view" id="v3">` (left empty; `field.js` fills it) and a fourth slider button |

---

## Upstream tests that fail here by design

`python3 -m pytest tests/ --ignore=tests/test_analysis.py` gives **80 passed,
4 failed** on this fork. All four of these pass on a clean upstream checkout,
so they are fork properties, not regressions. Two causes:

**The illustration library is re-encoded.** `optimize_illustrations.py` shrank
it by ~76% (Woodhouse's Scrub-Jay perched: 55 KB here vs 413 KB upstream), so
any test pinning upstream's exact PNG bytes cannot pass.

- `test_issue_80_art.py::test_approved_perched_pose_is_unchanged` — pins a SHA256

**Asset versions are one unified token, not per-file numbers.** Upstream bumps
`styles.css?v=r188`, `stamp-batch-c.css?v=r231` and so on independently; here a
single token covers all thirteen refs so `bump_version.sh` is one command and it
is impossible to bump the constants without the script tag. A single token
cannot equal six different pinned numbers.

- `test_atlas_classic.py::test_classic_atlas_preference_and_renderer`
- `test_issue_80_art.py::test_cache_revision_is_narrow_and_reaches_every_image_builder`
- `test_stamp_issues.py::test_reviewed_stamp_issue_assignments`

Both are deliberate. Check the count after a merge: **4 failures is the
expected state — a fifth is a real regression.**

`tests/test_analysis.py` needs `librosa`, which only exists in the Pi's BirdNET
venv, so it is skipped when testing elsewhere.

Note for editing `apt.js`: the atlas smoke test scans that source tracking
quote characters and does **not** skip comments, so a lone apostrophe in a
comment ("upstream's") hides the following closing brace from it and fails the
test with a confusing "no closing brace". Avoid apostrophes in `apt.js`
comments.

---

## Not in git

- `/etc/caddy/Caddyfile` — **machine-managed**, regenerate freely with
  `sudo /usr/local/sbin/avian-caddy-refresh`. It was hand-spliced until the
  v1.1.0 merge; upstream now generates the same `AVIAN_DIRECT_LOCAL` matcher
  itself (with one header more than the hand-written version), so the splice
  was deleted. Upstream's admin auth requires a managed Caddyfile — it
  re-renders it on every password change.
- `/etc/caddy/avian-site-overlay.caddy` — **must be `root:caddy 0640`** or the
  generator refuses it. Imported at the top of the site block, so its
  `handle` blocks match before the generated ones. Currently holds one thing:
  a `/stream` route for requests carrying `Cf-Access-Jwt-Assertion`, because
  the managed config 404s the stream for any forwarded request and that
  would kill live audio on ghlyms.com. This is the sanctioned place for
  local Caddy config — put anything new here, never in the Caddyfile.
- `/etc/avahi/avahi-daemon.conf` — `use-ipv6=no` and
  `publish-aaaa-on-ipv4=no`, so `ghlyms.local` resolves to the LAN IPv4.
  Caddy's `private_ranges` covers `fd00::/8` but **not** `fe80::/10`, so a
  browser reaching the station over IPv6 link-local fails the
  `@directAdminApi` matcher and is treated as if it came from the internet:
  `AVIAN_FORCE_AUTH 1`, `lan_policy: true`, and live audio disappears on the
  LAN while it still works on ghlyms.com. Diagnose with
  `curl -sv http://ghlyms.local/avian/api/menu.php 2>&1 | grep -E "Connected to|lan_policy"`
  from the client, not the Pi - `ping` prefers IPv4 and hides it, and Caddy
  logs only show the address it was reached on. Chosen over adding
  `fe80::/10` to the trusted ranges, which would mean restating
  auth-critical routing in the site overlay.
- Admin password — set with
  `sudo /usr/local/sbin/avian-admin-control password-reset` (12-64 letters and
  digits, SSH only). "Require password on local network" is **off**: with it
  on, upstream disables live audio everywhere, in both Caddy and the
  frontend, because Icecast has no auth of its own.
- `/etc/birdnet/birdnet.conf` — station config, holds secrets.
  Local additions: `LIVESTREAM_FILTER`, `EXTRACTION_FILTER`.
- `~/BirdNET-Pi/whitelist_species_list.txt` — species allowed past the
  occurrence-frequency filter (Rose-ringed Parakeet: established in
  Singapore, but BirdNET's model underrates introduced populations). The
  field-recording worker reads the same file, so a submission cannot
  offer a species the station itself would have refused.

---

## Updating the Pi

`git pull` on the station will usually refuse the first time:

```
error: Your local changes to the following files would be overwritten by merge:
	avian/frontend/index.html
```

That is not a real conflict. `bump_version.sh` rewrites the `?v=rNN`
tokens with `sed -i` on `index.html`, which is a **tracked** file, so
running it leaves the working tree permanently dirty. Discard it and
re-bump afterwards — nothing is lost, because the token itself lives in
the untracked `.cache-token-high` water mark, not in the checkout:

```bash
git checkout -- avian/frontend/index.html
git pull
sudo ./scripts/link_webroot.sh          # new files in the manifest
./avian/scripts/bump_version.sh         # reissues a ratcheted token
```

`link_webroot.sh` matters whenever the manifest gained an entry. The pull
itself swaps the live `index.html` immediately (the webroot is symlinks
into the checkout), so between the pull and the link the site references
a file that is not published yet — run them together.

The script now says all this when it finishes, rather than leaving it to
be discovered halfway through the next update.

---

## Trying a change without touching the station

**A plain `git pull` on the Pi is a deploy.** The webroot is a set of
symlinks *into this checkout* (`scripts/link_webroot.sh`), so the moment a
pull finishes, the live `index.html`, `apt.js`, `styles.css` and the rest
are the new ones. There is no separate release step to hold back, and the
undo is another checkout. `link_webroot.sh` is only needed for files that
are *new* to the manifest.

Two other things reach past the frontend: `submissions.php` runs
`ALTER TABLE` on `scripts/birds.db`, which is the same database the
collage, the stats and the BirdWeather export read; and the worker
*deletes* the audio of a clip it could not identify, so running it by
hand is not a read-only act.

So try it here instead:

```bash
./avian/scripts/preview.sh --seed                       # nothing real involved
./avian/scripts/preview.sh --db ~/BirdNET-Pi/scripts/birds.db
                                                        # a copy of the real data
./avian/scripts/preview.sh --as access                  # arrive as an Access visitor
```

It builds a second webroot in a scratch directory, points the API at a
copy of the database and a synthetic `birdnet.conf` whose `EXTRACTED`
lives in that scratch directory, and serves the lot on `127.0.0.1:8080`.
Safe on the Pi itself: it writes nothing in the repository, the real
webroot, the real recordings or the real database.

It reads the webroot manifest out of `link_webroot.sh` and the API
allowlist out of `update_caddyfile.sh` rather than restating them, so the
two invisible production failures both reproduce: a frontend file missing
from the manifest 404s, and so does an endpoint missing from the
allowlist. That is how you can see `sg-areas.php` really is unreachable.

`--as access` is the interesting mode. The admin gate stays **on** and the
preview signs a genuine Cloudflare Access assertion with a throwaway key,
so `menu.php` still answers 401 while `submissions.php` answers 200. That
is the property the feature exists for, demonstrated rather than asserted.

What it cannot do is score a recording: that needs the BirdNET model, so
`--seed` fabricates catches at real coordinates instead. To exercise the
real worker without risk, give it copies of both things it writes to:

```bash
~/BirdNET-Pi/birdnet/bin/python3 scripts/submission_worker.py \
    --once --db /tmp/copy.db --extracted /tmp/preview-recordings
```

### Seeing it on ghlyms.com

A preview can be published at **`https://ghlyms.com/preview/`** while the
real site carries on at `/`:

```bash
sudo -u "$(id -un)" -g caddy ./avian/scripts/preview.sh --expose --seed \\
    --db ~/BirdNET-Pi/scripts/birds.db
sudo ./avian/scripts/preview-expose.sh install 8080     # in another shell
# ... look at it on your phone, unlock with the admin password ...
sudo ./avian/scripts/preview-expose.sh remove
```

The group matters because the admin credential state is `root:caddy 0640`,
so a preview run by an ordinary shell cannot read it and every request
would 401 whatever password was typed. The preview checks up front and
prints the exact command rather than serving a login form that never
works. `sudo usermod -aG caddy "$(id -un)"` makes it permanent after a
re-login.

**Not `sg caddy`.** It asks for a group password that does not exist
unless you are already a member of the group - which is exactly the case
where you would not need it.

A path on the existing host rather than a subdomain: no DNS record, no
tunnel hostname, no second Access application. The site uses relative URLs
throughout, so it runs under a prefix unchanged — verified end to end, map
data and audio included.

`--expose` is a separate mode because exposure is a different risk from
localhost. On `127.0.0.1`, `--as admin` turning the password gate off costs
nothing — anyone who can reach it already has a shell. Behind the tunnel it
would cost a great deal, because **only `birds.db` and `birdnet.conf` are
redirected**: `config.php` still writes the real station config,
`generate.php` still spawns real work. So `--expose`:

- refuses `--as admin` outright, and fails before building anything;
- defaults to the station's own **password gate**, which needs nothing set
  up in Cloudflare. `--as access` opts into the identity path instead,
  using the station's real `ACCESS_TEAM_DOMAIN`/`ACCESS_AUD` so a genuine
  assertion from the edge verifies;
- narrows the API to the endpoints that cannot mutate. Everything else is
  not merely refused but **absent** — `config.php`, `maintenance.php`,
  `generate.php`, `export.php`, `birdnet-status.php` and `birdweather.php`
  all return 404.

The gate really does apply through the proxy, and this is worth knowing
rather than assuming: `avian_is_direct_local_request()` treats a bare
`127.0.0.1` request as local and lets it through when the LAN gate is off,
which is how this station is configured. Caddy's `reverse_proxy` adds
`X-Forwarded-*`, so a request arriving through `/preview/` is *not* direct
local and the password is required. Measured both ways.

One Caddy line earns its place: the admin session cookie is scoped to path
`/avian/`, so under a `/preview/` prefix the browser would set it and then
never send it back — unlocking would appear to work and silently not. The
overlay block rewrites the cookie path to `/preview/avian/`, which also
keeps the two sessions apart: same cookie name, different paths, so signing
in to the preview cannot log you out of the real site.

`preview-expose.sh` appends a delimited block to the site overlay rather
than rewriting it, because that file already carries the `/stream` route
keeping live audio alive on the public host. It backs the overlay up,
validates the config before reloading, and rolls back if validation fails.
A test asserts the add/remove round trip is byte-identical.

### Preview overrides

Four environment variables redirect paths, and **every one is read behind
a `PHP_SAPI` check** that admits only `cli` and `cli-server`. The station
is served by FPM, so none of them can affect a real request; a test pins
that. `AV_DB_FILE`, `AV_BIRDNET_CONF`, `AV_ACCESS_CONF`/`AV_ACCESS_CERTS`,
and `AV_REQUIRE_AUTH` — the last of which is upstream's own test override,
widened by one SAPI.

---

## The Map view

A fourth sheet beside Collage, Stats and Atlas. Three things live there:
a district map of Singapore shaded by how many birds have been caught in
each, a recorder, and the list of catches with their audio.

**Coordinates go in and never come out.** The station stores the fix
because the model needs it — the occurrence filter judges a clip by where
it was heard — but `submit` resolves it to a planning-area name once, on
the way in, and every response afterwards speaks in names. A recording
made at the station is called "Home" rather than its address; the map
still shades the station's district, because a blob has to go somewhere
and a district is not a street.

**Nobody picks the bird.** The person holding the phone almost never
knows which of five Latin names it was, so asking turns a guess into a
recorded fact. The worker's top score has to clear
`FIELD_MIN_CONFIDENCE` (default 0.5) on its own; below that the clip is
marked `unsure`, the audio is deleted, and the site says it could not
make that one out.

**Who may record.** Anyone Cloudflare Access has already let through —
`avian/api/access-auth.php` verifies the signed JWT properly (RS256
against the team's published certificates, plus audience, issuer and
expiry) rather than trusting a header. Failing that, the station's
ordinary admin rules apply, so nothing gets easier when Access is not in
play. Needs two settings in `birdnet.conf`:

```
ACCESS_TEAM_DOMAIN="yourteam.cloudflareaccess.com"
ACCESS_AUD="<Application Audience tag from the Access app>"
```

With either missing, Access auth is simply off.

Only needed for `--expose --as access` and for the real feature; the
preview's password gate needs none of it.

Finding them without transcribing a 64-character hex string:

```bash
./avian/scripts/access-setup.sh discover https://ghlyms.com
sudo ./avian/scripts/access-setup.sh install <team-domain> <aud>
./avian/scripts/access-setup.sh check    # fetches the team certificates
```

`discover` needs no sign-in and no devtools: an unauthenticated request to
a protected site is bounced to the team login page, and that redirect names
both values — the host is the team domain, and the `kid` query parameter is
the application audience. Run it from somewhere not already signed in, or
the browser cookie short-circuits the redirect. `read` is the fallback: it
decodes a token you already have (`CF_Authorization`, from a signed-in
browser).

`read` decodes a token you already have — a browser signed in to the site
holds one in its `CF_Authorization` cookie — and prints both values from
its `iss` and `aud` claims. It takes the token on stdin, not as an
argument, because an argument is visible to every process on the machine,
and it never echoes it back. It does **not** verify the signature; that is
`access-auth.php`'s job when it matters.

`install` replaces in place if present and appends otherwise — the same
shape as the station's own config writer, so `LIVESTREAM_FILTER` and
`EXTRACTION_FILTER` survive. It backs the file up first. A test pins that.

Saving settings from the web UI also preserves these keys:
`admin_control.sh`'s writer prints through every line it is not replacing.

**Rebuilding the map** after a boundary revision:

```bash
base=https://media.githubusercontent.com/media/wmgeolab/geoBoundaries
curl -sSL -o /tmp/sg-adm2.geojson \
  $base/main/releaseData/gbOpen/SGP/ADM2/geoBoundaries-SGP-ADM2.geojson
python3 avian/scripts/build_sg_map.py /tmp/sg-adm2.geojson
./avian/scripts/bump_version.sh
```

The `raw.githubusercontent.com` URL returns a Git LFS pointer, not the
data — only the media host gives the real file.

**This view assumes Singapore.** The boundaries are Singapore's planning
areas, so a station elsewhere gets a map of the wrong country and
`avian_area_at()` returning null for every fix. Moving stations means
regenerating from that country's ADM2 and re-cropping `VIEW` in
`field.js`.
