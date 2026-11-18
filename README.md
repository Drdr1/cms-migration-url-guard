# cms-migration-url-guard

A URL safety net for CMS migrations: Joomla 3 → Joomla 4/5/6, WordPress host moves, and template rebuilds.

The quickest way to lose a site's search rankings in a migration is a listing, article or image URL that quietly stops working. This tool takes a full inventory of the old site **before** anything changes. After the rebuild, it replays every one of those URLs against staging, so go-live depends on a pass/fail sheet and not on spot checks.

It's a single Python file that uses only the standard library, so there's nothing to install.

## What it does

| Command | When | Output |
|---|---|---|
| `crawl` | Before migration | `urls.csv` (every URL: status, redirect chain, title, canonical, Joomla component/view, page type, inlinks), `images.csv`, `meta.json` |
| `report` | Before migration | `report.md`: what the site is made of, URL patterns to keep, migration risks, image folders, existing redirects, current errors |
| `diff` | After rebuild, before go-live | `diff.md` / `diff.csv` (PASS / MISSING / CONTENT CHANGED / TEMP REDIRECT / NEEDS 301/410), `images-diff.csv`, `redirects.htaccess` (draft 301 rules with suggested targets) |

### Joomla-aware checks

- Classifies pages by component and view, from the URL or the `<body class>` that most Joomla templates (T3 included) emit: articles, categories, OS Property listings, categories, agents and search, contact, pagination.
- Flags URLs for features being dropped (EShop, AcyMailing, user login and registration, VIP or members areas), because each one needs a 301 or a 410 decision.
- Fingerprints the template stack: T3 framework, template folder, MooTools, jQuery 1.x, slider and popup scripts.
- Lists non-SEF `index.php?option=…` URLs that the new router won't generate.
- Reads `robots.txt` sitemaps plus the usual OSMap, JMap and Xmap endpoints, so orphan listings that aren't linked in menus still get checked.

### Diff logic

- An old 200 page is **PASS** if the new site serves it at the same path with a matching title, or reaches it through a permanent redirect (301/308).
- A 302 or 307 on the way is flagged **TEMP REDIRECT (use 301)**.
- An old redirect has to keep resolving.
- An out-of-scope URL that returns 404 is flagged **NEEDS 301/410**.
- For missing URLs, it suggests a target by matching the slug at a new location (`/properties/12-x` → `/listings/12-x`) or, failing that, the page title. It then writes draft `.htaccess` rules, including `QUERY_STRING` conditions for non-SEF URLs.
- An image that was already broken on the old site is reported as such, not blamed on the migration.

## Usage

```bash
# 1. Before migration: inventory the live site
python3 urlguard.py crawl https://www.example.com.au --out audit-live --check-images
python3 urlguard.py report audit-live        # -> audit-live/report.md

# 2. After the rebuild: crawl staging (used for redirect suggestions)
python3 urlguard.py crawl https://staging.example.com.au --out audit-staging

# 3. Replay every live URL against staging
python3 urlguard.py diff audit-live https://staging.example.com.au \
    --out diff-staging --check-images --new-inventory audit-staging
```

`diff` exits with `0` only when every URL passes, so it can serve as a go-live gate in a script or CI job.

### Options

```
crawl   --max-pages 3000  --max-depth 12  --delay 0.3  --timeout 20
        --check-images  --no-sitemap  --ignore-robots
diff    --check-images  --new-inventory DIR  --title-threshold 0.5  --delay 0.2
```

The crawler is polite by default: it waits 0.3 s between requests, respects `robots.txt`, and identifies itself in its User-Agent. If staging is behind HTTP basic auth, put the credentials in the URL (`https://user:pass@staging…`) or allow the auditing IP.

## Example report (excerpt)

```
## Migration risks found
- T3 framework in use. T3 does not run on Joomla 6, so the template has to be rebuilt …
- 214 OS Property pages. Listing URLs come from OS Property's own router …
- Out-of-scope features still have public URLs: EShop (37), VIP area (4). Each needs a decision: 301 or 410.
- 12 non-SEF URL(s) (index.php?option=…) are linked or indexed …

## Migration diff
| Verdict | URLs |
| PASS | 1,284 |
| MISSING | 3 |
| TEMP REDIRECT (use 301) | 1 |
```

(These numbers are illustrative.)

## Limits

- It only sees public pages. Anything behind a login, a form POST or JavaScript-only navigation won't be crawled.
- Search Console and server logs often show URLs that nothing links to anymore. Append them to `urls.csv` (path column) before running `diff`.
- Suggested redirect targets are suggestions. Review `redirects.htaccess` before deploying it.

## License

MIT
