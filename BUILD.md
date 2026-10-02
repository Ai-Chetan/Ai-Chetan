# Ai-Chetan

Auto-generated, self-updating GitHub profile.

| What | Where |
| --- | --- |
| Live animated profile SVG | `live` branch -> `profile.svg` |
| Editable source (art + animations) | `profile.template.svg` |
| All dynamic content configuration | `config.json` |
| Build script (SVG surgery + README generator) | `scripts/build.py` |
| Automation | `.github/workflows/update-live-profile.yml` |

## How it works

1. `config.json` is the single source of truth: username, stat options, social
   URLs, tech-stack links, project cards (with optional per-card overrides).
2. Every 15 minutes (`7,22,37,52 * * * *`), on every push to `master`, or on
   demand via **Run workflow**, GitHub Actions runs `scripts/build.py`:
   - fetches repositories, stars, forks, contributions, per-repo metadata and
     languages from the GitHub API
   - injects everything into `profile.template.svg` (stat badges, project
     titles/descriptions/status/tags/stars/last-updated, all link targets)
   - regenerates `README.md` from the same config
   - publishes `profile.svg` to the orphan `live` branch, and commits
     `README.md` back to `master` if it changed
3. Nothing is hardcoded: edit `config.json` and the next run updates the
   profile card and the README automatically.

## Local usage

```bash
python scripts/build.py --dry-run     # report values without writing
python scripts/build.py               # build profile.svg + README.md
python scripts/build.py --strict      # fail if any stat could not be fetched
python scripts/build.py --offline     # no network; copies the template through
```

Requires Python 3.10+ (standard library only). Set `STATS_PAT` (a classic PAT
with `read:user`) to fetch live values; without a token the build still works
but keeps template values.

## Config quick reference

```jsonc
{
  "github": { "user": "Ai-Chetan" },
  "stats": {
    "contributions_since": "2024-07-01",   // start of the contributions total
    "count_forks_as_repositories": true,   // include forks in the repo count
    "count_forks_in_stars": false,         // include fork stars in the total
    "stale_after_days": 90                 // push age after which a card reads "Stale"
  },
  "site": { "portfolio_url": "...", "profile_svg_url": "..." },
  "views": {
    "enabled": true,
    "source": "https://komarev.com/ghpvc/?username=<user>",
    "label": "PROFILE VIEWS"
  },
  "socials": [ { "id": "linkedin", "label": "LinkedIn", "url": "..." } ],   // id: linkedin | email | portfolio | discord
  "tech":    [ { "id": "python", "url": "..." } ],                          // id: python, js, cpp, react, next, tailwind, node, express, django, postgres, mongo, mysql, docker, aws, actions, git, vscode, figma
  "projects": [
    {
      "card": 1,
      "repo": "owner/name",
      "override": {                        // every field is optional
        "title": "...", "description": "...",
        "status": "active | wip | stale | archived",
        "tags": ["Python", "FastAPI"], "url": "..."
      }
    }
  ]
}
```

## Requirements

- Repository secret `STATS_PAT`: classic PAT with `read:user` (add `repo` scope
  only if private repositories should be counted).

## Profile view counter

`config.json -> views` points at a shields-style badge endpoint (komarev by
default). Each run scrapes the number out of the badge SVG and writes it into
`#views-count` in the footer chip, alongside the `#views-beacon` pulse rings.

It has to be baked in rather than embedded. `profile.svg` is loaded through
`<img>`, and browsers refuse to load *any* external resource from inside an SVG
used as an image, so a live badge `<image>` would render as nothing. The count
therefore updates when the workflow runs -- every 15 minutes, same as the stats.

- A fetch failure is a warning, not an error: the template's value is kept and
  `--strict` does not fail on it, since a view count is not a primary stat.
- The counter is comma-grouped and never rounded, and `#views-count` is
  `text-anchor="end"`, so 4 to 7 digits all stay flush right.
- The chip occupies absolute y 1462-1496, entirely inside the last README slice
  (1448-1510). Moving it across y=564 or y=626 (section-local) would split it
  across two slice images and cut it in half.

## Embedded artwork

`profile.template.svg` inlines seven bitmaps as base64 data URIs: the hero
banner, the social/tech/stats panel sheet, the featured-projects backdrop, and
four project card thumbnails. They were ~99% of the file, and because base64 of
already-compressed data barely responds to gzip, they dominated what a profile
visitor downloaded. They have since been re-encoded in place; there is no
separate asset pipeline and nothing in the build touches them.

Constraints to respect if you replace any of them:

- **The panel sheet is not only the clipped icons.** One unclipped `<use>` right
  after `</defs>` paints it full size, so it carries 11px text at roughly 1:1 and
  is encoded as JPEG q88 4:4:4. Palette quantization was measured here and
  rejected: 256 colours was smaller (117 KB vs 199 KB) but left visible olive
  blotching across 43% of the panel's dark areas.
- **The hero is downscaled to 1086px.** It is drawn at `width="2048"` inside a
  viewBox that is itself halved, so 2172 native is ~2.1x oversampled.
- **Everything uses 4:4:4 chroma** (`subsampling=0`). 4:2:0 smears colour
  fringes off the small text.
- **WebP is not used.** It reaches 336 KB gzipped instead of 579 KB, but an SVG
  renderer built without libwebp draws a `data:image/webp` `<image>` as nothing
  at all, so it would blank the artwork instead of degrading it.

Result: 5,424,498 bytes raw / 4,062,685 gzipped down to 817,750 / 578,743, a
7.0x reduction on the wire. Verified by rendering the profile through
`README.md` in headless Chrome before and after: PSNR 42.2 dB, 0.23% of pixels
differing by more than 8/255, layout height unchanged.

### Replacing a bitmap

The originals are not in the tree; they survive only in git history (before the
compression commit). If you swap the hero, a backdrop or a project thumbnail:

- encode it for the size it is actually displayed, not its native size
- keep 4:4:4 chroma (`subsampling=0`) for anything with text
- do not palette-quantize the panel sheet; it is on screen at ~1:1
- re-measure with `gzip -9` on the template afterwards, since base64 of
  already-compressed data is what defeats gzip here
