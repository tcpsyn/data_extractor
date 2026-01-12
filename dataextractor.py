#!/usr/bin/env python3
# serpapi_mt_with_email_distance_socials.py
# pip install google-search-results requests beautifulsoup4 tldextract

from serpapi import GoogleSearch
import time, csv, hashlib, re, math, urllib.parse, json
import requests, os, yaml
from bs4 import BeautifulSoup
from pathlib import Path
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")  # loads SERPAPI_KEY

SERPAPI_KEY = os.getenv("SERPAPI_KEY")
if not SERPAPI_KEY:
    raise RuntimeError("Missing SERPAPI_KEY. Put it in .env")

with open(BASE_DIR / "config.yaml", "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

keywords: list[str] = cfg.get("keywords", [])
towns: list[str] = cfg.get("towns", [])
towns = list(dict.fromkeys(towns))

# optional sanity checks
if not keywords or not towns:
    raise RuntimeError("config.yaml missing keywords or towns")

print(f"Loaded {len(keywords)} keywords and {len(towns)} towns.")

import tldextract


# Politeness / throttling
RATE_LIMIT_SLEEP = 1.5        # between SerpAPI queries
WEB_SLEEP = 1.0               # between website fetches
REQUESTS_TIMEOUT = 10        # seconds for website requests

OUTPUT_CSV = "serpapi_mt_leads_with_email_distance_socials.csv"

# Fort Smith coordinates (confirmed): use these as origin for distances
FT_SMITH_LAT = 45.3127372
FT_SMITH_LNG = -107.937055
# ============================

EMAIL_REGEX = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", re.I)

HEADERS = {
    "User-Agent": "serpapi-lead-scraper/1.0 (+https://yourdomain.example)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}

SOCIAL_PATTERNS = {
    "facebook": re.compile(r"(?:https?://)?(?:www\.)?facebook\.com/([^/?#]+)", re.I),
    "twitter": re.compile(r"(?:https?://)?(?:www\.)?(?:twitter|x)\.com/([^/?#]+)", re.I),
    "instagram": re.compile(r"(?:https?://)?(?:www\.)?instagram\.com/([^/?#]+)", re.I),
    "linkedin": re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/(?:company|in|school)/([^/?#]+)", re.I),
    "youtube": re.compile(r"(?:https?://)?(?:www\.)?youtube\.com/(?:channel|user|c)/([^/?#]+)", re.I),
    "tiktok": re.compile(r"(?:https?://)?(?:www\.)?tiktok\.com/@?([^/?#]+)", re.I)
}

def haversine_miles(lat1, lon1, lat2, lon2):
    R = 3958.8  # Earth radius in miles
    phi1 = math.radians(lat1); phi2 = math.radians(lat2)
    dphi = math.radians(lat2 - lat1); dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*(math.sin(dlambda/2)**2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    return R * c

def safe_get(dct, *keys):
    for k in keys:
        if isinstance(dct, dict) and k in dct:
            dct = dct[k]
        else:
            return None
    return dct

def make_row_from_result(r):
    title = r.get("title") or r.get("name")
    address = r.get("address") or safe_get(r, "address", "street") or r.get("vicinity")
    phone = r.get("phone") or r.get("formatted_phone_number") or r.get("telephone")
    website = r.get("website") or r.get("url")
    place_id = r.get("place_id") or r.get("place_id_from_url")
    lat = safe_get(r, "gps", "lat") or safe_get(r, "coordinates", "latitude") or None
    lng = safe_get(r, "gps", "lng") or safe_get(r, "coordinates", "longitude") or None
    rating = r.get("rating")
    snippet = r.get("snippet") or r.get("description") or ""
    maps_link = r.get("link") or r.get("maps_link") or (f"https://www.google.com/maps/place/?q=place_id:{place_id}" if place_id else None)

    return {
        "place_id": place_id,
        "title": title,
        "address": address,
        "phone": phone,
        "website": website,
        "lat": lat,
        "lng": lng,
        "rating": rating,
        "snippet": snippet,
        "maps_link": maps_link,
        "contact_email": None,
        "miles_from_fort_smith": None,
        # social fields
        "facebook": None,
        "twitter": None,
        "instagram": None,
        "linkedin": None,
        "youtube": None,
        "tiktok": None
    }

def unique_key_for_row(row):
    if row.get("place_id"):
        return row["place_id"]
    key = f"{row.get('title','')}-{row.get('address','')}".strip().lower()
    return hashlib.sha1(key.encode("utf-8")).hexdigest()

def query_serpapi(keyword, town, api_key):
    params = {
        "engine": "google_maps",
        "q": f"{keyword} near {town}",
        "api_key": api_key,
    }
    search = GoogleSearch(params)
    return search.get_dict()

def extract_local_results(resp):
    candidates = []
    for k in ("local_results", "results", "places", "organic_results"):
        val = resp.get(k)
        if isinstance(val, list) and val:
            candidates = val
            break
    if not candidates:
        local_map = resp.get("local_map") or resp.get("local_results")
        if isinstance(local_map, dict) and "places" in local_map and isinstance(local_map["places"], list):
            candidates = local_map["places"]
    return candidates

def fetch_place_details(place_id, api_key):
    params = {
        "engine": "google_maps",
        "place_id": place_id,
        "api_key": api_key
    }
    s = GoogleSearch(params)
    return s.get_dict()

def find_emails_on_page(url):
    """Fetch a page, return list of emails (unique) found on page (including mailto:)."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUESTS_TIMEOUT, allow_redirects=True)
    except Exception:
        return []
    if resp.status_code != 200 or not resp.text:
        return []
    text = resp.text
    emails = set(re.findall(EMAIL_REGEX, text))
    # add mailto links extracted from DOM
    try:
        soup = BeautifulSoup(text, "html.parser")
        for a in soup.select("a[href^=mailto]"):
            href = a.get("href", "")
            m = re.findall(EMAIL_REGEX, href)
            for e in m:
                emails.add(e)
    except Exception:
        pass
    return list(emails)

def extract_socials_from_soup(soup, base_url=None):
    """Return dict of first-matching social links found in anchors or JSON-LD."""
    found = {k: None for k in SOCIAL_PATTERNS.keys()}

    # 1) anchors
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        # normalize protocol-relative links
        if href.startswith("//"):
            href = "https:" + href
        # if relative and base_url provided, make absolute
        if base_url and href.startswith("/"):
            href = urllib.parse.urljoin(base_url, href)
        for network, patt in SOCIAL_PATTERNS.items():
            if not found[network]:
                m = patt.search(href)
                if m:
                    # prefer full URL in output
                    found[network] = href
    # 2) JSON-LD (structured data)
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "{}")
        except Exception:
            continue
        # data can be list or dict
        items = data if isinstance(data, list) else [data]
        for item in items:
            # look for sameAs or sameAs list
            same = item.get("sameAs") or item.get("same_as") or []
            if isinstance(same, str):
                same = [same]
            for s in same:
                for network, patt in SOCIAL_PATTERNS.items():
                    if not found[network]:
                        if patt.search(s):
                            found[network] = s
    return found

def pick_most_likely_email_and_socials(homepage_url):
    """Heuristic: check homepage then contact/about pages. Returns (email, socials_dict)."""
    if not homepage_url:
        return None, {k: None for k in SOCIAL_PATTERNS.keys()}
    parsed = urllib.parse.urlparse(homepage_url)
    if not parsed.scheme:
        homepage_url = "http://" + homepage_url
        parsed = urllib.parse.urlparse(homepage_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    # try homepage
    try:
        resp = requests.get(homepage_url, headers=HEADERS, timeout=REQUESTS_TIMEOUT, allow_redirects=True)
    except Exception:
        resp = None

    if resp and resp.status_code == 200 and resp.text:
        emails = set(re.findall(EMAIL_REGEX, resp.text))
        try:
            soup = BeautifulSoup(resp.text, "html.parser")
            for a in soup.select("a[href^=mailto]"):
                href = a.get("href", "")
                m = re.findall(EMAIL_REGEX, href)
                for e in m:
                    emails.add(e)
            socials = extract_socials_from_soup(soup, base_url=base)
            if emails:
                return list(emails)[0], socials
            # if socials found even without email, return socials with None email
            if any(socials.values()):
                return None, socials
        except Exception:
            pass

    time.sleep(WEB_SLEEP)

    # try common contact/about paths
    for path in ("/contact", "/contact-us", "/about", "/about-us", "/get-in-touch", "/wp-json"):
        url = base + path
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUESTS_TIMEOUT, allow_redirects=True)
        except Exception:
            resp = None
        if resp and resp.status_code == 200 and resp.text:
            emails = set(re.findall(EMAIL_REGEX, resp.text))
            try:
                soup = BeautifulSoup(resp.text, "html.parser")
                for a in soup.select("a[href^=mailto]"):
                    href = a.get("href", "")
                    m = re.findall(EMAIL_REGEX, href)
                    for e in m:
                        emails.add(e)
                socials = extract_socials_from_soup(soup, base_url=base)
                if emails:
                    return list(emails)[0], socials
                if any(socials.values()):
                    return None, socials
            except Exception:
                pass
        time.sleep(WEB_SLEEP)

    # nothing found
    return None, {k: None for k in SOCIAL_PATTERNS.keys()}

def main():
    seen = {}
    rows = []

    total_queries = len(keywords) * len(towns)
    print(f"Running approximately {total_queries} queries (keywords × towns).")

    for kw in keywords:
        for town in towns:
            print(f"Searching: '{kw}' near {town} ...")
            try:
                resp = query_serpapi(kw, town, SERPAPI_KEY)
            except Exception as e:
                print("SerpAPI request failed:", e)
                time.sleep(RATE_LIMIT_SLEEP)
                continue

            results = extract_local_results(resp)
            if not results:
                print("  -> no results found for this query.")
            for r in results:
                row = make_row_from_result(r)
                key = unique_key_for_row(row)
                if key in seen:
                    existing = seen[key]
                    for field in row:
                        if not existing.get(field) and row.get(field):
                            existing[field] = row[field]
                else:
                    seen[key] = row
                    rows.append(row)

                # Fill missing lat/lng and website via a place details request if available
                if row.get("place_id") and (not row.get("lat") or not row.get("lng") or not row.get("website")):
                    try:
                        time.sleep(RATE_LIMIT_SLEEP)
                        detail_resp = fetch_place_details(row["place_id"], SERPAPI_KEY)
                        detail_places = extract_local_results(detail_resp)
                        if detail_places:
                            detail_obj = detail_places[0]
                            for f in ("phone", "website", "address", "rating"):
                                value = detail_obj.get(f)
                                if value and not seen[key].get(f):
                                    seen[key][f] = value
                            # gps fields
                            lat = safe_get(detail_obj, "gps", "lat") or safe_get(detail_obj, "coordinates", "latitude")
                            lng = safe_get(detail_obj, "gps", "lng") or safe_get(detail_obj, "coordinates", "longitude")
                            if lat and not seen[key].get("lat"):
                                seen[key]["lat"] = lat
                            if lng and not seen[key].get("lng"):
                                seen[key]["lng"] = lng
                    except Exception as e:
                        print("  -> detail fetch failed:", e)

            time.sleep(RATE_LIMIT_SLEEP)

    # Post-process each row: compute distance
    for r in rows:
        lat = r.get("lat"); lng = r.get("lng")
        miles = None
        if lat and lng:
            try:
                lat_f = float(lat)
                lng_f = float(lng)
                miles = haversine_miles(FT_SMITH_LAT, FT_SMITH_LNG, lat_f, lng_f)
                miles = round(miles, 2)
            except Exception:
                miles = None
        r["miles_from_fort_smith"] = miles

    # Extract emails and socials
    for r in rows:
        if r.get("contact_email") and any(r.get(s) for s in SOCIAL_PATTERNS.keys()):
            continue  # already has email and socials
        homepage = r.get("website") or r.get("maps_link")
        if homepage:
            try:
                email, socials = pick_most_likely_email_and_socials(homepage)
                if email:
                    r["contact_email"] = email
                # merge socials into row only if empty
                for k, v in socials.items():
                    if v and not r.get(k):
                        r[k] = v
            except Exception as e:
                print("  -> website email/social fetch failed for", homepage, ":", e)
        # fallback: look for email in snippet
        if not r.get("contact_email"):
            m = re.findall(EMAIL_REGEX, (r.get("snippet") or ""))
            if m:
                r["contact_email"] = m[0]
        time.sleep(WEB_SLEEP)

    # write CSV
    fieldnames = [
        "place_id","title","address","phone","website","lat","lng","rating","maps_link",
        "contact_email","miles_from_fort_smith",
        "facebook","twitter","instagram","linkedin","youtube","tiktok",
        "snippet"
    ]
    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            out = {k: r.get(k) for k in fieldnames}
            writer.writerow(out)

    print(f"Saved {len(rows)} unique rows to {OUTPUT_CSV}")

if __name__ == "__main__":
    main()
