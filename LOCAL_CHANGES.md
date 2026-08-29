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
| `atlasDefault` | `'cards'` | upstream defaults to stamps |
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
| `renderAtlas` renamed `renderAtlasStamps`, clears `data-render` | keep ours — the dual-atlas dispatcher depends on it |
| live audio host test gains the allowlist check | keep ours |
| settings `.seg` selector gains `:not([data-atlas-seg])` | keep ours — without it the atlas toggle is claimed as a Pi config field and goes inert |

Plus insertions, which merge cleanly unless upstream edits the same
region: `ASSET_VERSION`, `localCfg()`, `atlasStyle()`/`renderAtlas()`
dispatcher, `renderAtlasCards()` (~250 lines), `atlasStyleRow()`.

`renderAtlasCards` was considered for extraction into its own file and
deliberately left in place: it calls ~14 `apt.js` internals (`audioClaim`,
`paintSpectrogram`, `getSpecCtx`, `stopCurrent`, `playAtlasEntrance` …),
so extracting it would mean a wide, fragile interface that upstream could
break by renaming any one of them — in exchange for moving an insertion
that rarely conflicts anyway.

### Other upstream files

| file | change |
|---|---|
| `avian/frontend/index.html` | loads `local-config.js`; single `?v=` token; theme resolver reads `themeDefault` |
| `avian/frontend/styles.css` | dual-source font path; `data-render="cards"` grid rules to out-specify `stamps.css` |
| `avian/scripts/cutout.py` | falls back to `u2net` under ~6 GB RAM — birefnet is ~1 GB and OOM-kills a 4 GB Pi |
| `avian/scripts/species-notes.json` | per-species prompt notes (e.g. Pygmy Cupwing) |
| `scripts/utils/reporting.py` | `maybe_auto_illustrate()` hook; `EXTRACTION_FILTER` support |
| `scripts/birdnet_analysis.py` | calls `maybe_auto_illustrate()` after `write_to_db` |
| `scripts/livestream.sh` | `LIVESTREAM_FILTER` — stream-only audio filter |
| `scripts/link_webroot.sh` | publishes `local-config.js` |
| `avian/scripts/pregen.py` | bare scientific-name label files, standalone eBird-region mode, NVIDIA backend (+278/-46 — the largest local change to an upstream file) |
| `.gitignore` | `birdnet.conf.save`, `*.whl`, `batch_requests.jsonl` |

---

## Not in git

- `/etc/caddy/Caddyfile` — hand-spliced for `AVIAN_DIRECT_LOCAL` and the
  Cloudflare Access matchers. **Do not regenerate it** with
  `scripts/update_caddyfile.sh`; that would discard those edits.
- `/etc/birdnet/birdnet.conf` — station config, holds secrets.
  Local additions: `LIVESTREAM_FILTER`, `EXTRACTION_FILTER`.
- `~/BirdNET-Pi/whitelist_species_list.txt` — species allowed past the
  occurrence-frequency filter (Rose-ringed Parakeet: established in
  Singapore, but BirdNET's model underrates introduced populations).
