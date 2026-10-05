"""Change Detection between snapshots (spec #19).

Entities are matched across runs by stable entity_id (hash of entity_key).

CRITICAL rules:
  * a channel must be AVAILABLE in BOTH snapshots before any NEW/REMOVED/INCREASE/
    DECREASE change is emitted for it;
  * available -> unavailable emits PROVIDER_AVAILABILITY_CHANGED, never a removal;
  * REMOVED_PAGE requires sitemap-based inventories on both sides, both untruncated;
  * identical observations produce no changes (idempotent re-runs).
"""

from __future__ import annotations

from functools import partial
from urllib.parse import urlsplit

from ..domain.identity import stable_id
from ..domain.models import Change, Entity, Observation, Snapshot
from ..domain.taxonomy import ChangeType, ClaimKind, EntityKind, ObservationType, PageKind, ProviderStatus
from ..net.parsing import topic_matches

OK = (ProviderStatus.SUCCESS, ProviderStatus.PARTIAL)
NEWSWORTHY_PAGE_KINDS = {
    PageKind.PRICING.value,
    PageKind.PRODUCT.value,
    PageKind.SERVICE.value,
    PageKind.LANDING.value,
    PageKind.LOCATION.value,
    PageKind.CAREERS.value,
    PageKind.CASE_STUDY.value,
}
MAX_CHANGES_PER_KIND = 25


def _obs(s: Snapshot, entity_id: str, t: ObservationType) -> Observation | None:
    return next((o for o in s.observations if o.entity_id == entity_id and o.type == t), None)


def _ok(s: Snapshot, entity_id: str, provider: str) -> bool:
    return s.provider_status.get(entity_id, {}).get(provider) in OK


class ChangeDetector:
    def __init__(self, topics: list[str] | None = None) -> None:
        #: the prospect's products/services; used to tag new URLs by topic (spec §15 example)
        self.topics = [t for t in (topics or []) if t.strip()]

    def diff(self, previous: Snapshot | None, current: Snapshot) -> list[Change]:
        if previous is None:
            return []
        out: list[Change] = []
        prev_entities = {e.entity_id: e for e in previous.entities}
        cur_entities = {e.entity_id: e for e in current.entities}

        def mk(
            t: ChangeType,
            ent: Entity,
            detail: str,
            *,
            before=None,
            after=None,
            kind=ClaimKind.FACT,
            conf=0.85,
            refs: list[str] | None = None,
            salt: str = "",
            channel: str | None = None,
        ) -> None:
            out.append(
                Change(
                    change_id=stable_id("chg", previous.snapshot_id, current.snapshot_id, ent.entity_id, t.value, salt),
                    type=t,
                    channel=channel,
                    entity_id=ent.entity_id,
                    entity_name=ent.company_name,
                    previous_snapshot_id=previous.snapshot_id,
                    current_snapshot_id=current.snapshot_id,
                    before=before,
                    after=after,
                    detail=detail[:400],
                    claim_kind=kind,
                    confidence=conf,
                    evidence_refs=(refs or [])[:6],
                )
            )

        # competitor set
        for eid, ent in cur_entities.items():
            if eid not in prev_entities and ent.kind is EntityKind.COMPETITOR:
                mk(
                    ChangeType.NEW_COMPETITOR,
                    ent,
                    f"{ent.company_name} appeared as a competitor in this snapshot",
                    after=ent.company_name,
                    conf=0.8,
                )
        for eid, ent in prev_entities.items():
            if eid not in cur_entities and ent.kind is EntityKind.COMPETITOR:
                mk(
                    ChangeType.COMPETITOR_NOT_SEEN,
                    ent,
                    f"{ent.company_name} was a competitor previously but was not surfaced this time (it may still compete)",
                    before=ent.company_name,
                    kind=ClaimKind.INFERENCE,
                    conf=0.5,
                )

        for eid, ent in cur_entities.items():
            if eid not in prev_entities:
                continue
            # provider availability transitions (never removals)
            providers = set(previous.provider_status.get(eid, {})) | set(current.provider_status.get(eid, {}))
            for prov in sorted(providers):
                was, now = _ok(previous, eid, prov), _ok(current, eid, prov)
                if was != now:
                    mk(
                        ChangeType.PROVIDER_AVAILABILITY_CHANGED,
                        ent,
                        f"{prov} was {'available' if was else 'unavailable'} previously and is "
                        f"{'available' if now else 'unavailable'} now; no activity change is inferred from this",
                        before=str(previous.provider_status.get(eid, {}).get(prov)),
                        after=str(current.provider_status.get(eid, {}).get(prov)),
                        conf=1.0,
                        salt=prov,
                    )
            self._pages(previous, current, ent, partial(mk, channel="website"))
            self._profile(previous, current, ent, partial(mk, channel="website"))
            self._content(previous, current, ent, partial(mk, channel="content"))
            self._meta(previous, current, ent, partial(mk, channel="meta_ads"))
            self._google(previous, current, ent, partial(mk, channel="google_ads"))
            self._twitter(previous, current, ent, partial(mk, channel="twitter"))
            self._offers(previous, current, ent, partial(mk, channel="website"))
        return out

    # ---------------------------------------------------------------- pages
    def _pages(self, prev: Snapshot, cur: Snapshot, ent: Entity, mk) -> None:
        if not (_ok(prev, ent.entity_id, "website") and _ok(cur, ent.entity_id, "website")):
            return
        a = _obs(prev, ent.entity_id, ObservationType.WEBSITE_PAGE_INVENTORY)
        b = _obs(cur, ent.entity_id, ObservationType.WEBSITE_PAGE_INVENTORY)
        if not a or not b:
            return
        both_sitemap = a.metrics.get("inventory_from_sitemap") == 1 and b.metrics.get("inventory_from_sitemap") == 1
        complete = len(a.items) >= (a.metrics.get("page_count") or 0) and len(b.items) >= (b.metrics.get("page_count") or 0)
        before = {i["url"]: i.get("kind") for i in a.items}
        after = {i["url"]: i.get("kind") for i in b.items}
        if not (both_sitemap and complete):
            # only page-count delta is reportable; URL sets are partial
            pa, pb = a.metrics.get("page_count"), b.metrics.get("page_count")
            if both_sitemap and pa is not None and pb is not None and pa != pb:
                mk(
                    ChangeType.PAGE_INVENTORY_CHANGED,
                    ent,
                    f"Sitemap page count changed from {int(pa)} to {int(pb)} on {ent.domain} "
                    "(URL-level diff not possible: inventory truncated)",
                    before={"page_count": pa},
                    after={"page_count": pb},
                    refs=b.evidence_refs[:1],
                    salt="count",
                )
            return
        added = [u for u in after if u not in before]
        removed = [u for u in before if u not in after]
        url_topics = {u: self._url_topics(u) for u in added}
        if added or removed:
            by_topic: dict[str, int] = {}
            for ts in url_topics.values():
                for t in ts:
                    by_topic[t] = by_topic.get(t, 0) + 1
            topic_txt = "; ".join(f"{n} appear related to {t}" for t, n in sorted(by_topic.items(), key=lambda kv: -kv[1]))
            mk(
                ChangeType.PAGE_INVENTORY_CHANGED,
                ent,
                f"{ent.domain}: {len(before)} -> {len(after)} pages; {len(added)} new URL(s), {len(removed)} removed"
                + (f"; {topic_txt}" if topic_txt else ""),
                before={"page_count": len(before)},
                after={"page_count": len(after), "new_urls": len(added), "removed_urls": len(removed), "new_by_topic": by_topic},
                refs=b.evidence_refs[:1],
                salt="summary",
            )
        for u in added[:MAX_CHANGES_PER_KIND]:
            kind = after[u]
            mk(
                ChangeType.NEW_PAGE,
                ent,
                f"New {kind} page on {ent.domain}: {u}" + (f" (topic: {', '.join(url_topics[u])})" if url_topics[u] else ""),
                after={"url": u, "kind": kind, "topics": url_topics[u]},
                refs=b.evidence_refs[:1],
                conf=0.85 if kind in NEWSWORTHY_PAGE_KINDS else 0.75,
                salt=u,
            )
        for u in removed[:MAX_CHANGES_PER_KIND]:
            mk(
                ChangeType.REMOVED_PAGE,
                ent,
                f"Page no longer in sitemap of {ent.domain}: {u}",
                before={"url": u, "kind": before[u]},
                refs=b.evidence_refs[:1],
                conf=0.75,
                salt=u,
            )

    # -------------------------------------------------------------- content
    def _content(self, prev: Snapshot, cur: Snapshot, ent: Entity, mk) -> None:
        if not (_ok(prev, ent.entity_id, "content") and _ok(cur, ent.entity_id, "content")):
            return
        a = _obs(prev, ent.entity_id, ObservationType.CONTENT_INVENTORY)
        b = _obs(cur, ent.entity_id, ObservationType.CONTENT_INVENTORY)
        if not a or not b:
            return
        old = {i.get("url") for i in a.items}
        new = [i for i in b.items if i.get("url") not in old]
        if new and a.items:
            mk(
                ChangeType.NEW_ARTICLE,
                ent,
                f"{len(new)} new article(s) on {ent.domain} since last snapshot, e.g. "
                f'"{str(new[0].get("title") or new[0].get("url") or "")[:100]}"',
                after=[i.get("url") for i in new[:10]],
                refs=b.evidence_refs[:3],
                salt="articles",
            )
        pa, pb = a.metrics.get("articles_last_60d"), b.metrics.get("articles_last_60d")
        if isinstance(pa, (int, float)) and isinstance(pb, (int, float)):
            if pb >= pa + 2 and pb >= pa * 1.25:
                mk(
                    ChangeType.CONTENT_ACTIVITY_INCREASED,
                    ent,
                    f"Articles in last 60 days rose from {int(pa)} to {int(pb)}",
                    before=pa,
                    after=pb,
                    refs=b.evidence_refs[:1],
                    salt="c60",
                )
            elif pa >= pb + 2 and pa >= pb * 1.25:
                mk(
                    ChangeType.CONTENT_ACTIVITY_DECREASED,
                    ent,
                    f"Articles in last 60 days fell from {int(pa)} to {int(pb)}",
                    before=pa,
                    after=pb,
                    refs=b.evidence_refs[:1],
                    salt="c60",
                )

    # ----------------------------------------------------------------- meta
    def _meta(self, prev: Snapshot, cur: Snapshot, ent: Entity, mk) -> None:
        if not (_ok(prev, ent.entity_id, "meta_ads") and _ok(cur, ent.entity_id, "meta_ads")):
            return
        a = _obs(prev, ent.entity_id, ObservationType.ADS_META)
        b = _obs(cur, ent.entity_id, ObservationType.ADS_META)
        if not a or not b:
            return
        if a.metrics.get("coverage_reliable") != b.metrics.get("coverage_reliable"):
            return  # different coverage -> not comparable
        ids_a = {i["ad_id"] for i in a.items if i.get("ad_id")}
        ids_b = {i["ad_id"] for i in b.items if i.get("ad_id")}
        new = ids_b - ids_a
        gone = ids_a - ids_b
        if new:
            mk(
                ChangeType.NEW_AD_CREATIVE,
                ent,
                f"{len(new)} new active Meta creative(s) observed",
                after=sorted(new)[:10],
                refs=b.evidence_refs[:3],
                salt="meta_new",
            )
        if gone and b.metrics.get("coverage_reliable") == 1:
            mk(
                ChangeType.REMOVED_AD_CREATIVE,
                ent,
                f"{len(gone)} Meta creative(s) no longer active",
                before=sorted(gone)[:10],
                refs=b.evidence_refs[:1],
                conf=0.8,
                salt="meta_gone",
            )
        ca, cb = a.metrics.get("active_creative_count"), b.metrics.get("active_creative_count")
        if isinstance(ca, (int, float)) and isinstance(cb, (int, float)):
            if cb >= ca + 2 and cb >= ca * 1.2:
                mk(
                    ChangeType.AD_ACTIVITY_INCREASED,
                    ent,
                    f"Active Meta creatives rose from {int(ca)} to {int(cb)}",
                    before=ca,
                    after=cb,
                    refs=b.evidence_refs[:1],
                    salt="meta_cnt",
                )
            elif ca >= cb + 2 and ca >= cb * 1.2 and b.metrics.get("coverage_reliable") == 1:
                mk(
                    ChangeType.AD_ACTIVITY_DECREASED,
                    ent,
                    f"Active Meta creatives fell from {int(ca)} to {int(cb)}",
                    before=ca,
                    after=cb,
                    refs=b.evidence_refs[:1],
                    salt="meta_cnt",
                )

    def _url_topics(self, url: str) -> list[str]:
        if not self.topics:
            return []
        path = urlsplit(url).path.replace("-", " ").replace("_", " ").replace("/", " ")
        return topic_matches(path, self.topics)

    # ------------------------------------------------------- CTA / services
    def _profile(self, prev: Snapshot, cur: Snapshot, ent: Entity, mk) -> None:
        if not (_ok(prev, ent.entity_id, "website") and _ok(cur, ent.entity_id, "website")):
            return
        a = _obs(prev, ent.entity_id, ObservationType.WEBSITE_COMPANY_PROFILE)
        b = _obs(cur, ent.entity_id, ObservationType.WEBSITE_COMPANY_PROFILE)
        if not a or not b or not a.items or not b.items:
            return
        pa, pb = a.items[0], b.items[0]
        ca, cb = set(pa.get("ctas") or []), set(pb.get("ctas") or [])
        if ca and cb and ca != cb:
            added, removed = sorted(cb - ca), sorted(ca - cb)
            mk(
                ChangeType.CTA_CHANGED,
                ent,
                f"Calls-to-action changed on {ent.domain}: +{added[:4]} -{removed[:4]}",
                before=sorted(ca)[:12],
                after=sorted(cb)[:12],
                refs=b.evidence_refs[:2],
                conf=0.8,
                salt="ctas",
            )
        for field in ("services", "products"):
            sa, sb = set(pa.get(field) or []), set(pb.get(field) or [])
            if sa and sb and sa != sb:
                added, removed = sorted(sb - sa), sorted(sa - sb)
                mk(
                    ChangeType.SERVICES_CHANGED,
                    ent,
                    f"{field.capitalize()} listed on {ent.domain} changed: added {added[:4]}, removed {removed[:4]}",
                    before=sorted(sa)[:20],
                    after=sorted(sb)[:20],
                    refs=b.evidence_refs[:2],
                    conf=0.75,
                    salt=field,
                )

    # --------------------------------------------------------------- google
    def _google(self, prev: Snapshot, cur: Snapshot, ent: Entity, mk) -> None:
        if not (_ok(prev, ent.entity_id, "google_ads") and _ok(cur, ent.entity_id, "google_ads")):
            return
        a = _obs(prev, ent.entity_id, ObservationType.ADS_GOOGLE)
        b = _obs(cur, ent.entity_id, ObservationType.ADS_GOOGLE)
        if not a or not b:
            return
        ids_a = {i["creative_id"] for i in a.items if i.get("creative_id")}
        ids_b = {i["creative_id"] for i in b.items if i.get("creative_id")}
        if ids_b - ids_a:
            mk(
                ChangeType.NEW_AD_CREATIVE,
                ent,
                f"{len(ids_b - ids_a)} new Google ad creative(s) listed",
                after=sorted(ids_b - ids_a)[:10],
                refs=b.evidence_refs[:3],
                conf=0.8,
                salt="google_new",
            )
        if ids_a - ids_b:
            mk(
                ChangeType.REMOVED_AD_CREATIVE,
                ent,
                f"{len(ids_a - ids_b)} Google ad creative(s) no longer listed",
                before=sorted(ids_a - ids_b)[:10],
                refs=b.evidence_refs[:1],
                conf=0.7,
                salt="google_gone",
            )
        ca, cb = a.metrics.get("shown_last_30d"), b.metrics.get("shown_last_30d")
        if isinstance(ca, (int, float)) and isinstance(cb, (int, float)):
            if cb >= ca + 2 and cb >= ca * 1.2:
                mk(
                    ChangeType.AD_ACTIVITY_INCREASED,
                    ent,
                    f"Google creatives shown in last 30 days rose from {int(ca)} to {int(cb)}",
                    before=ca,
                    after=cb,
                    refs=b.evidence_refs[:1],
                    conf=0.8,
                    salt="google_cnt",
                )
            elif ca >= cb + 2 and ca >= cb * 1.2:
                mk(
                    ChangeType.AD_ACTIVITY_DECREASED,
                    ent,
                    f"Google creatives shown in last 30 days fell from {int(ca)} to {int(cb)}",
                    before=ca,
                    after=cb,
                    refs=b.evidence_refs[:1],
                    conf=0.75,
                    salt="google_cnt",
                )

    # -------------------------------------------------------------- twitter
    def _twitter(self, prev: Snapshot, cur: Snapshot, ent: Entity, mk) -> None:
        if not (_ok(prev, ent.entity_id, "twitter") and _ok(cur, ent.entity_id, "twitter")):
            return
        a = _obs(prev, ent.entity_id, ObservationType.SOCIAL_TWITTER)
        b = _obs(cur, ent.entity_id, ObservationType.SOCIAL_TWITTER)
        if not a or not b:
            return
        pa, pb = a.metrics.get("posts_last_30d"), b.metrics.get("posts_last_30d")
        if isinstance(pa, (int, float)) and isinstance(pb, (int, float)):
            if pb >= pa + 3 and pb >= pa * 1.3:
                mk(
                    ChangeType.SOCIAL_ACTIVITY_INCREASED,
                    ent,
                    f"X posts in last 30 days rose from {int(pa)} to {int(pb)}",
                    before=pa,
                    after=pb,
                    refs=b.evidence_refs[:1],
                    salt="x_cnt",
                )
            elif pa >= pb + 3 and pa >= pb * 1.3:
                mk(
                    ChangeType.SOCIAL_ACTIVITY_DECREASED,
                    ent,
                    f"X posts in last 30 days fell from {int(pa)} to {int(pb)}",
                    before=pa,
                    after=pb,
                    refs=b.evidence_refs[:1],
                    salt="x_cnt",
                )

    # --------------------------------------------------------------- offers
    def _offers(self, prev: Snapshot, cur: Snapshot, ent: Entity, mk) -> None:
        if not (_ok(prev, ent.entity_id, "website") and _ok(cur, ent.entity_id, "website")):
            return
        a = _obs(prev, ent.entity_id, ObservationType.WEBSITE_COMPANY_PROFILE)
        b = _obs(cur, ent.entity_id, ObservationType.WEBSITE_COMPANY_PROFILE)
        if not a or not b or not a.items or not b.items:
            return
        old = set(a.items[0].get("offers") or [])
        new = [o for o in (b.items[0].get("offers") or []) if o not in old]
        for o in new[:5]:
            mk(
                ChangeType.NEW_OFFER,
                ent,
                f'New offer language on {ent.domain}: "{o[:160]}"',
                after=o,
                refs=b.evidence_refs[:2],
                conf=0.8,
                salt=o,
            )
        old_prices = set(a.items[0].get("prices") or [])
        new_prices = set(b.items[0].get("prices") or [])
        if old_prices and new_prices and old_prices != new_prices:
            mk(
                ChangeType.NEW_OFFER,
                ent,
                f"Published prices changed on {ent.domain}",
                before=sorted(old_prices)[:10],
                after=sorted(new_prices)[:10],
                refs=b.evidence_refs[:2],
                salt="prices",
            )
