from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

DEFAULT_ENDPOINT = "https://graphql.app-api.prod.aws.mybirdbuddy.com/graphql"
POSTCARD_TYPENAMES = {"FeedItemNewPostcard", "FeedItemCollectedPostcard"}


def get_output_timezone() -> ZoneInfo:
    try:
        return ZoneInfo("Europe/Berlin")
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(
            "Timezone Europe/Berlin is unavailable. Install tzdata (e.g. uv add tzdata) and retry."
        ) from exc


OUTPUT_TIMEZONE = get_output_timezone()

LOGIN_MUTATION = """
mutation Login($input: EmailSignInInput!) {
  authEmailSignIn(emailSignInInput: $input) {
    __typename
    ... on Auth {
      accessToken
      refreshToken
    }
  }
}
""".strip()

FEED_QUERY_FIRST_PAGE = """
query FeedFirst($first: Int!) {
    me {
        feed(first: $first) {
            edges {
                cursor
                node {
                    __typename
                    ... on Node {
                        id
                    }
                    ... on FeedItem {
                        createdAt
                    }
                }
            }
            pageInfo {
                hasNextPage
                endCursor
            }
        }
    }
}
""".strip()

FEED_QUERY_AFTER = """
query FeedAfter($first: Int!, $after: String!) {
  me {
    feed(first: $first, after: $after) {
      edges {
        cursor
        node {
          __typename
                    ... on Node {
            id
          }
                    ... on FeedItem {
            createdAt
          }
        }
      }
      pageInfo {
        hasNextPage
        endCursor
      }
    }
  }
}
""".strip()

POSTCARD_MEDIA_QUERY = """
query PostcardMedia($feedItemId: ID!) {
    postcardMediasDetails(feedItemId: $feedItemId) {
        __typename
        id
        ... on PostcardImageMediaDetails {
            inferenceMediaDecision {
                __typename
                ... on InferenceMediaRecognizedDecision {
                    species {
                        name
                    }
                    suggestions {
                        species {
                            name
                        }
                        confidence
                    }
                }
                ... on InferenceMediaCannotDecideDecision {
                    suggestions {
                        species {
                            name
                        }
                        confidence
                    }
                }
                ... on InferenceMediaNotRecognizedDecision {
                    suggestions {
                        species {
                            name
                        }
                        confidence
                    }
                }
            }
            media {
                id
                thumbnailUrl
                createdAt
                ... on MediaImage {
                    contentUrl(size: ORIGINAL)
                }
            }
        }
        ... on PostcardVideoMediaDetails {
            species {
                name
            }
            suggestions {
                species {
                    name
                }
                confidence
            }
            media {
                id
                thumbnailUrl
                createdAt
                ... on MediaVideo {
                    contentUrl(size: ORIGINAL)
                }
            }
        }
    }
}
""".strip()


class ApiError(RuntimeError):
    pass


@dataclass
class Postcard:
    postcard_id: str
    created_at: datetime


@dataclass
class MediaItem:
    postcard_id: str
    media_id: str
    media_type: str
    url: str
    bird_name: str
    created_at: datetime


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download BirdBuddy postcard media for a specific date.")
    parser.add_argument(
        "--date",
        default=datetime.now(UTC).strftime("%Y-%m-%d"),
        help="Date in YYYY-MM-DD (default: today in UTC)",
    )
    parser.add_argument("--output-dir", default="../fotos", help="Base output directory")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT, help="BirdBuddy GraphQL endpoint")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout seconds")
    parser.add_argument("--page-size", type=int, default=20, help="Feed page size")
    parser.add_argument("--max-pages", type=int, default=30, help="Maximum feed pages to scan")
    parser.add_argument("--retries", type=int, default=2, help="Retries per media download")
    parser.add_argument("--dry-run", action="store_true", help="Do not download files")
    args = parser.parse_args()

    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError as exc:
        raise ApiError("--date must be in YYYY-MM-DD format") from exc

    if args.page_size < 1:
        raise ApiError("--page-size must be >= 1")
    if args.max_pages < 1:
        raise ApiError("--max-pages must be >= 1")

    return args


def require_credentials() -> tuple[str, str]:
    file_vars = read_dotenv(Path(".env"))
    email = os.getenv("BIRDBUDDY_EMAIL") or file_vars.get("BIRDBUDDY_EMAIL")
    password = os.getenv("BIRDBUDDY_PASSWORD") or file_vars.get("BIRDBUDDY_PASSWORD")
    if not email or not password:
        raise ApiError("Set BIRDBUDDY_EMAIL and BIRDBUDDY_PASSWORD in environment or .env file.")
    return email, password


def read_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        item = line.strip()
        if not item or item.startswith("#") or "=" not in item:
            continue
        key, raw = item.split("=", 1)
        key = key.strip()
        value = raw.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def parse_iso(value: str) -> datetime:
    txt = value.replace("Z", "+00:00")
    return datetime.fromisoformat(txt)


def day_bounds_utc(date_str: str) -> tuple[datetime, datetime]:
    day = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)
    return day, day + timedelta(days=1)


def graphql_call(
    endpoint: str,
    query: str,
    variables: dict[str, Any],
    timeout: float,
    token: str | None = None,
) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = Request(
        endpoint,
        data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
        method="POST",
        headers=headers,
    )

    try:
        with urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise ApiError(f"HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise ApiError(f"Network error: {exc.reason}") from exc

    if payload.get("errors"):
        messages = "; ".join(err.get("message", "GraphQL error") for err in payload["errors"])
        raise ApiError(f"GraphQL error: {messages}")

    return payload


def login(endpoint: str, email: str, password: str, timeout: float) -> str:
    result = graphql_call(
        endpoint=endpoint,
        query=LOGIN_MUTATION,
        variables={"input": {"email": email, "password": password}},
        timeout=timeout,
    )
    auth_result = result["data"]["authEmailSignIn"]
    typename = auth_result.get("__typename")
    if typename != "Auth":
        raise ApiError(f"Login failed. Server returned {typename}.")

    token = auth_result.get("accessToken")
    if not token:
        raise ApiError("Login succeeded but no access token returned.")
    return token


def fetch_postcards_for_date(
    endpoint: str,
    token: str,
    date_str: str,
    page_size: int,
    max_pages: int,
    timeout: float,
) -> list[Postcard]:
    start, end = day_bounds_utc(date_str)
    cursor: str | None = None
    found: list[Postcard] = []

    for page in range(1, max_pages + 1):
        if cursor is None:
            response = graphql_call(
                endpoint=endpoint,
                query=FEED_QUERY_FIRST_PAGE,
                variables={"first": page_size},
                timeout=timeout,
                token=token,
            )
        else:
            response = graphql_call(
                endpoint=endpoint,
                query=FEED_QUERY_AFTER,
                variables={"first": page_size, "after": cursor},
                timeout=timeout,
                token=token,
            )

        feed = response["data"]["me"]["feed"]
        edges = feed.get("edges", [])
        print(f"Fetched feed page {page} with {len(edges)} items")

        stop_paging = False
        for edge in edges:
            node = edge.get("node") or {}
            typename = node.get("__typename")
            if typename not in POSTCARD_TYPENAMES:
                continue

            postcard_id = node.get("id")
            created_at_str = node.get("createdAt")
            if not postcard_id or not created_at_str:
                continue

            created_at = parse_iso(created_at_str)
            if created_at < start:
                stop_paging = True
            if start <= created_at < end:
                found.append(Postcard(postcard_id=postcard_id, created_at=created_at))

        page_info = feed.get("pageInfo") or {}
        has_next = bool(page_info.get("hasNextPage"))
        cursor = page_info.get("endCursor")

        if stop_paging or not has_next or not cursor:
            break

    unique: dict[str, Postcard] = {}
    for item in found:
        unique[item.postcard_id] = item
    return list(unique.values())


def fetch_postcard_media(
    endpoint: str,
    token: str,
    postcard_id: str,
    timeout: float,
) -> list[MediaItem]:
    response = graphql_call(
        endpoint=endpoint,
        query=POSTCARD_MEDIA_QUERY,
        variables={"feedItemId": postcard_id},
        timeout=timeout,
        token=token,
    )

    out: list[MediaItem] = []
    details = response["data"].get("postcardMediasDetails", [])
    for item in details:
        media = item.get("media") or {}
        typename = item.get("__typename", "PostcardImageMediaDetails")
        media_type = "video" if "Video" in typename else "image"
        # Use contentUrl(ORIGINAL) for both images and videos
        url = media.get("contentUrl") or media.get("thumbnailUrl")
        media_id = media.get("id") or item.get("id")
        created_at_raw = media.get("createdAt")
        species_list = item.get("species") or []
        suggestions = item.get("suggestions") or []
        if not species_list and not suggestions:
            inference_decision = item.get("inferenceMediaDecision") or {}
            species_list = inference_decision.get("species") or []
            suggestions = inference_decision.get("suggestions") or []

        bird_name = "unknown"
        if species_list:
            first_species = species_list[0] or {}
            species_name = str(first_species.get("name") or "").strip()
            if species_name:
                bird_name = species_name
        elif suggestions:
            first_suggestion = suggestions[0] or {}
            suggestion_species = first_suggestion.get("species") or {}
            suggestion_name = str(suggestion_species.get("name") or "").strip()
            if suggestion_name:
                bird_name = suggestion_name
        if url and media_id:
            created_at = parse_iso(str(created_at_raw)) if created_at_raw else datetime.now(UTC)
            out.append(
                MediaItem(
                    postcard_id=postcard_id,
                    media_id=str(media_id),
                    media_type=media_type,
                    url=str(url),
                    bird_name=bird_name,
                    created_at=created_at,
                )
            )

    dedup: dict[str, MediaItem] = {}
    for media in out:
        dedup[media.url] = media
    return list(dedup.values())


def extension_from_url(url: str) -> str:
    path = urlparse(url).path
    ext = Path(path).suffix
    if ext:
        return ext
    guessed = mimetypes.guess_extension("image/jpeg")
    return guessed or ".bin"


def media_extension(media_type: str, url: str) -> str:
    if media_type == "video":
        return ".mp4"
    ext = extension_from_url(url).lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp"}:
        return ext
    return ".jpg"


def slugify_bird_name(name: str) -> str:
    compact = re.sub(r"\s+", "_", name.strip().lower())
    cleaned = re.sub(r"[^a-z0-9_]", "", compact)
    return cleaned or "unknown"


def download_media(url: str, destination: Path, timeout: float, retries: int) -> None:
    req = Request(url, method="GET", headers={"Accept": "*/*"})
    attempt = 0
    while True:
        attempt += 1
        try:
            with urlopen(req, timeout=timeout) as response:
                destination.write_bytes(response.read())
            return
        except Exception as exc:
            if attempt > retries:
                raise ApiError(f"Download failed for {url}: {exc}") from exc
            time.sleep(1.0 * attempt)


def unique_destination(base_dir: Path, filename: str) -> Path:
    destination = base_dir / filename
    if not destination.exists():
        return destination

    stem = Path(filename).stem
    suffix = Path(filename).suffix
    index = 1
    while True:
        candidate = base_dir / f"{stem}_{index:02d}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def run() -> int:
    try:
        args = parse_args()
        email, password = require_credentials()

        print("Logging in...")
        token = login(args.endpoint, email, password, args.timeout)

        print("Fetching postcards for date...")
        postcards = fetch_postcards_for_date(
            endpoint=args.endpoint,
            token=token,
            date_str=args.date,
            page_size=args.page_size,
            max_pages=args.max_pages,
            timeout=args.timeout,
        )

        base_dir = Path(args.output_dir)
        base_dir.mkdir(parents=True, exist_ok=True)
        media_total = 0
        downloaded = 0
        failed = 0
        skipped_postcards = 0

        for postcard in postcards:
            try:
                media_items = fetch_postcard_media(
                    endpoint=args.endpoint,
                    token=token,
                    postcard_id=postcard.postcard_id,
                    timeout=args.timeout,
                )
            except ApiError as exc:
                skipped_postcards += 1
                print(f"Skipping postcard {postcard.postcard_id}: {exc}", file=sys.stderr)
                continue

            if not media_items:
                continue

            media_total += len(media_items)

            for media in media_items:
                timestamp = media.created_at.astimezone(OUTPUT_TIMEZONE).strftime("%Y%m%d_%H%M%S")
                bird_slug = slugify_bird_name(media.bird_name)
                ext = media_extension(media.media_type, media.url)
                filename = f"{timestamp}_{bird_slug}{ext}"
                destination = unique_destination(base_dir, filename)

                if args.dry_run:
                    print(f"[DRY-RUN] {media.url} -> {destination}")
                    downloaded += 1
                    continue

                try:
                    download_media(media.url, destination, args.timeout, args.retries)
                    downloaded += 1
                    print(f"Downloaded {destination}")
                except ApiError as exc:
                    failed += 1
                    print(str(exc), file=sys.stderr)

        print(
            "Summary: "
            f"date={args.date} "
            f"postcards={len(postcards)} "
            f"media_total={media_total} "
            f"downloaded={downloaded} "
            f"failed={failed} "
            f"skipped_postcards={skipped_postcards} "
            f"output={base_dir}"
        )
        return 0
    except ApiError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
