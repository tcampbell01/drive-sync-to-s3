# scripts/list_changes.py
from __future__ import annotations

from pathlib import Path
from typing import Optional

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

CLIENT_JSON = Path("secrets/google_oauth_client.json")
TOKEN_FILE = Path("secrets/drive_start_page_token.txt")


def get_creds():
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_JSON), scopes=SCOPES)
    # This will re-use your browser auth; it’s okay for now.
    return flow.run_local_server(port=0)


def read_token() -> Optional[str]:
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    return None


def write_token(token: str) -> None:
    TOKEN_FILE.write_text(token + "\n", encoding="utf-8")


def main() -> None:
    if not CLIENT_JSON.exists():
        raise SystemExit(f"Missing {CLIENT_JSON}. Put your OAuth JSON there.")

    creds = get_creds()
    drive = build("drive", "v3", credentials=creds)

    token = read_token()
    if token is None:
        # First run: establish a checkpoint
        start = drive.changes().getStartPageToken().execute()
        token = start["startPageToken"]
        write_token(token)
        print(f"Initialized startPageToken and saved to {TOKEN_FILE}")
        print("Run this script again after you edit/upload a Drive file.")
        return

    # Normal run: list changes since last token
    page_token = token
    total = 0

    while page_token is not None:
        resp = drive.changes().list(
            pageToken=page_token,
            spaces="drive",
            fields="newStartPageToken,nextPageToken,changes(fileId,removed,file(name,mimeType,modifiedTime,trashed))",
        ).execute()

        changes = resp.get("changes", [])
        for ch in changes:
            total += 1
            removed = ch.get("removed", False)
            f = ch.get("file") or {}
            print(
                f"- fileId={ch.get('fileId')} "
                f"removed={removed} "
                f"name={f.get('name')} "
                f"mimeType={f.get('mimeType')} "
                f"trashed={f.get('trashed')} "
                f"modifiedTime={f.get('modifiedTime')}"
            )

        # Update checkpoint if provided
        if "newStartPageToken" in resp:
            write_token(resp["newStartPageToken"])
            print(f"\nUpdated startPageToken saved to {TOKEN_FILE}")

        page_token = resp.get("nextPageToken")

    print(f"\nTotal changes seen: {total}")


if __name__ == "__main__":
    main()
