#!/usr/bin/env python3
"""
sync_streams.py — Smart stream synchronization for Libnan TV

Strategy (tries each in order until streams found):
  1. EXACT channel ID match against iptv-org streams.json
  2. ALTERNATE IDs (configured per channel)
  3. TITLE FUZZY match against streams.json (catches ID typos)

Then optionally HEAD-checks each candidate URL (8s timeout) before applying.

Failed channels keep their existing URLs (the safety net).

Outputs a markdown report (STREAMS_REPORT.md) showing exactly what happened
to each channel — so silent failures become loud failures.
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher

HTML_FILE = os.environ.get("HTML_FILE", "libnan-tv.html")
REPORT_FILE = os.environ.get("REPORT_FILE", "STREAMS_REPORT.md")
VALIDATE = os.environ.get("VALIDATE_STREAMS", "true").lower() == "true"

MAX_STREAMS_PER_CH = 5
MAX_NEW_FROM_API = 4   # leave room for at least one existing fallback
HEAD_TIMEOUT = 8       # seconds
HEAD_WORKERS = 8       # parallel HEAD checks

BAD_LABELS = {"geo-blocked", "error", "drm", "not 24/7", "offline"}

# ─────────────────────────────────────────────────────────────────────
# Channel mapping. Each entry: (primary_id, [alternate_ids], [title_keywords])
# title_keywords are used for fuzzy fallback if no ID matches.
# Use lowercase for keywords; matching is case-insensitive.
# ─────────────────────────────────────────────────────────────────────
CHANNEL_MAP = {
    # ── Lebanon ──
    "aljadeed":      ("AlJadeed.lb",         [],                              ["al jadeed", "jadeed"]),
    "lbc":           ("LBCInternational.lb", ["LBC.lb", "LBCI.lb"],           ["lbc", "lbci"]),
    "mtv-lb":        ("MTVLebanon.lb",       ["MTV.lb"],                      ["mtv lebanon"]),
    "mayadeen":      ("AlMayadeen.lb",       [],                              ["mayadeen"]),
    "manar":         ("AlManar.lb",          [],                              ["al manar", "manar"]),
    "alhiwar":       ("Alhiwar.gb",          ["AlHiwar.gb", "AlHiwar.lb"],    ["al hiwar", "alhiwar"]),  # broadcasts from London
    "aliman":        ("AlimanTV.lb",         ["Aliman.lb"],                   ["aliman"]),
    "assirat":       ("AssiratTV.lb",        ["Assirat.lb"],                  ["assirat"]),

    # ── News ──
    "aljazeera":     ("AlJazeera.qa",        [],                              ["al jazeera arabic", "aljazeera"]),
    "ajm":           ("AlJazeeraMubasher.qa",[],                              ["jazeera mubasher", "mubasher"]),
    "ajd":           ("AlJazeeraDocumentary.qa", [],                          ["jazeera documentary"]),
    "alarabiya":     ("AlArabiya.ae",        ["AlArabiya.sa"],                ["al arabiya"]),
    "alarabiya-b":   ("AlArabiyaBusiness.ae",["AlArabiya.business.ae"],       ["arabiya business"]),
    "alaraby":       ("AlAraby.qa",          ["AlArabyTV.qa", "AlAraby.gb"],  ["al araby"]),  # often London-based
    "france24":      ("France24Arabic.fr",   ["F24Arabic.fr"],                ["france 24 arabic"]),
    "dw-ar":         ("DWArabic.de",         ["DWAr.de"],                     ["dw arabic", "deutsche welle arabic"]),
    "trt-ar":        ("TRTArabi.tr",         ["TRTArabic.tr"],                ["trt arabi", "trt arabic"]),
    "alhurra":       ("Alhurra.us",          [],                              ["alhurra"]),
    "almamlaka":     ("AlMamlaka.jo",        [],                              ["mamlaka"]),
    "sky-news-ar":   ("SkyNewsArabia.ae",    [],                              ["sky news arabia"]),
    "bbc-arabic":    ("BBCArabic.gb",        [],                              ["bbc arabic"]),
    "rt-arabic":     ("RTArabic.ru",         [],                              ["rt arabic"]),
    "cgtn-arabic":   ("CGTNArabic.cn",       [],                              ["cgtn arabic"]),
    "al-hadath":     ("AlHadath.sa",         ["AlHadath.ae"],                 ["al hadath", "hadath"]),
    "euronews-ar":   ("EuronewsArabic.fr",   ["Euronews.fr"],                 ["euronews arab"]),
    "almashhad":     ("AlMashhad.ae",        ["AlMashhad.iq"],                ["al mashhad", "mashhad"]),

    # ── Drama / MBC ──
    "mbc1":          ("MBC1.sa",             ["MBC1.ae"],                     ["mbc 1"]),
    "mbc-drama":     ("MBCDrama.sa",         ["MBCDrama.ae"],                 ["mbc drama"]),
    "mbc4":          ("MBC4.sa",             ["MBC4.ae"],                     ["mbc 4"]),
    "mbc5":          ("MBC5.sa",             ["MBC5.ae"],                     ["mbc 5"]),
    "mbc-iraq":      ("MBCIraq.iq",          ["MBCIraq.sa"],                  ["mbc iraq"]),
    "ifilm-ar":      ("iFilmArabic.ir",      ["IFilmArabic.ir"],              ["ifilm arabic"]),

    # ── Egypt ──
    "mbc-masr":      ("MBCMasr.eg",          ["MBCMasr.sa"],                  ["mbc masr"]),
    "mbc-masr2":     ("MBCMasr2.eg",         ["MBCMasr2.sa"],                 ["mbc masr 2"]),

    # ── Gulf ──
    "ad-aloula":     ("AbuDhabi.ae",         ["AbuDhabiAloula.ae"],           ["abu dhabi aloula", "abu dhabi al oula"]),
    "emirates":      ("EmiratesChannel.ae",  ["Emirates.ae"],                 ["emirates channel"]),
    "sharjah-tv":    ("SharjahTV.ae",        ["Sharjah.ae"],                  ["sharjah tv"]),
    "roya":          ("RoyaTV.jo",           ["Roya.jo"],                     ["roya tv"]),
    "jordan-tv":     ("JordanTV.jo",         ["Jordan.jo"],                   ["jordan tv"]),
    "qatar-tv":      ("QatarTV.qa",          ["Qatar.qa"],                    ["qatar tv"]),
    "oman-tv":       ("OmanTV.om",           ["Oman.om"],                     ["oman tv"]),

    # ── Sports ──
    "ad-sport1":     ("AbuDhabiSports1.ae",  ["AbuDhabiSport1.ae"],           ["abu dhabi sport 1", "abu dhabi sports 1"]),
    "ad-sport2":     ("AbuDhabiSports2.ae",  ["AbuDhabiSport2.ae"],           ["abu dhabi sport 2", "abu dhabi sports 2"]),
    "sharjah-sp":    ("SharjahSport.ae",     ["SharjahSports.ae"],            ["sharjah sport"]),
    "dubai-sp2":     ("DubaiSports2.ae",     ["DubaiSport2.ae"],              ["dubai sport 2", "dubai sports 2"]),
    "dubai-sp3":     ("DubaiSports3.ae",     ["DubaiSport3.ae"],              ["dubai sport 3", "dubai sports 3"]),

    # ── Kids ──
    "spacetoon":     ("Spacetoon.ae",        ["SpaceToon.ae"],                ["spacetoon"]),
    "mbc3":          ("MBC3.sa",             ["MBC3.ae"],                     ["mbc 3"]),
    "majid":         ("Majid.ae",            ["MajidTV.ae"],                  ["majid"]),

    # ── Religious ──
    "iqraa":         ("Iqraa.sa",            ["IqraaTV.sa"],                  ["iqraa"]),

    # ── Documentary ──
    "asharq-doc":    ("AsharqDocumentary.sa",["AsharqDoc.sa"],                ["asharq documentary"]),

    # ── Iraq ──
    "al-iraqiya":    ("AlIraqiya.iq",        ["Iraqiya.iq"],                  ["al iraqiya", "iraqiya"]),
    "alsumaria":     ("AlSumaria.iq",        ["Sumaria.iq"],                  ["al sumaria", "sumaria"]),
    "alsharqiya":    ("AlSharqiya.iq",       ["Sharqiya.iq"],                 ["al sharqiya iraq", "sharqiya"]),
    "rudaw":         ("Rudaw.iq",            ["RudawTV.iq"],                  ["rudaw"]),
    "kurdistan24":   ("Kurdistan24.iq",      ["K24.iq"],                      ["kurdistan 24", "kurdistan24"]),
    "dijlah":        ("DijlahTV.iq",         ["Dijlah.iq"],                   ["dijlah"]),

    # ── Palestine ──
    "palestine-tv":  ("PalestineTV.ps",      ["Palestine.ps"],                ["palestine tv"]),
    "watan-tv":      ("WatanTV.ps",          ["Watan.ps"],                    ["watan tv"]),
    "alquds-tv":     ("AlQudsTV.ps",         ["AlQuds.ps"],                   ["al quds tv"]),

    # ── Syria ──
    "syria-tv":      ("SyriaTV.sy",          ["Syria.sy"],                    ["syria tv"]),
    "orient-news":   ("OrientNews.sy",       ["Orient.sy"],                   ["orient news"]),
    "syria-al-ikhbariya": ("AlIkhbariyaSyria.sy", ["Ikhbariya.sy"],          ["ikhbariya syria", "al ikhbariya"]),

    # ── Kuwait ──
    "kuwait-tv":     ("KuwaitTV.kw",         ["Kuwait.kw", "KTV1.kw"],        ["kuwait tv", "ktv"]),
    "kuwait-alrai":  ("AlRai.kw",            ["AlRaiTV.kw"],                  ["al rai"]),

    # ── Bahrain ──
    "bahrain-tv":    ("BahrainTV.bh",        ["Bahrain.bh"],                  ["bahrain tv"]),
    "bahrain-int":   ("BahrainInternational.bh", ["BahrainInt.bh"],           ["bahrain international"]),
}

# ─────────────────────────────────────────────────────────────────────
def load_apis():
    with open("streams.json", encoding="utf-8") as f:
        streams = json.load(f)
    print(f"Loaded {len(streams)} stream entries")
    try:
        with open("channels.json", encoding="utf-8") as f:
            channels = json.load(f)
        print(f"Loaded {len(channels)} channel entries")
    except Exception:
        channels = []
    return streams, channels

def filter_clean(s):
    """Is this stream entry usable?"""
    url = s.get("url", "")
    label = (s.get("label") or "").lower()
    status = (s.get("status") or "").lower()
    if not url or ".m3u8" not in url.lower():
        return False
    if any(bad in label for bad in BAD_LABELS):
        return False
    if status == "error":
        return False
    return True

def build_indexes(streams):
    """Returns: by_channel_id, by_title_lower"""
    by_id = {}
    by_title = {}
    for s in streams:
        if not filter_clean(s):
            continue
        url = s["url"]
        ch = s.get("channel")
        if ch:
            by_id.setdefault(ch, []).append(url)
        title = (s.get("title") or "").strip().lower()
        if title:
            by_title.setdefault(title, []).append(url)
    return by_id, by_title

def fuzzy_title_search(by_title, keywords, threshold=0.78):
    """Find streams whose title closely matches any keyword."""
    found = []
    seen = set()
    keywords = [k.lower() for k in keywords]
    for title, urls in by_title.items():
        for kw in keywords:
            # 1) substring (fast path)
            if kw in title:
                for u in urls:
                    if u not in seen:
                        seen.add(u)
                        found.append(u)
                break
            # 2) fuzzy similarity
            ratio = SequenceMatcher(None, kw, title).ratio()
            if ratio >= threshold:
                for u in urls:
                    if u not in seen:
                        seen.add(u)
                        found.append(u)
                break
    return found

def head_check(url, timeout=HEAD_TIMEOUT):
    """Returns True if the URL responds successfully."""
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except urllib.error.HTTPError as e:
        # Some CDNs reject HEAD but accept GET - try a tiny GET
        if e.code in (403, 405):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Range": "bytes=0-2047"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return 200 <= resp.status < 400
            except Exception:
                return False
        return False
    except Exception:
        return False

def validate_urls(urls):
    """Parallel HEAD-check; preserves order, drops dead ones."""
    if not urls:
        return []
    results = {}
    with ThreadPoolExecutor(max_workers=HEAD_WORKERS) as ex:
        futs = {ex.submit(head_check, u): u for u in urls}
        for fut in as_completed(futs):
            url = futs[fut]
            try:
                results[url] = fut.result()
            except Exception:
                results[url] = False
    return [u for u in urls if results.get(u)]

# ─────────────────────────────────────────────────────────────────────
def find_streams_for(our_id, by_id, by_title):
    """Try EXACT -> ALT -> FUZZY. Returns (urls, strategy_used)."""
    cfg = CHANNEL_MAP.get(our_id)
    if not cfg:
        return [], "no-config"
    primary, alts, keywords = cfg
    if primary in by_id and by_id[primary]:
        return by_id[primary], f"exact:{primary}"
    for alt in alts:
        if alt in by_id and by_id[alt]:
            return by_id[alt], f"alt:{alt}"
    fuzzy = fuzzy_title_search(by_title, keywords)
    if fuzzy:
        return fuzzy, "fuzzy-title"
    return [], "no-match"

# ─────────────────────────────────────────────────────────────────────
def patch_html(html, our_id, urls):
    """Replace the streams:[...] for a given channel id. Returns (new_html, existing_count, replaced)."""
    pattern = (
        r'(id\s*:\s*"' + re.escape(our_id) + r'"'
        r'.*?streams\s*:\s*\[)'
        r'([^\]]*)'
        r'(\])'
    )
    m = re.search(pattern, html, re.DOTALL)
    if not m:
        return html, 0, False
    existing = re.findall(r'"(https?://[^"]+)"', m.group(2))
    # Merge: fresh first, existing as fallback (max 5, dedup)
    seen, combined = set(), []
    for u in (urls + existing):
        if u not in seen:
            seen.add(u)
            combined.append(u)
    final = combined[:MAX_STREAMS_PER_CH]
    formatted = ",\n     ".join(f'"{u}"' for u in final)
    replacement = m.group(1) + "\n     " + formatted + "\n   " + m.group(3)
    new_html = html[:m.start()] + replacement + html[m.end():]
    return new_html, len(existing), True

# ─────────────────────────────────────────────────────────────────────
def main():
    streams, channels = load_apis()
    by_id, by_title = build_indexes(streams)
    print(f"Indexed {len(by_id)} channel-IDs, {len(by_title)} unique titles\n")

    with open(HTML_FILE, encoding="utf-8") as f:
        html = f.read()
    print(f"Loaded {HTML_FILE} ({len(html):,} chars)\n")

    rows = []  # (our_id, strategy, n_api, n_validated, n_existing, n_final, status)
    updated = kept = dead = 0

    for our_id in CHANNEL_MAP:
        api_urls, strategy = find_streams_for(our_id, by_id, by_title)
        api_count = len(api_urls)

        # Limit how many we take from API
        api_urls = api_urls[:MAX_NEW_FROM_API * 2]  # take extra so HEAD-validation still leaves enough

        if VALIDATE and api_urls:
            print(f"  [{our_id:<22}] HEAD-checking {len(api_urls)} URLs ({strategy})...", flush=True)
            t0 = time.time()
            api_urls = validate_urls(api_urls)
            print(f"  [{our_id:<22}] {len(api_urls)} alive in {time.time()-t0:.1f}s")

        api_urls = api_urls[:MAX_NEW_FROM_API]

        # Find existing streams (always)
        existing_match = re.search(
            r'id\s*:\s*"' + re.escape(our_id) + r'".*?streams\s*:\s*\[([^\]]*)\]',
            html, re.DOTALL
        )
        in_html = existing_match is not None
        existing = re.findall(r'"(https?://[^"]+)"', existing_match.group(1)) if in_html else []

        if api_urls:
            new_html, n_existing, ok = patch_html(html, our_id, api_urls)
            if ok:
                html = new_html
                final_count = min(MAX_STREAMS_PER_CH, len(set(api_urls + existing)))
                status = "UPDATED"
                updated += 1
            else:
                final_count = len(existing)
                status = "NOT_IN_HTML"
                dead += 1
        else:
            n_existing = len(existing)
            final_count = n_existing
            if not in_html:
                status = "NOT_IN_HTML"
                dead += 1
            elif strategy == "no-match":
                status = "KEPT_OLD" if n_existing > 0 else "EMPTY"
                if n_existing > 0:
                    kept += 1
                else:
                    dead += 1
            else:
                status = "ALL_DEAD"   # found in API but all failed HEAD
                kept += 1

        rows.append((our_id, strategy, api_count, len(api_urls), len(existing), final_count, status))

        emoji = {
            "UPDATED": "✅", "KEPT_OLD": "⚠️", "ALL_DEAD": "💀",
            "EMPTY": "❌", "NOT_IN_HTML": "🔥"
        }.get(status, "?")
        print(f"  {emoji} {our_id:<22} {status:<12} strategy={strategy:<22} api={api_count} validated={len(api_urls)} existing={len(existing)} → {final_count}")

    # ─── Write patched HTML ───
    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html)

    # ─── Write report ───
    lines = [
        "# Stream Sync Report",
        "",
        f"**Generated:** {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}",
        f"**Validation:** {'enabled (HEAD-checked)' if VALIDATE else 'disabled'}",
        "",
        f"- **Updated:** {updated}",
        f"- **Kept old (no fresh streams found):** {kept}",
        f"- **Completely dead (no streams anywhere):** {dead}",
        "",
        "## Per-channel detail",
        "",
        "| Channel | Status | Strategy | API found | Validated | Existing | Final |",
        "|---|---|---|---|---|---|---|",
    ]

    for our_id, strategy, api_c, val_c, exist_c, final_c, status in rows:
        lines.append(f"| {our_id} | {status} | {strategy} | {api_c} | {val_c} | {exist_c} | {final_c} |")

    lines += [
        "",
        "## Legend",
        "",
        "- ✅ **UPDATED** — fresh streams from iptv-org applied (existing kept as fallback)",
        "- ⚠️ **KEPT_OLD** — no match in iptv-org, your hardcoded streams preserved",
        "- 💀 **ALL_DEAD** — iptv-org had streams but ALL failed HEAD-check; old streams preserved",
        "- ❌ **EMPTY** — channel exists in HTML but has zero working streams (broken in app)",
        "- 🔥 **NOT_IN_HTML** — channel id from sync map is missing from the HTML (config drift — fix CHANNEL_MAP or HTML)",
        "",
        "## Strategies",
        "",
        "1. exact:<id> — direct iptv-org channel-ID match",
        "2. alt:<id> — alternate ID match (configured fallback)",
        "3. fuzzy-title — title keyword search (catches ID typos & moved channels)",
        "4. no-match — no streams found by any strategy",
    ]

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print()
    print("=" * 60)
    print(f"  Updated   : {updated}")
    print(f"  Kept old  : {kept}")
    print(f"  Dead      : {dead}")
    print("=" * 60)

    # GitHub Actions outputs
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write(f"updated={updated}\n")
            f.write(f"kept={kept}\n")
            f.write(f"dead={dead}\n")

if __name__ == "__main__":
    main()
