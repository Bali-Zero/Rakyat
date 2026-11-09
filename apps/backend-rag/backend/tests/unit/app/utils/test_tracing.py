"""
Unit tests for tracing utilities
Target: >95% coverage
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

backend_path = Path(__file__).parent.parent.parent.parent.parent / "backend"
if str(backend_path) not in sys.path:
    sys.path.insert(0, str(backend_path))

from backend.app.utils.tracing import (
    add_span_event,
    get_tracer,
    set_span_attribute,
    set_span_status,
    trace_span,
)


class TestTracingUtilities:
    """Tests for tracing utilities"""

    def test_get_tracer_not_available(self):
        """Test getting tracer when OpenTelemetry not available"""
        with patch("backend.app.utils.tracing.OTEL_AVAILABLE", False):
            tracer = get_tracer()
            assert tracer is None

    @patch("backend.app.utils.tracing.OTEL_AVAILABLE", True)
    @patch("backend.app.utils.tracing.trace")
    def test_get_tracer_available(self, mock_trace):
        """Test getting tracer when OpenTelemetry available"""
        mock_tracer = MagicMock()
        mock_trace.get_tracer.return_value = mock_tracer

        tracer = get_tracer("test_tracer")
        assert tracer == mock_tracer
        mock_trace.get_tracer.assert_called_once_with("test_tracer")

    @patch("backend.app.utils.tracing.OTEL_AVAILABLE", True)
    @patch("backend.app.utils.tracing.trace")
    def test_get_tracer_exception(self, mock_trace):
        """Test getting tracer with exception"""
        mock_trace.get_tracer.side_effect = Exception("Tracer error")

        tracer = get_tracer()
        assert tracer is None

    @patch("backend.app.utils.tracing.OTEL_AVAILABLE", False)
    def test_trace_span_not_available(self):
        """Test trace_span when OpenTelemetry not available"""
        with trace_span("test_span") as span:
            assert span is None

    @patch("backend.app.utils.tracing.OTEL_AVAILABLE", True)
    @patch("backend.app.utils.tracing.get_tracer")
    def test_trace_span_available(self, mock_get_tracer):
        """Test trace_span when OpenTelemetry available"""
        mock_tracer = MagicMock()
        mock_span = MagicMock()
        mock_context = MagicMock()
        mock_context.__enter__ = MagicMock(return_value=mock_span)
        mock_context.__exit__ = MagicMock(return_value=False)
        mock_tracer.start_as_current_span.return_value = mock_context
        mock_get_tracer.return_value = mock_tracer

        with trace_span("test_span", {"key": "value"}) as span:
            assert span == mock_span
            mock_span.set_attribute.assert_called()
            mock_tracer.start_as_current_span.assert_called_once_with("test_span")

    @patch("backend.app.utils.tracing.OTEL_AVAILABLE", False)
    @patch("backend.app.utils.tracing.trace")
    def test_add_span_event_not_available(self, mock_trace):
        """Test add_span_event when OpenTelemetry not available"""
        # Should not raise exception, and must take the early-return path —
        # never touching `trace` at all.
        result = add_span_event("test_event", {"key": "value"})
        assert result is None
        mock_trace.get_current_span.assert_not_called()

    @patch("backend.app.utils.tracing.OTEL_AVAILABLE", True)
    @patch("backend.app.utils.tracing.trace")
    def test_add_span_event_available(self, mock_trace):
        """Test add_span_event when OpenTelemetry available"""
        mock_span = MagicMock()
        mock_span.is_recording.return_value = True
        mock_trace.get_current_span.return_value = mock_span

        add_span_event("test_event", {"key": "value"})
        mock_span.add_event.assert_called_once_with("test_event", attributes={"key": "value"})

    @patch("backend.app.utils.tracing.OTEL_AVAILABLE", False)
    @patch("backend.app.utils.tracing.trace")
    def test_set_span_attribute_not_available(self, mock_trace):
        """Test set_span_attribute when OpenTelemetry not available"""
        # Should not raise exception, and must take the early-return path —
        # never touching `trace` at all.
        result = set_span_attribute("key", "value")
        assert result is None
        mock_trace.get_current_span.assert_not_called()

    @patch("backend.app.utils.tracing.OTEL_AVAILABLE", True)
    @patch("backend.app.utils.tracing.trace")
    def test_set_span_attribute_available(self, mock_trace):
        """Test set_span_attribute when OpenTelemetry available"""
        mock_span = MagicMock()
        mock_span.is_recording.return_value = True
        mock_trace.get_current_span.return_value = mock_span

        set_span_attribute("key", "value")
        mock_span.set_attribute.assert_called_once_with("key", "value")

    @patch("backend.app.utils.tracing.OTEL_AVAILABLE", False)
    @patch("backend.app.utils.tracing.trace")
    def test_set_span_status_not_available(self, mock_trace):
        """Test set_span_status when OpenTelemetry not available"""
        # Should not raise exception, and must take the early-return path —
        # never touching `trace` at all.
        result = set_span_status("ok")
        assert result is None
        mock_trace.get_current_span.assert_not_called()


@pytest.mark.parametrize("helper", ["get_tracer", "trace_span", "attribute", "status"])
def test_run2_tracing_helper_fault_logs_are_class_only(helper, caplog, monkeypatch):
    import logging

    from backend.app.utils import tracing

    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(tracing, "OTEL_AVAILABLE", True)
    error = ValueError("RUN2_CANARY_EXCEPTION")
    if helper == "get_tracer":
        monkeypatch.setattr(tracing.trace, "get_tracer", MagicMock(side_effect=error))
        assert tracing.get_tracer() is None
    elif helper == "trace_span":
        tracer = MagicMock()
        tracer.start_as_current_span.side_effect = error
        monkeypatch.setattr(tracing, "get_tracer", lambda: tracer)
        with pytest.raises(RuntimeError, match="generator didn't yield"):
            with tracing.trace_span("synthetic.failure", safe_diagnostics=True):
                pass
        tracer.start_as_current_span.assert_called_once_with(
            "synthetic.failure", record_exception=False, set_status_on_exception=False
        )
    else:
        monkeypatch.setattr(tracing.trace, "get_current_span", MagicMock(side_effect=error))
        if helper == "attribute":
            assert tracing.set_span_attribute("technical", "value") is None
        else:
            assert tracing.set_span_status("error", "ValueError") is None
    assert "ValueError" in caplog.text
    assert "RUN2_CANARY_EXCEPTION" not in caplog.text
    assert all(record.exc_info is None or record.exc_info is False for record in caplog.records)
