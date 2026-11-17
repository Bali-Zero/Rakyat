"""
Google Sheets Service — Read/write operations via Service Account (DWD).

Uses the same credential chain as TeamDriveService:
1. Domain-Wide Delegation → impersonates zero@balizero.com
2. Direct Service Account fallback

The SA must have the target spreadsheet shared with it (Editor role),
or DWD must be configured with the Sheets scope.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import threading
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

CREDENTIALS_ENV_VAR = "GOOGLE_SERVICE_ACCOUNT_JSON"

_singleton_lock = threading.Lock()
_sheets_service_singleton: SheetsService | None = None


def get_sheets_service() -> SheetsService:
    """Return a shared singleton SheetsService instance."""
    global _sheets_service_singleton
    if _sheets_service_singleton is None:
        with _singleton_lock:
            if _sheets_service_singleton is None:
                _sheets_service_singleton = SheetsService()
    return _sheets_service_singleton


class SheetsService:
    """Thin wrapper around Google Sheets API v4."""

    def __init__(self) -> None:
        self._service: Any | None = None
        self._connected_as: str | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Credential helpers (in-memory SA JSON, never written to /tmp)
    # ------------------------------------------------------------------

    def _get_credentials_info(self) -> dict[str, Any] | None:
        env_creds = os.environ.get(CREDENTIALS_ENV_VAR) or os.environ.get("GOOGLE_SERVICE_ACCOUNT")
        if not env_creds:
            return None

        # Try raw JSON
        try:
            parsed = json.loads(env_creds)
            if isinstance(parsed, dict) and parsed.get("type") == "service_account":
                return parsed
        except json.JSONDecodeError as e:
            logger.debug("SA creds not raw JSON, trying base64: %s", e)

        # Try base64
        try:
            decoded = base64.b64decode(env_creds).decode("utf-8")
            parsed = json.loads(decoded)
            if isinstance(parsed, dict) and parsed.get("type") == "service_account":
                return parsed
        except Exception as e:
            logger.debug("SA creds base64 decode failed: %s", e)

        return None

    def _get_service(self) -> Any:
        if self._service is not None:
            return self._service

        creds_info = self._get_credentials_info()
        if not creds_info:
            raise FileNotFoundError(
                "Google credentials not found. Set GOOGLE_SERVICE_ACCOUNT_JSON.",
            )

        # Use Direct SA (DWD doesn't have Sheets scope in Workspace config).
        # The target spreadsheets must be shared with the SA email as Editor.
        credentials = service_account.Credentials.from_service_account_info(
            creds_info,
            scopes=SCOPES,
        )
        svc = build("sheets", "v4", credentials=credentials, cache_discovery=False)
        self._connected_as = "service-account"
        logger.info("[SHEETS] ✅ Direct SA mode (spreadsheets must be shared with SA)")

        self._service = svc
        return self._service

    # ------------------------------------------------------------------
    # Sync execution helpers (run in worker thread under httplib2 lock)
    # ------------------------------------------------------------------

    def _read_range_sync(self, spreadsheet_id: str, range_: str) -> list[list[str]]:
        with self._lock:
            svc = self._get_service()
            result = (
                svc.spreadsheets()
                .values()
                .get(spreadsheetId=spreadsheet_id, range=range_)
                .execute()
            )
            return result.get("values", [])

    def _write_range_sync(
        self,
        spreadsheet_id: str,
        range_: str,
        values: list[list[str]],
        value_input_option: str,
    ) -> dict:
        body = {"values": values}
        with self._lock:
            svc = self._get_service()
            return (
                svc.spreadsheets()
                .values()
                .update(
                    spreadsheetId=spreadsheet_id,
                    range=range_,
                    valueInputOption=value_input_option,
                    body=body,
                )
                .execute()
            )

    def _append_row_sync(
        self,
        spreadsheet_id: str,
        range_: str,
        values: list[list[str]],
        value_input_option: str,
    ) -> dict:
        body = {"values": values}
        with self._lock:
            svc = self._get_service()
            return (
                svc.spreadsheets()
                .values()
                .append(
                    spreadsheetId=spreadsheet_id,
                    range=range_,
                    valueInputOption=value_input_option,
                    body=body,
                )
                .execute()
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def read_range(self, spreadsheet_id: str, range_: str) -> list[list[str]]:
        """Read a range from a spreadsheet. Returns list of rows."""
        return await asyncio.to_thread(self._read_range_sync, spreadsheet_id, range_)

    async def write_range(
        self,
        spreadsheet_id: str,
        range_: str,
        values: list[list[str]],
        value_input_option: str = "USER_ENTERED",
    ) -> dict:
        """Write values to a range. Returns API response."""
        result = await asyncio.to_thread(
            self._write_range_sync,
            spreadsheet_id,
            range_,
            values,
            value_input_option,
        )
        logger.info(f"[SHEETS] ✅ Updated {result.get('updatedCells', 0)} cells in {range_}")
        return result

    async def append_row(
        self,
        spreadsheet_id: str,
        range_: str,
        values: list[list[str]],
        value_input_option: str = "USER_ENTERED",
    ) -> dict:
        """Append rows after the last row with data."""
        result = await asyncio.to_thread(
            self._append_row_sync,
            spreadsheet_id,
            range_,
            values,
            value_input_option,
        )
        logger.info(f"[SHEETS] ✅ Appended {len(values)} row(s) to {range_}")
        return result

    async def find_row_by_value(
        self,
        spreadsheet_id: str,
        range_: str,
        search_column: int,
        search_value: str,
    ) -> int | None:
        """
        Find the row number (1-based) where column matches value.

        Args:
            spreadsheet_id: Sheet ID
            range_: Range to search (e.g. 'Company!A:A')
            search_column: 0-based column index within the range
            search_value: Value to match (case-insensitive)

        Returns:
            1-based row number or None
        """
        rows = await self.read_range(spreadsheet_id, range_)
        for i, row in enumerate(rows):
            if len(row) > search_column:
                if row[search_column].strip().lower() == search_value.strip().lower():
                    return i + 1  # 1-based
        return None
