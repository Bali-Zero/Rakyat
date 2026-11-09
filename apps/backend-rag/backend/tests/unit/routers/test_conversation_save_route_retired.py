"""The web `/save` route was retired: nothing calls it, and production persists through the stream."""

from backend.app.setup.app_factory import create_app
from backend.app.setup.route_walk import iter_leaf_routes

RETIRED_PATH = "/api/bali-zero/conversations/save"


def _conversation_routes() -> set[tuple[str, str]]:
    return {
        (path, method)
        for route in iter_leaf_routes(create_app())
        for path in [route.path]
        for method in (route.methods or ())
        if path.startswith("/api/bali-zero/conversations")
    }


def test_save_route_is_absent_from_the_app_route_table() -> None:
    routes = _conversation_routes()
    assert not [path for path, _ in routes if path == RETIRED_PATH]
    assert ("/api/bali-zero/conversations/save", "POST") not in routes


def test_the_remaining_conversation_routes_are_still_registered() -> None:
    # Control: the walk sees this router, so the absence above is not a blind walk.
    routes = _conversation_routes()
    assert ("/api/bali-zero/conversations/history", "GET") in routes
    assert ("/api/bali-zero/conversations/list", "GET") in routes
    assert ("/api/bali-zero/conversations/clear", "DELETE") in routes
    assert ("/api/bali-zero/conversations/{conversation_id}", "DELETE") in routes
