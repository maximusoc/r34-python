"""One-off helper: run locally to obtain GDRIVE_REFRESH_TOKEN.

Usage:
    pip install google-auth-oauthlib
    python get_refresh_token.py
"""
import os

from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

load_dotenv()  # also picks up a local .env file, same as main.py

try:
    client_id = os.environ["GDRIVE_CLIENT_ID"]
    client_secret = os.environ["GDRIVE_CLIENT_SECRET"]
except KeyError as e:
    raise SystemExit(f"Missing environment variable: {e.args[0]} (set it or add it to .env)")

flow = InstalledAppFlow.from_client_config(
    {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    },
    scopes=SCOPES,
)

# access_type=offline + prompt=consent forces Google to return a refresh token.
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

print("\nGDRIVE_REFRESH_TOKEN =", creds.refresh_token)
