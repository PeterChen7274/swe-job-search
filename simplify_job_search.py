
import csv
import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SOURCE_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/"
    "New-Grad-Positions/dev/.github/scripts/listings.json"
)

DAYS_AGO = int(os.getenv("DAYS_AGO", "7"))
REQUEST_TIMEOUT = 60

OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", str(Path.home() / "Downloads")))
STATE_FILE = Path(os.getenv("STATE_FILE", "./data/seen_jobs.json"))

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "").strip()

# Set this to false if you don't want notifications on the first run.
NOTIFY_ON_FIRST_RUN = (
    os.getenv("NOTIFY_ON_FIRST_RUN", "true").lower() == "true"
)


# Companies from our existing tracker, including previously unsupported
# companies and the major technology companies we added later.
#
# Keys are canonical company names. Values are alternate names that may
# appear in the Simplify listings.

TRACKED_COMPANIES = {
    "OpenAI": ["OpenAI"],
    "Anthropic": ["Anthropic"],
    "Databricks": ["Databricks"],
    "Snowflake": ["Snowflake"],
    "Cohere": ["Cohere"],
    "Scale AI": ["Scale AI", "Scale"],
    "Stripe": ["Stripe"],
    "Airbnb": ["Airbnb"],
    "Figma": ["Figma"],
    "Coinbase": ["Coinbase"],
    "DoorDash": ["DoorDash"],
    "Cloudflare": ["Cloudflare"],
    "Datadog": ["Datadog"],
    "Reddit": ["Reddit"],
    "Pinterest": ["Pinterest"],
    "Lyft": ["Lyft"],
    "Asana": ["Asana"],
    "Dropbox": ["Dropbox"],
    "Notion": ["Notion"],
    "Ramp": ["Ramp"],
    "Linear": ["Linear"],
    "Vercel": ["Vercel"],
    "Perplexity": ["Perplexity"],
    "Rippling": ["Rippling"],
    "Palantir": ["Palantir"],
    "Spotify": ["Spotify"],
    "Zoox": ["Zoox"],

    # Big Tech
    "Google": ["Google", "Google LLC"],
    "Meta": ["Meta", "Facebook"],
    "Amazon": ["Amazon", "Amazon.com"],
    "Apple": ["Apple"],
    "Netflix": ["Netflix"],
    "Microsoft": ["Microsoft"],
    "NVIDIA": ["NVIDIA", "Nvidia"],
    "Uber": ["Uber"],
    "Salesforce": ["Salesforce"],
    "Adobe": ["Adobe"],
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

SESSION = requests.Session()

SESSION.headers.update({
    "User-Agent": "SWEJobTracker/1.0",
    "Accept": "application/json",
})


def fetch_listings():
    logger.info("Fetching listings from SimplifyJobs...")

    response = SESSION.get(
        SOURCE_URL,
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    listings = response.json()

    if not isinstance(listings, list):
        raise ValueError(
            "Expected listings.json to contain a JSON array."
        )

    logger.info("Fetched %d total listings.", len(listings))

    return listings


# ---------------------------------------------------------------------------
# Company matching
# ---------------------------------------------------------------------------

def normalize_company(name):
    """
    Normalize company names for matching.

    Examples:
        "Scale AI" -> "scaleai"
        "NVIDIA"   -> "nvidia"
        "DoorDash" -> "doordash"
    """
    if not name:
        return ""

    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def build_company_lookup():
    lookup = {}

    for canonical_name, aliases in TRACKED_COMPANIES.items():
        lookup[normalize_company(canonical_name)] = canonical_name

        for alias in aliases:
            lookup[normalize_company(alias)] = canonical_name

    return lookup


COMPANY_LOOKUP = build_company_lookup()


def match_company(company_name):
    normalized = normalize_company(company_name)

    return COMPANY_LOOKUP.get(normalized)


# ---------------------------------------------------------------------------
# US location filtering
# ---------------------------------------------------------------------------

US_STATE_NAMES = {
    "alabama", "alaska", "arizona", "arkansas", "california",
    "colorado", "connecticut", "delaware", "florida", "georgia",
    "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas",
    "kentucky", "louisiana", "maine", "maryland", "massachusetts",
    "michigan", "minnesota", "mississippi", "missouri", "montana",
    "nebraska", "nevada", "new hampshire", "new jersey", "new mexico",
    "new york", "north carolina", "north dakota", "ohio", "oklahoma",
    "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont",
    "virginia", "washington", "west virginia", "wisconsin", "wyoming",
    "district of columbia",
}

US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
}

# Common US city names/abbreviations that sometimes appear without
# a state in job listings.
US_CITIES = {
    "san francisco",
    "sf",
    "new york",
    "nyc",
    "seattle",
    "austin",
    "boston",
    "chicago",
    "los angeles",
    "la",
    "san jose",
    "palo alto",
    "mountain view",
    "sunnyvale",
    "redmond",
    "bellevue",
    "san diego",
    "denver",
    "atlanta",
    "dallas",
    "houston",
    "miami",
    "portland",
    "washington dc",
    "washington, dc",
}


def is_us_location(location):
    """
    Return True if a location explicitly indicates a US location.

    Supports:
    - United States / USA
    - US states and state abbreviations
    - Common US city names
    - Remote in USA

    Locations such as Canada, UK, Europe, and generic Remote do not pass.
    """
    if not location:
        return False

    if isinstance(location, list):
        return any(is_us_location(item) for item in location)

    if isinstance(location, dict):
        # Prefer explicit country metadata when available.
        country = (
            location.get("country")
            or location.get("countryName")
            or location.get("countryCode")
        )

        if country:
            return str(country).strip().lower() in {
                "us",
                "usa",
                "united states",
                "united states of america",
            }

        # Otherwise inspect its location-related fields.
        return any(
            is_us_location(location.get(field))
            for field in (
                "name",
                "city",
                "state",
                "region",
                "address",
            )
            if location.get(field)
        )

    text = str(location).strip()
    normalized = text.lower()

    if not normalized:
        return False

    # Explicit US indicators.
    if re.search(
        r"\b(united states|united states of america|usa)\b",
        normalized,
    ):
        return True

    # Do not treat broad remote regions as US.
    if normalized in {
        "remote",
        "north america",
        "americas",
        "worldwide",
        "global",
    }:
        return False

    # US state names.
    for state in US_STATE_NAMES:
        if re.search(rf"\b{re.escape(state)}\b", normalized):
            return True

    # State abbreviations, requiring a comma or beginning of string.
    if re.search(
        r"(?:^|,\s*)(" + "|".join(US_STATE_CODES) + r")\b",
        text.upper(),
    ):
        return True

    # Common city names when state information is missing.
    for city in US_CITIES:
        if re.search(rf"\b{re.escape(city)}\b", normalized):
            return True

    return False


def get_us_locations(listing):
    """
    Return the US locations associated with a listing.

    A job with multiple locations is retained if at least one location
    is in the US.
    """
    locations = listing.get("locations") or []

    if isinstance(locations, str):
        locations = [locations]

    return [
        str(location)
        for location in locations
        if is_us_location(location)
    ]


# ---------------------------------------------------------------------------
# Date filtering
# ---------------------------------------------------------------------------

def parse_posted_date(value):
    """
    Simplify's date_posted field is normally a Unix timestamp in seconds.
    Also support milliseconds and ISO date strings.
    """
    if value is None:
        return None

    try:
        if isinstance(value, (int, float)):
            timestamp = value

            if timestamp > 10_000_000_000:
                timestamp /= 1000

            return datetime.fromtimestamp(
                timestamp,
                tz=timezone.utc,
            )

        value = str(value).strip()

        if value.isdigit():
            return parse_posted_date(int(value))

        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except (ValueError, TypeError, OverflowError, OSError):
        return None


# ---------------------------------------------------------------------------
# Job identity and filtering
# ---------------------------------------------------------------------------

def get_job_id(listing):
    """
    Use the source's stable ID when available.
    Otherwise generate a deterministic ID from the listing fields.
    """
    source_id = listing.get("id")

    if source_id:
        return str(source_id)

    unique_text = "|".join([
        str(listing.get("company_name", "")),
        str(listing.get("title", "")),
        str(listing.get("url", "")),
    ])

    return hashlib.sha256(
        unique_text.encode("utf-8")
    ).hexdigest()


def filter_listings(listings):
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=DAYS_AGO)

    logger.info("Filtering listings posted since %s", cutoff.isoformat())

    matches = []

    for listing in listings:

        # Only active, visible software engineering listings.
        if not listing.get("is_visible", True):
            continue

        if not listing.get("active", True):
            continue

        category = str(listing.get("category", "")).strip().lower()

        if category not in {
            "software",
            "software engineering",
        }:
            continue

        # Match only companies in our configured list.
        original_company = listing.get("company_name", "")
        canonical_company = match_company(original_company)

        if not canonical_company:
            continue

        # Only listings with a known date are eligible.
        posted_date = parse_posted_date(
            listing.get("date_posted")
        )

        if posted_date is None:
            continue

        if posted_date < cutoff:
            continue

        # At least one listed location must be US-based.
        us_locations = get_us_locations(listing)

        if not us_locations:
            continue

        matches.append({
            "id": get_job_id(listing),
            "company": canonical_company,
            "original_company": original_company,
            "title": listing.get("title", ""),
            "location": "; ".join(us_locations),
            "date_posted": posted_date.isoformat(),
            "url": listing.get("url", ""),
            "company_url": listing.get("company_url", ""),
            "category": listing.get("category", ""),
            "sponsorship": listing.get("sponsorship", ""),
            "degrees": ", ".join(listing.get("degrees") or []),
            "source": "SimplifyJobs New-Grad-Positions",
        })

    # Deduplicate by job ID.
    unique = {}

    for job in matches:
        unique[job["id"]] = job

    matches = list(unique.values())

    matches.sort(
        key=lambda job: job["date_posted"],
        reverse=True,
    )

    logger.info("Found %d matching jobs.", len(matches))

    return matches


# ---------------------------------------------------------------------------
# Persistent state
# ---------------------------------------------------------------------------

def load_seen_jobs():
    if not STATE_FILE.exists():
        return None

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)

        return set(data.get("seen_job_ids", []))

    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not load state file: %s", exc)
        return set()


def save_seen_jobs(seen_ids):
    STATE_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_file = STATE_FILE.with_suffix(".tmp")

    with temporary_file.open("w", encoding="utf-8") as file:
        json.dump(
            {
                "updated_at": datetime.now(
                    timezone.utc
                ).isoformat(),
                "seen_job_ids": sorted(seen_ids),
            },
            file,
            indent=2,
        )

    temporary_file.replace(STATE_FILE)


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------


def export_csv(jobs):
    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_path = OUTPUT_DIR / "simplify_jobs.csv"

    columns = [
        "id",
        "company",
        "title",
        "location",
        "date_posted",
        "url",
        "company_url",
        "sponsorship",
        "degrees",
        "source",
    ]

    existing_ids = set()

    # Read existing records to prevent duplicates.
    if output_path.exists():
        with output_path.open(
            "r",
            newline="",
            encoding="utf-8-sig",
        ) as file:
            reader = csv.DictReader(file)

            for row in reader:
                if row.get("id"):
                    existing_ids.add(row["id"])
                elif row.get("url"):
                    existing_ids.add(row["url"])

    new_jobs = []

    for job in jobs:
        job_id = job.get("id") or job.get("url")

        if not job_id or job_id in existing_ids:
            continue

        new_jobs.append(job)
        existing_ids.add(job_id)

    # Create the file with headers if it doesn't exist.
    # Otherwise append only new records.
    write_header = not output_path.exists()

    with output_path.open(
        "a",
        newline="",
        encoding="utf-8-sig",
    ) as file:
        writer = csv.DictWriter(
            file,
            fieldnames=columns,
            extrasaction="ignore",
        )

        if write_header:
            writer.writeheader()

        writer.writerows(new_jobs)

    logger.info(
        "CSV: %d new jobs appended, %d total unique jobs tracked.",
        len(new_jobs),
        len(existing_ids),
    )

    return output_path



# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def format_job_notification(job):
    posted = parse_posted_date(job["date_posted"])

    posted_text = (
        posted.strftime("%Y-%m-%d")
        if posted
        else "Unknown"
    )

    return (
        f"🆕 **{job['company']} — {job['title']}**\n"
        f"📍 {job['location']}\n"
        f"📅 Posted: {posted_text}\n"
        f"🔗 {job['url']}"
    )


def send_discord_notifications(jobs):
    """
    Discord webhook messages have a 2000-character content limit.
    Split the notifications into multiple messages when necessary.
    """
    if not jobs:
        return

    if not DISCORD_WEBHOOK_URL:
        raise ValueError("Discord webhook URL is missing.")

    messages = []
    current_message = "🚀 **New matching SWE jobs**\n\n"

    for job in jobs:
        block = format_job_notification(job) + "\n\n"

        if len(current_message) + len(block) > 1900:
            messages.append(current_message)
            current_message = "🚀 **More matching jobs**\n\n"

        current_message += block

    if current_message.strip():
        messages.append(current_message)

    for message in messages:
        response = SESSION.post(
            DISCORD_WEBHOOK_URL,
            json={"content": message},
            timeout=REQUEST_TIMEOUT,
        )

        response.raise_for_status()

    logger.info(
        "Sent %d Discord notification message(s).",
        len(messages),
    )


def send_slack_notification(jobs):
    if not jobs:
        return

    if not SLACK_WEBHOOK_URL:
        raise ValueError("Slack webhook URL is missing.")

    blocks = [
        "🚀 *New matching SWE jobs*",
        "",
    ]

    for job in jobs:
        posted = parse_posted_date(job["date_posted"])

        posted_text = (
            posted.strftime("%Y-%m-%d")
            if posted
            else "Unknown"
        )

        blocks.extend([
            f"*{job['company']} — {job['title']}*",
            f"📍 {job['location']}",
            f"📅 Posted: {posted_text}",
            f"<{job['url']}|Apply>",
            "",
        ])

    response = SESSION.post(
        SLACK_WEBHOOK_URL,
        json={"text": "\n".join(blocks)},
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    logger.info("Sent Slack notification.")


def notify_new_jobs(new_jobs):
    if not new_jobs:
        logger.info("No new jobs to notify.")
        return True

    if DISCORD_WEBHOOK_URL:
        send_discord_notifications(new_jobs)
        return True

    if SLACK_WEBHOOK_URL:
        send_slack_notification(new_jobs)
        return True

    logger.info(
        "No notification webhook configured. "
        "Skipping notifications."
    )

    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    logger.info("=" * 65)
    logger.info("SimplifyJobs New Grad SWE Tracker")
    logger.info("Date: %s", datetime.now(timezone.utc).isoformat())
    logger.info("Lookback: %d days", DAYS_AGO)
    logger.info("Tracked companies: %d", len(TRACKED_COMPANIES))
    logger.info("=" * 65)

    listings = fetch_listings()

    matching_jobs = filter_listings(listings)

    # Always export all currently matching jobs.
    output_path = export_csv(matching_jobs)

    previous_seen = load_seen_jobs()

    if previous_seen is None:
        logger.info("First run detected.")

        if NOTIFY_ON_FIRST_RUN:
            new_jobs = matching_jobs
        else:
            new_jobs = []

        previous_seen = set()

    else:
        new_jobs = [
            job
            for job in matching_jobs
            if job["id"] not in previous_seen
        ]

    logger.info("Total matching jobs: %d", len(matching_jobs))
    logger.info("Newly discovered jobs: %d", len(new_jobs))

    notification_success = notify_new_jobs(new_jobs)

    # Update state only after successful notifications.
    # This prevents a failed webhook from silently losing alerts.
    #
    # When no webhook is configured, leave the state untouched so
    # that the first notification-enabled run can alert on current jobs.
    if notification_success:
        updated_seen = previous_seen | {
            job["id"] for job in matching_jobs
        }

        save_seen_jobs(updated_seen)

    print("\nMatching jobs:\n")

    for job in matching_jobs:
        print(
            f"[{job['date_posted'][:10]}] "
            f"{job['company']} — {job['title']}"
        )
        print(f"  Location: {job['location']}")
        print(f"  Apply: {job['url']}")
        print()

    logger.info("Finished. CSV: %s", output_path)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.warning("Interrupted.")
        sys.exit(130)
    except Exception:
        logger.exception("Job search failed.")
        sys.exit(1)
