"""ocr_pages with Apple Vision as the primary engine (INTAKE_OCR_PROVIDER=apple_vision).

The engine itself is covered by test_apple_vision_ocr.py. Here it is replaced by fakes, so
the routing contract is pinned without a Mac:

  * the provider switch parses the three aliases and nothing else turns it on;
  * a page Apple Vision reads is NOT sent to Ollama, and says so (via/model);
  * an empty, too-short, failed, timed-out or unavailable read falls through to the Ollama
    cascade UNCHANGED, and is never mistaken for an unreachable Ollama endpoint;
  * the default provider never touches Apple Vision at all (innocence);
  * recognised text never reaches the log.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import pytest

from backend.services.intake import apple_vision_ocr as av
from backend.services.intake import classify as cls
from backend.services.intake.preprocess import PageImage

_TEXT = "PASPOR X1234567\nNo. 0123456789012"


class _Page:
    def __init__(self, index: int = 0, data: bytes = b"page-bytes", text: str | None = None):
        self.index = index
        self.png_bytes = data
        self.text = text


class _Engine:
    """Stand-in for apple_vision_ocr: records calls, answers or raises as told."""

    def __init__(
        self, observations: list[dict[str, Any]] | None = None, raises: Exception | None = None
    ):
        self.observations = observations if observations is not None else _lines(_TEXT)
        self.raises = raises
        self.calls: list[tuple[bytes, float]] = []

    async def recognize(self, image: bytes, timeout: float) -> dict[str, Any]:
        self.calls.append((image, timeout))
        if self.raises is not None:
            raise self.raises
        return {"width": 100, "height": 100, "observations": self.observations}


def _lines(text: str) -> list[dict[str, Any]]:
    return [
        {"text": line, "confidence": 1.0, "x": 0.1, "y": 0.1 + 0.1 * i, "w": 0.5, "h": 0.05}
        for i, line in enumerate(text.split("\n"))
    ]


class _Ollama:
    """Stand-in for classify._ollama_vision."""

    def __init__(self, answer: str = "OLLAMA TEXT", raises: Exception | None = None):
        self.answer = answer
        self.raises = raises
        self.models: list[str] = []

    async def __call__(self, model: str, b64: str, **kwargs: Any) -> tuple[str, str | None]:
        self.models.append(model)
        if self.raises is not None:
            raise self.raises
        return self.answer, ""


@pytest.fixture
def apple(monkeypatch: pytest.MonkeyPatch):
    """Provider switched on, host 'available', engine replaced; returns the fake engine."""
    engine = _Engine()
    monkeypatch.setenv("INTAKE_OCR_PROVIDER", "apple_vision")
    monkeypatch.delenv("INTAKE_APPLE_VISION_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("INTAKE_APPLE_VISION_MIN_CHARS", raising=False)
    monkeypatch.setattr(av, "available", lambda: True)
    monkeypatch.setattr(av, "recognize", engine.recognize)
    return engine


@pytest.fixture
def ollama(monkeypatch: pytest.MonkeyPatch) -> _Ollama:
    fake = _Ollama()
    monkeypatch.setattr(cls, "_ollama_vision", fake)
    return fake


# ---------------------------------------------------------------------------
# Provider parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("apple_vision", "apple_vision"),
        ("apple-vision", "apple_vision"),
        ("vision", "apple_vision"),
        ("  Apple_Vision \n", "apple_vision"),
        ("APPLE-VISION", "apple_vision"),
        ("gemini", "ollama"),  # retired cloud provider fails closed to local Ollama
        ("gemini_first", "ollama"),
        ("ollama", "ollama"),
        ("", "ollama"),
        ("apple", "ollama"),  # not an alias: a typo must fail closed to local Ollama
        ("applevision", "ollama"),
        ("visionkit", "ollama"),
        ("anything-else", "ollama"),
    ],
)
def test_ocr_provider_parsing(monkeypatch: pytest.MonkeyPatch, raw: str, expected: str) -> None:
    monkeypatch.setenv("INTAKE_OCR_PROVIDER", raw)
    assert cls._ocr_provider() == expected


def test_the_code_default_stays_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INTAKE_OCR_PROVIDER", raising=False)
    assert cls._ocr_provider() == "ollama"


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


async def test_a_page_apple_vision_reads_skips_ollama(apple: _Engine, ollama: _Ollama) -> None:
    out = await cls.ocr_pages([_Page(0, b"original-page-bytes")])

    assert out == [
        {
            "page": 0,
            "text": _TEXT,
            "confidence": cls._heuristic_confidence(_TEXT),
            "model": "apple-vision-rev3",
            "via": "apple_vision",
        }
    ]
    assert ollama.models == []  # Ollama never ran
    assert apple.calls[0][0] == b"original-page-bytes"  # the page as preprocess produced it
    assert apple.calls[0][1] == cls.APPLE_VISION_TIMEOUT_SECONDS == 60


async def test_a_text_layer_page_never_reaches_apple_vision(
    apple: _Engine, ollama: _Ollama
) -> None:
    digital = "KEMENTERIAN INVESTASI NOMOR INDUK BERUSAHA 1234567890123 " * 3
    page = PageImage(index=0, png_bytes=b"png", enhanced=False, text=digital)

    out = await cls.ocr_pages([page])

    assert out[0]["via"] == "textlayer"
    assert apple.calls == [] and ollama.models == []


async def test_timeout_and_min_chars_are_configurable(
    monkeypatch: pytest.MonkeyPatch, apple: _Engine, ollama: _Ollama
) -> None:
    monkeypatch.setenv("INTAKE_APPLE_VISION_TIMEOUT_SECONDS", "12")
    monkeypatch.setenv("INTAKE_APPLE_VISION_MIN_CHARS", "3")
    apple.observations = _lines("AB C")  # 3 non-whitespace characters

    out = await cls.ocr_pages([_Page()])

    assert apple.calls[0][1] == 12
    assert out[0]["via"] == "apple_vision" and out[0]["text"] == "AB C"


@pytest.mark.parametrize("raw", ["0", "-5", "soon", "nan", "inf", ""])
def test_a_bad_timeout_value_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("INTAKE_APPLE_VISION_TIMEOUT_SECONDS", raw)
    assert cls._apple_vision_timeout() == cls.APPLE_VISION_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Fall-through: the Ollama chain runs exactly as before
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    [
        "no_observations",
        "blank_observations",
        "unavailable",
        "engine_error",
        "unexpected_exception",
        "host_not_available",
    ],
)
async def test_every_non_read_falls_back_to_ollama(
    monkeypatch: pytest.MonkeyPatch, apple: _Engine, ollama: _Ollama, case: str
) -> None:
    if case == "no_observations":
        apple.observations = []
    elif case == "blank_observations":
        apple.observations = _lines("   \n\t")
    elif case == "unavailable":
        apple.raises = av.AppleVisionUnavailable("preparing")
    elif case == "engine_error":
        apple.raises = av.AppleVisionError("timeout")
    elif case == "unexpected_exception":
        apple.raises = RuntimeError("boom")
    else:
        monkeypatch.setattr(av, "available", lambda: False)

    out = await cls.ocr_pages([_Page(0)])

    assert out[0]["via"] == "response"  # the Ollama primary answered
    assert out[0]["text"] == "OLLAMA TEXT"
    assert out[0]["model"] == cls._resolve_ocr_model()
    assert ollama.models == [cls._resolve_ocr_model()]


def _helper_answer(box: str = '"x": 0.1, "y": 0.1, "w": 0.5, "h": 0.05') -> str:
    return (
        '{"ok": true, "width": 1, "height": 1, "observations": '
        f'[{{"text": "PASPOR X1234567", {box}}}]}}'
    )


@pytest.mark.parametrize(
    ("answer", "returncode"),
    [
        (_helper_answer(), 9),  # a good answer from a helper that then exited non-zero
        (_helper_answer(), -9),  # ... or was killed by a signal
        (_helper_answer('"x": 1' + "0" * 400 + ', "y": 0.1, "w": 0.5, "h": 0.05'), 0),  # 10**400
        (_helper_answer('"x": NaN, "y": 0.1, "w": 0.5, "h": 0.05'), 0),
        (_helper_answer('"x": 0.1, "y": 0.1, "w": Infinity, "h": 0.05'), 0),
        (_helper_answer('"x": 0.1, "y": 0.1, "w": 0.5, "h": -0.05'), 0),  # negative height
    ],
)
async def test_a_helper_answer_the_driver_refuses_falls_back_to_ollama(
    monkeypatch: pytest.MonkeyPatch, apple: _Engine, ollama: _Ollama, answer: str, returncode: int
) -> None:
    """The real _parse judges the wire; only the subprocess is replaced."""

    async def recognize(image: bytes, timeout: float) -> dict[str, Any]:
        return av._parse(answer.encode(), returncode)

    monkeypatch.setattr(av, "recognize", recognize)

    out = await cls.ocr_pages([_Page(0)])

    assert out[0]["via"] == "response" and out[0]["text"] == "OLLAMA TEXT"
    assert ollama.models == [cls._resolve_ocr_model()]


@pytest.mark.parametrize("error", [OverflowError("int too large to convert to float"), TypeError()])
async def test_an_exception_while_reading_the_answer_falls_back_to_ollama(
    monkeypatch: pytest.MonkeyPatch, apple: _Engine, ollama: _Ollama, error: Exception
) -> None:
    """Safety net behind _parse: row grouping is inside the boundary, not after it."""

    def broken(observations: object) -> str:
        raise error

    monkeypatch.setattr(av, "group_rows", broken)

    out = await cls.ocr_pages([_Page(0)])

    assert out[0]["via"] == "response" and ollama.models == [cls._resolve_ocr_model()]


async def test_cancellation_is_not_swallowed_by_the_fallback(
    apple: _Engine, ollama: _Ollama
) -> None:
    apple.raises = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await cls.ocr_pages([_Page(0)])

    assert ollama.models == []


async def test_the_seven_characters_boundary(apple: _Engine, ollama: _Ollama) -> None:
    apple.observations = _lines("ABCDEFG")  # 7 < 8 -> Ollama
    assert (await cls.ocr_pages([_Page()]))[0]["via"] == "response"

    apple.observations = _lines("ABCDEFGH")  # 8 -> Apple Vision
    assert (await cls.ocr_pages([_Page()]))[0]["via"] == "apple_vision"


async def test_an_apple_vision_failure_is_not_an_unreachable_endpoint(
    apple: _Engine, ollama: _Ollama
) -> None:
    # Every Apple Vision failure class, then an Ollama that reads nothing: the page is
    # honestly empty. It must NOT raise ConnectError (which would retry the whole document).
    apple.raises = av.AppleVisionUnavailable("compile_failed_recently")
    ollama.answer = ""

    out = await cls.ocr_pages([_Page()])

    assert out[0]["via"] == "empty" and out[0]["text"] == ""


async def test_a_dead_ollama_is_still_transient_after_an_apple_vision_miss(
    apple: _Engine, ollama: _Ollama
) -> None:
    apple.observations = []
    ollama.raises = httpx.ConnectError("refused")

    with pytest.raises(httpx.ConnectError, match="unreachable"):
        await cls.ocr_pages([_Page()])


async def test_one_unreadable_page_does_not_change_the_next(
    monkeypatch: pytest.MonkeyPatch, apple: _Engine, ollama: _Ollama
) -> None:
    answers = iter([[], _lines(_TEXT)])

    async def first_blank_then_text(image: bytes, timeout: float) -> dict[str, Any]:
        return {"width": 1, "height": 1, "observations": next(answers)}

    monkeypatch.setattr(av, "recognize", first_blank_then_text)

    out = await cls.ocr_pages([_Page(0), _Page(1)])

    assert [p["via"] for p in out] == ["response", "apple_vision"]
    assert ollama.models == [cls._resolve_ocr_model()]  # only the page Apple Vision missed


# ---------------------------------------------------------------------------
# Innocence: nothing changes where the variable is unset or says something else
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", [None, "ollama", "", "apple", "gemini"])
async def test_other_providers_never_touch_apple_vision(
    monkeypatch: pytest.MonkeyPatch, ollama: _Ollama, provider: str | None
) -> None:
    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("Apple Vision must not be touched unless it is the provider")

    monkeypatch.setattr(av, "available", forbidden)
    monkeypatch.setattr(av, "recognize", forbidden)
    monkeypatch.setattr(av, "prepare", forbidden)
    if provider is None:
        monkeypatch.delenv("INTAKE_OCR_PROVIDER", raising=False)
    else:
        monkeypatch.setenv("INTAKE_OCR_PROVIDER", provider)
    monkeypatch.setattr(cls, "_note_cloud_ocr_blocked", lambda context: None)

    out = await cls.ocr_pages([_Page()])

    assert out[0]["via"] == "response" and out[0]["text"] == "OLLAMA TEXT"


async def test_non_darwin_host_with_the_switch_on_behaves_like_the_default(
    monkeypatch: pytest.MonkeyPatch, ollama: _Ollama
) -> None:
    """Fly runs Linux: even if the variable were set there, the page goes to Ollama."""
    monkeypatch.setenv("INTAKE_OCR_PROVIDER", "apple_vision")
    monkeypatch.setattr(av, "_platform", lambda: "linux")

    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("no recognition on a non-macOS host")

    monkeypatch.setattr(av, "recognize", forbidden)

    out = await cls.ocr_pages([_Page()])

    assert out[0]["via"] == "response" and ollama.models


# ---------------------------------------------------------------------------
# PII: counts and durations only
# ---------------------------------------------------------------------------


async def test_recognised_text_is_never_logged(
    caplog: pytest.LogCaptureFixture, apple: _Engine, ollama: _Ollama
) -> None:
    caplog.set_level(logging.DEBUG)

    await cls.ocr_pages([_Page(0)])  # a successful read
    apple.observations = _lines("X12 345")  # 6 chars -> too short, logged as a count only
    await cls.ocr_pages([_Page(1)])
    apple.raises = av.AppleVisionError("engine_decode_failed")
    await cls.ocr_pages([_Page(2)])
    apple.raises = RuntimeError("Passport X1234567 leaked into an exception message")
    await cls.ocr_pages([_Page(3)])

    assert "X1234567" not in caplog.text
    assert "0123456789012" not in caplog.text
    assert "PASPOR" not in caplog.text
    assert "apple_vision" in caplog.text  # the counts and the outcome ARE logged


# ---------------------------------------------------------------------------
# The classify stage's model label
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pages", "label"),
    [
        ([], None),
        (
            [("response", "qwen2.5vl:7b"), ("textlayer", "textlayer"), ("empty", "qwen2.5vl:7b")],
            None,
        ),
        ([("apple_vision", "apple-vision-rev3"), ("textlayer", "textlayer")], "apple-vision-rev3"),
        (
            [("apple_vision", "apple-vision-rev3"), ("fallback", "qwen2.5vl:7b")],
            "apple-vision-rev3+qwen2.5vl:7b",
        ),
    ],
)
def test_stage_label_names_the_engines_that_really_read_pages(
    pages: list[tuple[str, str]], label: str | None
) -> None:
    """None = Apple Vision read nothing: the registry OCR model, exactly as before."""
    page_dicts = [{"via": via, "model": model} for via, model in pages]
    assert cls._stage_ocr_model(page_dicts) == (label or cls._resolve_ocr_model())
