# Claybourne Wholesale Menu — GitHub Pages mirror

Public menu: https://marleneincannabiz.github.io/claybourne-wholesale-menu/

## Automated refresh

`.github/workflows/refresh-menu.yml` runs on GitHub's own servers on a schedule
(weekdays at 7am/10am/1pm/4pm/7pm ET) and on-demand (Actions tab → "Refresh
Claybourne wholesale menu" → Run workflow). It:

1. Uses a headless browser (`refresh_menu.py` + Playwright) to download the
   latest export from vireonymenu.com.
2. Filters to Claybourne rows, consolidates by category/strain.
3. Splices the fresh numbers into `index.html`, using `categories.json` as the
   source of truth for category names, pricing, and genetics tags.
4. Commits and pushes if anything changed.

This keeps this GitHub Pages copy current automatically — no computer or
Claude session needs to be running.

**The private Claude artifact is separate and still needs a manual refresh.**
Vireo's download requires a real click in a real browser, which only a live
Claude session with device/browser access can do; that step can't run
unattended. Ask Claude to "refresh the menu" in a live conversation to update
both the Claude artifact and this repo together.

## If a scheduled run fails

GitHub emails the repo owner on workflow failure by default (check
github.com → Settings → Notifications → Actions, if you want to confirm or
change this). Common causes:

- **Vireo changed their page** (button text/selector) — `refresh_menu.py`'s
  `download_vireo_export()` needs updating.
- **A new product category appeared** — the script deliberately fails loudly
  rather than guessing; add it to `categories.json`'s `desc_to_cat` mapping.
- **The workbook's columns changed** — `consolidate()` checks for the expected
  column names and fails with a clear message if they're missing.

Failed runs upload the downloaded workbook as a build artifact (Actions tab →
the failed run → Artifacts) for 3 days, to help debug without needing to
reproduce the failure.

## DST note

The cron schedule is pinned to UTC. See the comment in
`.github/workflows/refresh-menu.yml` for the one-line change needed when
daylight saving starts/ends, to keep firing at the same New York clock time.
