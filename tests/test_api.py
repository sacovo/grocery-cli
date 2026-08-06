"""The HTTP API's browser-free surface: routing, auth, validation, refusals.

Nothing here touches Chrome - every request either never reaches a store client or
is rejected before one is opened.
"""

import pytest
from fastapi.testclient import TestClient

from grocery.api import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _no_ambient_config(monkeypatch):
    """Tests must not inherit the developer's token or store selection."""
    monkeypatch.delenv("GROCERY_API_TOKEN", raising=False)
    monkeypatch.delenv("GROCERY_STORE", raising=False)


def test_health_is_open_without_token():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_token_is_enforced_when_set(monkeypatch):
    monkeypatch.setenv("GROCERY_API_TOKEN", "sekrit")
    assert client.get("/health").status_code == 401
    assert client.get("/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
    ok = client.get("/health", headers={"Authorization": "Bearer sekrit"})
    assert ok.status_code == 200


def test_unknown_store_is_rejected():
    response = client.get("/lists", params={"store": "aldi"})
    assert response.status_code == 400
    assert "aldi" in response.json()["detail"]


def test_store_env_default_is_validated(monkeypatch):
    monkeypatch.setenv("GROCERY_STORE", "aldi")
    assert client.get("/lists").status_code == 400


def test_coop_list_delete_is_refused():
    response = client.request("DELETE", "/lists/whatever", params={"store": "coop"})
    assert response.status_code == 400
    assert "delete" in response.json()["detail"]


def test_search_requires_query():
    assert client.get("/search").status_code == 422


def test_recipe_rejects_empty_input():
    response = client.post("/recipe", json={"ingredients": []})
    assert response.status_code == 400


def test_recipe_rejects_unknown_preference():
    response = client.post(
        "/recipe", json={"ingredients": ["500 g Spaghetti"], "prefer": "fancy"}
    )
    assert response.status_code == 400
    assert "Unknown preference" in response.json()["detail"]


def test_openapi_lists_all_endpoints():
    paths = client.get("/openapi.json").json()["paths"]
    for path in (
        "/health",
        "/status",
        "/search",
        "/categories",
        "/products/{product_id}",
        "/list",
        "/lists",
        "/lists/active",
        "/lists/{name_or_id}",
        "/list/add",
        "/list/remove",
        "/recipe",
    ):
        assert path in paths
