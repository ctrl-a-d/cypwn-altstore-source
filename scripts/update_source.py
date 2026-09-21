#!/usr/bin/env python3
"""Clean CyPwn metadata without fetching any referenced assets."""
import argparse
import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

UPSTREAM = "https://ipa.cypwn.xyz/cypwn.json"
MAX_BYTES = 10 * 1024 * 1024


def log(message):
    # Prefix and escape newlines so untrusted metadata cannot emit Actions commands.
    print("[cleaner] " + str(message).replace("\r", "\\r").replace("\n", "\\n"))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def parse_json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, f"Duplicate JSON key: {key!r}")
            result[key] = value
        return result

    def constant(value):
        raise ValueError(f"Invalid JSON constant: {value}")

    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)


def fetch_json(url, allow_missing=False):
    validate_url(url)
    for attempt in range(3):
        try:
            request = Request(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; CyPwnClean/1.0)",
                "Accept": "application/json", "Cache-Control": "no-cache",
            })
            with urlopen(request, timeout=30) as response:
                require(response.status == 200, "Expected HTTP 200")
                data = response.read(MAX_BYTES + 1)
                require(len(data) <= MAX_BYTES, "Metadata exceeds 10 MiB limit")
                result = parse_json(data)
                log(f"Fetched JSON successfully: {url}")
                return result
        except HTTPError as exc:
            if exc.code == 404 and allow_missing:
                log("Published source is missing; explicit bootstrap permitted")
                return None
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
            log(f"HTTP {exc.code}; retrying metadata fetch")
        except (URLError, TimeoutError):
            if attempt == 2:
                raise
            log("Network error; retrying metadata fetch")
        time.sleep(2 ** attempt)


def validate_url(value):
    require(isinstance(value, str), "URL must be a string")
    parsed = urlsplit(value)
    require(parsed.scheme in ("http", "https") and bool(parsed.hostname)
            and not parsed.username and not parsed.password
            and not any(ord(c) < 32 for c in value), f"Invalid URL: {value!r}")


def date_key(value):
    require(isinstance(value, str), "Release date must be a string")
    require(bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T.+)?", value)),
            f"Invalid ISO release date: {value!r}")
    date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date.astimezone(timezone.utc)


def version_key(value):
    """Numeric components plus SemVer prerelease ordering; unknown syntax is opaque."""
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?", value)
    if not match:
        return None
    numbers = [int(part) for part in match[1].split(".")]
    while len(numbers) > 1 and numbers[-1] == 0:
        numbers.pop()
    pre = match[2]
    if pre is not None and any(not part for part in pre.split(".")):
        return None
    prerelease = tuple((0, int(p)) if p.isdigit() else (1, p) for p in pre.split(".")) if pre else ()
    return tuple(numbers), pre is None, prerelease


def release(app):
    return app["versions"][0] if "versions" in app else app


def validate_release(item, modern):
    require(isinstance(item, dict), "Release must be an object")
    require(isinstance(item.get("version"), str) and bool(item["version"]), "Missing version")
    date_key(item.get("date" if modern else "versionDate"))
    validate_url(item.get("downloadURL"))
    require(type(item.get("size")) is int and 0 < item["size"] <= 2**31 - 1,
            "Invalid size or exceeds Classic's signed 32-bit app size limit")
    for key in ("buildVersion", "minOSVersion", "maxOSVersion", "localizedDescription" if modern else "versionDescription"):
        if key in item:
            require(isinstance(item[key], str), f"{key} must be a string")


def validate_source(source, unique=True):
    require(isinstance(source, dict), "Source must be a JSON object")
    require(isinstance(source.get("name"), str) and bool(source["name"]), "Missing source name")
    apps = source.get("apps")
    require(isinstance(apps, list) and bool(apps), "apps must be a nonempty array")
    identifiers = []
    for app in apps:
        require(isinstance(app, dict), "App must be an object")
        for field in ("name", "bundleIdentifier", "developerName", "localizedDescription"):
            require(isinstance(app.get(field), str), f"Missing/string required: {field}")
            if field != "localizedDescription":
                require(bool(app[field].strip()), f"Empty {field}")
        identifiers.append(app["bundleIdentifier"])
        validate_url(app.get("iconURL"))
        if "screenshotURLs" in app:
            require(isinstance(app["screenshotURLs"], list), "screenshotURLs must be an array")
            for url in app["screenshotURLs"]:
                validate_url(url)
        if "versions" in app:
            require(isinstance(app["versions"], list) and bool(app["versions"]), "versions must be nonempty")
            for item in app["versions"]:
                validate_release(item, True)
        else:
            validate_release(app, False)
        if "appPermissions" in app:
            require(isinstance(app["appPermissions"], dict), "appPermissions must be an object")
        if "category" in app:
            require(app["category"] in ("developer", "entertainment", "games", "lifestyle", "other", "photo-video", "social", "utilities"), "Invalid category")
    if unique:
        require(len(set(identifiers)) == len(identifiers), "Duplicate bundle identifiers remain")
    news = source.get("news", [])
    require(isinstance(news, list), "news must be an array")
    for obj in [source, *apps, *news]:
        require(isinstance(obj, dict), "Metadata entry must be an object")
        if "tintColor" in obj:
            require(isinstance(obj["tintColor"], str) and bool(re.fullmatch(r"#?[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?", obj["tintColor"])), "Invalid tintColor")
    for field in ("iconURL", "headerURL", "website", "sourceURL"):
        if field in source:
            validate_url(source[field])
    for field in ("identifier", "subtitle", "description"):
        if field in source:
            require(isinstance(source[field], str), f"Invalid source {field}")
    if "featuredApps" in source:
        require(isinstance(source["featuredApps"], list) and all(isinstance(i, str) and i in identifiers for i in source["featuredApps"]), "Invalid featuredApps")
    news_ids = set()
    for item in news:
        require(isinstance(item, dict), "News item must be an object")
        for field in ("title", "identifier", "caption"):
            require(isinstance(item.get(field), str), f"Invalid news {field}")
        require(item["identifier"] not in news_ids, "Duplicate news identifier")
        news_ids.add(item["identifier"])
        date_key(item.get("date"))
        for field in ("imageURL", "url"):
            if field in item:
                validate_url(item[field])
        if "notify" in item:
            require(type(item["notify"]) is bool, "Invalid news notify")


def choose_newest(entries):
    candidates = entries[:]
    reason = "identical metadata"
    # Narrow the entire group at each stage; pairwise mixed comparisons can cycle.
    selectors = (
        ("numeric/SemVer version", lambda a: version_key(release(a)["version"])),
        ("release date", lambda a: date_key(release(a).get("date" if "versions" in a else "versionDate"))),
        ("numeric build", lambda a: version_key(release(a).get("buildVersion", ""))),
    )
    for label, key in selectors:
        values = [key(a) for a in candidates]
        if all(v is not None for v in values):
            maximum = max(values)
            candidates = [a for a, v in zip(candidates, values) if v == maximum]
            reason = label
            if len(candidates) == 1:
                return candidates[0], reason
    require(all(a == candidates[0] for a in candidates),
            f"Ambiguous duplicate {entries[0]['bundleIdentifier']!r}: no reliable ordering; refusing publication")
    return candidates[0], reason + " / identical metadata tie"


def transform(source, source_url):
    validate_source(source, unique=False)
    validate_url(source_url)
    groups = {}
    for app in source["apps"]:
        groups.setdefault(app["bundleIdentifier"], []).append(app)
    result = copy.deepcopy(source)
    result.update(name="CyPwn Clean", subtitle="Unofficial, automatically cleaned CyPwn source",
                  identifier="cypwn.clean", sourceURL=source_url,
                  description="Unofficial metadata mirror. Not affiliated with CyPwn or AltStore. App downloads come directly from upstream URLs.")
    retained = []
    duplicates = 0
    for bundle, entries in groups.items():
        if len(entries) == 1:
            winner = entries[0]
        else:
            duplicates += 1
            log(f"Duplicate bundleIdentifier: {bundle!r} ({len(entries)} entries)")
            winner, reason = choose_newest(entries)
            skipped_winner = False
            for app in entries:
                keep = app is winner and not skipped_winner
                skipped_winner |= keep
                log(f"  {'KEEP' if keep else 'REMOVE'} {app['name']!r} {release(app)['version']!r}; {reason}")
        retained.append(copy.deepcopy(winner))
    result["apps"] = retained
    validate_source(result)
    # Whole-record equality also proves that every retained asset URL is preserved.
    require(all(app in groups[app["bundleIdentifier"]] for app in retained), "App metadata changed")
    log(f"Upstream apps: {len(source['apps'])}; duplicate groups: {duplicates}; removed: {len(source['apps']) - len(retained)}; generated: {len(retained)}")
    return result


def guard_count(candidate, previous, min_apps=100, allow_large_drop=False):
    count = len(candidate["apps"])
    require(count >= min_apps, f"Only {count} apps; minimum is {min_apps}")
    if previous is not None:
        validate_source(previous)
        require(allow_large_drop or count >= len(previous["apps"]) * 0.8,
                "App count fell by more than 20%; inspect upstream before --allow-large-drop")


def write_atomic(path, source):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(source, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Read upstream JSON locally instead of fetching")
    parser.add_argument("--output", type=Path, default=Path("docs/source.json"))
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--previous-url", help="Published JSON; failures block updates")
    parser.add_argument("--bootstrap", action="store_true", help="Allow initial published URL 404")
    parser.add_argument("--allow-large-drop", action="store_true")
    parser.add_argument("--min-apps", type=int, default=100)
    args = parser.parse_args(argv)
    require(args.min_apps > 0, "min-apps must be positive")
    previous = None
    if args.previous_url:
        previous = fetch_json(args.previous_url, allow_missing=args.bootstrap)
    elif args.output.exists():
        previous = parse_json(args.output.read_bytes())
    upstream = parse_json(args.input.read_bytes()) if args.input else fetch_json(UPSTREAM)
    candidate = transform(upstream, args.source_url)
    guard_count(candidate, previous, args.min_apps, args.allow_large_drop)
    changed = candidate != previous
    log("Validation successful; retained app records and URLs are unchanged")
    write_atomic(args.output, candidate)
    log("Deployment required" if changed else "Unchanged; deployment skipped")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
            handle.write(f"changed={str(changed).lower()}\n")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, RecursionError) as exc:
        log(f"ERROR: {exc}")
        raise SystemExit(1)
