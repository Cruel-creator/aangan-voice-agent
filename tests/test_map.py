"""Components map page and its public status endpoint."""

import json

from app.config import get_settings


def test_map_page_is_served_and_linked_from_every_page(world):
    page = world.client.get("/map")
    assert page.status_code == 200 and "Components map" in page.text
    assert page.headers["cache-control"] == "no-cache"
    for path in ("/", "/dashboard", "/map"):
        assert 'href="/map"' in world.client.get(path).text
    for asset in ("/assets/map.css", "/assets/map.js"):
        assert world.client.get(asset).status_code == 200


def test_status_reports_switches_and_never_secrets(world, monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_secret_value")
    monkeypatch.setenv("VAANI_API_KEY", "vaani_secret_value")
    get_settings.cache_clear()
    s = world.client.get("/dashboard/api/status").json()
    assert s["vaani_sync"] is True and s["email"] == {"key": True, "studio_head": False}
    assert s["telegram"] is True and s["designers"] == 2 and s["website"]["on"] is False
    text = json.dumps(s)
    assert "secret_value" not in text and "TEST-TOKEN" not in text
