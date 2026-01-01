from __future__ import annotations

import argparse
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
CLIENT_JSON = Path("secrets/google_oauth_client.json")


def get_creds():
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_JSON), scopes=SCOPES)
    return flow.run_local_server(port=0)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--file-id", required=True)
    p.add_argument("--out", required=True, help="Output path, e.g. downloads/file.pdf")
    args = p.parse_args()

    if not CLIENT_JSON.exists():
        raise SystemExit(f"Missing {CLIENT_JSON}")

    creds = get_creds()
    drive = build("drive", "v3", credentials=creds)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Fetch basic metadata so we can print what we’re downloading
    meta = drive.files().get(fileId=args.file_id, fields="id,name,mimeType,size").execute()
    print(f"Downloading: {meta.get('name')} ({meta.get('mimeType')})")

    request = drive.files().get_media(fileId=args.file_id)

    with out_path.open("wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                print(f"  progress: {int(status.progress() * 100)}%")

    print(f"Saved to: {out_path}")


if __name__ == "__main__":
    main()
