"""
insta_unfollow.py — Instagram cleanup daemon

Dynamically fetches followers/following via the instagrapi API and:
  1. Unfollows users who don't follow you back (after a 1-2 day grace period)
  2. Withdraws pending outgoing follow requests older than 3 days
     (read from mounted Instagram data-export JSON)

Designed to run continuously inside a Docker container on a configurable
cycle (default: every 48 hours).

Rate limits are tuned conservatively for newer / low-trust accounts
(~30-50 unfollows per day, 30-60s between actions, long breaks every 5-8
actions).  Adjust via environment variables.
"""

import json
import logging
import os
import random
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from instagrapi import Client
from instagrapi.exceptions import (
    ChallengeRequired,
    ClientError,
    ClientThrottledError,
    LoginRequired,
    PleaseWaitFewMinutes,
    RateLimitError,
)

# ──────────────────────────────────────────────
# Graceful shutdown
# ──────────────────────────────────────────────
_shutdown_requested = False


def _signal_handler(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    logger.info(f"Received signal {signum} — will shut down after current action")


signal.signal(signal.SIGTERM, _signal_handler)
signal.signal(signal.SIGINT, _signal_handler)

# ──────────────────────────────────────────────
# Configuration (all overridable via env vars)
# ──────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent

# Paths
DATA_DIR = Path(os.getenv("DATA_DIR", str(PROJECT_ROOT / "data")))
SESSION_FILE = Path(os.getenv("SESSION_FILE", str(PROJECT_ROOT / "session.json")))
PROGRESS_FILE = Path(os.getenv("PROGRESS_FILE", str(PROJECT_ROOT / "progress.json")))
ENV_FILE = PROJECT_ROOT / ".env"

# Mode
DRY_RUN = os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes")

# Rate limits — conservative defaults for new accounts (< 6 months)
MAX_UNFOLLOWS_PER_CYCLE = int(os.getenv("MAX_UNFOLLOWS_PER_CYCLE", "30"))
MAX_WITHDRAWALS_PER_CYCLE = int(os.getenv("MAX_WITHDRAWALS_PER_CYCLE", "15"))

# Timing (seconds)
DELAY_MIN = int(os.getenv("DELAY_MIN", "30"))
DELAY_MAX = int(os.getenv("DELAY_MAX", "60"))
LONG_BREAK_MIN = int(os.getenv("LONG_BREAK_MIN", "300"))   # 5 min
LONG_BREAK_MAX = int(os.getenv("LONG_BREAK_MAX", "600"))   # 10 min
ACTIONS_BEFORE_BREAK = int(os.getenv("ACTIONS_BEFORE_BREAK", "5"))
RATE_LIMIT_SLEEP = int(os.getenv("RATE_LIMIT_SLEEP", "900"))  # 15 min

# Cycle interval — how often to re-fetch and run cleanup (seconds)
CYCLE_INTERVAL_HOURS = float(os.getenv("CYCLE_INTERVAL_HOURS", "48"))
CYCLE_INTERVAL = int(CYCLE_INTERVAL_HOURS * 3600)

# Grace periods
PENDING_REQUEST_STALE_DAYS = int(os.getenv("PENDING_REQUEST_STALE_DAYS", "3"))
NON_FOLLOWER_GRACE_DAYS = int(os.getenv("NON_FOLLOWER_GRACE_DAYS", "2"))

# Pending requests JSON file (mounted from Instagram data export)
PENDING_REQUESTS_FILE = Path(
    os.getenv(
        "PENDING_REQUESTS_FILE",
        str(DATA_DIR / "connections" / "followers_and_following" / "pending_follow_requests.json"),
    )
)

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
LOG_FILE = Path(os.getenv("LOG_FILE", str(PROJECT_ROOT / "actions.log")))

logger = logging.getLogger("insta_unfollow")
logger.setLevel(logging.DEBUG)

# File handler — detailed log
fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
fh.setLevel(logging.DEBUG)
fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s"))

# Console handler — user-facing
ch = logging.StreamHandler(sys.stdout)
ch.setLevel(logging.INFO)
ch.setFormatter(logging.Formatter("%(asctime)s | %(message)s"))

logger.addHandler(fh)
logger.addHandler(ch)


# ──────────────────────────────────────────────
# Progress / State persistence
# ──────────────────────────────────────────────
def load_progress() -> dict:
    """Load progress state for resume capability.

    State schema:
        unfollowed: list[str]           — usernames we've unfollowed
        withdrawn: list[str]            — usernames whose requests we withdrew
        non_followers_first_seen: dict  — {username: ISO-timestamp} tracking
                                          when a user was first detected as
                                          a non-follower (for grace period)
    """
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Migration: ensure all expected keys exist
        data.setdefault("unfollowed", [])
        data.setdefault("withdrawn", [])
        data.setdefault("non_followers_first_seen", {})
        return data
    return {"unfollowed": [], "withdrawn": [], "non_followers_first_seen": {}}


def save_progress(progress: dict) -> None:
    """Persist progress state to disk."""
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2)


# ──────────────────────────────────────────────
# Anti-bot helpers
# ──────────────────────────────────────────────
def human_delay() -> None:
    """Random delay between actions to mimic human behavior."""
    delay = random.uniform(DELAY_MIN, DELAY_MAX)
    logger.debug(f"Sleeping {delay:.1f}s between actions")
    time.sleep(delay)


def long_break() -> None:
    """Longer break after a batch of actions."""
    delay = random.uniform(LONG_BREAK_MIN, LONG_BREAK_MAX)
    logger.info(f"☕ Long break: {delay:.0f}s after {ACTIONS_BEFORE_BREAK} actions")
    time.sleep(delay)


def interruptible_sleep(seconds: int) -> None:
    """Sleep in 1-second increments so we can honour shutdown signals."""
    for _ in range(seconds):
        if _shutdown_requested:
            return
        time.sleep(1)


# ──────────────────────────────────────────────
# Instagram client setup
# ──────────────────────────────────────────────
def get_client(username: str, password: str) -> Client:
    """Create and authenticate an instagrapi Client, reusing session if available."""
    cl = Client()
    cl.delay_range = [2, 5]  # built-in per-request delay (seconds)

    proxy = os.getenv("PROXY")
    if proxy:
        logger.info(f"Using configured proxy: {proxy.split('@')[-1] if '@' in proxy else proxy}")
        cl.set_proxy(proxy)

    if SESSION_FILE.exists():
        logger.info(f"Loading saved session from {SESSION_FILE}...")
        try:
            cl.load_settings(SESSION_FILE)
            cl.get_timeline_feed()  # test if session is alive without re-submitting login form
            logger.info("Session restored successfully (no login request needed)")
            return cl
        except (LoginRequired, ChallengeRequired) as e:
            logger.warning(f"Saved session expired ({e}), re-authenticating...")
        except Exception as e:
            logger.warning(f"Could not restore session ({e}), attempting fresh login...")

    logger.info("Performing fresh login...")
    try:
        cl.login(username, password)
        cl.dump_settings(SESSION_FILE)
        logger.info("Logged in and session saved")
        return cl
    except ClientThrottledError as e:
        logger.error(
            "❌ Instagram blocked/throttled the fresh login request (HTTP 429 Too Many Requests).\n"
            "   Cloud datacenter IPs (Oracle Cloud, AWS, GCP, etc.) are blocked by Instagram from performing fresh password logins.\n"
            "   HOW TO FIX:\n"
            "   1. You already have an authenticated 'session.json' on your local computer.\n"
            "   2. Copy your local 'session.json' into the 'state/' folder on your server: '~/insta-unfollow/state/session.json'\n"
            "   3. Restart the container: 'docker compose restart'\n"
            "   The container will use your valid session cookies and won't need to call the login endpoint!"
        )
        raise


def save_session(cl: Client) -> None:
    """Persist session to disk."""
    cl.dump_settings(SESSION_FILE)
    logger.debug("Session saved")


# ──────────────────────────────────────────────
# Dynamic data fetching via API
# ──────────────────────────────────────────────
def fetch_followers(cl: Client) -> set[str]:
    """Fetch the authenticated user's followers via the API.

    Returns a set of lowercase usernames.
    """
    user_id = cl.user_id
    logger.info("Fetching followers via API (this may take a while)...")
    followers_dict = cl.user_followers(user_id, amount=0)
    usernames = {u.username.strip().lower() for u in followers_dict.values()}
    logger.info(f"Fetched {len(usernames)} followers")
    return usernames


def fetch_following(cl: Client) -> set[str]:
    """Fetch the list of accounts the authenticated user is following.

    Returns a set of lowercase usernames.
    """
    user_id = cl.user_id
    logger.info("Fetching following via API (this may take a while)...")
    following_dict = cl.user_following(user_id, amount=0)
    usernames = {u.username.strip().lower() for u in following_dict.values()}
    logger.info(f"Fetched {len(usernames)} following")
    return usernames


# ──────────────────────────────────────────────
# Pending requests (from mounted data-export JSON)
# ──────────────────────────────────────────────
def load_pending_requests(stale_days: int = 3) -> list[dict]:
    """Extract pending outgoing follow requests older than `stale_days`.

    Reads from the Instagram data-export JSON file if available.
    """
    if not PENDING_REQUESTS_FILE.exists():
        logger.info(
            f"Pending requests file not found at {PENDING_REQUESTS_FILE} — skipping"
        )
        return []

    with open(PENDING_REQUESTS_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
    cutoff_ts = int(cutoff.timestamp())

    stale = []
    for entry in data:
        ts = entry.get("timestamp", 0)
        if ts < cutoff_ts:
            username = None
            for label_val in entry.get("label_values", []):
                if label_val.get("label") == "Username":
                    username = label_val.get("value", "").strip().lower()
                    break
            if username:
                stale.append({"username": username, "timestamp": ts})

    logger.info(
        f"Found {len(stale)} pending requests older than {stale_days} days "
        f"(cutoff: {cutoff.isoformat()})"
    )
    return stale


# ──────────────────────────────────────────────
# Non-follower grace-period logic
# ──────────────────────────────────────────────
def update_non_follower_tracking(
    non_followers: set[str], progress: dict
) -> list[str]:
    """Track when non-followers were first seen and return those past the
    grace period (NON_FOLLOWER_GRACE_DAYS).

    - New non-followers get timestamped now (not unfollowed yet).
    - Non-followers already tracked whose first-seen date is older than
      the grace period are returned for unfollowing.
    - Users who are no longer in the non-followers set are cleaned out of
      tracking (they re-followed you).
    """
    first_seen = progress.setdefault("non_followers_first_seen", {})
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=NON_FOLLOWER_GRACE_DAYS)

    # Add newly discovered non-followers
    new_count = 0
    for username in non_followers:
        if username not in first_seen:
            first_seen[username] = now.isoformat()
            new_count += 1

    # Remove users that are no longer non-followers (they followed back)
    removed = [u for u in list(first_seen.keys()) if u not in non_followers]
    for u in removed:
        del first_seen[u]

    if new_count > 0:
        logger.info(f"📋 Newly detected non-followers: {new_count} (grace period starts)")
    if removed:
        logger.info(f"✅ {len(removed)} users followed back — removed from tracking")

    # Determine who's past the grace period
    ready_to_unfollow = []
    for username, seen_iso in first_seen.items():
        seen_dt = datetime.fromisoformat(seen_iso)
        if seen_dt <= cutoff:
            ready_to_unfollow.append(username)

    logger.info(
        f"Non-followers past {NON_FOLLOWER_GRACE_DAYS}-day grace period: "
        f"{len(ready_to_unfollow)}"
    )
    return ready_to_unfollow


# ──────────────────────────────────────────────
# Core actions
# ──────────────────────────────────────────────
def unfollow_user(cl: Client, username: str, dry_run: bool = True) -> bool:
    """Unfollow a single user. Returns True on success."""
    if dry_run:
        logger.info(f"[DRY RUN] Would unfollow: {username}")
        return True

    try:
        user_id = cl.user_id_from_username(username)
        cl.user_unfollow(user_id)
        logger.info(f"✂️  Unfollowed: {username}")
        return True
    except (RateLimitError, PleaseWaitFewMinutes) as e:
        logger.error(f"Rate limited while unfollowing {username}: {e}")
        raise
    except ChallengeRequired as e:
        logger.error(f"Challenge required while unfollowing {username}: {e}")
        raise
    except Exception as e:
        logger.error(f"Failed to unfollow {username}: {e}")
        return False


def withdraw_request(cl: Client, username: str, dry_run: bool = True) -> bool:
    """Withdraw a pending follow request. Returns True on success."""
    if dry_run:
        logger.info(f"[DRY RUN] Would withdraw request: {username}")
        return True

    try:
        user_id = cl.user_id_from_username(username)
        cl.user_unfollow(user_id)  # unfollow on a pending request = withdraw
        logger.info(f"🗑️  Withdrew follow request: {username}")
        return True
    except (RateLimitError, PleaseWaitFewMinutes) as e:
        logger.error(f"Rate limited while withdrawing request for {username}: {e}")
        raise
    except ChallengeRequired as e:
        logger.error(f"Challenge required while withdrawing {username}: {e}")
        raise
    except Exception as e:
        logger.error(f"Failed to withdraw request for {username}: {e}")
        return False


# ──────────────────────────────────────────────
# Single cycle
# ──────────────────────────────────────────────
def run_cycle(cl: Client | None, ig_username: str, ig_password: str) -> Client | None:
    """Execute one full cleanup cycle.

    Returns the (possibly refreshed) Client instance.
    """
    logger.info("=" * 60)
    logger.info("🔄 Starting cleanup cycle")
    logger.info(f"   Mode: {'DRY RUN' if DRY_RUN else 'LIVE'}")
    logger.info(f"   Max unfollows: {MAX_UNFOLLOWS_PER_CYCLE}")
    logger.info(f"   Max withdrawals: {MAX_WITHDRAWALS_PER_CYCLE}")
    logger.info(f"   Non-follower grace: {NON_FOLLOWER_GRACE_DAYS} days")
    logger.info(f"   Pending request stale: {PENDING_REQUEST_STALE_DAYS} days")
    logger.info("=" * 60)

    # ── Login / refresh session ──
    if not DRY_RUN:
        if cl is None:
            cl = get_client(ig_username, ig_password)
        else:
            # Re-validate existing session
            try:
                cl.get_timeline_feed()
                logger.info("Existing session still valid")
            except Exception:
                logger.info("Session expired, re-logging in...")
                cl = get_client(ig_username, ig_password)
    else:
        logger.info("[DRY RUN] Skipping login")

    # ── Fetch followers / following dynamically ──
    if DRY_RUN:
        logger.info("[DRY RUN] Cannot fetch followers/following without a live session")
        logger.info("[DRY RUN] Cycle skipped — set DRY_RUN=false to run for real")
        return cl

    followers = fetch_followers(cl)
    following = fetch_following(cl)

    # People I follow who don't follow me back
    non_followers = following - followers

    # People who follow me but I don't follow back — NEVER TOUCH THESE
    followers_only = followers - following
    logger.info(f"People following me that I don't follow back: {len(followers_only)} (untouched)")

    logger.info(f"Non-followers detected: {len(non_followers)}")

    # ── Load progress ──
    progress = load_progress()
    already_unfollowed = set(progress.get("unfollowed", []))
    already_withdrawn = set(progress.get("withdrawn", []))

    # ── Apply grace period for non-followers ──
    eligible_to_unfollow = update_non_follower_tracking(non_followers, progress)
    save_progress(progress)  # persist the first-seen timestamps

    # Filter out already-processed users
    to_unfollow = [u for u in eligible_to_unfollow if u not in already_unfollowed]
    random.shuffle(to_unfollow)
    to_unfollow = to_unfollow[:MAX_UNFOLLOWS_PER_CYCLE]

    # ── Pending requests (from JSON file) ──
    stale_requests = load_pending_requests(stale_days=PENDING_REQUEST_STALE_DAYS)
    stale_request_usernames = [r["username"] for r in stale_requests]
    to_withdraw = [u for u in stale_request_usernames if u not in already_withdrawn]
    random.shuffle(to_withdraw)
    to_withdraw = to_withdraw[:MAX_WITHDRAWALS_PER_CYCLE]

    logger.info(f"This cycle: {len(to_unfollow)} unfollows, {len(to_withdraw)} withdrawals")

    # ── Counters ──
    unfollowed_count = 0
    withdrawn_count = 0
    skipped_count = 0
    total_actions = 0
    rate_limited = False

    # ── Phase 1: Unfollow non-followers past grace period ──
    logger.info("-" * 40)
    logger.info("Phase 1: Unfollowing non-followers (past grace period)")
    logger.info("-" * 40)

    for username in to_unfollow:
        if _shutdown_requested or rate_limited:
            skipped_count += 1
            continue

        try:
            success = unfollow_user(cl, username, dry_run=DRY_RUN)
            if success:
                unfollowed_count += 1
                progress["unfollowed"].append(username)
                # Remove from first-seen tracking since we've dealt with them
                progress["non_followers_first_seen"].pop(username, None)
                save_progress(progress)

            total_actions += 1

            if total_actions % ACTIONS_BEFORE_BREAK == 0:
                long_break()
            else:
                human_delay()

        except (RateLimitError, PleaseWaitFewMinutes, ChallengeRequired, ClientThrottledError) as e:
            logger.error(f"⚠️  Rate limit / challenge hit: {e}")
            logger.info(f"Sleeping {RATE_LIMIT_SLEEP}s before continuing...")
            skipped_count += len(to_unfollow) - unfollowed_count - skipped_count
            save_session(cl)
            interruptible_sleep(RATE_LIMIT_SLEEP)
            rate_limited = True

    # ── Phase 2: Withdraw stale pending requests ──
    logger.info("-" * 40)
    logger.info("Phase 2: Withdrawing stale pending requests")
    logger.info("-" * 40)

    for username in to_withdraw:
        if _shutdown_requested or rate_limited:
            skipped_count += 1
            continue

        try:
            success = withdraw_request(cl, username, dry_run=DRY_RUN)
            if success:
                withdrawn_count += 1
                progress["withdrawn"].append(username)
                save_progress(progress)

            total_actions += 1

            if total_actions % ACTIONS_BEFORE_BREAK == 0:
                long_break()
            else:
                human_delay()

        except (RateLimitError, PleaseWaitFewMinutes, ChallengeRequired, ClientThrottledError) as e:
            logger.error(f"⚠️  Rate limit / challenge hit: {e}")
            logger.info(f"Sleeping {RATE_LIMIT_SLEEP}s before continuing...")
            skipped_count += len(to_withdraw) - withdrawn_count - skipped_count
            save_session(cl)
            interruptible_sleep(RATE_LIMIT_SLEEP)
            rate_limited = True

    # ── Save session ──
    if cl:
        save_session(cl)

    # ── Summary ──
    logger.info("=" * 60)
    logger.info("CYCLE COMPLETE — Summary")
    logger.info(f"  Unfollowed:            {unfollowed_count}")
    logger.info(f"  Requests withdrawn:    {withdrawn_count}")
    logger.info(f"  Skipped (rate limit):  {skipped_count}")
    logger.info(f"  Total non-followers:   {len(non_followers)}")
    logger.info(f"  Eligible (past grace): {len(eligible_to_unfollow)}")
    logger.info(f"  Total stale pending:   {len(stale_requests)}")
    if rate_limited:
        logger.warning("  ⚠  Cycle ended early due to rate limiting")
    logger.info(f"  Progress saved to:     {PROGRESS_FILE}")
    logger.info("=" * 60)

    return cl


# ──────────────────────────────────────────────
# Main loop (daemon mode)
# ──────────────────────────────────────────────
def main() -> None:
    # Load .env if present (Docker passes env vars directly, but .env
    # is handy for local dev)
    if ENV_FILE.exists():
        load_dotenv(ENV_FILE)

    ig_username = os.getenv("INSTAGRAM_USERNAME")
    ig_password = os.getenv("INSTAGRAM_PASSWORD")

    if not ig_username or not ig_password:
        logger.error("Missing INSTAGRAM_USERNAME or INSTAGRAM_PASSWORD")
        sys.exit(1)

    run_once = os.getenv("RUN_ONCE", "false").lower() in ("true", "1", "yes")

    logger.info("🚀 Instagram Cleanup Daemon starting")
    logger.info(f"   Cycle interval: {CYCLE_INTERVAL_HOURS}h")
    logger.info(f"   Dry run: {DRY_RUN}")
    logger.info(f"   Run once: {run_once}")

    cl = None  # will be created on first cycle

    while not _shutdown_requested:
        try:
            cl = run_cycle(cl, ig_username, ig_password)
        except Exception as e:
            logger.exception(f"💥 Unhandled error during cycle: {e}")

        if run_once:
            logger.info("RUN_ONCE=true — exiting after single cycle")
            break

        next_run = datetime.now() + timedelta(seconds=CYCLE_INTERVAL)
        logger.info(f"💤 Next cycle at {next_run.strftime('%Y-%m-%d %H:%M:%S')} "
                     f"(sleeping {CYCLE_INTERVAL_HOURS}h)")
        interruptible_sleep(CYCLE_INTERVAL)

    logger.info("👋 Daemon shutting down gracefully")
    if cl and not DRY_RUN:
        save_session(cl)


if __name__ == "__main__":
    main()
