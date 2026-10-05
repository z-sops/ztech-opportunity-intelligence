"""Social Intelligence Providers (spec #13, #14).

LinkedInProvider — UNSUPPORTED by design. LinkedIn's User Agreement prohibits
  scraping/automation and its official APIs (Marketing, Community Management)
  only expose data for pages the caller administers. The engine does not design
  around that. The interface is kept so an authorised integration can be added.
  (A LinkedIn company URL found on the website is still recorded by the
  website provider as a social-profile FACT.)

TwitterProvider — OFFICIAL X API v2 with X_BEARER_TOKEN (paid tiers grant
  read access). The account handle is taken from the entity's own website
  (website provider) — never guessed. Without a token or a handle the provider
  is UNAVAILABLE.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import timedelta
from typing import Any

from ..domain.errors import FetchError, UnsafeTarget
from ..domain.identity import iso, parse_iso, utcnow
from ..domain.models import ProviderResult
from ..domain.taxonomy import ClaimKind, ErrorCode, ObservationType, ProviderStatus
from ..net.http import SafeHttpClient
from ..net.parsing import keywords, topic_matches, twitter_handle
from .base import ResearchRequest, ResultBuilder, simple_result


class LinkedInProvider:
    name = "linkedin"
    source_type = "linkedin"
    scope = "per_entity"

    async def collect(self, request: ResearchRequest) -> ProviderResult:
        return simple_result(
            self.name,
            self.source_type,
            request,
            ProviderStatus.UNSUPPORTED,
            ErrorCode.UNSUPPORTED_SOURCE,
            "LinkedIn provider: UNSUPPORTED — no legitimate programmatic source for third-party company activity",
            [
                "LinkedIn provider: UNSUPPORTED. Company activity, hiring posts, job changes and announcements are not collected.",
                "Reason: LinkedIn prohibits scraping/automation; official APIs only cover pages the caller administers.",
            ],
        )


class TwitterProvider:
    name = "twitter"
    source_type = "x_api_v2"
    scope = "per_entity"
    BASE = "https://api.x.com/2"

    def __init__(self, http: SafeHttpClient, bearer_token: str | None, *, max_posts: int = 50) -> None:
        self.http = http
        self._token = bearer_token
        self.max_posts = max(5, min(max_posts, 100))

    async def collect(self, request: ResearchRequest) -> ProviderResult:
        if not self._token:
            return simple_result(
                self.name,
                self.source_type,
                request,
                ProviderStatus.UNAVAILABLE,
                ErrorCode.PROVIDER_NOT_CONFIGURED,
                "X API not configured (set X_BEARER_TOKEN)",
                ["X/Twitter provider: NOT CONFIGURED — no X data was collected."],
            )
        handle = twitter_handle((request.context.get("social_profiles") or {}).get("x"))
        if not handle:
            return simple_result(
                self.name,
                self.source_type,
                request,
                ProviderStatus.UNAVAILABLE,
                ErrorCode.INSUFFICIENT_EVIDENCE,
                "no X/Twitter account linked from the entity's website; handle is never guessed",
            )
        b = ResultBuilder(self.name, self.source_type, request)
        headers = {"Authorization": f"Bearer {self._token}"}
        try:
            u = await self.http.get(
                f"{self.BASE}/users/by/username/{handle}",
                params={"user.fields": "public_metrics,created_at,verified"},
                headers=headers,
            )
            if u.status in (401, 402, 403):
                b.error(ErrorCode.PROVIDER_NOT_CONFIGURED, f"X API denied access (HTTP {u.status}; tier may not include reads)")
                return b.build(ProviderStatus.UNAVAILABLE)
            payload = u.json() if u.ok else None
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict) or "id" not in data:
                b.error(ErrorCode.COMPANY_NOT_FOUND, f"X account @{handle} not found")
                return b.build(ProviderStatus.UNAVAILABLE)
            user: dict[str, Any] = data
            t = await self.http.get(
                f"{self.BASE}/users/{user['id']}/tweets",
                params={
                    "max_results": str(self.max_posts),
                    "tweet.fields": "created_at,public_metrics,in_reply_to_user_id,referenced_tweets",
                    "exclude": "retweets",
                },
                headers=headers,
            )
            tweets = (t.json() or {}).get("data") or [] if t.ok else []
        except (FetchError, UnsafeTarget) as e:
            b.error(e.code, f"X API request failed: {e.message}")
            return b.build(ProviderStatus.RATE_LIMITED if e.code is ErrorCode.RATE_LIMITED else ProviderStatus.UNAVAILABLE)
        if not isinstance(tweets, list):
            b.error(ErrorCode.MALFORMED_RESPONSE, "X API tweets payload malformed")
            tweets = []
        now = utcnow()
        topics = request.products_services
        last30 = replies30 = 0
        kinds: Counter[str] = Counter()
        likes: list[int] = []
        texts: list[str] = []
        relevant30 = 0
        items: list[dict] = []
        refs: list[str] = []
        for tw in tweets[: self.max_posts]:
            if not isinstance(tw, dict):
                continue
            created = parse_iso(str(tw.get("created_at") or ""))
            recent = bool(created and now - created <= timedelta(days=30))
            text = str(tw.get("text", ""))[:280]
            is_reply = bool(tw.get("in_reply_to_user_id")) or any(
                isinstance(r, dict) and r.get("type") == "replied_to" for r in (tw.get("referenced_tweets") or [])
            )
            raw_pm = tw.get("public_metrics")
            pm: dict[str, Any] = raw_pm if isinstance(raw_pm, dict) else {}
            labels = classify_post(text, is_reply)
            post_topics = topic_matches(text, topics) if topics else []
            if recent:
                if is_reply:
                    replies30 += 1
                else:
                    last30 += 1
                    kinds.update(labels)
                    relevant30 += 1 if post_topics else 0
            if not is_reply:
                texts.append(text)
                if isinstance(pm.get("like_count"), int):
                    likes.append(pm["like_count"])
            items.append(
                {
                    "id": tw.get("id"),
                    "created_at": iso(created) if created else None,
                    "text": text,
                    "is_reply": is_reply,
                    "labels": labels,
                    "topics": post_topics,
                    "likes": pm.get("like_count"),
                    "reposts": pm.get("retweet_count"),
                    "replies": pm.get("reply_count"),
                }
            )
            if len(refs) < 20 and not is_reply:
                refs.append(
                    b.add_evidence(
                        observation_type=ObservationType.SOCIAL_TWITTER,
                        claim=f'@{handle} posted on {iso(created)[:10] if created else "?"}: "{text[:120]}"',
                        source_ref=f"tweet:{tw.get('id')}",
                        source_url=f"https://x.com/{handle}/status/{tw.get('id')}",
                        confidence=0.95,
                        observed_at=iso(created) if created else None,
                        raw={"labels": labels, "topics": post_topics},
                    )
                )
        raw_upm = user.get("public_metrics")
        pm_user: dict[str, Any] = raw_upm if isinstance(raw_upm, dict) else {}
        summary = b.add_evidence(
            observation_type=ObservationType.SOCIAL_TWITTER,
            claim=(
                f"@{handle}: {last30} original post(s) and {replies30} repl(ies) in the last 30 days among {len(items)} retrieved; "
                f"{pm_user.get('followers_count', '?')} followers"
            ),
            source_ref=f"x_summary:{handle}",
            source_url=f"https://x.com/{handle}",
            confidence=0.95,
            metric="posts_last_30d",
            value=last30,
        )
        class_ref = b.add_evidence(
            observation_type=ObservationType.SOCIAL_TWITTER,
            claim=(
                f"Rule-based labels for @{handle}'s last-30-day posts: "
                + (", ".join(f"{k}={v}" for k, v in sorted(kinds.items())) or "none")
            ),
            source_ref=f"x_labels:{handle}",
            source_type="rule_classification",
            claim_kind=ClaimKind.INFERENCE,
            confidence=0.7,
            raw={"labels": dict(kinds), "method": "keyword rules on observed post text"},
        )
        if len(items) >= self.max_posts:
            b.limit(f"Only the latest {self.max_posts} posts were retrieved; older activity not counted.")
        b.limit("Post labels (announcement, product_launch, offer, event, hiring) are rule-based INFERENCES over observed text.")
        b.add_observation(
            ObservationType.SOCIAL_TWITTER,
            metrics={
                "posts_retrieved": len(items),
                "posts_last_30d": last30,
                "replies_last_30d": replies30,
                "announcements_last_30d": kinds.get("announcement", 0),
                "product_launch_posts_last_30d": kinds.get("product_launch", 0),
                "offer_posts_last_30d": kinds.get("offer", 0),
                "relevant_posts_last_30d": relevant30 if topics else None,
                "followers": pm_user.get("followers_count"),
                "avg_likes_per_post": round(sum(likes) / len(likes), 1) if likes else None,
            },
            items=[{"handle": handle, "themes": _post_themes(texts)}, *items],
            evidence_refs=[summary, class_ref, *refs],
        )
        return b.build()


_POST_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "product_launch",
        re.compile(
            r"\b(introducing|launch(ed|ing)?|now available|just dropped|meet the new|new (product|menu|feature|collection))\b",
            re.I,
        ),
    ),
    (
        "announcement",
        re.compile(r"\b(announc\w*|excited to (share|announce)|we('| a)re (thrilled|proud)|big news|update:)", re.I),
    ),
    ("offer", re.compile(r"(\d{1,2}\s?% off|discount|promo|sale|free (shipping|delivery|trial)|limited[- ]time|deal)", re.I)),
    ("event", re.compile(r"\b(join us|webinar|event|live (on|at)|this (weekend|saturday|sunday))\b", re.I)),
    ("hiring", re.compile(r"\b(we('| a)re hiring|join (our|the) team|open (role|position)s?)\b", re.I)),
]


def classify_post(text: str, is_reply: bool) -> list[str]:
    """Deterministic, rule-based post labels (INFERENCE). Replies are customer conversations."""
    if is_reply:
        return ["customer_conversation"]
    return [label for label, pat in _POST_RULES if pat.search(text or "")] or ["general"]


def _post_themes(texts: list[str], top: int = 8) -> list[dict]:
    c: Counter[str] = Counter()
    for t in texts:
        c.update({w for w in keywords(t) if not w.startswith(("http", "co/"))})
    return [{"term": k, "posts": v} for k, v in c.most_common(top) if v >= 2]
