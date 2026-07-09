"""Fetch latest tweets from @CrunchyrollMENA via Twitter's internal GraphQL API."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

log = logging.getLogger(__name__)

ACCOUNT        = "CrunchyrollMENA"
ACCOUNT_ID     = "3095710434"          # Stable numeric ID for @CrunchyrollMENA
X_URL          = f"https://x.com/{ACCOUNT}"
CACHE_TTL      = 300                   # 5 minutes — matches the polling interval
GUEST_TTL      = 10800                 # Guest tokens last ~3 hours
TWEET_MAX_AGE  = timedelta(days=365)   # Ignore tweets older than 1 year
_TWITTER_EPOCH = 1288834974657         # ms — Twitter's Snowflake epoch

# Twitter's public bearer token (used by Twitter's own web client)
_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# GraphQL endpoint IDs (Twitter's own internal IDs used by twitter.com/x.com)
_GQL_USER_TWEETS = "V7H0Ap3_Hh2FyS75OCDO3Q"

_TWEET_FEATURES = {
    "rweb_lists_timeline_redesign_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "tweetypie_unmention_optimization_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": False,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
    "rweb_tipjar_consumption_enabled": True,
    "subscriptions_verification_info_verified_since_enabled": True,
    "highlights_tweets_tab_ui_enabled": True,
    "hidden_profile_subscriptions_enabled": True,
    "responsive_web_twitter_article_notes_tab_enabled": True,
    "subscriptions_feature_can_gift_premium": False,
}

# In-memory state
_guest_token: Optional[str] = None
_guest_token_at: float = 0.0
_cache: dict[str, tuple[float, list["Tweet"]]] = {}


@dataclass
class Tweet:
    tweet_id:  str
    text:      str
    url:       str
    images:    list[str] = field(default_factory=list)
    timestamp: str = ""


def _clean_text(text: str) -> str:
    text = re.sub(r"\s*https://t\.co/\S+", "", text)
    return text.strip()


def _make_headers(guest_token: str) -> dict:
    return {
        "Authorization": f"Bearer {_BEARER}",
        "x-guest-token": guest_token,
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
        "Referer": "https://twitter.com/",
        "Origin": "https://twitter.com",
    }


def _get_guest_token() -> Optional[str]:
    """Fetch (or return cached) a Twitter guest token from api.x.com."""
    global _guest_token, _guest_token_at

    if _guest_token and (time.time() - _guest_token_at) < GUEST_TTL:
        return _guest_token

    try:
        resp = requests.post(
            "https://api.x.com/1.1/guest/activate.json",
            headers={
                "Authorization": f"Bearer {_BEARER}",
                "User-Agent": (
                    "TwitterAndroid/10.21.0 (29170007-r-0) "
                    "ONEPLUS+A3003/9 (OnePlus;ONEPLUS+A3003;OnePlus;OnePlus3;0;;1;2016)"
                ),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=15,
        )
        resp.raise_for_status()
        token = resp.json().get("guest_token")
        if token:
            _guest_token = token
            _guest_token_at = time.time()
            log.info("[news] new guest token acquired")
            return _guest_token
    except Exception as exc:
        log.error("[news] failed to get guest token: %s", exc)

    return None


def _tweet_date(tweet_id: str) -> Optional[datetime]:
    """Derive a UTC datetime from a Twitter Snowflake ID."""
    try:
        ts_ms = (int(tweet_id) >> 22) + _TWITTER_EPOCH
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    except Exception:
        return None


def _extract_tweets_from_graphql(data: dict) -> list[Tweet]:
    """Parse tweet entries from a UserTweets GraphQL response."""
    tweets: list[Tweet] = []
    cutoff = datetime.now(tz=timezone.utc) - TWEET_MAX_AGE

    instructions = (
        data.get("data", {})
            .get("user", {})
            .get("result", {})
            .get("timeline_v2", {})
            .get("timeline", {})
            .get("instructions", [])
    )

    for instruction in instructions:
        for entry in instruction.get("entries", []):
            item_content = entry.get("content", {}).get("itemContent", {})
            tweet_result = item_content.get("tweet_results", {}).get("result", {})
            if not tweet_result:
                continue

            legacy = tweet_result.get("legacy", {})
            tweet_id  = legacy.get("id_str", "")
            full_text = _clean_text(legacy.get("full_text", legacy.get("text", "")))
            timestamp = legacy.get("created_at", "")

            if not tweet_id:
                continue

            # Skip tweets older than TWEET_MAX_AGE
            dt = _tweet_date(tweet_id)
            if dt and dt < cutoff:
                continue

            tweet_url = f"https://x.com/{ACCOUNT}/status/{tweet_id}"

            images: list[str] = []
            for media in legacy.get("extended_entities", {}).get("media", []):
                img = media.get("media_url_https") or media.get("media_url", "")
                if img and img not in images:
                    images.append(img)
            for media in legacy.get("entities", {}).get("media", []):
                img = media.get("media_url_https") or media.get("media_url", "")
                if img and img not in images:
                    images.append(img)

            if not full_text and not images:
                continue

            tweets.append(Tweet(
                tweet_id=tweet_id,
                text=full_text,
                url=tweet_url,
                images=images,
                timestamp=timestamp,
            ))

    # Sort newest-first (larger ID = newer tweet)
    tweets.sort(key=lambda t: int(t.tweet_id) if t.tweet_id.isdigit() else 0, reverse=True)
    return tweets


def _fetch_via_graphql(limit: int = 20) -> Optional[list[Tweet]]:
    """Fetch user tweets via Twitter's internal GraphQL API using a guest token."""
    global _guest_token, _guest_token_at

    token = _get_guest_token()
    if not token:
        return None

    variables = json.dumps({
        "userId": ACCOUNT_ID,
        "count": min(limit * 2, 100),
        "includePromotedContent": False,
        "withQuickPromoteEligibilityTweetFields": True,
        "withVoice": True,
        "withV2Timeline": True,
    })

    try:
        resp = requests.get(
            f"https://api.x.com/graphql/{_GQL_USER_TWEETS}/UserTweets",
            params={
                "variables": variables,
                "features": json.dumps(_TWEET_FEATURES),
            },
            headers=_make_headers(token),
            timeout=20,
        )

        if resp.status_code in (401, 403):
            # Guest token expired or rejected — invalidate and retry once
            log.warning("[news] guest token rejected (%s), refreshing", resp.status_code)
            _guest_token = None
            _guest_token_at = 0.0
            new_token = _get_guest_token()
            if not new_token:
                return None
            resp = requests.get(
                f"https://api.x.com/graphql/{_GQL_USER_TWEETS}/UserTweets",
                params={
                    "variables": variables,
                    "features": json.dumps(_TWEET_FEATURES),
                },
                headers=_make_headers(new_token),
                timeout=20,
            )

        if resp.status_code == 429:
            log.warning("[news] rate-limited (429) on GraphQL endpoint")
            return None

        resp.raise_for_status()
        data = resp.json()
        tweets = _extract_tweets_from_graphql(data)
        log.info("[news] GraphQL returned %d tweets", len(tweets))
        return tweets

    except Exception as exc:
        log.error("[news] GraphQL fetch failed: %s", exc)
        return None


def get_cached_tweets(limit: int = 10) -> list[Tweet]:
    """Return cached tweets without making an API call."""
    _, cached = _cache.get(ACCOUNT, (0.0, []))
    return cached[:limit]


def fetch_latest_tweets(limit: int = 10) -> list[Tweet]:
    """Fetch latest tweets from @CrunchyrollMENA (GraphQL API, with cache)."""
    cached_at, cached_tweets = _cache.get(ACCOUNT, (0.0, []))
    if cached_tweets and (time.time() - cached_at) < CACHE_TTL:
        log.info("[news] returning %d cached tweets", len(cached_tweets))
        return cached_tweets[:limit]

    tweets = _fetch_via_graphql(limit=max(limit, 20))

    if tweets:
        _cache[ACCOUNT] = (time.time(), tweets)
        return tweets[:limit]

    # Return stale cache rather than empty on failure
    if cached_tweets:
        log.warning("[news] using stale cache (%d tweets)", len(cached_tweets))
    return cached_tweets[:limit]
