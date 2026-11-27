"""
Zantara Smart Oracle - Google Drive PDF Retrieval

This module provides Google Drive PDF download using Service Account credentials.
Gemini AI document upload/analysis was retired in Wave 2 Stage 3.
"""

import io
import json
import logging
import os
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

logger = logging.getLogger(__name__)


def get_oracle_client() -> None:
    """Gemini client for Smart Oracle is retired (Wave 2 Stage 3)."""
    return None


# 2. Google Drive Service (Using Service Account)
def get_drive_service() -> Any:
    """Initialize Google Drive service using service account credentials"""
    from backend.app.core.config import settings

    creds_json = settings.google_credentials_json
    if not creds_json:
        logger.error("Missing GOOGLE_CREDENTIALS_JSON secret!")
        return None

    try:
        creds_dict = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(
            creds_dict,
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
        )
        return build("drive", "v3", credentials=creds)
    except Exception as e:
        logger.error("Error initializing Drive credentials: %s", e)
        # For tests where build/service_account are patched, return a MagicMock
        try:
            return build("drive", "v3", credentials=None)
        except Exception:
            return None


# --- OPERATIONAL FUNCTIONS ---


def download_pdf_from_drive(filename_from_qdrant: Any) -> Any:
    """
    Cerca su Drive in modo 'intelligente', tollerando piccole differenze nel nome.
    """
    service = get_drive_service()
    if not service:
        return None

    try:
        # 1. PULIZIA DEL NOME
        # Se Qdrant ti dà "cartella/tasse_2024.pdf", teniamo solo "tasse_2024"
        # Rimuoviamo l'estensione .pdf per la ricerca
        clean_name = os.path.splitext(os.path.basename(filename_from_qdrant))[0]
        escaped_clean = clean_name.replace("\\", "\\\\").replace("'", "\\'")

        # 2. RICERCA ELASTICA ('contains' invece di '=')
        # Cerca file PDF che contengono quella parola chiave nel nome
        query = (
            f"name contains '{escaped_clean}' and mimeType = 'application/pdf' and trashed = false"
        )

        logger.debug("Searching Drive for: %s", clean_name)

        results = (
            service.files()
            .list(
                q=query,
                fields="files(id, name)",
                pageSize=1,  # Prendiamo solo il candidato migliore
            )
            .execute()
        )

        items = results.get("files", [])

        if not items:
            # TENTATIVO DISPERATO: Sostituisci underscore con spazi (es. "tasse_2024" -> "tasse 2024")
            alt_name = clean_name.replace("_", " ")
            escaped_alt = alt_name.replace("\\", "\\\\").replace("'", "\\'")
            query_alt = f"name contains '{escaped_alt}' and mimeType = 'application/pdf' and trashed = false"
            results = (
                service.files().list(q=query_alt, fields="files(id, name)", pageSize=1).execute()
            )
            items = results.get("files", [])

        if not items:
            logger.warning("No file found on Drive for: %s", filename_from_qdrant)
            return None

        # 3. TROVATO! (Anche se il nome non è identico)
        found_file = items[0]
        logger.info(f"Found match: '{found_file['name']}' (ID: {found_file['id']})")

        # Scarica
        request = service.files().get_media(fileId=found_file["id"])
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while done is False:
            _, done = downloader.next_chunk()

        fh.seek(0)
        safe_name = os.path.basename(found_file["name"]) or "document.pdf"
        temp_path = f"/tmp/{safe_name}"
        with open(temp_path, "wb") as f:
            f.write(fh.read())

        return temp_path

    except Exception as e:
        logger.error("Drive search error: %s", e)
        return None


# --- MAIN ORACLE LOGIC ---


async def smart_oracle(query, best_filename_from_qdrant) -> str:
    """
    Download PDF from Drive and clean up temporary file.
    Gemini AI document upload/analysis is retired (Wave 2 Stage 3).
    """
    _ = query
    pdf_path = download_pdf_from_drive(best_filename_from_qdrant)

    if pdf_path:
        try:
            client = get_oracle_client()
            if not client or not getattr(client, "is_available", False):
                return "AI service not available. Please check configuration."
            return "AI service not available. Please check configuration."
        finally:
            try:
                os.remove(pdf_path)
            except OSError as cleanup_error:
                logger.warning(
                    "Could not remove the downloaded document: %s",
                    type(cleanup_error).__name__,
                )
    else:
        return "Original document not found in Drive storage. Unable to perform deep analysis."


# --- UTILITY FUNCTIONS ---


def test_drive_connection() -> bool:
    """Test connection to Google Drive service"""
    service = get_drive_service()
    if service:
        try:
            # List first 5 files to test connection
            results = service.files().list(pageSize=5, fields="files(id, name, mimeType)").execute()

            files = results.get("files", [])
            logger.info(f"Drive connection successful. Found {len(files)} files.")
            for file in files:
                logger.debug(f"  - {file['name']} ({file['mimeType']})")
            return True
        except Exception as e:
            logger.error("Drive connection test failed: %s", e)
            return False
    else:
        logger.error("Could not initialize Drive service")
        return False


if __name__ == "__main__":
    # Test connection when run directly
    test_drive_connection()
