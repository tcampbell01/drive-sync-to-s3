# scripts/get_refresh_token.py
from __future__ import annotations

from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


def main() -> None:
    client_path = Path("secrets/google_oauth_client.json")
    if not client_path.exists():
        raise SystemExit(f"Missing {client_path}. Put your downloaded OAuth JSON there.")

    flow = InstalledAppFlow.from_client_secrets_file(str(client_path), scopes=SCOPES)
    creds = flow.run_local_server(port=0)

    print("\n=== REFRESH TOKEN (save this) ===")
    print(creds.refresh_token)
    print("=== END REFRESH TOKEN ===\n")

    # quick sanity check: list a few files
    service = build("drive", "v3", credentials=creds)
    resp = service.files().list(pageSize=5, fields="files(id,name)").execute()
    print("Sample files:")
    for f in resp.get("files", []):
        print(f"- {f['name']} ({f['id']})")


if __name__ == "__main__":
    main()
