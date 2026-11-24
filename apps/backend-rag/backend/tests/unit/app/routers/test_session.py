"""
Unit tests for session router
Target: >95% coverage
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

backend_path = Path(__file__).parent.parent.parent.parent.parent / "backend"
if str(backend_path) not in sys.path:
    sys.path.insert(0, str(backend_path))

from backend.app.routers.session import get_session_service, router


@pytest.fixture
def mock_session_service():
    """Mock SessionService"""
    service = MagicMock()
    service.health_check = AsyncMock(return_value=True)
    return service


@pytest.fixture
def app(mock_session_service):
    """Create FastAPI app with router and dependency override"""
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session_service] = lambda: mock_session_service
    return app


@pytest.fixture
def client(app):
    """Create test client"""
    return TestClient(app)


class TestSessionRouter:
    """Tests for session router"""

    def test_get_session_service(self):
        """Test getting session service"""
        # Reset global
        import backend.app.routers.session as module

        module._session_service = None

        with patch("backend.app.routers.session.SessionService") as mock_service_class:
            mock_service = MagicMock()
            mock_service_class.return_value = mock_service
            service = get_session_service()
            assert service == mock_service

    def test_health_check(self, client, mock_session_service):
        """The health check is the one route left"""
        response = client.get("/api/sessions/health")
        assert response.status_code == 200
        assert response.json() == {"success": True, "service": "session"}

    def test_health_check_failure(self, client, mock_session_service):
        """A failing health check answers success false, never a 500"""
        mock_session_service.health_check.side_effect = Exception("redis down")
        response = client.get("/api/sessions/health")
        assert response.status_code == 200
        assert response.json()["success"] is False

    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("post", "/api/sessions/create"),
            ("get", "/api/sessions/abc"),
            ("put", "/api/sessions/abc"),
            ("put", "/api/sessions/abc/ttl"),
            ("delete", "/api/sessions/abc"),
            ("post", "/api/sessions/abc/extend"),
            ("post", "/api/sessions/abc/extend-custom"),
            ("get", "/api/sessions/abc/info"),
            ("get", "/api/sessions/abc/export"),
            ("get", "/api/sessions/analytics/overview"),
            ("post", "/api/sessions/cleanup"),
        ],
    )
    def test_the_conversation_routes_are_retired(self, client, mock_session_service, method, path):
        """No route reads, writes or lists a conversation session any more"""
        response = getattr(client, method)(path)
        assert response.status_code in (404, 405)
        assert mock_session_service.method_calls == []
        assert [route.path for route in router.routes] == ["/api/sessions/health"]
