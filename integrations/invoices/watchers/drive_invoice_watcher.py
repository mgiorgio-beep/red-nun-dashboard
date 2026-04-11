"""
Google Drive Invoice Watcher
Checks "Red Nun Invoices" folder for new files, downloads them,
and runs OCR processing.
"""
import os
import pickle
import io
import logging
from datetime import datetime
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.auth.transport.requests import Request

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
logger = logging.getLogger(__name__)

FOLDER_ID = '175w8j4FQ1j-4NMvPAVE7Dt4BVNxtA5Mc'
TOKEN_PATH = '/opt/rednun/google_token.pickle'
DOWNLOAD_DIR = '/opt/rednun/invoice_images'
PROCESSED_FILE = '/opt/rednun/drive_processed.txt'

def get_service():
    with open(TOKEN_PATH, 'rb') as f:
        creds = pickle.load(f)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(TOKEN_PATH, 'wb') as f:
            pickle.dump(creds, f)
    return build('drive', 'v3', credentials=creds)

def get_processed():
    if os.path.exists(PROCESSED_FILE):
        with open(PROCESSED_FILE) as f:
            return set(f.read().strip().split('\n'))
    return set()

def mark_processed(file_id):
    with open(PROCESSED_FILE, 'a') as f:
        f.write(file_id + '\n')
def trash_file(service, file_id, filename):
    """Move a file to trash in Google Drive"""
    try:
        service.files().update(fileId=file_id, body={'trashed': True}).execute()
        logger.info(f'Trashed {filename} (ID: {file_id}) from Google Drive after download')
        return True
    except Exception as e:
        logger.warning(f'Failed to trash {filename} from Google Drive: {e}')
        return False


def main():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    service = get_service()
    processed = get_processed()

    results = service.files().list(
        q=f"'{FOLDER_ID}' in parents and trashed=false",
        fields='files(id, name, mimeType, createdTime)',
        orderBy='createdTime desc',
        pageSize=50
    ).execute()

    files = results.get('files', [])
    new_files = [f for f in files if f['id'] not in processed]

    if not new_files:
        logger.info('No new files found.')
        return

    logger.info(f'Found {len(new_files)} new file(s)')

    for f in new_files:
        name = f['name']
        file_id = f['id']
        mime = f['mimeType']
        logger.info(f'Downloading: {name}')

        # Handle Google Docs exports
        if mime == 'application/pdf' or mime.startswith('image/'):
            request = service.files().get_media(fileId=file_id)
        elif mime == 'application/vnd.google-apps.document':
            request = service.files().export_media(fileId=file_id, mimeType='application/pdf')
            name = name + '.pdf'
        else:
            request = service.files().get_media(fileId=file_id)

        filepath = os.path.join(DOWNLOAD_DIR, name)
        with io.FileIO(filepath, 'wb') as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()

        mark_processed(file_id)
        logger.info(f'Saved: {filepath}')
        
        # Trash the file from Google Drive after successful download
        trash_file(service, file_id, name)

    logger.info(f'Downloaded {len(new_files)} file(s). Trashed from Drive. Local invoice watcher will process.')

if __name__ == '__main__':
    main()
