import asyncio
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from backend.services.misc.image_generation_service import ImageGenerationService

router = APIRouter(prefix="/media", tags=["media"])
logger = logging.getLogger(__name__)

_MAX_MEDIA_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB OOM bound
_BLOCKED_MEDIA_EXTENSIONS = frozenset(
    {
        ".html",
        ".htm",
        ".svg",
        ".xhtml",
        ".xml",
        ".js",
        ".mjs",
        ".cjs",
        ".php",
        ".py",
        ".sh",
        ".exe",
    }
)


class _UploadTooLargeError(Exception):
    """Raised when an uploaded media stream exceeds _MAX_MEDIA_UPLOAD_BYTES."""


class _BoundedReader:
    """Wrap a binary stream so reads abort once max_bytes is exceeded."""

    def __init__(self, stream: Any, max_bytes: int) -> None:
        self._stream = stream
        self._max_bytes = max_bytes
        self._total = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self._max_bytes - self._total
        to_read = remaining + 1 if size is None or size < 0 else min(size, remaining + 1)
        chunk = self._stream.read(to_read)
        if not chunk:
            return chunk
        self._total += len(chunk)
        if self._total > self._max_bytes:
            raise _UploadTooLargeError()
        return chunk


def _copy_upload_to_path_sync(stream: Any, file_path: Any, max_bytes: int) -> None:
    bounded = _BoundedReader(stream, max_bytes)
    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(bounded, buffer)
    except _UploadTooLargeError:
        unlink_fn = getattr(file_path, "unlink", None)
        if callable(unlink_fn):
            try:
                unlink_fn(missing_ok=True)
            except TypeError:
                pass
        raise


class ImagePrompt(BaseModel):
    prompt: str


@router.post("/generate-image")
async def generate_image(request: ImagePrompt) -> Any:
    """
    Generate an image from a text prompt using pollinations.ai fallback.

    This endpoint always succeeds if prompt is valid, returning a pollinations.ai URL.
    No API key required.
    """
    try:
        logger.info(f"🎨 Image generation request: prompt='{request.prompt[:100]}...'")

        # Initialize service (uses pollinations.ai fallback, no API key needed)
        service = ImageGenerationService(api_key="dummy")  # Pollinations doesn't need real key

        # Generate image
        result = await service.generate_image(request.prompt)

        logger.info(
            f"🎨 Service result: success={result['success']}, url={result.get('url', 'N/A')[:100]}",
        )

        if result["success"]:
            response = {
                "success": True,
                "url": result["url"],
                "prompt": result.get("prompt"),
                "service": result.get("service", "pollinations_fallback"),
            }
            logger.info(f"✅ Image generation successful: {response['url'][:100]}")
            return response
        # Service returned an error
        error_msg = result.get("error", "Unknown error")
        error_details = result.get("details", "")
        logger.error("❌ Image generation failed: %s - %s", error_msg, error_details)

        # Return proper HTTP status codes for different error types
        if "not configured" in error_msg:
            raise HTTPException(status_code=503, detail=result)
        if "Invalid prompt" in error_msg:
            raise HTTPException(status_code=400, detail=result)
        raise HTTPException(status_code=500, detail=result)

    except HTTPException:
        # Re-raise HTTP exceptions as-is
        raise
    except Exception as e:
        logger.error("❌ Unexpected image generation error: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail={"success": False, "error": "Internal server error", "details": str(e)},
        ) from e


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)) -> dict[str, Any]:
    """
    Upload a file (image, audio, doc) to the server.
    Returns the URL/path to the file.
    """
    try:
        raw_filename = file.filename or "upload"
        ext_lower = os.path.splitext(raw_filename)[1].lower()
        if ext_lower in _BLOCKED_MEDIA_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail="Unsupported or active file extension",
            )

        raw_size = getattr(file, "size", None)
        if isinstance(raw_size, int) and raw_size > _MAX_MEDIA_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail="File exceeds maximum upload size (25 MB)",
            )

        # Create uploads directory if not exists
        upload_dir = Path("static/uploads")
        upload_dir.mkdir(parents=True, exist_ok=True)

        # Generate unique filename
        file_ext = Path(raw_filename).suffix
        filename = f"{uuid.uuid4()}{file_ext}"
        file_path = upload_dir / filename

        # Save file in worker thread with strict byte bound
        try:
            await asyncio.to_thread(
                _copy_upload_to_path_sync,
                file.file,
                file_path,
                _MAX_MEDIA_UPLOAD_BYTES,
            )
        except _UploadTooLargeError as exc:
            raise HTTPException(
                status_code=413,
                detail="File exceeds maximum upload size (25 MB)",
            ) from exc

        # In a real app, you'd return a public URL.
        # For now, we return the relative static path we can serve.
        # Assuming '/static' is mounted.
        # If not, we might need to adjust or return just the path for internal use.
        return {
            "success": True,
            "filename": file.filename,
            "url": f"/static/uploads/{filename}",
            "type": file.content_type,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("File upload error: %s", e)
        raise HTTPException(
            status_code=500,
            detail={"success": False, "error": "Upload failed", "details": str(e)},
        ) from e
