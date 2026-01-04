from __future__ import annotations

import io
import json
import re
from datetime import datetime, timezone
from typing import Dict, Optional, Set

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
S3_PREFIX = "drivesync"

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
}

FOLDER_CACHE: Dict[str, Dict] = {}
PROCESSED_FILE_IDS: Set[str] = set()

# ======================
# Helpers
# ======================

def safe_name(name: str) -> str:
    name = (name or "").strip()
    name = re.sub(r"[^\w.\- ()]", "_", name)
    name = re.sub(r"\s+", " ", name)
    return name[:150]


def stable_key(prefix: str, name: str, file_id: str) -> str:
    """
    Stable S3 key: one object per Drive file.
    Overwrites on update, no duplicates.
    """
    base = safe_name(name)
    return f"{prefix}{base}__{file_id}"


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


def resolve_folder_path(drive, parents) -> str:
    if not parents:
        return ""

    parent_id = parents[0]
    parts = []

    while parent_id:
        if parent_id in FOLDER_CACHE:
            meta = FOLDER_CACHE[parent_id]
        else:
            meta = drive.files().get(
                fileId=parent_id,
                fields="id,name,parents,mimeType",
                supportsAllDrives=True,
            ).execute()
            FOLDER_CACHE[parent_id] = meta

        if meta.get("mimeType") != "application/vnd.google-apps.folder":
            break

        parts.append(meta.get("name") or parent_id)
        p = meta.get("parents") or []
        parent_id = p[0] if p else None

    return "/".join(reversed(parts))


def download_bytes(drive, file_id: str) -> bytes:
    req = drive.files().get_media(fileId=file_id, supportsAllDrives=True)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()


def export_bytes(drive, file_id: str, export_mime: str) -> bytes:
    req = drive.files().export_media(fileId=file_id, mimeType=export_mime)
    buf = io.BytesIO()
    dl = MediaIoBaseDownload(buf, req)
    done = False
    while not done:
        _, done = dl.next_chunk()
    return buf.getvalue()


def resolve_shortcut(drive, file: dict) -> Optional[dict]:
    sd = file.get("shortcutDetails")
    if not sd:
        return None

    target_id = sd.get("targetId")
    if not target_id:
        return None

    return drive.files().get(
        fileId=target_id,
        fields="id,name,mimeType,modifiedTime,trashed,parents",
        supportsAllDrives=True,
    ).execute()


# ======================
# Lambda entrypoint
# ======================

def handler(event, context):
    secret = get_secret()
    creds = creds_from_secret(secret)
    drive = build("drive", "v3", credentials=creds)
    s3 = boto3.client("s3")

    token = ssm_get()

    # First run: initialize token
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

    while page_token:
        resp = drive.changes().list(
            pageToken=page_token,
            spaces="drive",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            fields=(
                "newStartPageToken,nextPageToken,"
                "changes(fileId,removed,file("
                "name,mimeType,modifiedTime,trashed,parents,shortcutDetails))"
            ),
        ).execute()

        for change in resp.get("changes", []):
            file_id = change.get("fileId")
            removed = change.get("removed", False)
            file = change.get("file") or {}

            if removed or file.get("trashed") or not file_id:
                skipped += 1
                continue

            # Shortcut resolution
            if file.get("mimeType") == "application/vnd.google-apps.shortcut":
                target = resolve_shortcut(drive, file)
                if not target or target.get("trashed"):
                    skipped += 1
                    continue
                file = target
                file_id = target["id"]

            if file_id in PROCESSED_FILE_IDS:
                skipped += 1
                continue
            PROCESSED_FILE_IDS.add(file_id)

            name = safe_name(file.get("name") or file_id)
            mime = file.get("mimeType", "application/octet-stream")
            modified = file.get("modifiedTime", "")

            folder_path = resolve_folder_path(drive, file.get("parents"))
            prefix = (
                f"{S3_PREFIX}/{safe_name(folder_path)}/"
                if folder_path
                else f"{S3_PREFIX}/"
            )

            # Skip folders
            if mime == "application/vnd.google-apps.folder":
                skipped += 1
                continue

            # Google Docs exports
            if mime in GOOGLE_EXPORTS:
                export_mime, ext = GOOGLE_EXPORTS[mime]
                out_name = name if name.lower().endswith(ext) else f"{name}{ext}"
                key = stable_key(prefix, out_name, file_id)

                data = export_bytes(drive, file_id, export_mime)
                s3.put_object(
                    Bucket=S3_BUCKET,
                    Key=key,
                    Body=data,
                    ContentType=export_mime,
                    Metadata={
                        "drive_file_id": file_id,
                        "drive_modified_time": modified,
                        "drive_source_mime": mime,
                    },
                )
                uploaded += 1
                continue

            # Skip non-exportable Google-native files
            if mime.startswith("application/vnd.google-apps."):
                print(f"Skipping non-exportable Google file type: {mime} name={name}")
                skipped += 1
                continue

            # Binary files
            key = stable_key(prefix, name, file_id)
            data = download_bytes(drive, file_id)
            s3.put_object(
                Bucket=S3_BUCKET,
                Key=key,
                Body=data,
                ContentType=mime,
                Metadata={
                    "drive_file_id": file_id,
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
