from fastapi.testclient import TestClient

from app.api.app import create_app


def test_liveness_returns_ok(settings):
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/health/live", headers={"X-Request-ID": "test-request"})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["X-Request-ID"] == "test-request"
