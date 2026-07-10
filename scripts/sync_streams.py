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

────────────────────────────────────────────────────────────────────────
HARDENING (why this version exists):
The previous version corrupted libnan-tv.html and truncated it live. Root
causes, now fixed:
  • filter_clean() only checked for ".m3u8", so it accepted ad-tagged URLs
    full of [MACRO] placeholders (e.g. a FashionTV stream for ON E). The
    "]" characters inside those URLs broke the streams:[ ... ] regex.
  • patch_html() matched the array with  streams:\\[([^\\]]*)\\]  which stops
    at the FIRST "]" — a "]" inside a URL orphaned the rest of the URL and,
    via the greedy .*? on later channels, truncated the file.
  • Nothing validated the result before it was committed and pushed.

Fixes: reject any URL containing brackets/quotes/spaces or ad macros;
quote-aware bracket scanning instead of a fragile regex; and a full
integrity check (balanced brackets, unchanged channel count, ends with
</html>, no ad-macro signatures, no big size drop) that ABORTS without
writing if anything looks wrong. Writes are atomic (temp + os.replace).
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
MIN_OUTPUT_RATIO = 0.9  # abort if patched file shrinks below 90% of original

BAD_LABELS = {"geo-blocked", "error", "drm", "not 24/7", "offline"}

# A safe HLS URL contains none of these characters. iptv-org occasionally
# returns ad-tagged URLs (VAST/SSAI) stuffed with [MACRO] placeholders and
# spaces — those brackets/quotes are what corrupted the HTML array before.
_URL_FORBIDDEN_CHARS = set('[]{}"\'`<>\\ \t\r\n')
_URL_BAD_SIGNATURES = (
    "[ads", "av_apppkgname", "cachebuster", "[cache", "[timestamp",
    "[player", "%5b", "%5d", "vast", "gdpr_consent", "us_privacy",
)


# ─────────────────────────────────────────────────────────────────────
def url_is_safe(url):
    """Accept only clean, embeddable HLS URLs. Rejects the ad-macro URLs
    that previously corrupted the file."""
    if not url or not isinstance(url, str):
        return False
    if len(url) > 500:                       # real HLS URLs are short; ad URLs are huge
        return False
    low = url.lower()
    if not low.startswith(("http://", "https://")):
        return False
    if ".m3u8" not in low:
        return False
    if any(c in _URL_FORBIDDEN_CHARS for c in url):
        return False
    if any(sig in low for sig in _URL_BAD_SIGNATURES):
        return False
    return True


def filter_clean(s):
    """Is this stream entry usable?"""
    url = s.get("url", "")
    label = (s.get("label") or "").lower()
    status = (s.get("status") or "").lower()
    if not url_is_safe(url):
        return False
    if any(bad in label for bad in BAD_LABELS):
        return False
    if status == "error":
        return False
    return True


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
    "otv-lb":        ("OneTV.lb",            ["OTV.lb", "OTVLebanon.lb"],     ["one tv lebanon", "otv lebanon"]),
    "nbn":           ("NBN.lb",              ["NBNLebanon.lb"],               ["nbn"]),
    "teleliban":     ("TeleLiban.lb",        ["TL.lb"],                       ["tele liban", "teleliban"]),

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

    # ── Saudi Arabia ──
    "al-ekhbariya":  ("AlEkhbariya.sa",      ["Ekhbariya.sa"],                ["al ekhbariya", "ekhbariya"]),
    "saudi-tv":      ("AlSaudiya.sa",        ["SaudiTV.sa", "Saudi1.sa"],     ["al saudiya", "saudi tv"]),
    "saudi-alaan":   ("AlSaudiyaAlaan.sa",   [],                              ["saudiya alaan", "saudi alaan"]),
    "rotana-khalijia":("RotanaKhalijia.sa",  ["Rotana Khalijia"],             ["rotana khalijia", "khalijia"]),
    "rotana-classic":("RotanaClassic.sa",    [],                              ["rotana classic"]),
    "rotana-cinema": ("RotanaCinema.sa",     ["RotanaCinemaKSA.sa"],          ["rotana cinema"]),

    # ── Egypt ──
    "mbc-masr":      ("MBCMasr.eg",          ["MBCMasr.sa"],                  ["mbc masr"]),
    "mbc-masr2":     ("MBCMasr2.eg",         ["MBCMasr2.sa"],                 ["mbc masr 2"]),
    "on-tv":         ("ONE.eg",              ["ONTV.eg", "ON.eg"],            ["on e", "on tv egypt", "ontv"]),
    "alnahar":       ("AlNaharTV.eg",        ["AlNahar.eg", "Nahar.eg"],      ["al nahar", "nahar"]),
    "alnahar-drama": ("AlNaharDrama.eg",     [],                              ["al nahar drama", "nahar drama"]),
    "cbc-egy":       ("CBC.eg",              ["CBCEgypt.eg"],                 ["cbc egypt", "cbc"]),

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
    "quran-kareem":  ("AlQuranAlKareemTV.sa",["QuranKareem.sa"],              ["quran al kareem", "quran kareem"]),
    "sunna-tv":      ("AlSunnahAlNabawiyahTV.sa", ["Sunnah.sa"],              ["sunnah", "sunna nabawiya", "al sunnah"]),

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
    """Try EXACT → ALT → FUZZY. Returns (urls, strategy_used)."""
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
def find_streams_span(html, our_id):
    """Locate the streams:[ ... ] array for a channel id using QUOTE-AWARE
    bracket scanning (so a ']' inside a URL string can never fool us).

    Returns (obj_start, open_idx, close_idx) where open_idx/close_idx point at
    the '[' and its matching ']', or None if not found / malformed.
    """
    idm = re.search(r'id\s*:\s*"' + re.escape(our_id) + r'"', html)
    if not idm:
        return None
    sidx = html.find("streams", idm.end())
    if sidx == -1:
        return None
    open_idx = html.find("[", sidx)
    if open_idx == -1:
        return None
    i = open_idx + 1
    in_str = False
    esc = False
    while i < len(html):
        c = html[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "[":
                # nested '[' outside a string shouldn't happen; bail safely
                return None
            elif c == "]":
                return (idm.start(), open_idx, i)
            elif c == "}":
                # reached end of the object before the array closed → malformed
                return None
        i += 1
    return None


def patch_html(html, our_id, urls):
    """Replace the streams:[...] for a given channel id.
    Returns (new_html, existing_count, replaced)."""
    span = find_streams_span(html, our_id)
    if not span:
        return html, 0, False
    _, open_idx, close_idx = span
    inner = html[open_idx + 1:close_idx]

    # existing URLs, keeping only safe ones (self-heals any prior garbage)
    existing = [u for u in re.findall(r'"(https?://[^"]+)"', inner) if url_is_safe(u)]

    # Merge: fresh first, existing as fallback (max 5, dedup, all safe)
    seen, combined = set(), []
    for u in (urls + existing):
        if url_is_safe(u) and u not in seen:
            seen.add(u)
            combined.append(u)
    final = combined[:MAX_STREAMS_PER_CH]
    if not final:
        # never write an empty streams array — keep the original untouched
        return html, len(existing), False

    formatted = ",\n     ".join(f'"{u}"' for u in final)
    new_inner = "\n     " + formatted + "\n   "
    new_html = html[:open_idx + 1] + new_inner + html[close_idx:]
    return new_html, len(existing), True


# ─────────────────────────────────────────────────────────────────────
def extract_ch_array(html):
    """Return the exact 'var CH = [ ... ]' text via quote-aware depth scan,
    or None if it can't be located/closed."""
    m = re.search(r'var\s+CH\s*=\s*\[', html)
    if not m:
        return None
    i = m.end() - 1  # index of the opening '['
    depth = 0
    in_str = False
    esc = False
    while i < len(html):
        c = html[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    return html[m.start():i + 1]
        i += 1
    return None


def integrity_problems(html, expected_channels):
    """Return a list of reasons the HTML looks corrupted (empty list = OK)."""
    problems = []
    if not html.rstrip().endswith("</html>"):
        problems.append("file does not end with </html> (truncated?)")
    if "</script>" not in html:
        problems.append("missing </script> (script block not closed)")
    for sig in ("AV_APPPKGNAME", "[ADS", "CACHEBUSTER", "content_livestream"):
        if sig in html:
            problems.append(f"ad-macro signature present: {sig}")
    ch = extract_ch_array(html)
    if ch is None:
        problems.append("could not locate/close 'var CH = [ ... ]'")
    else:
        if ch.count("{") != ch.count("}"):
            problems.append(f"unbalanced braces in CH array ({ch.count('{')} vs {ch.count('}')})")
        if ch.count("[") != ch.count("]"):
            problems.append(f"unbalanced brackets in CH array ({ch.count('[')} vs {ch.count(']')})")
        if ch.count('"') % 2 != 0:
            problems.append("odd number of quotes in CH array (unterminated string)")
        n = len(re.findall(r'\{\s*id\s*:\s*"', ch))
        if n != expected_channels:
            problems.append(f"channel count changed: {expected_channels} → {n}")
    return problems


def count_channels(html):
    ch = extract_ch_array(html)
    return len(re.findall(r'\{\s*id\s*:\s*"', ch if ch else html))


# ─────────────────────────────────────────────────────────────────────
def main():
    streams, channels = load_apis()
    by_id, by_title = build_indexes(streams)
    print(f"Indexed {len(by_id)} channel-IDs, {len(by_title)} unique titles\n")

    with open(HTML_FILE, encoding="utf-8") as f:
        original_html = f.read()
    html = original_html
    expected_channels = count_channels(original_html)
    print(f"Loaded {HTML_FILE} ({len(html):,} chars, {expected_channels} channels)\n")

    # Refuse to run against an already-broken file (don't make it worse)
    pre = integrity_problems(original_html, expected_channels)
    if pre:
        print("ERROR: input HTML already looks corrupted; aborting before any change:", file=sys.stderr)
        for p in pre:
            print("  - " + p, file=sys.stderr)
        sys.exit(1)

    rows = []  # (our_id, strategy, api_count, validated, existing, final_count, status)
    updated = kept = dead = 0

    for our_id in CHANNEL_MAP:
        api_urls, strategy = find_streams_for(our_id, by_id, by_title)
        api_count = len(api_urls)

        # Limit how many we take from API (take extra so HEAD-validation still leaves enough)
        api_urls = api_urls[:MAX_NEW_FROM_API * 2]

        if VALIDATE and api_urls:
            print(f"  [{our_id:<22}] HEAD-checking {len(api_urls)} URLs ({strategy})...", flush=True)
            t0 = time.time()
            api_urls = validate_urls(api_urls)
            print(f"  [{our_id:<22}] {len(api_urls)} alive in {time.time()-t0:.1f}s")

        api_urls = [u for u in api_urls if url_is_safe(u)][:MAX_NEW_FROM_API]

        # Find existing streams (always), for reporting
        span = find_streams_span(html, our_id)
        in_html = span is not None
        if in_html:
            _, oi, ci = span
            existing = [u for u in re.findall(r'"(https?://[^"]+)"', html[oi + 1:ci]) if url_is_safe(u)]
        else:
            existing = []

        if api_urls and in_html:
            new_html, _n_existing, ok = patch_html(html, our_id, api_urls)
            if ok:
                html = new_html
                final_count = min(MAX_STREAMS_PER_CH, len(set(api_urls + existing)))
                status = "UPDATED"
                updated += 1
            else:
                final_count = len(existing)
                status = "KEPT_OLD"
                kept += 1
        else:
            final_count = len(existing)
            if not in_html:
                status = "NOT_IN_HTML"
                dead += 1
            elif strategy in ("no-match", "no-config"):
                status = "KEPT_OLD" if existing else "EMPTY"
                if existing:
                    kept += 1
                else:
                    dead += 1
            else:
                status = "ALL_DEAD"   # found in API but all failed HEAD/safety
                kept += 1

        rows.append((our_id, strategy, api_count, len(api_urls), len(existing), final_count, status))
        emoji = {
            "UPDATED": "✅", "KEPT_OLD": "⚠️", "ALL_DEAD": "💀",
            "EMPTY": "❌", "NOT_IN_HTML": "🔥"
        }.get(status, "?")
        print(f"  {emoji} {our_id:<22} {status:<12} strategy={strategy:<22} api={api_count} validated={len(api_urls)} existing={len(existing)} → {final_count}")

    # ─── INTEGRITY GATE: never write/commit a corrupted or truncated file ───
    problems = integrity_problems(html, expected_channels)
    if len(html) < len(original_html) * MIN_OUTPUT_RATIO:
        problems.append(f"output shrank too much ({len(original_html):,} → {len(html):,} chars)")
    if problems:
        print("\n" + "=" * 60, file=sys.stderr)
        print("ABORT: patched HTML failed integrity checks — NOT writing.", file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        print("The live file is left exactly as it was.", file=sys.stderr)
        print("=" * 60, file=sys.stderr)
        sys.exit(1)

    # ─── Atomic write (temp + replace) so a crash can't truncate the file ───
    tmp = HTML_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, HTML_FILE)
    print(f"\nWrote {HTML_FILE} ({len(html):,} chars) — integrity OK")

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
        lines.append(f"| `{our_id}` | {status} | `{strategy}` | {api_c} | {val_c} | {exist_c} | {final_c} |")
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
        "1. `exact:<id>` — direct iptv-org channel-ID match",
        "2. `alt:<id>` — alternate ID match (configured fallback)",
        "3. `fuzzy-title` — title keyword search (catches ID typos & moved channels)",
        "4. `no-match` — no streams found by any strategy",
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
