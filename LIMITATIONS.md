# Limitations (honest, spec §46)

## Provider truth table

| Provider | Status | What is NOT observed |
|---|---|---|
| Website | REAL | JavaScript-only content (no headless browser); pages beyond 2000 sitemap URLs; pages disallowed by robots.txt |
| Content | REAL | articles with no feed, page date or JSON-LD date → window counts are **null**, not zero; sitemap `lastmod` is reported only as "modified", never as "published" |
| Competitor discovery | REAL with a search key | not exhaustive; bounded to ≤4 queries × 10 results. Without a key only `known_competitors` are used |
| Meta ads | REAL with token (official Ad Library API) | **outside the EU/UK the API returns only political/issue ads**, so non-EU zero counts are flagged `coverage_reliable=0` and treated as unknown. Spend, impressions and audience are never reported |
| Google ads | THIRD-PARTY with SerpApi key | no official API exists; SerpApi scrapes the Transparency Center and every item is labelled `third_party:serpapi`. Spend and performance are never reported |
| X / Twitter | REAL with paid API token | account must be linked from the company's own website (handles are never guessed); only the latest ≤50 posts |
| LinkedIn | **UNSUPPORTED** | everything. LinkedIn prohibits scraping and its APIs only cover pages you administer. Company LinkedIn URLs found on websites are still recorded as profile facts |

## Field-level notes (second pass)

- **Meta creative type.** The Ad Library API exposes media type only as a search *filter*, so one extra bounded query (`media_type=VIDEO`) gives a video count. Everything else is reported as `non_video`. Image versus meme versus text is not separated.
- **Google ad copy.** SerpApi's transparency-centre listing does not always include copy. The parser reads `title / headline / snippet / description / body / text / displayed_link / call_to_action` *if present*. When no copy is returned, offers, themes and topic counts are `null` with a limitation. **These field names have not been verified against a live SerpApi response.**
- **X post labels** (announcement, product_launch, offer, event, hiring) and **business model** labels are deterministic keyword rules over observed text. They are stored as INFERENCE, and a business-model label needs at least two independent indicators.
- **Topic relevance** uses the prospect's `products_services` as the topic. It is keyword matching, not semantic similarity, so synonyms ("bridal cakes") are missed unless listed.
- **Competitor location** is taken only from the competitor's own website (JSON-LD address). It is never copied from the prospect, so it is often `null`.

## Analytical limits

- **First run has no history.** Changes, momentum, `recent_changes` and `market_timing` need a second run (recommended: weekly per active lead).
- **Counts are observations, not market truth.** "7 articles in 60 days" means 7 were observable with dates.
- **Page classification is URL-pattern based** (confidence 0.70-0.75). `/work` may be portfolio or careers depending on the site.
- **Hiring** means a careers page exists. Open-role volume is not observed.
- **ICP fit** is keyword matching against observed profile text. It is not a firmographic model.
- **Ad spend** is never estimated. The `EstimateRange` model exists for a future provider that legitimately supplies ranges (they would be stored as `claim_kind=estimate`).

## Operational limits

- The DNS check runs before the request. A sub-second DNS-rebinding race between the check and httpx's own resolution is theoretically possible. Mitigations: public-only resolution, every redirect re-checked, no credentials sent to scraped sites. Pinning connections to the checked IP is a possible hardening step.
- A single research is bounded but not fast: about 10-60 HTTP requests per entity, with a per-host politeness delay of 0.5 s. Expect roughly 20-90 s for a prospect plus 3 competitors.
- SQLite is single-writer (WAL mode). For many concurrent writers, implement `Repository` on Postgres.
- Not built yet, by design: Zuni-SEO provider adapter (spec §44), authorised LinkedIn integration, headless rendering.
- The Node contract test (`clients/node`) proves the MCP boundary with the official JS SDK. It is not an Electron integration; ZTech owns that (spec §8, §42).

## Verification limit of this build

The build sandbox had **no outbound internet**. All providers were exercised against realistic offline fixtures through the real HTTP stack, but not yet against live sites and APIs. Run `TESTING.md` §5 on a networked machine before production use.
