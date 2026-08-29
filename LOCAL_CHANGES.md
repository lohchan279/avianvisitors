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
| `avian/scripts/bump_version.sh` | bump the frontend cache token |
| `avian/scripts/optimize_illustrations.py` | shrink the illustration library |
| `avian/scripts/fetch_fork_illustrations.py`, `fork_species.tsv` | pull illustrations from community forks |
| `scripts/noise_profile.sh` | diagnose what the background noise actually is |
| `scripts/filter_audition.sh` | compare audio filters by ear on one recording |
| `scripts/filter_test.sh`, `scripts/score_file.py` | A/B filters against the real model |
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

Seven modified lines in `apt.js`. All are structurally stable — none needs
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

---

## Upstream tests that fail here by design

`python3 -m pytest tests/ --ignore=tests/test_analysis.py` gives **69 passed,
4 failed** on this fork. All six of these pass on a clean upstream checkout,
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
- Admin password — set with
  `sudo /usr/local/sbin/avian-admin-control password-reset` (12-64 letters and
  digits, SSH only). "Require password on local network" is **off**: with it
  on, upstream disables live audio everywhere, in both Caddy and the
  frontend, because Icecast has no auth of its own.
- `/etc/birdnet/birdnet.conf` — station config, holds secrets.
  Local additions: `LIVESTREAM_FILTER`, `EXTRACTION_FILTER`.
- `~/BirdNET-Pi/whitelist_species_list.txt` — species allowed past the
  occurrence-frequency filter (Rose-ringed Parakeet: established in
  Singapore, but BirdNET's model underrates introduced populations).
