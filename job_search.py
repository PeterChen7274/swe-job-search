
import csv
import html
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DAYS_AGO = int(os.getenv("DAYS_AGO", "7"))
REQUEST_TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (compatible; SWEJobSearch/1.0)"

# The company list is intentionally limited to boards with known ATS tokens.
# If a company's endpoint returns 404, verify its current career-site URL
# before changing the token or provider.

COMPANIES = [
    # Ashby
    {"name": "OpenAI", "provider": "ashby", "token": "openai"},
    {"name": "Cohere", "provider": "ashby", "token": "cohere"},
    {"name": "Notion", "provider": "ashby", "token": "notion"},
    {"name": "Ramp", "provider": "ashby", "token": "ramp"},
    {"name": "Linear", "provider": "ashby", "token": "linear"},
    {"name": "Vercel", "provider": "ashby", "token": "vercel"},
    {"name": "Perplexity", "provider": "ashby", "token": "perplexity"},
    {"name": "Snowflake", "provider": "ashby", "token": "snowflake"},

    # Greenhouse
    {"name": "Anthropic", "provider": "greenhouse", "token": "anthropic"},
    {"name": "Scale AI", "provider": "greenhouse", "token": "scaleai"},
    {"name": "Airbnb", "provider": "greenhouse", "token": "airbnb"},
    {"name": "Figma", "provider": "greenhouse", "token": "figma"},
    {"name": "Coinbase", "provider": "greenhouse", "token": "coinbase"},
    {"name": "Cloudflare", "provider": "greenhouse", "token": "cloudflare"},
    {"name": "Datadog", "provider": "greenhouse", "token": "datadog"},
    {"name": "Reddit", "provider": "greenhouse", "token": "reddit"},
    {"name": "Pinterest", "provider": "greenhouse", "token": "pinterest"},
    {"name": "Lyft", "provider": "greenhouse", "token": "lyft"},
    {"name": "Asana", "provider": "greenhouse", "token": "asana"},
    {"name": "Dropbox", "provider": "greenhouse", "token": "dropbox"},

    # Lever
    {"name": "Palantir", "provider": "lever", "token": "palantir"},
    {"name": "Spotify", "provider": "lever", "token": "spotify"},
    {"name": "Zoox", "provider": "lever", "token": "zoox"},

    # Custom integration
    {"name": "Amazon", "provider": "amazon"},
]


# These companies are included in the tracker, but do not yet have a
# verified integration in this script. Their career pages are provided
# so you can still check them manually.
UNSUPPORTED_COMPANIES = [
    {
        "name": "Google",
        "career_url": "https://www.google.com/about/careers/applications/",
        "reason": "No verified public job-listing endpoint implemented.",
    },
    {
        "name": "Meta",
        "career_url": "https://www.metacareers.com/",
        "reason": "No verified public job-listing endpoint implemented.",
    },
    {
        "name": "Apple",
        "career_url": "https://jobs.apple.com/",
        "reason": "Career-site search requires a separate integration.",
    },
    {
        "name": "Netflix",
        "career_url": "https://explore.jobs.netflix.net/careers",
        "reason": "Workday-based career site; integration not implemented.",
    },
    {
        "name": "Microsoft",
        "career_url": "https://careers.microsoft.com/",
        "reason": "No verified public job-listing endpoint implemented.",
    },
    {
        "name": "NVIDIA",
        "career_url": "https://www.nvidia.com/en-us/about-nvidia/careers/",
        "reason": "Workday integration not implemented.",
    },
    {
        "name": "Uber",
        "career_url": "https://www.uber.com/us/en/careers/",
        "reason": "No verified public job-listing endpoint implemented.",
    },
    {
        "name": "Salesforce",
        "career_url": "https://careers.salesforce.com/",
        "reason": "No verified public job-listing endpoint implemented.",
    },
    {
        "name": "Adobe",
        "career_url": "https://careers.adobe.com/",
        "reason": "No verified public job-listing endpoint implemented.",
    },
    {
        "name": "Databricks",
        "career_url": "https://www.databricks.com/company/careers/open-positions",
        "reason": "Previously configured Ashby endpoint returned 404.",
    },
    {
        "name": "DoorDash",
        "career_url": "https://careersatdoordash.com/job-search/",
        "reason": "Previously configured Greenhouse endpoint returned 404.",
    },
    {
        "name": "Rippling",
        "career_url": "https://www.rippling.com/careers/open-roles",
        "reason": "Previously configured Ashby endpoint returned 404.",
    },
    {
        "name": "Stripe",
        "career_url": "https://stripe.com/jobs",
        "reason": "No verified endpoint configured.",
    },
]


# ---------------------------------------------------------------------------
# Logging and HTTP
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
})


def get_json(url: str, **kwargs) -> Any:
    """GET a JSON endpoint with consistent timeout and error handling."""
    response = SESSION.get(
        url,
        timeout=REQUEST_TIMEOUT,
        **kwargs,
    )

    response.raise_for_status()

    try:
        return response.json()
    except ValueError as exc:
        raise ValueError(
            f"Expected JSON from {url}, got: {response.text[:300]}"
        ) from exc


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def parse_date(value: Any) -> date | None:
    """
    Parse common date formats:
    - Unix timestamps in seconds or milliseconds
    - ISO 8601 strings
    - YYYY-MM-DD strings
    """
    if value is None or value == "":
        return None

    if isinstance(value, (int, float)):
        try:
            # Millisecond timestamps are much larger than second timestamps.
            timestamp = value / 1000 if value > 10_000_000_000 else value
            return datetime.fromtimestamp(
                timestamp,
                tz=timezone.utc,
            ).date()
        except (ValueError, OverflowError, OSError):
            return None

    if not isinstance(value, str):
        return None

    value = value.strip()

    if not value:
        return None

    # Try ISO-8601 first.
    try:
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).date()
    except ValueError:
        pass

    # Then try date-only formats.
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue

    return None


# ---------------------------------------------------------------------------
# Job description normalization
# ---------------------------------------------------------------------------

def strip_html(value: str) -> str:
    """Convert HTML descriptions to plain text for keyword matching."""
    if not value:
        return ""

    value = html.unescape(value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\s+", " ", value)

    return value.strip()


# ---------------------------------------------------------------------------
# Job classification
# ---------------------------------------------------------------------------

SWE_TITLE_PATTERNS = [
    r"\bsoftware engineer\b",
    r"\bsoftware developer\b",
    r"\bbackend engineer\b",
    r"\bback-end engineer\b",
    r"\bfull[- ]stack engineer\b",
    r"\bapplication engineer\b",
    r"\bproduct engineer\b",
    r"\bdeveloper\b",
]

# These are strong indications that a position is not entry-level.
SENIOR_PATTERNS = [
    r"\bsenior\b",
    r"\bstaff\b",
    r"\bprincipal\b",
    r"\blead\b",
    r"\bmanager\b",
    r"\bdirector\b",
    r"\barchitect\b",
    r"\bhead of\b",
    r"\bvice president\b",
    r"\bvp\b",
    r"\biii\b",
    r"\biv\b",
    r"\b5\+ years\b",
    r"\b6\+ years\b",
    r"\b7\+ years\b",
    r"\b8\+ years\b",
    r"\b10\+ years\b",
]

# These indicate an entry-level or early-career position.
ENTRY_LEVEL_PATTERNS = [
    r"\bentry[- ]level\b",
    r"\bnew grad(?:uate)?\b",
    r"\brecent grad(?:uate)?\b",
    r"\buniversity grad(?:uate)?\b",
    r"\bgraduate engineer\b",
    r"\bearly career\b",
    r"\bjunior engineer\b",
    r"\bassociate software engineer\b",
    r"\bsoftware engineer i\b",
    r"\bswe i\b",
    r"\blevel 1\b",
    r"\b0[-– ]2 years?\b",
    r"\b1[-– ]2 years?\b",
    r"\b1\+ years?\b",
    r"\bno experience required\b",
    r"\bgraduating in 202[5-9]\b",
    r"\bclass of 202[5-9]\b",
]


def matches_any(text: str, patterns: list[str]) -> bool:
    text = text.lower()
    return any(re.search(pattern, text) for pattern in patterns)


def is_swe_title(title: str) -> bool:
    return matches_any(title, SWE_TITLE_PATTERNS)


def is_entry_level_swe(job: dict) -> bool:
    """
    Keep SWE positions that have an entry-level signal.

    The entry-level signal can appear in either the title or description.
    Seniority exclusions are checked against both.
    """
    title = job.get("title", "")
    description = strip_html(job.get("description", ""))

    combined = f"{title} {description}".lower()

    if not is_swe_title(title):
        return False

    if matches_any(combined, SENIOR_PATTERNS):
        return False

    return matches_any(combined, ENTRY_LEVEL_PATTERNS)


# ---------------------------------------------------------------------------
# US location filtering
# ---------------------------------------------------------------------------

US_COUNTRY_NAMES = {
    "united states",
    "united states of america",
    "usa",
    "us",
    "u.s.",
    "u.s.a.",
}

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


def is_us_location(location: Any) -> bool:
    """
    Determine whether a location is explicitly in the United States.

    Important:
    - An explicit non-US country overrides a US-looking city/state.
    - 'Remote' and 'North America' alone are not sufficient.
    - A US state name or a comma-separated state code is accepted
      when there is no contradictory country information.
    """
    if location is None:
        return False

    if isinstance(location, list):
        return any(is_us_location(item) for item in location)

    if isinstance(location, dict):
        country = (
            location.get("country")
            or location.get("countryName")
            or location.get("countryCode")
            or location.get("country_code")
        )

        if country:
            return str(country).strip().lower() in US_COUNTRY_NAMES | {"us", "usa", "united states"}

        # If country is not provided, inspect the available location fields.
        parts = [
            location.get("name"),
            location.get("location"),
            location.get("city"),
            location.get("region"),
            location.get("state"),
            location.get("address"),
            location.get("address1"),
        ]

        return any(is_us_location(part) for part in parts if part)

    text = str(location).strip()

    if not text:
        return False

    normalized = text.lower()

    # Explicit country indicators.
    if any(country in normalized for country in US_COUNTRY_NAMES):
        return True

    # Avoid treating broad remote regions as US.
    if normalized in {"remote", "north america", "americas", "worldwide"}:
        return False

    # State names are generally safe when included in a location string.
    for state in US_STATE_NAMES:
        if re.search(rf"\b{re.escape(state)}\b", normalized):
            return True

    # Require a comma before a state abbreviation to reduce false positives.
    if re.search(
        r",\s*(" + "|".join(US_STATE_CODES) + r")\b",
        text.upper(),
    ):
        return True

    return False


def is_us_job(job: dict) -> bool:
    """
    Check all location fields provided by the relevant ATS.
    """
    locations = [
        job.get("location"),
        job.get("address"),
        job.get("offices"),
        job.get("secondaryLocations"),
        job.get("categories", {}).get("location")
        if isinstance(job.get("categories"), dict)
        else None,
    ]

    return any(is_us_location(location) for location in locations if location)


# ---------------------------------------------------------------------------
# Greenhouse
# ---------------------------------------------------------------------------

def fetch_greenhouse(token: str) -> list[dict]:
    url = (
        f"https://boards-api.greenhouse.io/v1/boards/"
        f"{token}/jobs?content=true"
    )

    data = get_json(url)

    jobs = []

    for item in data.get("jobs", []):
        location = item.get("location") or {}

        jobs.append({
            "title": item.get("title", ""),
            "url": item.get("absolute_url", ""),
            "description": item.get("content", "") or "",
            "date": parse_date(
                item.get("first_published")
                or item.get("updated_at")
            ),
            "date_type": (
                "first published"
                if item.get("first_published")
                else "last updated"
            ),
            "location": location.get("name", ""),
            "offices": item.get("offices", []),
        })

    return jobs


# ---------------------------------------------------------------------------
# Lever
# ---------------------------------------------------------------------------

def fetch_lever(token: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{token}?mode=json"

    data = get_json(url)

    jobs = []

    for item in data:
        categories = item.get("categories") or {}

        # Lever descriptions may be split across multiple fields.
        description_parts = [
            item.get("description", ""),
            item.get("descriptionPlain", ""),
            item.get("additional", ""),
            item.get("additionalPlain", ""),
        ]

        description = " ".join(
            part for part in description_parts if part
        )

        jobs.append({
            "title": item.get("text", ""),
            "url": item.get("hostedUrl", ""),
            "description": description,
            "date": parse_date(item.get("createdAt")),
            "date_type": "published",
            "location": categories.get("location", ""),
            "categories": categories,
            "workplaceType": item.get("workplaceType", ""),
        })

    return jobs


# ---------------------------------------------------------------------------
# Ashby
# ---------------------------------------------------------------------------

def fetch_ashby(token: str) -> list[dict]:
    url = (
        f"https://api.ashbyhq.com/posting-api/job-board/"
        f"{token}?includeCompensation=true"
    )

    data = get_json(url)

    jobs = []

    for item in data.get("jobs", []):
        jobs.append({
            "title": item.get("title", ""),
            "url": item.get("jobUrl", ""),
            "description": item.get("descriptionPlain", ""),
            "date": parse_date(item.get("publishedAt")),
            "date_type": "published",
            "location": item.get("location", ""),
            "address": item.get("address", {}),
            "secondaryLocations": item.get("secondaryLocations", []),
            "employmentType": item.get("employmentType", ""),
            "isRemote": item.get("isRemote", False),
        })

    return jobs


# ---------------------------------------------------------------------------
# Amazon
# ---------------------------------------------------------------------------

def fetch_amazon() -> list[dict]:
    """
    Fetch jobs from Amazon's public jobs JSON endpoint.

    The endpoint returns up to 100 jobs per page. We paginate until the
    returned page is empty or the API's reported result count is reached.
    """
    base_url = "https://amazon.jobs/en/search.json"

    jobs = []
    offset = 0
    page_size = 100
    max_pages = 100

    for _ in range(max_pages):
        params = {
            "offset": offset,
            "result_limit": page_size,
            "sort": "recent",
            "base_query": "",
        }

        data = get_json(base_url, params=params)

        page = data.get("jobs", [])

        if not page:
            break

        for item in page:
            job_path = (
                item.get("job_path")
                or item.get("url")
                or ""
            )

            if job_path.startswith("http"):
                job_url = job_path
            else:
                job_url = urljoin("https://amazon.jobs", job_path)

            location = (
                item.get("location")
                or item.get("locations")
                or item.get("location_name")
                or ""
            )

            jobs.append({
                "title": item.get("title", ""),
                "url": job_url,
                "description": item.get("description", "") or "",
                "date": parse_date(
                    item.get("posted_date")
                    or item.get("postedDate")
                ),
                "date_type": "published",
                "location": location,
            })

        offset += len(page)

        total = data.get("hits")

        if total is not None and offset >= int(total):
            break

        # Be polite to the public endpoint.
        time.sleep(0.25)

    return jobs


# ---------------------------------------------------------------------------
# Provider dispatch
# ---------------------------------------------------------------------------

def fetch_company_jobs(company: dict) -> list[dict]:
    provider = company["provider"]
    token = company.get("token")

    if provider == "greenhouse":
        return fetch_greenhouse(token)

    if provider == "lever":
        return fetch_lever(token)

    if provider == "ashby":
        return fetch_ashby(token)

    if provider == "amazon":
        return fetch_amazon()

    raise ValueError(f"Unsupported provider: {provider}")


# ---------------------------------------------------------------------------
# Company search
# ---------------------------------------------------------------------------

def search_company(
    company: dict,
    cutoff: date,
) -> list[dict]:
    name = company["name"]

    logger.info("Searching %s...", name)

    try:
        jobs = fetch_company_jobs(company)

    except requests.HTTPError as exc:
        status = (
            exc.response.status_code
            if exc.response is not None
            else "unknown"
        )
        logger.error("%s returned HTTP %s: %s", name, status, exc)
        return []

    except requests.RequestException as exc:
        logger.error("Request failed for %s: %s", name, exc)
        return []

    except (ValueError, KeyError, TypeError) as exc:
        logger.error("Could not parse jobs for %s: %s", name, exc)
        return []

    logger.info("%s: fetched %d jobs", name, len(jobs))

    matched_jobs = []

    for job in jobs:
        title = job.get("title", "")
        posted_date = job.get("date")

        # Require a known posting date. This avoids treating old jobs
        # with missing dates as newly posted.
        if posted_date is None:
            continue

        if posted_date < cutoff:
            continue

        if not is_us_job(job):
            continue

        if not is_entry_level_swe(job):
            continue

        matched_jobs.append({
            "company": name,
            "title": title,
            "date": posted_date.isoformat(),
            "date_type": job.get("date_type", ""),
            "url": job.get("url", ""),
            "location": format_location(job),
        })

    logger.info(
        "%s: found %d matching jobs",
        name,
        len(matched_jobs),
    )

    return matched_jobs


def format_location(job: dict) -> str:
    """Return a readable location string for CSV output."""
    location = job.get("location", "")

    if isinstance(location, dict):
        return (
            location.get("name")
            or location.get("city")
            or str(location)
        )

    if isinstance(location, list):
        return "; ".join(str(item) for item in location)

    return str(location or "")


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------

def export_csv(jobs: list[dict]) -> Path:
    output_dir = Path(
        os.environ.get(
            "OUTPUT_DIR",
            str(Path.home() / "Downloads"),
        )
    )

    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"swe_jobs_{timestamp}.csv"

    fieldnames = [
        "company",
        "title",
        "date",
        "date_type",
        "location",
        "url",
    ]

    with output_path.open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=fieldnames,
        )

        writer.writeheader()
        writer.writerows(jobs)

    return output_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    today = date.today()
    cutoff = today - timedelta(days=DAYS_AGO)

    logger.info("=" * 70)
    logger.info("SWE job search")
    logger.info("Today: %s", today)
    logger.info("Looking back %d days (since %s)", DAYS_AGO, cutoff)
    logger.info("Companies with integrations: %d", len(COMPANIES))
    logger.info("=" * 70)

    all_jobs = []

    for company in COMPANIES:
        results = search_company(company, cutoff)
        all_jobs.extend(results)

    # Remove duplicate URLs.
    unique_jobs = {}
    for job in all_jobs:
        url = job.get("url")

        if url:
            unique_jobs[url] = job

    all_jobs = list(unique_jobs.values())

    # Sort newest first, then company name.
    all_jobs.sort(
        key=lambda job: (
            job["date"],
            job["company"].lower(),
            job["title"].lower(),
        ),
        reverse=True,
    )

    output_path = export_csv(all_jobs)

    logger.info("=" * 70)
    logger.info("Search complete")
    logger.info("Matching jobs: %d", len(all_jobs))
    logger.info("CSV saved to: %s", output_path)
    logger.info("=" * 70)

    if all_jobs:
        print("\nMatching jobs:\n")

        for job in all_jobs:
            print(
                f"[{job['date']}] "
                f"{job['company']} — "
                f"{job['title']} "
                f"({job['location']})"
            )
            print(f"  {job['url']}")

    else:
        print("\nNo matching jobs found.")

    if UNSUPPORTED_COMPANIES:
        print("\nCompanies not yet integrated:")
        for company in UNSUPPORTED_COMPANIES:
            print(
                f"- {company['name']}: "
                f"{company['career_url']}"
            )
            print(f"  {company['reason']}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.warning("Interrupted by user.")
        sys.exit(130)
