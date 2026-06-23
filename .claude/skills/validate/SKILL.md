---
name: validate
description: Validate scraped business leads from the OpenCraft Supabase database. FIRST refreshes OpenCraft's brand knowledge graph from the public showcase (https://ocraft.id/en/showcase), THEN pulls pending leads from the `scraper_leads` table via the Supabase MCP, verifies each business with the Claude-in-Chrome browser extension (is the website live? is the business real and active? what market is it in?), and writes back a validation status, a marketing angle, and a ready-to-send cold message IN BAHASA INDONESIA that SENDS THE ACTIVE LIVE LINK of the most relevant OpenCraft showcase project (e.g. a Korean-market lead gets Kiyoo's live link, a skincare brand gets VERDA's live link) so the prospect can click and see a real example. Use when the user types /validate, optionally with a category, location, or count, e.g. "/validate", "/validate korean-market in Jakarta", "/validate 10 pending leads", "/validate otomotif".
---

# /validate — refresh brand knowledge → verify leads → showcase-driven outreach

The flow, in order:

1. **Brand knowledge first.** Pull OpenCraft's live showcase from `https://ocraft.id/en/showcase` and refresh the **brand knowledge graph** (Supabase `brand_knowledge_nodes` / `brand_knowledge_edges`, brand `opencraft`) so the showcase projects + the markets they serve are current and loaded into context.
2. **Then analyse the lead's website** with the Claude-in-Chrome extension — confirm it's a real, active business and detect **which market/segment** it's in.
3. **Then write the cold message** so the pitch is grounded in a real OpenCraft showcase project that matches the lead's market, and **send the project's active live link** so the prospect can click straight through to a real example — e.g. a **Korean / K-pop** lead gets **Kiyoo**'s live link, a **skincare / kecantikan** brand gets **VERDA**'s live link.

Source leads come from the project `/scrape` pushed into Supabase (`central-apps`, ref **`wdzmuniyqqyngzckeoph`**, table `scraper_leads`). Each validated row is enriched with:

- `validation_status` — `valid` / `invalid` / `needs_review` (was `pending`)
- `marketing_angle` — **the marketing field**: the tailored selling angle, anchored to the matched showcase project
- `outreach_message` — a ready-to-send cold message **in Bahasa Indonesia**, personalised per business, that **includes the active live link of the matched showcase project** (a clickable `https://…` URL) so the prospect can open a real example

These columns already exist on `scraper_leads` (added by migration `add_validation_and_marketing_columns_to_scraper_leads`): `validation_status` (default `pending`), `validation_notes`, `outreach_message`, `marketing_angle`, `validated_at`.

## Step 0 — Refresh OpenCraft brand knowledge from the showcase (do this FIRST)

Before touching any lead, make sure the brand knowledge graph reflects the current showcase. This is what lets the cold message propose a *real* OpenCraft project instead of a generic pitch.

1. **Fetch the showcase.** Read `https://ocraft.id/en/showcase` (WebFetch). For every project listed, extract: project **name**, what it is (1-line), its **live URL**, the **market/segment** it serves, and the **deliverables** (what OpenCraft built). Capture the **exact live URL the showcase links to** — this is the link the cold message will send, so use the real working URL (it may be a `*.vercel.app` deploy rather than a custom domain). **Verify each live URL is actually reachable** before storing it: `navigate` to it in the `PC Gaming Raka` browser (or WebFetch it) and confirm it loads a real page (not 404/parked/expired). If a project's live link is dead, mark it so and **do not** send that link in any message — fall back to a relevant service pitch instead. Only links confirmed active in this step may be put into an `outreach_message`.
2. **Upsert into the brand knowledge graph** in Supabase (`mcp__supabase__execute_sql`, `project_id: "wdzmuniyqqyngzckeoph"`, brand_slug `opencraft`). Each showcase project is a `studi_kasus` node; the market it serves is a `pasar` node; link them with a `melayani_pasar` edge. There is **no unique constraint** on `(brand_slug, label)`, so guard every insert with `where not exists (...)` to stay idempotent (re-running must not duplicate). Store the live URL + segment in `props`:

   ```sql
   -- market node
   insert into brand_knowledge_nodes (brand_slug, type, label, description, props)
   select 'opencraft','pasar','<Market label>','<desc>','{"segment":"<slug>"}'::jsonb
   where not exists (select 1 from brand_knowledge_nodes
                     where brand_slug='opencraft' and label='<Market label>');

   -- case-study node — live_url must be the EXACT active link verified reachable in step 1;
   -- link_active records whether that link loaded (only active links go into outreach messages)
   insert into brand_knowledge_nodes (brand_slug, type, label, description, props)
   select 'opencraft','studi_kasus','<Project> — <one-liner>','<desc>',
     '{"live_url":"https://...","link_active":true,"segment":"<slug>","propose_for":["<slug>"],"deliverables":["..."]}'::jsonb
   where not exists (select 1 from brand_knowledge_nodes
                     where brand_slug='opencraft' and label='<Project> — <one-liner>');

   -- edge: case study serves market (resolve ids by label, idempotent)
   insert into brand_knowledge_edges (brand_slug, source_id, target_id, relation)
   select 'opencraft', s.id, t.id, 'melayani_pasar'
   from brand_knowledge_nodes s, brand_knowledge_nodes t
   where s.brand_slug='opencraft' and s.label='<Project> — <one-liner>'
     and t.brand_slug='opencraft' and t.label='<Market label>'
     and not exists (select 1 from brand_knowledge_edges x
                     where x.brand_slug='opencraft' and x.source_id=s.id
                       and x.target_id=t.id and x.relation='melayani_pasar');
   ```

   Map the showcase segment to one of the 6 scrape buckets where it fits (`kecantikan`, `wisata`, `otomotif`, `akomodasi`, `kesehatan`, `korean-market`) so it lines up with how leads are categorised. As of last refresh the showcase contained: **Kiyoo** (kiyoo.id — pre-order website for K-pop merch → `korean-market`), **VERDA** (D2C skincare landing page → `kecantikan`) and **VELOCE Motors** (automotive-landing.vercel.app — performance-auto landing page → `otomotif`). Add any new projects you find; don't assume this list is complete.

   **Reconcile the live link on existing nodes.** Because the inserts are guarded with `where not exists`, a project already in the graph keeps its old `props` — so its stored `live_url` can drift from the link the showcase currently points to (e.g. a custom domain in the graph vs. a `*.vercel.app` link on the showcase). After the inserts, for every showcase project **update the stored link to the exact active URL you verified in step 1** so the message sends the link that actually works:

   ```sql
   update brand_knowledge_nodes
   set props = props || '{"live_url":"<verified active url>","link_active":true}'::jsonb
   where brand_slug='opencraft' and type='studi_kasus' and label='<Project> — <one-liner>'
     and props->>'live_url' is distinct from '<verified active url>';
   ```
3. **Load the showcase map into context.** Read back the current case studies + their markets so Steps 4–5 can match a lead to a project:

   ```sql
   select n.label, n.props->>'live_url' as live_url,
          n.props->>'link_active' as link_active, n.props->>'segment' as segment,
          n.description
   from brand_knowledge_nodes n
   where n.brand_slug='opencraft' and n.type='studi_kasus'
   order by n.label;
   ```

   Keep this `segment → showcase project → active live link` table in mind — it is the lookup the cold message uses, and **the `live_url` is the exact link you will paste into each message** (only send links where `link_active` is true).

> The same graph is browsable in the dashboard's **Knowledge Graph** view and via the `brand-knowledge` MCP (`search_nodes`, `get_graph`, `upsert_node`, `add_edge`) — using raw SQL here keeps the skill self-contained, but the MCP tools are an equivalent way to read/curate it.

## Step 1 — Parse the request

From the user's arguments, extract (all optional):

- **count** — how many leads to validate this run. Default `10`. Validating drives a real browser per lead, so keep batches small unless asked.
- **category** — one of the 6 scrape buckets to filter on: `kecantikan`, `wisata`, `otomotif`, `akomodasi`, `kesehatan`, `korean-market`.
- **location** — city/area filter (matches the `location` column).
- **status filter** — default is leads where `validation_status = 'pending'`. If the user says "re-validate" or names a status, filter on that instead.

Examples:
- `/validate` → 10 pending leads, any category.
- `/validate kecantikan in Jakarta` → up to 10 pending `kecantikan` leads in Jakarta.
- `/validate 25 otomotif` → up to 25 pending `otomotif` leads.

## Step 2 — Pull the candidate leads from Supabase

Use `mcp__supabase__execute_sql` with `project_id: "wdzmuniyqqyngzckeoph"`. Select only what you need to verify and message:

```sql
select id, category, business_name, phone, website, email, address,
       rating, reviews, maps_url, query, location, validation_status
from scraper_leads
where validation_status = 'pending'          -- or the requested status
  -- and category = '<category>'             -- add if a category was given
  -- and location ilike '%<location>%'       -- add if a location was given
order by reviews desc nulls last, rating desc nulls last
limit <COUNT>;
```

Order by `reviews`/`rating` so the most established (best-fit) businesses get validated first. If zero rows come back, tell the user there's nothing pending matching the filter and stop.

## Step 3 — Identify / verify each business with the browser extension

This is the core step. Per the project rules, **always target the `PC Gaming Raka` browser** (deviceId `5c446d2c-c073-486b-bf11-24e5341890cb`):

1. `select_browser` with that deviceId, then `tabs_context_mcp`, then create a fresh tab with `tabs_create_mcp`. (Load these tools in one `ToolSearch` call if deferred — see the claude-in-chrome instructions.)
2. For each lead, verify it is a **real, active, reachable** business. Use the cheapest signal that settles it:
   - **Has a website** → `navigate` to it. Confirm it loads (not parked/404/expired), looks like the same business, and scrape any better contact (email, WhatsApp, contact page). `read_page` / `get_page_text` to read it.
   - **No website** → open the `maps_url` (or search `"<business_name> <location>"` on Google Maps) to confirm the listing exists, is not permanently closed, and note hours/recent reviews.
   - Cross-check that `phone` / `email` look plausible and belong to the business.
3. Decide a **validation_status** from what you see:
   - `valid` — business is real, active, and reachable (a usable outreach target).
   - `invalid` — permanently closed, dead/parked domain, duplicate, or clearly not a real prospect. Note why.
   - `needs_review` — ambiguous (site down today, no contact found, can't confirm it's the same business). Note what's missing.
4. Keep a one-line `validation_notes` per lead recording what you found (e.g. `website live, found email on /kontak`, `domain parked`, `permanently closed on Maps`).
5. **Detect the lead's market / segment** — this drives the showcase match in Step 4. Use the lead's `category`, what the site/listing sells, and language/audience cues. Examples: a K-pop merch / idol-goods / album pre-order store → **`korean-market`**; a skincare / beauty brand → **`kecantikan`**; a workshop/bengkel → **`otomotif`**, etc. Note it (e.g. `segment: korean-market — jual album & photocard K-pop`).

Stay focused — if a page won't load or the extension errors after 2–3 tries, mark the lead `needs_review` with a note and move on. Don't rabbit-hole.

## Step 4 — Match a showcase project, then derive the marketing angle

This is where the showcase comes in. For each `valid` lead:

1. **Match the lead's segment (from Step 3.5) to a showcase project** loaded in Step 0. Pick the case study whose `segment` / `propose_for` matches, e.g.:

   | Lead segment | Showcase project to propose | Why |
   |---|---|---|
   | `korean-market` (K-pop merch, idol goods, album PO) | **Kiyoo** — kiyoo.id | Pre-order website: pilih varian, DP 90%, order masuk WhatsApp — ganti DM + spreadsheet manual |
   | `kecantikan` (skincare / beauty D2C) | **VERDA** — verda.id | Landing page premium: hero sinematik, storytelling, koleksi produk, store locator |
   | *(other segments)* | nearest case study, else a relevant `layanan` | Fall back to the closest showcase project; if none fits, pitch the most relevant service node |

   Note the matched project's **active live link** (`live_url` from Step 0, the one with `link_active=true`) — this is the link the message will send as concrete proof ("lihat langsung contohnya: <live link>"). If **no** showcase project fits the segment, fall back to the gap you found on the lead's site (no website, parked domain, no booking system) and the matching OpenCraft `layanan` — don't force an irrelevant project, and send no project link.

2. **Write a short, concrete `marketing_angle`** — one line, anchored to the matched project + the gap you saw. Examples:

   | Lead | marketing_angle |
   |---|---|
   | K-pop store taking pre-orders via DM/spreadsheet | "Pasar K-pop, PO masih lewat DM — tawarkan pre-order website ala Kiyoo (kiyoo.id): varian + DP + order auto ke WhatsApp" |
   | Skincare brand, only on Instagram | "Brand skincare, belum ada landing page — tawarkan landing page premium ala VERDA (verda.id) buat bangun trust & konversi" |
   | Strong site already | "Sudah kuat online — upsell otomasi (customer service WhatsApp / reviews) seperti layanan OpenCraft" |

   This field is what the marketing team filters and prioritises on, so keep it actionable and tie it to the matched showcase project or service.

## Step 5 — Write the Indonesian outreach message

For each `valid` lead, draft `outreach_message` in **Bahasa Indonesia**, personalised using the business name, the marketing angle, and **the showcase project matched in Step 4**. Rules:

- Warm, professional, **not** spammy. Use natural Indonesian (sapaan "Halo Tim <Nama Bisnis>," or "Selamat siang,").
- 1 short paragraph + 1 clear call-to-action. Mention something specific you noticed about *this* business (the angle) so it doesn't read like a mass blast.
- **Send the matched showcase project's active live link.** Name the project and **paste its full active link as a clickable URL on its own line** so the prospect can open it immediately — this is the change: don't just mention the brand in passing, give them the link to click. Put it on a dedicated line, e.g.:

  ```
  Lihat langsung contohnya di sini:
  https://www.kiyoo.id/
  ```

  Use the exact `live_url` (with `link_active=true`) loaded in Step 0 — full `https://…`, no markdown link syntax, no shortening (this text gets pasted raw into WhatsApp/email/DM, so a bare URL is what becomes clickable). Connect what that project does to the lead's situation right before the link. If **no** showcase project matched, pitch the relevant OpenCraft service instead and send **no** link — never invent a project or paste a link that wasn't confirmed active in Step 0.
- End with a soft CTA (ask permission to share a quick proposal / portfolio, or a 15-minute call).
- No fake claims, no fabricated stats, no fake URLs. Only send showcase links confirmed active in Step 0. Sign off generically as the OpenCraft team.

Template to adapt (do **not** send verbatim for every lead — personalise the bolded bits; swap the showcase project to whichever matched the lead's market):

```
Halo Tim **<Business Name>**,

Saya dari OpenCraft. Saya lihat **<observasi spesifik — mis. "pre-order album & photocard-nya masih lewat DM dan spreadsheet">**, dan ini mirip banget sama yang kami kerjain bareng **<Showcase Project — mis. "Kiyoo">** — **<apa yang project itu selesaikan — mis. "website pre-order: pembeli pilih varian, bayar DP, ordernya langsung masuk WhatsApp admin, tanpa fee marketplace">**.

Boleh dilihat langsung contohnya di sini:
<active live link — mis. https://www.kiyoo.id/>

Kami rasa **<Business Name>** bisa dapet manfaat serupa untuk **<angle ringkas>**. Boleh saya kirimkan proposal singkatnya? Tidak ada kewajiban apa pun.

Terima kasih,
Tim OpenCraft
```

Example — a `korean-market` lead (K-pop merch store) gets pitched **Kiyoo**:

```
Halo Tim **K-Pop Corner**,

Saya dari OpenCraft. Saya lihat pre-order album & merch-nya masih dikelola lewat DM Instagram dan spreadsheet, dan ini mirip banget sama yang kami kerjain bareng **Kiyoo** — website pre-order di mana pembeli pilih produk & varian, bayar DP 90%, lalu ordernya otomatis masuk ke WhatsApp admin. Jadi rapi, kelacak, dan tanpa fee marketplace.

Boleh dilihat langsung contohnya di sini:
https://www.kiyoo.id/

Kami rasa **K-Pop Corner** bisa dapet sistem serupa biar PO-nya nggak lagi manual. Boleh saya kirimkan proposal singkatnya? Tidak ada kewajiban apa pun.

Terima kasih,
Tim OpenCraft
```

For `invalid` / `needs_review` leads, leave `outreach_message` and `marketing_angle` null (no point messaging a dead lead).

## Step 6 — Write the results back to Supabase

Update each lead by `id` via `mcp__supabase__execute_sql` (`project_id: "wdzmuniyqqyngzckeoph"`). Batch the updates in one statement using a `VALUES` list + `update … from`, with SQL-safe escaping (double every `'`):

```sql
update scraper_leads as s set
  validation_status = v.validation_status,
  validation_notes  = v.validation_notes,
  marketing_angle   = v.marketing_angle,
  outreach_message  = v.outreach_message,
  validated_at      = now(),
  updated_at        = now()
from (values
  ('<uuid>'::uuid, 'valid',        'website live, email di /kontak', 'Belum ada booking online — tawarkan sistem reservasi', 'Halo Tim ...'),
  ('<uuid>'::uuid, 'invalid',      'domain parked',                  null,                                                 null),
  ('<uuid>'::uuid, 'needs_review', 'situs down hari ini',            null,                                                 null)
  -- one row per validated lead
) as v(id, validation_status, validation_notes, marketing_angle, outreach_message)
where s.id = v.id;
```

Escape every single quote inside the Indonesian text as `''` before building the statement. If a batch is large, split into chunks of ~100 rows.

## Step 7 — Report

Tell the user:
- That the brand knowledge graph was refreshed from the showcase, and which showcase projects are now in play (e.g. "Kiyoo, VERDA").
- How many leads were validated this run, broken down by status (`valid` / `invalid` / `needs_review`).
- **Which showcase project each `valid` lead was matched to** (e.g. "3 → Kiyoo, 2 → VERDA, 1 → generic service") so the showcase-driven targeting is visible.
- That the results are written back to `scraper_leads` and visible in the dashboard's **Scrapers** menu (now with status, marketing angle, and a ready Indonesian message per lead), and that the graph is visible in the **Knowledge Graph** view.
- A small sample table (first 3 `valid` leads): business name, matched showcase project, **the active live link the message sends**, and marketing_angle.
- How many pending leads remain (so they can run `/validate` again to continue).

## Caveats
- The browser extension drives the **real `PC Gaming Raka` Chrome** — never reuse tab IDs across sessions, and re-run `tabs_context_mcp` if a tab/ID goes stale.
- Validation is best-effort from public info; mark anything you can't confirm as `needs_review` rather than guessing `valid`.
- Scraped contacts are cold leads — the generated messages must respect GDPR/CAN-SPAM/Indonesian PDP norms (consent-aware, easy opt-out). Don't auto-send from this skill; it only drafts and stores.
- Don't trigger JavaScript `alert`/`confirm`/`prompt` dialogs while browsing — they freeze the extension (see the claude-in-chrome guidance).
