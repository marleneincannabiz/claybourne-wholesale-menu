#!/usr/bin/env python3
"""
Automated Claybourne wholesale menu refresh.

Runs entirely on GitHub Actions infrastructure (no local device needed):
1. Uses a headless browser to download the latest Vireo wholesale inventory export.
2. Filters to Claybourne rows, consolidates by (category, strain).
3. Splices the fresh DATA/BATCHES blocks into index.html, preserving structure,
   genetics tags, and category metadata from categories.json.
4. Updates the "last refreshed" footer line.

Exits non-zero (failing the GitHub Actions run, which triggers GitHub's own
failure-notification email) if anything looks wrong, rather than silently
publishing bad or stale data:
  - the download never appears
  - a Claybourne row's Description doesn't match any known category
  - a category ends up with zero visible strains it didn't have before

New strains found within already-known categories ARE added automatically
(same shape as their siblings, no genetics guessed). A completely new
category is NOT auto-added -- the script fails loudly instead so a human
reviews it.
"""
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
INDEX_HTML = ROOT / "index.html"
CATEGORIES_JSON = ROOT / "categories.json"
DOWNLOAD_DIR = ROOT / "download"

VIREO_URL = "https://www.vireonymenu.com/"


def log(msg):
    print(f"[refresh_menu] {msg}", flush=True)


def download_vireo_export() -> Path:
    from playwright.sync_api import sync_playwright

    DOWNLOAD_DIR.mkdir(exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(accept_downloads=True)
        log(f"navigating to {VIREO_URL}")
        page.goto(VIREO_URL, wait_until="domcontentloaded", timeout=60000)
        # Let the inventory table finish loading.
        page.wait_for_selector("text=DOWNLOAD EXCEL", timeout=30000)
        time.sleep(2)

        with page.expect_download(timeout=60000) as download_info:
            page.click("text=DOWNLOAD EXCEL")
        download = download_info.value
        dest = DOWNLOAD_DIR / "vireo_export.xlsx"
        download.save_as(str(dest))
        browser.close()

    if not dest.exists() or dest.stat().st_size < 1000:
        raise RuntimeError("download did not produce a usable file")
    log(f"downloaded {dest} ({dest.stat().st_size} bytes)")
    return dest


def consolidate(xlsx_path: Path, desc_to_cat: dict):
    import openpyxl

    wb = openpyxl.load_workbook(xlsx_path, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    header = [str(h).strip() if h else "" for h in rows[0]]
    idx = {name: i for i, name in enumerate(header)}
    required = ["Brand", "Description", "Strain", "Type", "THC", "Qty Available"]
    missing = [r for r in required if r not in idx]
    if missing:
        raise RuntimeError(f"workbook columns changed, missing: {missing} (saw: {header})")

    claybourne = [r for r in rows[1:] if r[idx["Brand"]] == "Claybourne"]
    if not claybourne:
        raise RuntimeError("no Claybourne rows found in the export -- workbook may be malformed")
    log(f"{len(claybourne)} Claybourne rows found")

    unmapped = set()
    groups = {}  # (cat, strain) -> list[(thc, qty)]
    type_by_key = {}
    for r in claybourne:
        desc = r[idx["Description"]]
        strain = r[idx["Strain"]]
        typ = r[idx["Type"]]
        thc_raw = r[idx["THC"]]
        qty = r[idx["Qty Available"]] or 0
        cat = desc_to_cat.get(desc)
        if cat is None:
            unmapped.add(desc)
            continue
        thc = float(str(thc_raw).replace("%", ""))
        key = (cat, strain)
        groups.setdefault(key, []).append((thc, qty))
        type_by_key[key] = typ

    if unmapped:
        raise RuntimeError(
            "unrecognized product Description(s) not in desc_to_cat mapping -- "
            f"needs a human to classify: {sorted(unmapped)}"
        )

    consolidated = {}
    batches = {}
    for (cat, strain), lst in groups.items():
        total_qty = sum(q for _, q in lst)
        min_thc = min(t for t, _ in lst)
        consolidated[(cat, strain)] = {
            "qty": total_qty,
            "thc": min_thc,
            "type": type_by_key[(cat, strain)],
        }
        blist = sorted(lst, key=lambda x: -x[1])
        batches[f"{cat}||{strain}"] = [
            {"thc": fmt_thc(t), "qty": q} for t, q in blist
        ]
    return consolidated, batches


def fmt_thc(v: float) -> str:
    if v == int(v):
        return f"{v:.1f}"
    return str(v)


def build_data_and_batches(config: dict, consolidated: dict):
    desc_to_cat = config["desc_to_cat"]
    known_cat_ids = {c["id"] for c in config["categories"]}
    seen_keys = set()
    new_data = []

    for cat in config["categories"]:
        new_strains = []
        for s in cat["strains"]:
            key = (cat["id"], s["n"])
            seen_keys.add(key)
            c = consolidated.get(key)
            entry = {"n": s["n"], "t": s["t"]}
            if "gen" in s:
                entry["gen"] = s["gen"]
            if c:
                entry["thc"] = fmt_thc(c["thc"])
                entry["qty"] = c["qty"]
            else:
                # No current batches in the feed -- keep last-known THC, qty 0.
                entry["thc"] = s.get("thc", "0.0")
                entry["qty"] = 0
            new_strains.append(entry)

        # Genuinely new strains the feed now shows in this already-known category.
        for (cat_id, strain), c in consolidated.items():
            if cat_id != cat["id"] or (cat_id, strain) in seen_keys:
                continue
            log(f"NEW STRAIN found in existing category {cat_id}: {strain!r} -- adding")
            new_strains.append({
                "n": strain, "t": c["type"], "thc": fmt_thc(c["thc"]), "qty": c["qty"],
            })
            seen_keys.add((cat_id, strain))

        new_cat = dict(cat)
        new_cat["strains"] = new_strains
        new_data.append(new_cat)

    # A genuinely new category (Description mapped to a cat id we don't know) --
    # shouldn't happen since desc_to_cat only maps to known ids, but check anyway.
    stray_cats = {cat_id for (cat_id, _s) in consolidated if cat_id not in known_cat_ids}
    if stray_cats:
        raise RuntimeError(f"consolidated data references unknown category ids: {stray_cats}")

    return new_data


def js_str(s: str) -> str:
    return json.dumps(s)


def render_data_block(data: list) -> str:
    lines = ["  var DATA = ["]
    for i, cat in enumerate(data):
        lines.append("    {")
        lines.append(f'      id:{js_str(cat["id"])},')
        lines.append(f'      name:{js_str(cat["name"])},')
        lines.append(f'      desc:{js_str(cat["desc"])},')
        lines.append(
            f'      unit:{js_str(cat["unit"])}, caseSize:{js_str(cat["caseSize"])}, '
            f'caseCost:{js_str(cat["caseCost"])},'
        )
        lines.append("      strains:[")
        srows = []
        for s in cat["strains"]:
            parts = [f'n:{js_str(s["n"])}', f't:{js_str(s["t"])}']
            if "gen" in s:
                parts.append(f'gen:{js_str(s["gen"])}')
            parts.append(f'thc:{js_str(s["thc"])}')
            parts.append(f'qty:{s["qty"]}')
            srows.append("        {" + ", ".join(parts) + "}")
        lines.append(",\n".join(srows))
        lines.append("      ]")
        lines.append("    }" + ("," if i < len(data) - 1 else ""))
    lines.append("  ];")
    return "\n".join(lines)


def render_batches_block(batches: dict) -> str:
    return "  var BATCHES = " + json.dumps(batches, separators=(",", ":")) + ";"


def splice_html(html: str, data_js: str, batches_js: str) -> str:
    html, n = re.subn(
        r"  var DATA = \[.*?\n  \];",
        lambda _m: data_js.replace("\\", "\\\\"),
        html,
        count=1,
        flags=re.DOTALL,
    )
    if n == 0:
        raise RuntimeError("could not locate 'var DATA = [ ... ];' block in index.html")

    html, n = re.subn(
        r"  var BATCHES = \{.*?\};",
        lambda _m: batches_js.replace("\\", "\\\\"),
        html,
        count=1,
        flags=re.DOTALL,
    )
    if n == 0:
        raise RuntimeError("could not locate 'var BATCHES = { ... };' block in index.html")

    et = timezone(timedelta(hours=-4))  # EDT; flip to -5 after DST ends in Nov if this drifts
    ts = datetime.now(et).strftime("%B %-d, %Y at %-I:%M %p ET")
    html, n = re.subn(
        r"last refreshed .*? from the Vireo wholesale inventory feed",
        f"last refreshed {ts} from the Vireo wholesale inventory feed",
        html,
        count=1,
    )
    if n == 0:
        raise RuntimeError("could not locate the 'last refreshed ... from the Vireo' footer text")
    return html


def main():
    config = json.loads(CATEGORIES_JSON.read_text())
    xlsx_path = download_vireo_export()
    consolidated, batches = consolidate(xlsx_path, config["desc_to_cat"])
    new_data = build_data_and_batches(config, consolidated)

    # sanity check: no existing category should go from >0 visible strains to 0
    floor = config["sold_out_floor"]
    for old_cat, new_cat in zip(config["categories"], new_data):
        old_visible = sum(1 for s in old_cat["strains"] if s.get("qty", 0) >= floor)
        new_visible = sum(1 for s in new_cat["strains"] if s.get("qty", 0) >= floor)
        if old_visible > 0 and new_visible == 0:
            raise RuntimeError(
                f"category {old_cat['id']!r} went from {old_visible} visible strains to 0 -- "
                "looks like a data problem, not a real sellout. Refusing to publish."
            )

    data_js = render_data_block(new_data)
    batches_js = render_batches_block(batches)

    html = INDEX_HTML.read_text()
    new_html = splice_html(html, data_js, batches_js)
    INDEX_HTML.write_text(new_html)

    # persist the fresh structure (incl. last-known THC) as the new baseline
    config["categories"] = new_data
    CATEGORIES_JSON.write_text(json.dumps(config, indent=1))

    total_strains = sum(len(c["strains"]) for c in new_data)
    visible = sum(1 for c in new_data for s in c["strains"] if s.get("qty", 0) >= floor)
    log(f"updated index.html: {total_strains} strains tracked, {visible} visible")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"FAILED: {e}")
        sys.exit(1)
