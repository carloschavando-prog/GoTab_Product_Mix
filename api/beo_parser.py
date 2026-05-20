"""
Tripleseat Banquet Event Order (BEO) HTML parser.

Fetches the publicly-accessible portal document for a Tripleseat event and
extracts a food/beverage dollar split from the line items.

Three split strategies (tried in order):
  1. api_split        — category_totals already has a "Beverage" row; no fetch needed.
  2. desc_card        — Food+Bev combo package description contains "$X preloaded drink card".
  3. bev_section_card — BEVERAGES section lists "@$X each, included in package".

If none apply, the full food total is returned as food with $0 beverage and
split_method="unsplit" so the record can be reviewed manually.
"""

import re
import urllib.request


# ---------------------------------------------------------------------------
# Regex constants
# ---------------------------------------------------------------------------

# Detect activity/game line items — these are not food or beverage
_GAME_RE = re.compile(
    r'\b(bowling|darts?|mini\s*golf|neo\s*shuffleboard|shuffleboard|'
    r'pool\s*table|billiards|golf\s*per\s*person)\b',
    re.I
)

# Detect explicitly beverage-only line items
_BEV_ONLY_RE = re.compile(
    r'\b(beverage\s+only|the\s+tap|bar\s+tab|open\s+bar|'
    r'individual\s+drink|master\s+drink|pre.?loaded\s+card|drink\s+package)\b',
    re.I
)

# Detect food+beverage combo packages
_FOOD_BEV_COMBO_RE = re.compile(
    r'\b(food\s*\+\s*beverage|food\s+and\s+beverage|food\s*\+\s*bev)\b',
    re.I
)

# Extract a per-person drink card $ amount from a description string.
# Handles: "$20 preloaded drink card", "$15 drink card", "a $20 pre-loaded card"
_CARD_IN_DESC_RE = re.compile(
    r'\$\s*(\d+(?:\.\d+)?)\s+(?:pre.?loaded|preloaded|drink\s+card)'
    r'|(?:pre.?loaded|drink\s+card)[^.]{0,60}\$\s*(\d+(?:\.\d+)?)',
    re.I
)

# Detect "included in package" card notes in the BEVERAGES section.
# e.g. "30 Pre-loaded cards @$15 each, included in package"
_INCLUDED_CARD_RE = re.compile(
    r'(\d+)\s+[^@\n]{0,60}@\s*\$\s*(\d+(?:\.\d+)?)\s+each[^,\n]*,?\s*'
    r'included\s+in\s+package',
    re.I
)

# A BEO line item row: integer qty  +  description text  +  $unit  +  $total
_LINE_ITEM_RE = re.compile(
    r'(\d+)\s+(.{5,300}?)\s+\$\s*(\d[\d,]*\.?\d*)\s+\$\s*(\d[\d,]*\.?\d*)'
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch_html(url: str) -> str:
    req = urllib.request.Request(url, headers={"Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read().decode(errors="replace")
    raw = re.sub(r'<script[^>]*>.*?</script>', ' ', raw, flags=re.S)
    raw = re.sub(r'<style[^>]*>.*?</style>', ' ', raw, flags=re.S)
    return raw


def _html_to_text(html: str) -> str:
    text = re.sub(r'<[^>]+>', ' ', html)
    return re.sub(r'\s{2,}', ' ', text)


_COL_HEADER_RE = re.compile(
    # Section name must be ALL-CAPS (e.g. BEVERAGES, ADDITIONAL CHARGES).
    # No re.I so that lowercase mid-sentence words like "included in package"
    # are not mistaken for section headers.
    r'([A-Z][A-Z &]*)?\s*Qty\.?\s+Qty\s+Description\s+Price\s+Total'
)


def _split_beo_sections(text: str) -> dict:
    """
    Split BEO text into named sections by detecting the repeating column-header
    'Qty. Qty Description Price Total'.  Each occurrence is preceded by the
    uppercase section name (BEVERAGES, ADDITIONAL CHARGES, …) or nothing (→ FOOD).

    Returns a dict keyed by section name (uppercase).
    """
    matches = list(_COL_HEADER_RE.finditer(text))
    sections = {}
    for i, m in enumerate(matches):
        name = (m.group(1) or "FOOD").strip().upper()
        # Strip trailing noise from multi-word names (e.g. "2 BEVERAGES" → "BEVERAGES")
        name = re.sub(r'^\d+\s+', '', name)
        body_start = m.end()
        body_end   = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[name] = text[body_start:body_end].strip()
    return sections


def _parse_line_items(section: str) -> list:
    rows = []
    for qty_s, desc, unit_s, total_s in _LINE_ITEM_RE.findall(section):
        rows.append({
            "qty":   int(qty_s),
            "desc":  desc.strip(),
            "unit":  float(unit_s.replace(",", "")),
            "total": float(total_s.replace(",", "")),
        })
    return rows


def _card_from_desc(desc: str):
    """Return the drink-card per-person $ amount hidden in a line-item description, or None."""
    m = _CARD_IN_DESC_RE.search(desc)
    if not m:
        return None
    val = m.group(1) or m.group(2)
    return float(val) if val else None


def _included_card_from_bev_section(bev_section: str):
    """
    Return (qty, per_person_amount) for 'included in package' drink card notes,
    or (None, None) if not found.
    """
    m = _INCLUDED_CARD_RE.search(bev_section)
    if m:
        return int(m.group(1)), float(m.group(2))
    return None, None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_food_bev(event: dict) -> tuple:
    """
    Derive (food_amount, beverage_amount, split_method) for a Tripleseat event.

    split_method values:
      api_split        — beverage already a separate category in Tripleseat
      desc_card        — drink card $ found in line-item description
      bev_section_card — drink card $ found in BEVERAGES 'included in package' note
      no_food_bev      — no food or beverage in category_totals
      no_doc           — food present but event has no BEO document
      unsplit          — BEO parsed but drink card amount not determinable
      doc_error        — BEO fetch/parse failed; food total returned as-is
    """
    category_totals = event.get("category_totals") or []
    cats = {c["name"].lower(): float(c["total"]) for c in category_totals}

    food_total = sum(v for k, v in cats.items() if "food" in k)
    bev_total  = sum(v for k, v in cats.items()
                     if "beverage" in k or k.startswith("bar") or "drink" in k)

    # --- Strategy 1: API already split food and beverage ---
    if bev_total > 0:
        return round(food_total, 2), round(bev_total, 2), "api_split"

    if food_total == 0:
        return 0.0, 0.0, "no_food_bev"

    # --- Need to read the BEO document ---
    beo_url = None
    for doc in (event.get("documents") or []):
        for view in (doc.get("views") or []):
            if view.get("url"):
                beo_url = view["url"]
                break
        if beo_url:
            break

    if not beo_url:
        return round(food_total, 2), 0.0, "no_doc"

    try:
        html = _fetch_html(beo_url)
        text = _html_to_text(html)

        sections = _split_beo_sections(text)

        # The food line items live in the first section (keyed "FOOD" or similar)
        food_section = sections.get("FOOD", "")

        # The BEVERAGES section may or may not exist
        bev_section = sections.get("BEVERAGES", "")

        # --- Strategy 2: drink card $ amount stated inside a combo package description ---
        if food_section:
            embedded_bev = 0.0
            for item in _parse_line_items(food_section):
                if _GAME_RE.search(item["desc"]):
                    continue  # activity, skip
                if _FOOD_BEV_COMBO_RE.search(item["desc"]):
                    card = _card_from_desc(item["desc"])
                    if card:
                        embedded_bev += item["qty"] * card
            if embedded_bev > 0:
                return (
                    round(food_total - embedded_bev, 2),
                    round(embedded_bev, 2),
                    "desc_card",
                )

        # --- Strategy 3: drink card $ stated in BEVERAGES section as "included in package" ---
        if bev_section:
            inc_qty, inc_card = _included_card_from_bev_section(bev_section)
            if inc_qty and inc_card:
                embedded_bev = round(inc_qty * inc_card, 2)
                return (
                    round(food_total - embedded_bev, 2),
                    embedded_bev,
                    "bev_section_card",
                )

        # Could not determine drink card amount
        return round(food_total, 2), 0.0, "unsplit"

    except Exception:
        return round(food_total, 2), 0.0, "doc_error"
