#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
from requests_oauthlib.oauth1_session import TokenRequestDenied
from requests_oauthlib import OAuth1Session


ROOT = Path(__file__).resolve().parent
MEDIA_DIR = ROOT / "media"
ALBUMS_MD = MEDIA_DIR / "albums.md"
DEFAULT_MANIFEST = ROOT / "upload_manifest.json"
DEFAULT_STATE = ROOT / "upload_state.json"
DEFAULT_TOKENS = ROOT / ".flickr_tokens.json"
DEFAULT_CREDENTIALS = ROOT / ".flickr_api_credentials.json"
REPORTS_DIR = ROOT / "upload_reports"

REST_URL = "https://www.flickr.com/services/rest"
UPLOAD_URL = "https://up.flickr.com/services/upload/"
REQUEST_TOKEN_URL = "https://www.flickr.com/services/oauth/request_token"
AUTHORIZE_URL = "https://www.flickr.com/services/oauth/authorize"
ACCESS_TOKEN_URL = "https://www.flickr.com/services/oauth/access_token"

MEDIA_EXTS = {".jpg", ".jpeg", ".png", ".gif"}
GLOBAL_FALLBACK_TAGS = ["flickr", "photo", "archive"]
CC_BY_NC_4_LICENSE_ID = "14"

CAMERA_TITLE_RE = re.compile(
    r"^(?:IMG[_-]?\d+|DSC[_-]?\d+|DSCN[_-]?\d+|DSCF[_-]?\d+|P\d{7,}|MVI[_-]?\d+)(?:\.[A-Za-z0-9]+)?$",
    re.IGNORECASE,
)
FLICKR_ID_RE = re.compile(r"_(\d+)_?[A-Za-z]?$")
HEADING_RE = re.compile(r"^#\s+(.+?)\s*$")


@dataclass
class AlbumMeta:
    folder: str
    title: str
    credit: str
    body_lines: list[str]
    start_line: int

    @property
    def description(self) -> str:
        lines: list[str] = []
        if self.credit:
            lines.append(self.credit)
            lines.append("")
        lines.extend(self.body_lines)
        return "\n".join(lines).strip()

    @property
    def photo_description_fallback(self) -> str:
        return "\n".join(part for part in [self.title, self.credit] if part).strip()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def parse_albums_md(path: Path = ALBUMS_MD) -> tuple[dict[str, AlbumMeta], list[dict[str, Any]]]:
    if not path.exists():
        return {}, [{"level": "error", "kind": "missing_albums_md", "path": str(path)}]

    lines = path.read_text(encoding="utf-8").splitlines()
    sections: list[tuple[str, int, list[str]]] = []
    current_name: str | None = None
    current_start = 0
    current_lines: list[str] = []
    issues: list[dict[str, Any]] = []

    for idx, line in enumerate(lines, start=1):
        match = HEADING_RE.match(line)
        if match:
            if current_name is not None:
                sections.append((current_name, current_start, current_lines))
            current_name = match.group(1).strip()
            current_start = idx
            current_lines = []
        elif current_name is not None:
            current_lines.append(line)

    if current_name is not None:
        sections.append((current_name, current_start, current_lines))

    albums: dict[str, AlbumMeta] = {}
    for name, start_line, body in sections:
        title = ""
        credit = ""
        description_lines: list[str] = []
        for raw in body:
            line = raw.rstrip()
            if line.strip() == "---":
                continue
            if line.startswith("Title:"):
                title = line.removeprefix("Title:").strip()
                continue
            if line.startswith("Credit:"):
                credit = line.removeprefix("Credit:").strip()
                continue
            description_lines.append(line)

        description_lines = trim_blank_lines(description_lines)
        if name in albums:
            issues.append({"level": "error", "kind": "duplicate_album_section", "album": name, "line": start_line})
        albums[name] = AlbumMeta(
            folder=name,
            title=title,
            credit=credit,
            body_lines=description_lines,
            start_line=start_line,
        )

    return albums, issues


def trim_blank_lines(lines: list[str]) -> list[str]:
    start = 0
    end = len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


def media_files(album_dir: Path) -> list[Path]:
    return sorted(
        (path for path in album_dir.iterdir() if path.is_file() and path.suffix.lower() in MEDIA_EXTS),
        key=media_sort_key,
    )


def media_sort_key(path: Path) -> tuple[int, int, int, str]:
    numbers = [int(value) for value in re.findall(r"\d+", path.stem)]
    flickr_id = extract_flickr_id(path) or 0
    sequence = numbers[-2] if len(numbers) >= 2 else None
    if sequence is not None:
        return (0, sequence, flickr_id, path.name.lower())
    if flickr_id:
        return (1, flickr_id, flickr_id, path.name.lower())
    return (2, 0, 0, path.name.lower())


def extract_flickr_id(path: Path) -> int | None:
    match = FLICKR_ID_RE.search(path.stem)
    if not match:
        return None
    return int(match.group(1))


def sidecar_path(media_path: Path) -> Path:
    return media_path.with_name(media_path.name + ".json")


def normalize_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        raw = " ".join(str(item) for item in value)
    else:
        raw = str(value)
    raw = raw.replace(",", " ")
    return [part.strip() for part in raw.split() if part.strip()]


def is_camera_default_title(title: str) -> bool:
    return bool(CAMERA_TITLE_RE.match(title.strip()))


def filename_title(path: Path) -> str:
    stem = path.stem
    flickr_id = extract_flickr_id(path)
    if flickr_id:
        stem = re.sub(rf"_?{flickr_id}_?[A-Za-z]?$", "", stem)
    return stem.replace("_", " ").replace("-", " ").strip() or path.stem


def upload_title(raw_title: str, album_title: str, media_path: Path) -> str:
    title = (raw_title or "").strip()
    if not title:
        title = filename_title(media_path)
    if is_camera_default_title(title):
        return f"{album_title} - {title}" if album_title else title
    return title


def album_common_tags(sidecars: list[Path]) -> list[str]:
    counter: Counter[str] = Counter()
    tagged_files = 0
    for path in sidecars:
        try:
            data = read_json(path)
        except Exception:
            continue
        tags = normalize_tags(data.get("Tags"))
        if tags:
            tagged_files += 1
            counter.update(tags)
    if not counter:
        return []

    threshold = max(2, tagged_files // 2)
    common = [tag for tag, count in counter.most_common() if count >= threshold]
    if common:
        return common
    return [tag for tag, _ in counter.most_common(8)]


def build_manifest(album_filter: str | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    albums, issues = parse_albums_md()
    if not MEDIA_DIR.exists():
        issues.append({"level": "error", "kind": "missing_media_dir", "path": rel(MEDIA_DIR)})
        folder_paths = {}
    else:
        folder_paths = {
            path.name: path
            for path in MEDIA_DIR.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        }

    for folder in sorted(folder_paths):
        if folder not in albums:
            issues.append({"level": "error", "kind": "folder_missing_album_section", "album": folder})

    for album_name in sorted(albums):
        if album_name not in folder_paths:
            issues.append({"level": "warning", "kind": "album_section_missing_folder", "album": album_name})

    manifest_albums = []
    if album_filter:
        selected_names = [album_filter]
    else:
        selected_names = [name for name in albums if name in folder_paths]
        selected_names.extend(sorted(name for name in folder_paths if name not in albums))
    for album_name in selected_names:
        album_dir = folder_paths.get(album_name)
        meta = albums.get(album_name)
        if album_dir is None:
            issues.append({"level": "error", "kind": "selected_album_missing_folder", "album": album_name})
            continue
        if meta is None:
            meta = AlbumMeta(album_name, "", "", [], 0)

        if not meta.title:
            issues.append({"level": "error", "kind": "album_missing_title", "album": album_name, "line": meta.start_line})
        if not meta.credit:
            issues.append({"level": "error", "kind": "album_missing_credit", "album": album_name, "line": meta.start_line})
        if not meta.body_lines:
            issues.append({"level": "warning", "kind": "album_missing_description", "album": album_name, "line": meta.start_line})

        album_media = media_files(album_dir)
        sidecars = [sidecar_path(path) for path in album_media if sidecar_path(path).exists()]
        borrowed_tags = album_common_tags(sidecars) or GLOBAL_FALLBACK_TAGS
        media_names = {path.name for path in album_media}

        photos = []
        for path in album_media:
            json_path = sidecar_path(path)
            data: dict[str, Any] = {}
            json_status = "ok"
            if json_path.exists():
                try:
                    loaded = read_json(json_path)
                    data = loaded if isinstance(loaded, dict) else {}
                    if not isinstance(loaded, dict):
                        json_status = "invalid_shape"
                        issues.append({"level": "warning", "kind": "sidecar_not_object", "path": rel(json_path)})
                except Exception as exc:
                    json_status = "malformed"
                    issues.append({"level": "warning", "kind": "sidecar_malformed", "path": rel(json_path), "error": str(exc)})
            else:
                json_status = "missing"
                issues.append({"level": "warning", "kind": "media_missing_sidecar", "path": rel(path)})

            raw_title = str(data.get("Title", "") or "")
            raw_description = str(data.get("Description", "") or "")
            tags = normalize_tags(data.get("Tags"))
            if json_status == "missing":
                tags = borrowed_tags
            elif not tags:
                tags = GLOBAL_FALLBACK_TAGS

            description = raw_description.strip() or meta.photo_description_fallback
            if not raw_title.strip():
                issues.append({"level": "warning", "kind": "photo_missing_title", "path": rel(path)})
            if not raw_description.strip():
                issues.append({"level": "info", "kind": "photo_description_fallback", "path": rel(path)})
            if not normalize_tags(data.get("Tags")):
                issues.append({"level": "info", "kind": "photo_tag_fallback", "path": rel(path)})

            photos.append(
                {
                    "path": rel(path),
                    "sidecar": rel(json_path) if json_path.exists() else None,
                    "sidecar_status": json_status,
                    "flickr_id": extract_flickr_id(path),
                    "title": upload_title(raw_title, meta.title, path),
                    "description": description,
                    "tags": tags,
                    "sort_key": media_sort_key(path),
                }
            )

        for json_path in sorted(album_dir.glob("*.json")):
            media_name = json_path.name.removesuffix(".json")
            if media_name not in media_names:
                issues.append({"level": "warning", "kind": "orphan_sidecar_ignored", "path": rel(json_path)})

        manifest_albums.append(
            {
                "folder": album_name,
                "title": meta.title,
                "credit": meta.credit,
                "description": meta.description,
                "photo_count": len(photos),
                "photos": photos,
            }
        )

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_dir": rel(MEDIA_DIR),
        "albums_md": rel(ALBUMS_MD),
        "license": {
            "name": "CC BY-NC 4.0",
            "url": "https://creativecommons.org/licenses/by-nc/4.0/",
            "flickr_license_id": CC_BY_NC_4_LICENSE_ID,
        },
        "fallback_tags": GLOBAL_FALLBACK_TAGS,
        "albums": manifest_albums,
    }
    return manifest, issues


def rel(path: Path) -> str:
    path = path.resolve()
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def issue_summary(issues: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for issue in issues:
        counts[f"{issue.get('level', 'unknown')}:{issue.get('kind', 'unknown')}"] += 1
    return dict(sorted(counts.items()))


def load_credentials(path: Path) -> dict[str, str]:
    key = os.environ.get("FLICKR_API_KEY")
    secret = os.environ.get("FLICKR_API_SECRET")
    if key and secret:
        return {"api_key": key, "api_secret": secret}
    if path.exists():
        data = read_json(path)
        key = data.get("api_key") or data.get("key")
        secret = data.get("api_secret") or data.get("secret")
        if key and secret:
            return {"api_key": key, "api_secret": secret}
    raise SystemExit(
        f"Missing Flickr API credentials. Set FLICKR_API_KEY/FLICKR_API_SECRET or create {path}."
    )


def load_tokens(path: Path) -> dict[str, str]:
    if not path.exists():
        raise SystemExit(f"Missing Flickr OAuth tokens. Run auth first to create {path}.")
    data = read_json(path)
    token = data.get("oauth_token")
    secret = data.get("oauth_token_secret")
    if not token or not secret:
        raise SystemExit(f"Invalid token file: {path}")
    return {"oauth_token": token, "oauth_token_secret": secret}


def flickr_session(credentials_path: Path, tokens_path: Path) -> OAuth1Session:
    credentials = load_credentials(credentials_path)
    tokens = load_tokens(tokens_path)
    return OAuth1Session(
        credentials["api_key"],
        client_secret=credentials["api_secret"],
        resource_owner_key=tokens["oauth_token"],
        resource_owner_secret=tokens["oauth_token_secret"],
    )


def rest_call(session: OAuth1Session, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {"method": method, "format": "json", "nojsoncallback": "1"}
    if params:
        payload.update({key: value for key, value in params.items() if value is not None})
    response = session.post(REST_URL, data=payload, timeout=60)
    response.raise_for_status()
    data = response.json()
    if data.get("stat") != "ok":
        raise RuntimeError(f"Flickr API error for {method}: {data}")
    return data


def upload_photo(
    session: OAuth1Session,
    photo: dict[str, Any],
    visibility: str,
) -> str:
    is_public = "1" if visibility == "public" else "0"
    params = {
        "title": photo["title"],
        "description": photo["description"],
        "tags": " ".join(photo["tags"]),
        "is_public": is_public,
        "is_friend": "0",
        "is_family": "0",
        "safety_level": "1",
        "content_type": "1",
    }
    path = ROOT / photo["path"]
    headers = {"Authorization": upload_oauth_header(session, UPLOAD_URL, params)}
    with path.open("rb") as handle:
        response = requests.post(UPLOAD_URL, data=params, files={"photo": handle}, headers=headers, timeout=180)
    if response.status_code >= 400:
        raise RuntimeError(f"Flickr upload HTTP {response.status_code} for {photo['path']}: {safe_flickr_error(response.text)}")
    root = ET.fromstring(response.content)
    if root.attrib.get("stat") != "ok":
        raise RuntimeError(f"Flickr upload error for {photo['path']}: {safe_flickr_error(response.text)}")
    photoid = root.findtext("photoid")
    if not photoid:
        raise RuntimeError(f"Flickr upload did not return a photoid for {photo['path']}: {response.text}")
    return photoid


def oauth_percent(value: Any) -> str:
    return quote(str(value), safe="~")


def upload_oauth_header(session: OAuth1Session, url: str, params: dict[str, Any]) -> str:
    client = session.auth.client
    oauth_params = {
        "oauth_consumer_key": client.client_key,
        "oauth_token": client.resource_owner_key,
        "oauth_nonce": secrets.token_hex(16),
        "oauth_timestamp": str(int(time.time())),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_version": "1.0",
    }
    signature_params = {**params, **oauth_params}
    normalized = "&".join(
        f"{oauth_percent(key)}={oauth_percent(value)}"
        for key, value in sorted(signature_params.items(), key=lambda item: (oauth_percent(item[0]), oauth_percent(item[1])))
    )
    base_string = "&".join(["POST", oauth_percent(url), oauth_percent(normalized)])
    signing_key = f"{oauth_percent(client.client_secret)}&{oauth_percent(client.resource_owner_secret)}"
    digest = hmac.new(signing_key.encode("utf-8"), base_string.encode("utf-8"), hashlib.sha1).digest()
    oauth_params["oauth_signature"] = base64.b64encode(digest).decode("ascii")
    return "OAuth " + ", ".join(
        f'{oauth_percent(key)}="{oauth_percent(value)}"'
        for key, value in sorted(oauth_params.items())
    )


def safe_flickr_error(text: str) -> str:
    if "oauth_problem=" in text:
        problem = re.search(r"oauth_problem=([^&\\s<]+)", text)
        return problem.group(0) if problem else "oauth_problem"
    return text[:500]


def load_state(path: Path) -> dict[str, Any]:
    if path.exists():
        return read_json(path)
    return {"photos": {}, "albums": {}}


def save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = datetime.now(timezone.utc).isoformat()
    write_json(path, state)


def cmd_auth(args: argparse.Namespace) -> int:
    credentials = load_credentials(Path(args.credentials))
    oauth = OAuth1Session(
        credentials["api_key"],
        client_secret=credentials["api_secret"],
        callback_uri="oob",
    )
    try:
        token = oauth.fetch_request_token(REQUEST_TOKEN_URL)
    except TokenRequestDenied as exc:
        message = str(exc)
        if "consumer_key_unknown" in message:
            print("Flickr rejected the API key. Check .flickr_api_credentials.json api_key.", file=sys.stderr)
        elif "signature_invalid" in message:
            print("Flickr recognized the API key but rejected the OAuth signature. Check .flickr_api_credentials.json api_secret.", file=sys.stderr)
        else:
            print("Flickr rejected the OAuth request token request. Check API credentials.", file=sys.stderr)
        return 2
    resource_owner_key = token.get("oauth_token")
    resource_owner_secret = token.get("oauth_token_secret")
    authorization_url = oauth.authorization_url(AUTHORIZE_URL, perms="write")
    print("Open this URL in a browser and authorize write access:")
    print(authorization_url)
    verifier = input("Paste the Flickr verifier code: ").strip()
    oauth = OAuth1Session(
        credentials["api_key"],
        client_secret=credentials["api_secret"],
        resource_owner_key=resource_owner_key,
        resource_owner_secret=resource_owner_secret,
        verifier=verifier,
    )
    access = oauth.fetch_access_token(ACCESS_TOKEN_URL)
    token_data = {
        "oauth_token": access["oauth_token"],
        "oauth_token_secret": access["oauth_token_secret"],
        "fullname": access.get("fullname"),
        "username": access.get("username"),
        "user_nsid": access.get("user_nsid"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    write_json(Path(args.tokens), token_data)
    print(f"Wrote OAuth tokens to {args.tokens}")
    return 0


def cmd_manifest(args: argparse.Namespace) -> int:
    manifest, issues = build_manifest(args.album)
    output = Path(args.output)
    write_json(output, manifest)
    report = {"issue_count": len(issues), "issue_summary": issue_summary(issues), "issues": issues}
    write_json(output.with_suffix(".validation.json"), report)
    print(f"Wrote manifest to {output}")
    print(f"Wrote validation report to {output.with_suffix('.validation.json')}")
    print(json.dumps({"albums": len(manifest["albums"]), "issues": len(issues), "issue_summary": report["issue_summary"]}, indent=2))
    return 1 if any(issue.get("level") == "error" for issue in issues) else 0


def cmd_validate(args: argparse.Namespace) -> int:
    manifest, issues = build_manifest(args.album)
    print(json.dumps({"albums": len(manifest["albums"]), "issues": len(issues), "issue_summary": issue_summary(issues)}, indent=2))
    if args.show_issues:
        for issue in issues:
            print(json.dumps(issue, ensure_ascii=False))
    return 1 if any(issue.get("level") == "error" for issue in issues) else 0


def cmd_upload(args: argparse.Namespace) -> int:
    manifest, issues = build_manifest(args.album)
    errors = [issue for issue in issues if issue.get("level") == "error"]
    if errors and not args.allow_validation_errors:
        print("Refusing to upload because validation has errors. Re-run validate for details.", file=sys.stderr)
        print(json.dumps(issue_summary(issues), indent=2), file=sys.stderr)
        return 2

    if args.visibility == "public" and not args.confirm_public_upload:
        print("Refusing public upload without --confirm-public-upload.", file=sys.stderr)
        return 2

    if args.dry_run:
        print(json.dumps({"dry_run": True, "albums": len(manifest["albums"]), "photo_count": sum(a["photo_count"] for a in manifest["albums"])}, indent=2))
        return 0

    session = flickr_session(Path(args.credentials), Path(args.tokens))
    state_path = Path(args.state)
    state = load_state(state_path)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    for album_index, album in enumerate(manifest["albums"], start=1):
        album_report = {
            "album": album["folder"],
            "title": album["title"],
            "started_at": datetime.now(timezone.utc).isoformat(),
            "visibility": args.visibility,
            "uploaded": [],
            "reused": [],
            "errors": [],
        }
        photo_ids: list[str] = []
        for photo_index, photo in enumerate(album["photos"], start=1):
            photo_key = photo["path"]
            if photo_key in state["photos"]:
                photo_id = state["photos"][photo_key]["photo_id"]
                if state["photos"][photo_key].get("visibility") != args.visibility:
                    set_photo_permissions(session, photo_id, args.visibility)
                    state["photos"][photo_key]["visibility"] = args.visibility
                    state["photos"][photo_key]["permissions_updated_at"] = datetime.now(timezone.utc).isoformat()
                    save_state(state_path, state)
                album_report["reused"].append({"path": photo_key, "photo_id": photo_id})
            else:
                print(f"[{album['folder']}] uploading {photo_index}/{album['photo_count']}: {photo_key}")
                try:
                    photo_id = upload_photo(session, photo, args.visibility)
                    rest_call(session, "flickr.photos.licenses.setLicense", {"photo_id": photo_id, "license_id": CC_BY_NC_4_LICENSE_ID})
                    state["photos"][photo_key] = {
                        "photo_id": photo_id,
                        "uploaded_at": datetime.now(timezone.utc).isoformat(),
                        "title": photo["title"],
                        "album": album["folder"],
                        "visibility": args.visibility,
                    }
                    save_state(state_path, state)
                    album_report["uploaded"].append({"path": photo_key, "photo_id": photo_id})
                    time.sleep(args.image_delay)
                except Exception as exc:
                    album_report["errors"].append({"path": photo_key, "error": str(exc)})
                    write_album_report(album_report)
                    save_state(state_path, state)
                    raise
            photo_ids.append(photo_id)

        if album["folder"] in state["albums"]:
            photoset_id = state["albums"][album["folder"]]["photoset_id"]
            rest_call(
                session,
                "flickr.photosets.editMeta",
                {"photoset_id": photoset_id, "title": album["title"], "description": album["description"]},
            )
            if photo_ids:
                rest_call(
                    session,
                    "flickr.photosets.editPhotos",
                    {"photoset_id": photoset_id, "primary_photo_id": photo_ids[0], "photo_ids": ",".join(photo_ids)},
                )
        elif photo_ids:
            result = rest_call(
                session,
                "flickr.photosets.create",
                {"title": album["title"], "description": album["description"], "primary_photo_id": photo_ids[0]},
            )
            photoset_id = result["photoset"]["id"]
            state["albums"][album["folder"]] = {
                "photoset_id": photoset_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "title": album["title"],
            }
            save_state(state_path, state)
            if len(photo_ids) > 1:
                rest_call(
                    session,
                    "flickr.photosets.editPhotos",
                    {"photoset_id": photoset_id, "primary_photo_id": photo_ids[0], "photo_ids": ",".join(photo_ids)},
                )

        album_report["finished_at"] = datetime.now(timezone.utc).isoformat()
        album_report["photoset_id"] = state["albums"].get(album["folder"], {}).get("photoset_id")
        write_album_report(album_report)
        print(
            f"[{album['folder']}] complete: "
            f"{len(album_report['uploaded'])} uploaded, {len(album_report['reused'])} reused, "
            f"photoset {album_report.get('photoset_id')}"
        )
        if album_index < len(manifest["albums"]):
            time.sleep(args.album_delay)

    return 0


def write_album_report(report: dict[str, Any]) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS_DIR / f"{stamp}_{report['album']}.json"
    write_json(path, report)


def set_photo_permissions(session: OAuth1Session, photo_id: str, visibility: str) -> None:
    rest_call(
        session,
        "flickr.photos.setPerms",
        {
            "photo_id": photo_id,
            "is_public": "1" if visibility == "public" else "0",
            "is_friend": "0",
            "is_family": "0",
            "perm_comment": "3",
            "perm_addmeta": "2",
        },
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate and upload local media folders to Flickr albums.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    manifest = subparsers.add_parser("manifest", help="Build a local upload manifest and validation report.")
    manifest.add_argument("--album", help="Only include one album folder.")
    manifest.add_argument("--output", default=str(DEFAULT_MANIFEST))
    manifest.set_defaults(func=cmd_manifest)

    validate = subparsers.add_parser("validate", help="Validate media/albums.md and local media/JSON files.")
    validate.add_argument("--album", help="Only validate one album folder.")
    validate.add_argument("--show-issues", action="store_true")
    validate.set_defaults(func=cmd_validate)

    auth = subparsers.add_parser("auth", help="Run Flickr OAuth and save access tokens.")
    auth.add_argument("--credentials", default=str(DEFAULT_CREDENTIALS))
    auth.add_argument("--tokens", default=str(DEFAULT_TOKENS))
    auth.set_defaults(func=cmd_auth)

    upload = subparsers.add_parser("upload", help="Upload photos and create/update Flickr albums.")
    upload_target = upload.add_mutually_exclusive_group(required=True)
    upload_target.add_argument("--album", help="Album folder to upload.")
    upload_target.add_argument("--all", action="store_true", help="Upload all albums from media/albums.md/folder order.")
    upload.add_argument("--visibility", choices=["private", "public"], default="private")
    upload.add_argument("--confirm-public-upload", action="store_true")
    upload.add_argument("--credentials", default=str(DEFAULT_CREDENTIALS))
    upload.add_argument("--tokens", default=str(DEFAULT_TOKENS))
    upload.add_argument("--state", default=str(DEFAULT_STATE))
    upload.add_argument("--image-delay", type=float, default=1.0)
    upload.add_argument("--album-delay", type=float, default=60.0)
    upload.add_argument("--allow-validation-errors", action="store_true")
    upload.add_argument("--dry-run", action="store_true")
    upload.set_defaults(func=cmd_upload)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
