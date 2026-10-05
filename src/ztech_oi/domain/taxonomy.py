"""Closed, controlled vocabularies for the engine.

Every enum here is part of the external contract (DATA-SCHEMA.md). Adding a
value is a minor schema change; renaming or removing one is a major change.
"""

from __future__ import annotations

from enum import StrEnum


class ClaimKind(StrEnum):
    """ZTech epistemic categories. The engine produces only the first three.

    DECISION and ACTION belong to ZTech (human approval / outreach) and are
    never produced here.
    """

    FACT = "fact"  # directly observed from a source
    ESTIMATE = "estimate"  # numeric range from a declared third-party/method
    INFERENCE = "inference"  # interpretation derived from facts (rules or AI)


class ProviderStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"  # source exists but could not be used (not configured, blocked, down)
    UNSUPPORTED = "unsupported"  # no legitimate/reliable source exists for this build
    FAILED = "failed"
    RATE_LIMITED = "rate_limited"


class FreshnessState(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class EntityKind(StrEnum):
    PROSPECT = "prospect"
    COMPETITOR = "competitor"


class RelationshipType(StrEnum):
    USER_PROVIDED = "user_provided"  # caller named it as a competitor
    DIRECT_COMPETITOR = "direct_competitor"
    LIKELY_COMPETITOR = "likely_competitor"
    ADJACENT_PLAYER = "adjacent_player"
    CANDIDATE = "candidate"


class ObservationType(StrEnum):
    """Structured observation kinds. Engines read these, never free-text claims."""

    WEBSITE_HOMEPAGE = "website.homepage"
    WEBSITE_PAGE_INVENTORY = "website.page_inventory"
    WEBSITE_COMPANY_PROFILE = "website.company_profile"
    CONTENT_INVENTORY = "content.inventory"
    CONTENT_TOPICS = "content.topics"
    ADS_META = "ads.meta"
    ADS_GOOGLE = "ads.google"
    SOCIAL_TWITTER = "social.twitter"
    SOCIAL_LINKEDIN = "social.linkedin"
    COMPETITOR_CANDIDATES = "competitors.candidates"


class PageKind(StrEnum):
    HOMEPAGE = "homepage"
    PRICING = "pricing"
    PRODUCT = "product"
    SERVICE = "service"
    LANDING = "landing"
    CASE_STUDY = "case_study"
    BLOG = "blog"
    ABOUT = "about"
    CONTACT = "contact"
    CAREERS = "careers"
    LOCATION = "location"
    LEGAL = "legal"
    OTHER = "other"


COMMERCIAL_PAGE_KINDS = frozenset({PageKind.PRICING, PageKind.PRODUCT, PageKind.SERVICE, PageKind.LANDING, PageKind.CASE_STUDY})


class SignalType(StrEnum):
    # advertising
    AD_ACTIVITY_OBSERVED = "AD_ACTIVITY_OBSERVED"
    AD_ACTIVITY_INCREASE = "AD_ACTIVITY_INCREASE"
    AD_ACTIVITY_DECREASE = "AD_ACTIVITY_DECREASE"
    NEW_AD_CREATIVE = "NEW_AD_CREATIVE"
    # content
    CONTENT_ACTIVITY_OBSERVED = "CONTENT_ACTIVITY_OBSERVED"
    CONTENT_ACTIVITY_INCREASE = "CONTENT_ACTIVITY_INCREASE"
    NEW_ARTICLE = "NEW_ARTICLE"
    # website / commercial
    NEW_LANDING_PAGE = "NEW_LANDING_PAGE"
    REMOVED_PAGE = "REMOVED_PAGE"
    NEW_OFFER = "NEW_OFFER"
    COMMERCIAL_INTENT = "COMMERCIAL_INTENT"
    PRODUCT_LAUNCH = "PRODUCT_LAUNCH"
    # organisation
    HIRING = "HIRING"
    EXPANSION = "EXPANSION"
    # social
    SOCIAL_ACTIVITY = "SOCIAL_ACTIVITY"
    SOCIAL_ACTIVITY_INCREASE = "SOCIAL_ACTIVITY_INCREASE"
    MESSAGING_CHANGE = "MESSAGING_CHANGE"
    # competitive (derived from comparisons)
    COMPETITOR_PRESSURE = "COMPETITOR_PRESSURE"
    CONTENT_GAP = "CONTENT_GAP"
    ADVERTISING_GAP = "ADVERTISING_GAP"
    MARKET_CHANGE = "MARKET_CHANGE"


class ComparisonDimension(StrEnum):
    META_CREATIVE_ACTIVITY = "meta_creative_activity"
    GOOGLE_AD_ACTIVITY = "google_ad_activity"
    CONTENT_PRODUCTION_60D = "content_production_60d"
    COMMERCIAL_CONTENT = "commercial_content"
    LANDING_PAGE_ACTIVITY = "landing_page_activity"
    OFFERS = "offers"
    HIRING = "hiring"
    SOCIAL_ACTIVITY_30D = "social_activity_30d"
    SEO_CONTENT_COVERAGE = "seo_content_coverage"
    EXPANSION = "expansion"
    RELEVANT_CONTENT_60D = "relevant_content_60d"
    RELEVANT_AD_CREATIVES = "relevant_ad_creatives"


class ComparisonInterpretation(StrEnum):
    COMPETITOR_MORE_ACTIVE = "competitor_more_active"
    PROSPECT_MORE_ACTIVE = "prospect_more_active"
    PARITY = "parity"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class OpportunityType(StrEnum):
    ADVERTISING_GAP = "ADVERTISING_GAP"
    CONTENT_GAP = "CONTENT_GAP"
    COMPETITIVE_VISIBILITY_GAP = "COMPETITIVE_VISIBILITY_GAP"
    LANDING_PAGE_GAP = "LANDING_PAGE_GAP"
    OFFER_GAP = "OFFER_GAP"
    SOCIAL_GAP = "SOCIAL_GAP"
    COMPETITOR_MOMENTUM = "COMPETITOR_MOMENTUM"
    TIMING_WINDOW = "TIMING_WINDOW"


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ChangeType(StrEnum):
    NEW_PAGE = "NEW_PAGE"
    REMOVED_PAGE = "REMOVED_PAGE"
    NEW_ARTICLE = "NEW_ARTICLE"
    NEW_OFFER = "NEW_OFFER"
    NEW_AD_CREATIVE = "NEW_AD_CREATIVE"
    REMOVED_AD_CREATIVE = "REMOVED_AD_CREATIVE"
    AD_ACTIVITY_INCREASED = "AD_ACTIVITY_INCREASED"
    AD_ACTIVITY_DECREASED = "AD_ACTIVITY_DECREASED"
    CONTENT_ACTIVITY_INCREASED = "CONTENT_ACTIVITY_INCREASED"
    CONTENT_ACTIVITY_DECREASED = "CONTENT_ACTIVITY_DECREASED"
    NEW_COMPETITOR = "NEW_COMPETITOR"
    COMPETITOR_NOT_SEEN = "COMPETITOR_NOT_SEEN"
    PROVIDER_AVAILABILITY_CHANGED = "PROVIDER_AVAILABILITY_CHANGED"
    PAGE_INVENTORY_CHANGED = "PAGE_INVENTORY_CHANGED"
    CTA_CHANGED = "CTA_CHANGED"
    SERVICES_CHANGED = "SERVICES_CHANGED"
    SOCIAL_ACTIVITY_INCREASED = "SOCIAL_ACTIVITY_INCREASED"
    SOCIAL_ACTIVITY_DECREASED = "SOCIAL_ACTIVITY_DECREASED"


class TimelineEventType(StrEnum):
    RESEARCH_STARTED = "RESEARCH_STARTED"
    COMPANY_RESOLVED = "COMPANY_RESOLVED"
    COMPETITOR_DISCOVERED = "COMPETITOR_DISCOVERED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    ARTICLE_PUBLISHED = "ARTICLE_PUBLISHED"
    META_AD_STARTED = "META_AD_STARTED"
    GOOGLE_AD_FIRST_SHOWN = "GOOGLE_AD_FIRST_SHOWN"
    SIGNAL_DETECTED = "SIGNAL_DETECTED"
    CHANGE_DETECTED = "CHANGE_DETECTED"
    COMPETITOR_ACTIVITY_INCREASE = "COMPETITOR_ACTIVITY_INCREASE"
    CONTENT_GAP_DETECTED = "CONTENT_GAP_DETECTED"
    ADVERTISING_GAP_DETECTED = "ADVERTISING_GAP_DETECTED"
    OPPORTUNITY_IDENTIFIED = "OPPORTUNITY_IDENTIFIED"
    OPPORTUNITY_SCORE_CHANGED = "OPPORTUNITY_SCORE_CHANGED"
    SNAPSHOT_CAPTURED = "SNAPSHOT_CAPTURED"
    RESEARCH_COMPLETED = "RESEARCH_COMPLETED"
    RESEARCH_PARTIAL = "RESEARCH_PARTIAL"


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    PROVIDER_NOT_CONFIGURED = "PROVIDER_NOT_CONFIGURED"
    UNSUPPORTED_SOURCE = "UNSUPPORTED_SOURCE"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    INVALID_DOMAIN = "INVALID_DOMAIN"
    INVALID_URL = "INVALID_URL"
    SSRF_BLOCKED = "SSRF_BLOCKED"
    RESPONSE_TOO_LARGE = "RESPONSE_TOO_LARGE"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    COMPANY_NOT_FOUND = "COMPANY_NOT_FOUND"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    PROVIDER_FAILED = "PROVIDER_FAILED"
    PARTIAL_RESEARCH = "PARTIAL_RESEARCH"
    NOT_FOUND = "NOT_FOUND"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ResearchStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


# Human-readable documentation of how each confidence value is produced
# (spec #34). Exposed verbatim through the contract / MCP `get_engine_info`.
CONFIDENCE_PRODUCERS: dict[str, str] = {
    "evidence": (
        "Fixed per extraction method: direct HTTP/API field = 0.90-0.95; parsed from HTML "
        "structure = 0.80-0.88; URL-pattern classification = 0.70; LLM labelling = LLM-reported "
        "value capped at 0.75. Conflicting evidence on the same metric is multiplied by 0.6."
    ),
    "entity_match": (
        "Domain match = 0.95; name token overlap on result host = 0.75; name-only = 0.5. "
        "Meta/Google advertiser match uses the same scale against page/advertiser name."
    ),
    "competitor_relationship": (
        "user_provided = 0.95; search + LLM classification = LLM value capped at 0.85 and "
        "multiplied by number-of-independent-mentions factor (1 -> 0.8, 2 -> 0.9, 3+ -> 1.0); "
        "search-only fallback = 0.4 (candidate)."
    ),
    "signal": "signal.confidence = strength_rule_constant * mean(confidence of supporting evidence).",
    "comparison": (
        "insufficient_evidence = 0.0; otherwise min(prospect evidence conf, competitor evidence conf) "
        "* magnitude factor (0.7 at the minimum delta, rising to 1.0 at >=3x)."
    ),
    "opportunity": (
        "min(confidence of supporting comparisons/signals); -0.1 if supported by a single "
        "competitor only; capped at 0.6 when any underlying provider was partial."
    ),
    "sales_angle": "Equal to the underlying opportunity confidence; never higher.",
    "rule_classification": (
        "Business-model labels: min(0.85, 0.45 + 0.1 * independent indicators), >=2 indicators required. "
        "X post labels: 0.70. Both are INFERENCE over observed text."
    ),
    "topic_relevance": (
        "Topic-relevant counts (articles, ad creatives, posts) use deterministic stemmed keyword matching against the "
        "prospect's products_services: Meta 0.80 (0.55 if coverage unreliable), Google 0.70 (third-party copy)."
    ),
    "media_type": "Meta video count comes from the API's media_type=VIDEO filter (fact); non_video = total - video.",
}
