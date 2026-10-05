# Data Schema (canonical report v1.0)

The machine-readable source of truth is `schemas/intelligence_report.v1.schema.json`, exported from the Pydantic models. A test fails if the published schema drifts from the code. Regenerate with `python scripts/export_schema.py`.

**Versioning.** `schema_version` is a literal `"1.0"`. Adding optional fields or enum values is a minor change. Renaming or removing fields or enum values, or changing semantics, is a major change and moves to `"2.0"`.

## IntelligenceReport

| Field | Type | Notes |
|---|---|---|
| `schema_version` | `"1.0"` | |
| `research_id` | str | `res_<UTC timestamp>_<rand>` |
| `status` | `completed\|partial\|failed` | see API-CONTRACT §2 |
| `generated_at` | ISO-8601 UTC | |
| `snapshot_id`, `previous_snapshot_id?` | str | the diff baseline |
| `prospect` | Prospect | entity + `input` + `profile` |
| `competitors[]` | Competitor | `relationship_type`, `relationship_confidence`, `reason`, `evidence_refs`, `discovered_via`, `profile` |
| `evidence[]` | Evidence | provenance records (below) |
| `observations[]` | Observation | structured metrics/items per entity × provider |
| `signals[]` | Signal | controlled taxonomy |
| `advertising_intelligence` | `{meta: ChannelSummary[], google: ChannelSummary[]}` | one row per entity |
| `content_intelligence` | `{website: [...], content: [...]}` | |
| `social_intelligence` | `{linkedin: [...], twitter: [...]}` | |
| `comparisons[]` | Comparison | prospect vs each competitor × dimension; `topic` set on topic-scoped dimensions |
| `changes[]` | Change | vs previous snapshot (empty on first run); `channel` = website / content / meta_ads / google_ads / twitter |
| `opportunities[]` | Opportunity | always `claim_kind: inference` |
| `opportunity_score` | OpportunityScore | explainable components |
| `sales_angles[]` | SalesAngle | including `do_not_claim[]` guard-rails |
| `timeline[]` | TimelineEvent | engine-owned events only |
| `conflicts[]` | EvidenceConflict | disagreeing evidence on the same metric |
| `limitations[]` | str | `[provider] ...` prefixed |
| `provider_status` | `{entity_id: {provider: status}}` | |
| `telemetry` | `{started_at, completed_at, duration_ms, providers: ProviderTelemetry[]}` | |

## Evidence (first-class object, spec §17)

`evidence_id`, `entity_id`, `provider`, `source_type` (`website`, `meta_ad_library_api`, `third_party:serpapi`, `x_api_v2`, `web_search`, `caller_input`, `llm_interpretation`), `source_url?`, `observation_type`, `observed_at?` (when the thing happened at the source, e.g. article date), `captured_at`, `expires_at`, `freshness` (`fresh` ≤7d < `stale` ≤30d < `expired`), `claim_kind` (`fact|estimate|inference`), `claim` (≤600 chars, human-readable), `metric?` + `value?` (structured), `estimate?` (`{low, high, unit, method, source}`; only allowed with `claim_kind=estimate`), `raw_reference` (bounded excerpt, never a whole page), `content_hash?`, `confidence` 0-1, `conflicts_with[]`.

## Observation (what the engine reads)

| `type` | key metrics |
|---|---|
| `website.homepage` | `internal_link_count`, `word_count` |
| `website.page_inventory` | `page_count`, `commercial_page_count`, `pricing_page_count`, `product_page_count`, `service_page_count`, `landing_page_count`, `case_study_page_count`, `blog_page_count`, `careers_page_count`, `contact_page_count`, `location_page_count`, `inventory_from_sitemap` (1/0); items `{url, kind, lastmod}` |
| `website.company_profile` | `email_count`, `phone_count`, `social_profile_count`, `cta_count`, `offer_mentions`, `published_price_points`, `location_count`; item = profile (incl. `business_model` + `business_model_indicators`, INFERENCE) + `field_evidence` |
| `content.inventory` | `article_count_observed`, `dated_article_count`, `articles_last_30d/60d/90d` (**null** if undatable), `relevant_articles_last_60d`, `articles_modified_last_60d`, `from_feed` |
| `content.topics` | items `{topic, article_count, method: title_keywords\|llm_label}` |
| `ads.meta` | `active_creative_count`, `started_last_30d`, `distinct_pages`, `coverage_reliable` (1 only for EU/UK), `offer_creative_count`, `relevant_creative_count`, `video_creative_count`, `non_video_creative_count`, `distinct_ctas`, `distinct_landing_domains`; items per creative (`offers`, `topics`, `media`) + one attributes item (`themes`, `ctas`, `landing_domains`) |
| `ads.google` | `creative_count`, `shown_last_30d`, `text_ads`, `image_ads`, `video_ads`, `creatives_with_copy`, `offer_creative_count`/`relevant_creative_count` (null when no copy); items incl. `copy`, `landing_domain`, `topics` |
| `social.twitter` | `posts_retrieved`, `posts_last_30d`, `replies_last_30d` (customer conversations), `announcements_last_30d`, `product_launch_posts_last_30d`, `offer_posts_last_30d`, `relevant_posts_last_30d`, `followers`, `avg_likes_per_post`; items with rule-based `labels` (INFERENCE) |
| `competitors.candidates` | `candidate_count`, `user_provided` |

## Controlled taxonomies

- **ProviderStatus:** `success, partial, unavailable, unsupported, failed, rate_limited`
- **SignalType:** `AD_ACTIVITY_OBSERVED, AD_ACTIVITY_INCREASE, AD_ACTIVITY_DECREASE, NEW_AD_CREATIVE, CONTENT_ACTIVITY_OBSERVED, CONTENT_ACTIVITY_INCREASE, NEW_ARTICLE, NEW_LANDING_PAGE, REMOVED_PAGE, NEW_OFFER, COMMERCIAL_INTENT, PRODUCT_LAUNCH, HIRING, EXPANSION, SOCIAL_ACTIVITY, SOCIAL_ACTIVITY_INCREASE, MESSAGING_CHANGE, COMPETITOR_PRESSURE, CONTENT_GAP, ADVERTISING_GAP, MARKET_CHANGE`
- **ComparisonDimension** (unit, window, min Δ):
  - `meta_creative_activity` (active creatives, at capture, 3)
  - `google_ad_activity` (listed creatives, 3)
  - `content_production_60d` (articles, last 60 days, 2)
  - `commercial_content` (pages, 3)
  - `landing_page_activity` (2)
  - `offers` (2)
  - `hiring` (careers page present, 1)
  - `social_activity_30d` (X posts, 4)
  - `seo_content_coverage` (sitemap pages, both sides from sitemap only, 15)
  - `expansion` (location pages, 2)
  - `relevant_content_60d` (articles about the prospect's products/services, last 60 days, 2; **topic-scoped**)
  - `relevant_ad_creatives` (Meta + Google creatives mentioning those products/services, 2; **topic-scoped**)
  - Topic-scoped dimensions are only computed when `products_services` is supplied. Matching is deterministic: stemmed keywords, all of a ≤2-word phrase or ⅔ of a longer one.
- **ComparisonInterpretation:** `competitor_more_active, prospect_more_active, parity, insufficient_evidence`
- **OpportunityType:** `ADVERTISING_GAP, CONTENT_GAP, COMPETITIVE_VISIBILITY_GAP, LANDING_PAGE_GAP, OFFER_GAP, SOCIAL_GAP, COMPETITOR_MOMENTUM, TIMING_WINDOW`
- **ChangeType:** `NEW_PAGE` (with `topics`), `REMOVED_PAGE`, `PAGE_INVENTORY_CHANGED` ("42 → 48 pages; 6 new URLs; 4 appear related to wedding cakes"), `CTA_CHANGED`, `SERVICES_CHANGED`, `NEW_ARTICLE`, `NEW_OFFER`, `NEW_AD_CREATIVE`, `REMOVED_AD_CREATIVE` (Meta and Google), `AD_ACTIVITY_INCREASED/DECREASED` (Meta and Google), `CONTENT_ACTIVITY_INCREASED/DECREASED`, `SOCIAL_ACTIVITY_INCREASED/DECREASED`, `NEW_COMPETITOR`, `COMPETITOR_NOT_SEEN`, `PROVIDER_AVAILABILITY_CHANGED`
- **TimelineEventType:** `RESEARCH_STARTED, COMPANY_RESOLVED, COMPETITOR_DISCOVERED, PROVIDER_UNAVAILABLE, ARTICLE_PUBLISHED, META_AD_STARTED, GOOGLE_AD_FIRST_SHOWN, SIGNAL_DETECTED, CHANGE_DETECTED, COMPETITOR_ACTIVITY_INCREASE, CONTENT_GAP_DETECTED, ADVERTISING_GAP_DETECTED, OPPORTUNITY_IDENTIFIED, OPPORTUNITY_SCORE_CHANGED, SNAPSHOT_CAPTURED, RESEARCH_COMPLETED, RESEARCH_PARTIAL`

## Opportunity Score (model `score-1.0`)

| Component | Weight | How it is computed |
|---|---|---|
| `competitive_pressure` | .22 | confidence-weighted share of decided comparisons where a competitor is more active |
| `commercial_intent` | .14 | 5 indicators: pricing page, published prices, offers, CTAs, landing pages |
| `prospect_activity` | .12 | mean of observable channels (articles/60d, Meta, Google, X) |
| `evidence_quality` | .12 | mean confidence of fresh prospect facts, −10 per conflict |
| `icp_fit` | .10 | only when `icp` is supplied |
| `recent_changes` | .10 | prospect changes since the previous snapshot |
| `market_timing` | .08 | competitor/market changes since the previous snapshot |
| `research_confidence` | .07 | success ratio of **configured** providers |
| `contactability` | .05 | email 40 + phone 30 + contact page 30 |

A component that cannot be computed is `computed:false` and its weight is redistributed (`effective_weight`). Nothing gets a made-up default. The confidence formulas are in `domain/taxonomy.py:CONFIDENCE_PRODUCERS` and are also returned by `get_engine_info`.

## SQLite storage (engine-owned, isolated)

Tables: `research_jobs, entities, competitors, evidence, snapshots, signals, opportunities, reports, schema_meta`. Each row stores the validated JSON document plus indexed keys. This is **not** ZTech's database and shares nothing with it.
