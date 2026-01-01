#!/usr/bin/env python3
"""
Incrementally sync Google Drive changes to Amazon S3.

Features:
- Uses Drive Changes API (efficient, incremental)
- Exports Google Docs/Sheets/Slides
- Preserves My Drive folder structure in S3
- Uploads timestamped backups
- Non-interactive OAuth (token cached locally)
"""

from __future__ import annotations

import io
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

import boto3
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

# --------------------
# Configuration
# --------------------

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

CLIENT_JSON = Path("secrets/google_oauth_client.json")
TOKEN_JSON = Path("secrets/google_token.json")
STATE_FILE = Path("state/start_page_token.json")

AWS_PROFILE = "google-drivesync-local"
AWS_REGION = "us-east-1"

S3_BUCKET = "google-drivesync-backup"
S3_PREFIX = "drivesync"

# Google native formats → export formats
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

# Cache for folder metadata to avoid repeated API calls
FOLDER_CACHE: Dict[str, Dict] = {}

# --------------------
# Helpers
# --------------------

def safe_name(name: str) -> str:
    return name.replace("/", "_").strip()


def now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# --------------------
# Auth
# --------------------

def get_creds() -> Credentials:
    creds = None

    if TOKEN_JSON.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_JSON), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CLIENT_JSON),
                scopes=SCOPES,
            )
            creds = flow.run_local_server(port=0)

        TOKEN_JSON.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_JSON.write_text(creds.to_json(), encoding="utf-8")

    return creds


# --------------------
# Drive helpers
# --------------------

def load_start_token() -> str | None:
    if not STATE_FILE.exists():
        return None
    return json.loads(STATE_FILE.read_text()).get("startPageToken")


def save_start_token(token: str) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps({"startPageToken": token}, indent=2))


def resolve_folder_path(drive, parents: List[str] | None) -> str:
    """
    Walk up the My Drive folder tree and build a human-readable path.
    """
    if not parents:
        return ""

    parent_id = parents[0]
    parts: List[str] = []

    while parent_id:
        if parent_id in FOLDER_CACHE:
            meta = FOLDER_CACHE[parent_id]
        else:
            meta = drive.files().get(
                fileId=parent_id,
                fields="id,name,parents,mimeType",
            ).execute()
            FOLDER_CACHE[parent_id] = meta

        if meta.get("mimeType") != "application/vnd.google-apps.folder":
            break

        parts.append(meta.get("name") or parent_id)
        parent_id = (meta.get("parents") or [None])[0]

    return "/".join(reversed(parts))


# --------------------
# Main sync
# --------------------

def main() -> None:
    creds = get_creds()
    drive = build("drive", "v3", credentials=creds)

    session = boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    s3 = session.client("s3")

    start_token = load_start_token()
    if not start_token:
        start_token = drive.changes().getStartPageToken().execute()["startPageToken"]

    uploaded = 0
    skipped = 0
    page_token = start_token

    while page_token:
        resp = drive.changes().list(
            pageToken=page_token,
            spaces="drive",
            includeRemoved=False,
            fields=(
                "newStartPageToken,nextPageToken,"
                "changes(fileId,file(name,mimeType,modifiedTime,trashed,parents))"
            ),
        ).execute()

        for change in resp.get("changes", []):
            f = change.get("file")
            if not f or f.get("trashed"):
                skipped += 1
                continue

            file_id = change["fileId"]
            name = f["name"]
            mime = f["mimeType"]
            ts = now_ts()

            folder_path = resolve_folder_path(drive, f.get("parents"))
            prefix = (
                f"{S3_PREFIX}/{safe_name(folder_path)}/"
                if folder_path
                else f"{S3_PREFIX}/"
            )

            # Google-native → export
            if mime in GOOGLE_EXPORTS:
                export_mime, ext = GOOGLE_EXPORTS[mime]
                out_name = f"{name}{ext}"
                request = drive.files().export_media(
                    fileId=file_id,
                    mimeType=export_mime,
                )
                buf = io.BytesIO()
                downloader = MediaIoBaseDownload(buf, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()

                buf.seek(0)
                key = f"{prefix}{ts}-{safe_name(out_name)}"
                s3.upload_fileobj(buf, S3_BUCKET, key)
                uploaded += 1
                print(f"Uploaded export: {key}")

            # Regular files
            else:
                request = drive.files().get_media(fileId=file_id)
                buf = io.BytesIO()
                downloader = MediaIoBaseDownload(buf, request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()

                buf.seek(0)
                key = f"{prefix}{ts}-{safe_name(name)}"
                s3.upload_fileobj(buf, S3_BUCKET, key)
                uploaded += 1
                print(f"Uploaded file: {key}")

        page_token = resp.get("nextPageToken")
        if "newStartPageToken" in resp:
            save_start_token(resp["newStartPageToken"])

    print(f"\nSync complete. Uploaded={uploaded}, Skipped={skipped}")


if __name__ == "__main__":
    main()
