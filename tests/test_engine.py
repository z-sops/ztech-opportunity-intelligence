"""Deterministic engine rules: comparisons, diff, conflicts, opportunities, scoring."""

from __future__ import annotations

from datetime import timedelta

from ztech_oi.domain.identity import iso, utcnow
from ztech_oi.domain.models import Entity, Evidence, Observation, ProspectInput, Snapshot
from ztech_oi.domain.taxonomy import (
    ChangeType,
    ClaimKind,
    ComparisonDimension,
    ComparisonInterpretation,
    EntityKind,
    ObservationType,
    ProviderStatus,
)
from ztech_oi.engine.comparison import ComparisonEngine, interpret
from ztech_oi.engine.conflicts import detect_conflicts
from ztech_oi.engine.diff import ChangeDetector
from ztech_oi.engine.index import RunIndex
from ztech_oi.engine.opportunities import OpportunityEngine
from ztech_oi.engine.scoring import compute_score

P = Entity(entity_id="ent_p", entity_key="dom:p.com", kind=EntityKind.PROSPECT, company_name="P", domain="p.com")
C = Entity(entity_id="ent_c", entity_key="dom:c.com", kind=EntityKind.COMPETITOR, company_name="C", domain="c.com")
NOW = iso(utcnow())


def ev(eid, ent, otype, metric=None, value=None, conf=0.9, captured=NOW) -> Evidence:
    return Evidence(
        evidence_id=eid,
        entity_id=ent,
        provider="x",
        source_type="website",
        observation_type=otype,
        captured_at=captured,
        claim_kind=ClaimKind.FACT,
        claim="c",
        metric=metric,
        value=value,
        confidence=conf,
    )


def obs(ent, otype, metrics, refs, items=None) -> Observation:
    return Observation(
        observation_id=f"o_{ent}_{otype.value}",
        entity_id=ent,
        provider="x",
        type=otype,
        captured_at=NOW,
        metrics=metrics,
        evidence_refs=refs,
        items=items or [],
    )


def test_interpret_requires_ratio_and_min_delta():
    assert interpret(1, 2, 2)[0] is ComparisonInterpretation.PARITY  # 1 vs 2 is not a gap
    assert interpret(1, 7, 2)[0] is ComparisonInterpretation.COMPETITOR_MORE_ACTIVE
    assert interpret(10, 12, 2)[0] is ComparisonInterpretation.PARITY  # ratio < 1.5
    assert interpret(8, 0, 2)[0] is ComparisonInterpretation.PROSPECT_MORE_ACTIVE
    assert interpret(None, 5, 2)[0] is ComparisonInterpretation.INSUFFICIENT_EVIDENCE


def _ix(observations, evidence, statuses=None):
    st = statuses or {
        "ent_p": {"meta_ads": ProviderStatus.SUCCESS, "website": ProviderStatus.SUCCESS},
        "ent_c": {"meta_ads": ProviderStatus.SUCCESS, "website": ProviderStatus.SUCCESS},
    }
    return RunIndex(observations, evidence, st)


def _cmp(comps, dim):
    return next(c for c in comps if c.dimension is dim)


def test_absence_of_evidence_is_not_evidence_of_absence():
    evs = [
        ev("e1", "ent_p", ObservationType.ADS_META, "active_creative_count", 0, 0.6),
        ev("e2", "ent_c", ObservationType.ADS_META, "active_creative_count", 9),
        ev("e3", "ent_p", ObservationType.WEBSITE_PAGE_INVENTORY, "page_count", 5),
        ev("e4", "ent_c", ObservationType.WEBSITE_PAGE_INVENTORY, "page_count", 30),
    ]
    observations = [
        obs("ent_p", ObservationType.ADS_META, {"active_creative_count": 0, "coverage_reliable": 0}, ["e1"]),
        obs("ent_c", ObservationType.ADS_META, {"active_creative_count": 9, "coverage_reliable": 0}, ["e2"]),
        obs(
            "ent_p",
            ObservationType.WEBSITE_PAGE_INVENTORY,
            {"landing_page_count": 0, "inventory_from_sitemap": 0, "page_count": 5},
            ["e3"],
        ),
        obs(
            "ent_c",
            ObservationType.WEBSITE_PAGE_INVENTORY,
            {"landing_page_count": 4, "inventory_from_sitemap": 1, "page_count": 30},
            ["e4"],
        ),
    ]
    comps = ComparisonEngine().build("r", _ix(observations, evs), P, [C])
    meta = _cmp(comps, ComparisonDimension.META_CREATIVE_ACTIVITY)
    assert meta.prospect_observed is None and meta.interpretation is ComparisonInterpretation.INSUFFICIENT_EVIDENCE
    landing = _cmp(comps, ComparisonDimension.LANDING_PAGE_ACTIVITY)
    assert landing.prospect_observed is None, "homepage-links inventory zero must not count as zero"
    assert _cmp(comps, ComparisonDimension.SEO_CONTENT_COVERAGE).interpretation is ComparisonInterpretation.INSUFFICIENT_EVIDENCE
    assert _cmp(comps, ComparisonDimension.SOCIAL_ACTIVITY_30D).confidence == 0.0


def test_reliable_zero_does_create_a_gap_and_opportunity():
    evs = [
        ev("e1", "ent_p", ObservationType.ADS_META, "active_creative_count", 0, 0.9),
        ev("e2", "ent_c", ObservationType.ADS_META, "active_creative_count", 9, 0.9),
    ]
    observations = [
        obs("ent_p", ObservationType.ADS_META, {"active_creative_count": 0, "coverage_reliable": 1}, ["e1"]),
        obs("ent_c", ObservationType.ADS_META, {"active_creative_count": 9, "coverage_reliable": 1}, ["e2"]),
    ]
    ix = _ix(observations, evs)
    comps = ComparisonEngine().build("r", ix, P, [C])
    assert (
        _cmp(comps, ComparisonDimension.META_CREATIVE_ACTIVITY).interpretation is ComparisonInterpretation.COMPETITOR_MORE_ACTIVE
    )
    opps = OpportunityEngine("r", ix).build(P, comps, [], {"ent_c": "C"}, {})
    assert [o.type.value for o in opps] == ["ADVERTISING_GAP"]
    assert opps[0].confidence <= 0.8, "single-competitor opportunities get the -0.1 auditability penalty"
    assert opps[0].evidence_refs and opps[0].claim_kind is ClaimKind.INFERENCE


def test_zero_opportunities_is_valid():
    ix = _ix([], [])
    comps = ComparisonEngine().build("r", ix, P, [C])
    assert all(c.interpretation is ComparisonInterpretation.INSUFFICIENT_EVIDENCE for c in comps)
    assert OpportunityEngine("r", ix).build(P, comps, [], {}, {}) == []


def test_expired_evidence_is_excluded_from_comparisons():
    old = iso(utcnow() - timedelta(days=45))
    evs = [
        ev("e1", "ent_p", ObservationType.CONTENT_INVENTORY, "articles_last_60d", 0, captured=old),
        ev("e2", "ent_c", ObservationType.CONTENT_INVENTORY, "articles_last_60d", 9),
    ]
    from ztech_oi.domain.identity import freshness_of

    evs = [e.model_copy(update={"freshness": freshness_of(e.captured_at)}) for e in evs]
    observations = [
        obs("ent_p", ObservationType.CONTENT_INVENTORY, {"articles_last_60d": 0}, ["e1"]),
        obs("ent_c", ObservationType.CONTENT_INVENTORY, {"articles_last_60d": 9}, ["e2"]),
    ]
    comps = ComparisonEngine().build("r", _ix(observations, evs), P, [C])
    assert (
        _cmp(comps, ComparisonDimension.CONTENT_PRODUCTION_60D).interpretation is ComparisonInterpretation.INSUFFICIENT_EVIDENCE
    )


def test_conflicting_evidence_is_kept_and_downweighted():
    a = ev("e1", "ent_p", ObservationType.WEBSITE_PAGE_INVENTORY, "page_count", 40, 0.9)
    b = ev("e2", "ent_p", ObservationType.WEBSITE_PAGE_INVENTORY, "page_count", 12, 0.7)
    same = ev("e3", "ent_c", ObservationType.WEBSITE_PAGE_INVENTORY, "page_count", 5, 0.9)
    updated, conflicts = detect_conflicts([a, b, same])
    assert len(conflicts) == 1 and conflicts[0].values == [40, 12]
    by = {e.evidence_id: e for e in updated}
    assert by["e1"].confidence == 0.9 and by["e2"].confidence == round(0.7 * 0.6, 3)
    assert by["e1"].conflicts_with == ["e2"] and by["e3"].conflicts_with == []


# ------------------------------------------------------------------- diff
def snap(sid, observations, statuses, entities=(P, C)) -> Snapshot:
    return Snapshot(
        snapshot_id=sid,
        research_id="r" + sid,
        prospect_entity_key="dom:p.com",
        captured_at=NOW,
        entities=list(entities),
        observations=observations,
        provider_status=statuses,
    )


def inv(ent, urls, from_sitemap=1):
    return obs(
        ent,
        ObservationType.WEBSITE_PAGE_INVENTORY,
        {"page_count": len(urls), "inventory_from_sitemap": from_sitemap},
        ["e"],
        items=[{"url": u, "kind": k} for u, k in urls],
    )


OK = {
    "ent_p": {"website": ProviderStatus.SUCCESS, "meta_ads": ProviderStatus.SUCCESS},
    "ent_c": {"website": ProviderStatus.SUCCESS, "meta_ads": ProviderStatus.SUCCESS},
}


def test_diff_no_history_and_identical_runs_produce_nothing():
    s = snap("s1", [inv("ent_c", [("https://c.com/", "homepage")])], OK)
    assert ChangeDetector().diff(None, s) == []
    s2 = snap("s2", [inv("ent_c", [("https://c.com/", "homepage")])], OK)
    assert ChangeDetector().diff(s, s2) == []


def test_diff_new_and_removed_pages():
    a = snap("s1", [inv("ent_c", [("https://c.com/", "homepage"), ("https://c.com/old", "other")])], OK)
    b = snap("s2", [inv("ent_c", [("https://c.com/", "homepage"), ("https://c.com/offers/summer", "landing")])], OK)
    changes = ChangeDetector().diff(a, b)
    assert sorted(ch.type.value for ch in changes) == ["NEW_PAGE", "PAGE_INVENTORY_CHANGED", "REMOVED_PAGE"]
    assert all(ch.channel == "website" for ch in changes)


def test_diff_provider_failure_is_not_removal():
    a = snap("s1", [inv("ent_c", [("https://c.com/", "homepage"), ("https://c.com/x", "other")])], OK)
    failed = {**OK, "ent_c": {"website": ProviderStatus.UNAVAILABLE, "meta_ads": ProviderStatus.SUCCESS}}
    b = snap("s2", [], failed)
    changes = ChangeDetector().diff(a, b)
    assert [c.type for c in changes] == [ChangeType.PROVIDER_AVAILABILITY_CHANGED]


def test_diff_homepage_link_inventories_never_emit_removals():
    a = snap("s1", [inv("ent_c", [("https://c.com/", "homepage"), ("https://c.com/x", "other")], from_sitemap=0)], OK)
    b = snap("s2", [inv("ent_c", [("https://c.com/", "homepage")], from_sitemap=0)], OK)
    assert ChangeDetector().diff(a, b) == []


def test_diff_meta_increase_and_new_competitor():
    def m(n):
        return obs(
            "ent_c",
            ObservationType.ADS_META,
            {"active_creative_count": n, "coverage_reliable": 1},
            ["e"],
            items=[{"ad_id": f"a{i}"} for i in range(n)],
        )

    a = snap("s1", [m(2)], OK, entities=(P, C))
    newc = Entity(entity_id="ent_n", entity_key="dom:n.com", kind=EntityKind.COMPETITOR, company_name="N")
    b = snap("s2", [m(6)], OK, entities=(P, C, newc))
    types = {c.type for c in ChangeDetector().diff(a, b)}
    assert {ChangeType.AD_ACTIVITY_INCREASED, ChangeType.NEW_AD_CREATIVE, ChangeType.NEW_COMPETITOR} <= types


# ---------------------------------------------------------------- scoring
def test_score_redistributes_uncomputable_components_and_is_deterministic():
    ix = _ix([], [], {"ent_p": {"website": ProviderStatus.UNAVAILABLE}})
    kw = dict(
        ix=ix,
        prospect=P,
        pinput=ProspectInput(company_name="P"),
        comparisons=[],
        changes=None,
        evidence=[],
        conflicts=[],
        provider_errors={},
        previous_score=None,
    )
    s1, s2 = compute_score(**kw), compute_score(**kw)
    assert s1 == s2
    assert s1.score == 0 and all(not c.computed for c in s1.components if c.key != "research_confidence")
    assert sum(c.effective_weight for c in s1.components) in (0, 1.0)
    assert any("Not computed" in x for x in s1.explanation)
