import io
import logging
import os
from pathlib import Path

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

log = logging.getLogger("drive")
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]


class GoogleDriveClient:
    def __init__(self, folder_id: str):
        self.folder_id = folder_id
        creds = Credentials(
            token=None,
            refresh_token=os.environ["GDRIVE_REFRESH_TOKEN"],
            client_id=os.environ["GDRIVE_CLIENT_ID"],
            client_secret=os.environ["GDRIVE_CLIENT_SECRET"],
            token_uri="https://oauth2.googleapis.com/token",
            scopes=SCOPES,
        )
        creds.refresh(Request())
        self.service = build("drive", "v3", credentials=creds, cache_discovery=False)

    def _find_file_id(self, filename: str) -> str:
        safe = filename.replace("\\", "\\\\").replace("'", "\\'")
        query = f"name = '{safe}' and '{self.folder_id}' in parents and trashed = false"
        res = self.service.files().list(
            q=query, fields="files(id, name, mimeType)", pageSize=5,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        files = res.get("files", [])
        if not files:
            raise FileNotFoundError(f"'{filename}' not found in Drive folder {self.folder_id}")
        return files[0]["id"]

    def download(self, filename: str, dest_dir: str) -> Path:
        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        out_path = dest / Path(filename).name

        file_id = self._find_file_id(filename)
        request = self.service.files().get_media(fileId=file_id, supportsAllDrives=True)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request, chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        out_path.write_bytes(buf.getvalue())
        log.info("Downloaded %s (%d bytes)", filename, out_path.stat().st_size)
        return out_path