from __future__ import annotations

import io
import json
import re
from datetime import datetime, timezone
from typing import Dict, Optional, List

import boto3
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# ======================
# Configuration
# ======================

SECRET_ID = "drivesync/google-oauth"
SSM_PARAM = "/drivesync/startPageToken"
S3_BUCKET = "google-drivesync-backup"
S3_PREFIX = "drivesync"  # final keys look like: drivesync/My Drive/path/file.ext

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

GOOGLE_EXPORTS = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
    # Optional later: drawings can be exported (PNG/PDF). Keeping out for now to avoid surprises.
    # "application/vnd.google-apps.drawing": ("image/png", ".png"),
}

# Cache folder metadata to reduce API calls across files in one invocation
FOLDER_CACHE: Dict[str, Dict] = {}

# ======================
# Helpers
# ======================


def now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def safe_name(name: str) -> str:
    """
    Safe for filenames (not for full paths). Used for S3 object "leaf" names.
    """
    name = (name or "").strip()
    name = re.sub(r"[^\w.\- ()]", "_", name)
    name = re.sub(r"\s+", " ", name)
    return name[:150]


def safe_segment(seg: str) -> str:
    """
    Safe for one folder segment (keeps '/' out).
    """
    seg = (seg or "").strip()
    seg = re.sub(r"[^\w.\- ()]", "_", seg)
    seg = re.sub(r"\s+", " ", seg)
    seg = seg.strip(" .")
    return (seg or "_")[:100]


def build_s3_key(prefix: str, folder_segments: List[str], filename: str) -> str:
    """
    Construct a stable S3 key that preserves folder structure as prefixes.
    """
    cleaned_segments = [safe_segment(s) for s in folder_segments if s]
    cleaned_filename = safe_name(filename)
    if cleaned_segments:
        return f"{prefix}/" + "/".join(cleaned_segments) + f"/{cleaned_filename}"
    return f"{prefix}/{cleaned_filename}"


def get_secret() -> dict:
    sm = boto3.client("secretsmanager")
    resp = sm.get_secret_value(SecretId=SECRET_ID)
    return json.loads(resp["SecretString"])


def creds_from_secret(secret: dict) -> Credentials:
    token = secret["token"]

    creds = Credentials(
        token=None,
        refresh_token=token["refresh_token"],
        token_uri=token.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=token["client_id"],
        client_secret=token["client_secret"],
        scopes=token.get("scopes", SCOPES),
    )
    creds.refresh(Request())
    return creds


def ssm_get() -> Optional[str]:
    ssm = boto3.client("ssm")
    try:
        resp = ssm.get_parameter(Name=SSM_PARAM)
        val = resp["Parameter"]["Value"].strip()
        return val if val else None
    except ssm.exceptions.ParameterNotFound:
        return None


def ssm_put(val: str) -> None:
    ssm = boto3.client("ssm")
    ssm.put_parameter(Name=SSM_PARAM, Value=val, Type="String", Overwrite=True)


def resolve_folder_segments(drive, parents) -> List[str]:
    """
    Build folder segments like: ["My Drive", "Top", "Child", "Grandchild"]
    by walking parents[0] upward.
    Uses a cache to reduce API calls.
    """
    # Always anchor to "My Drive" for readability (S3 doesn't have a true Drive root)
    segments: List[str] = ["My Drive"]

    if not parents:
        return segments

    parent_id = parents[0]
    parts = []

    while parent_id:
        if parent_id in FOLDER_CACHE:
            meta = FOLDER_CACHE[parent_id]
        else:
            meta = (
                drive.files()
                .get(
                    fileId=parent_id,
                    fields="id,name,parents,mimeType",
                    supportsAllDrives=True,
                )
                .execute()
            )
            FOLDER_CACHE[parent_id] = meta

        # Only folders contribute to the path
        if meta.get("mimeType") != "application/vnd.google-apps.folder":
            break

        parts.append(meta.get("name") or parent_id)
        p = meta.get("parents") or []
        parent_id = p[0] if p else None

    # reverse to root->leaf order, then append after "My Drive"
    parts = list(reversed(parts))
    segments.extend(parts)
    return segments


def download_bytes(drive, file_id: str) -> bytes:
    """
    Download binary content for non-Google-native files.
    (Will 403 for application/vnd.google-apps.* types.)
    """
    req = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()


def export_bytes(drive, file_id: str, export_mime: str) -> bytes:
    """
    Export Google Docs Editors files (Docs/Sheets/Slides) into a downloadable format.
    """
    req = drive.files().export_media(fileId=file_id, mimeType=export_mime)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()


def resolve_shortcut(drive, file: dict) -> Optional[dict]:
    """
    If this is a Drive shortcut, return the target file metadata.
    Otherwise return None.
    """
    sd = file.get("shortcutDetails")
    if not sd:
        return None

    target_id = sd.get("targetId")
    if not target_id:
        return None

    target = (
        drive.files()
        .get(
            fileId=target_id,
            fields="id,name,mimeType,modifiedTime,trashed,parents,shortcutDetails",
            supportsAllDrives=True,
        )
        .execute()
    )
    return target


# ======================
# Lambda entrypoint
# ======================


def handler(event, context):
    secret = get_secret()
    creds = creds_from_secret(secret)
    drive = build("drive", "v3", credentials=creds)
    s3 = boto3.client("s3")

    token = ssm_get()

    # First run: initialize startPageToken
    if not token or token == "INIT":
        start = drive.changes().getStartPageToken().execute()
        ssm_put(start["startPageToken"])
        return {
            "status": "initialized",
            "message": "Saved startPageToken. Run again after changes.",
        }

    uploaded = 0
    skipped = 0
    page_token = token

    # Deduplicate within a single invocation (Drive can emit multiple changes for same file)
    seen_file_ids = set()

    while page_token:
        resp = (
            drive.changes()
            .list(
                pageToken=page_token,
                spaces="drive",
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                fields=(
                    "newStartPageToken,nextPageToken,"
                    "changes(fileId,removed,file("
                    "name,mimeType,modifiedTime,trashed,parents,shortcutDetails))"
                ),
            )
            .execute()
        )

        for change in resp.get("changes", []):
            change_file_id = change.get("fileId")
            removed = change.get("removed", False)
            file = change.get("file") or {}

            if removed or file.get("trashed") or not change_file_id:
                skipped += 1
                continue

            name = file.get("name") or change_file_id
            mime = file.get("mimeType", "application/octet-stream")
            modified = file.get("modifiedTime", "")

            # --- Handle shortcuts first (we want stable keys based on the *target* file) ---
            if mime == "application/vnd.google-apps.shortcut":
                target = resolve_shortcut(drive, file)
                if not target or target.get("trashed"):
                    skipped += 1
                    continue

                # Replace current metadata with target metadata
                file = target
                change_file_id = target.get("id")
                name = target.get("name") or change_file_id
                mime = target.get("mimeType", "application/octet-stream")
                modified = target.get("modifiedTime", modified)

            # Dedupe (after shortcut resolution so we dedupe on real target id)
            if change_file_id in seen_file_ids:
                skipped += 1
                continue
            seen_file_ids.add(change_file_id)

            # Skip folders (not downloadable)
            if mime == "application/vnd.google-apps.folder":
                skipped += 1
                continue

            # Build stable folder segments and stable key (overwrite in place)
            folder_segments = resolve_folder_segments(drive, file.get("parents"))
            # Note: folder_segments already starts with ["My Drive", ...]
            # filename will be adjusted for exports below
            base_filename = safe_name(name)

            # --- Export Docs/Sheets/Slides (stable .docx/.xlsx/.pptx name) ---
            if mime in GOOGLE_EXPORTS:
                export_mime, ext = GOOGLE_EXPORTS[mime]
                out_name = base_filename if base_filename.lower().endswith(ext) else f"{base_filename}{ext}"

                key = build_s3_key(S3_PREFIX, folder_segments, out_name)

                data = export_bytes(drive, change_file_id, export_mime)
                s3.put_object(
                    Bucket=S3_BUCKET,
                    Key=key,
                    Body=data,
                    ContentType=export_mime,
                    Metadata={
                        "drive_file_id": change_file_id,
                        "drive_modified_time": modified,
                        "drive_source_mime": mime,
                    },
                )
                uploaded += 1
                continue

            # Any other Google-native types cannot be downloaded with get_media()
            if mime.startswith("application/vnd.google-apps."):
                print(
                    f"Skipping non-exportable Google file type: {mime} "
                    f"name={base_filename} id={change_file_id}"
                )
                skipped += 1
                continue

            # --- Binary files (regular downloads) ---
            key = build_s3_key(S3_PREFIX, folder_segments, base_filename)

            data = download_bytes(drive, change_file_id)
            s3.put_object(
                Bucket=S3_BUCKET,
                Key=key,
                Body=data,
                ContentType=mime,
                Metadata={
                    "drive_file_id": change_file_id,
                    "drive_modified_time": modified,
                },
            )
            uploaded += 1

        if "newStartPageToken" in resp:
            ssm_put(resp["newStartPageToken"])

        page_token = resp.get("nextPageToken")

    return {
        "status": "ok",
        "uploaded": uploaded,
        "skipped": skipped,
    }
