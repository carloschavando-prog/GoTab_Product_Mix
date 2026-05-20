# On Par Entertainment — GoTab + Tripleseat Data Pipeline

Two automated nightly pipelines that pull revenue data from GoTab (POS) and Tripleseat (events) into a shared Supabase database. Both run via Vercel Cron on the Pro plan.

---

## Pipelines

| Pipeline | Source | Schedule (UTC) | Schedule (ET) |
|---|---|---|---|
| GoTab daily sales | GoTab GraphQL API | `0 9 * * *` | 5:00 AM |
| Tripleseat event sync | Tripleseat REST API + BEO docs | `0 11 * * *` | 7:00 AM |

---

## Project Structure

```
GoTab_Product_Mix/
  api/
    daily_fetch.py        # GoTab: ledger entries → sales table
    tripleseat_fetch.py   # Tripleseat: bookings + events + leads → ts_* tables
    beo_parser.py         # Reads Tripleseat BEO documents to split food/bev/events
  tripleseat_schema.sql   # Run once in Supabase SQL Editor to create ts_* tables
  vercel.json             # Both cron schedules
  pyproject.toml
  requirements.txt
  README.md
```

---

## GoTab Pipeline

### What It Does
1. Triggers at 9:00 AM UTC (5 AM ET) every day
2. Fetches all ledger entries for yesterday from the GoTab GraphQL API
3. Aggregates into a product mix summary (gross/net qty, gross/net sales, refunds, comps, voids)
4. Writes to Supabase (`report_dates` + `sales` tables)
5. Skips silently if that date is already loaded

### Supabase Schema

```sql
CREATE TABLE IF NOT EXISTS report_dates (
    id          BIGSERIAL PRIMARY KEY,
    report_date DATE NOT NULL UNIQUE,
    filename    TEXT
);

CREATE TABLE IF NOT EXISTS sales (
    id               BIGSERIAL PRIMARY KEY,
    report_date_id   BIGINT REFERENCES report_dates(id),
    report_date      DATE NOT NULL,
    category         TEXT,
    product          TEXT NOT NULL,
    zone             TEXT,
    gross_qty        NUMERIC,
    net_qty          NUMERIC,
    gross_sales      NUMERIC,
    net_sales        NUMERIC,
    refund_qty       NUMERIC,
    refund_amount    NUMERIC,
    comp_qty         NUMERIC,
    comp_amount      NUMERIC,
    void_qty         NUMERIC,
    void_amount      NUMERIC,
    is_discount      BOOLEAN DEFAULT FALSE
);
```

### Notes
- Only `NET_SALES` accounting stream entries are included — taxes, tips, autograt, deferred revenue, and processor entries are excluded
- GoTab `DEFERRED_REVENUE` entries are Tripleseat event deposits — excluded here, tracked in the Tripleseat pipeline
- Discounts (negative net sales or "Discount" in product name) are flagged `is_discount = TRUE`

---

## Tripleseat Pipeline

### What It Does
1. Triggers at 11:00 AM UTC (7 AM ET) every day
2. Gets a fresh OAuth2 token via `client_credentials` (2-hour expiry)
3. Paginates through all bookings, events, and leads (~1,600 records each)
4. For each event, fetches the Banquet Event Order (BEO) portal document to compute the food/beverage/events revenue split
5. Upserts everything into Supabase (`ts_bookings`, `ts_events`, `ts_leads`)

### Revenue Split Logic (`beo_parser.py`)

The BEO parser determines the food/beverage breakdown using three strategies (tried in order):

| Method | Trigger | Example |
|---|---|---|
| `api_split` | Tripleseat already tracks beverage as a separate line item | "The Tap - Beverage Only" sold separately |
| `desc_card` | Drink card dollar amount found in the combo package description | "Includes a $20 preloaded drink card" |
| `bev_section_card` | Drink card amount in BEVERAGES section as "included in package" | "@$15 each, included in package" |
| `unsplit` | No drink card amount determinable | Package description has no card amount |
| `no_food_bev` | No food or beverage in category totals | Games-only event |

`events_amount` captures booking fees and extra-hour charges, which are separate from food, beverage, and game revenue.

### Supabase Schema

Run `tripleseat_schema.sql` once in the Supabase SQL Editor. Creates:

**`ts_bookings`** — booking-level records (name, status, dates, contact, financials)

**`ts_events`** — event-level records including:
- `food_amount`, `beverage_amount` — split from BEO document
- `events_amount` — booking fees + extra hours
- `split_method` — how food/bev was determined (for auditability)
- `actual_amount`, `grand_total`, `amount_due` — full financial totals

**`ts_leads`** — lead/inquiry records (contact info, event date, lead source, conversion status)

### Authentication
Tripleseat uses OAuth2 `client_credentials` grant. A new token is fetched at the start of each sync run — no refresh token storage needed.

---

## Environment Variables

Set in Vercel → Project Settings → Environment Variables:

| Variable | Pipeline | Description |
|---|---|---|
| `GOTAB_API_ACCESS_ID` | GoTab | GoTab API access ID |
| `GOTAB_API_ACCESS_SECRET` | GoTab | GoTab API access secret |
| `GOTAB_LOCATION_ID` | GoTab | GoTab location ID (defaults to `112479`) |
| `TS_CLIENT_ID` | Tripleseat | Tripleseat OAuth2 application UID |
| `TS_CLIENT_SECRET` | Tripleseat | Tripleseat OAuth2 application secret |
| `SUPABASE_URL` | Both | Supabase project URL (`https://xxx.supabase.co`) |
| `SUPABASE_SERVICE_KEY` | Both | Supabase service role key (`sb_secret_...`) |
| `CRON_SECRET` | Both | Random secret to secure cron endpoints |

---

## Manual Trigger

```bash
# GoTab
curl -s -H "Authorization: Bearer YOUR_CRON_SECRET" \
  https://go-tab-product-mix.vercel.app/api/daily_fetch

# Tripleseat
curl -s -H "Authorization: Bearer YOUR_CRON_SECRET" \
  https://go-tab-product-mix.vercel.app/api/tripleseat_fetch
```

---

## Deployment

1. Push to GitHub (this repo)
2. Connect to Vercel → Import repo → Add all env vars above → Deploy
3. Verify both cron jobs appear in Vercel → **Cron Jobs** tab
4. Run `tripleseat_schema.sql` once in the Supabase SQL Editor to create the `ts_*` tables
