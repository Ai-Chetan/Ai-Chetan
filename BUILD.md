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
