"""Service API auth, health checks, *arr, qBittorrent, Plex and vision, over mocked HTTP."""

import json

import httpx
import pytest

from openowl.ops import media, policy, services, vision
from openowl.ops.config import ConfigError

CFG = {
    "services": {
        "radarr": {"kind": "arr", "url": "http://radarr", "api_key": "RKEY"},
        "romarr": {"kind": "arr", "url": "http://romarr", "api_key": "GKEY"},
        "qbit": {"kind": "qbittorrent", "url": "http://qb", "username": "u", "password": "p"},
        "plex": {"kind": "plex", "url": "http://plex", "token": "PTOK"},
        "yarrit": {"kind": "http", "url": "https://yarrit", "health": "/health"},
        "forge": {"kind": "http", "url": "https://forge", "auth": "query:key", "api_key": "Q"},
    },
}


@pytest.fixture
def http(ops_config):
    """Route every service request to a handler; returns the request log."""
    log = []
    handlers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        fn = handlers.get((request.url.host, request.url.path))
        if fn is None:
            return httpx.Response(404, text="no route")
        return fn(request)

    services.transport_factory = lambda svc: httpx.MockTransport(handler)

    def setup(routes, mode="read"):
        ops_config(CFG, mode=mode)
        handlers.clear()
        handlers.update(routes)
        return log

    return setup


def j(data, status=200):
    return lambda req: httpx.Response(status, json=data)


def test_service_api_attaches_auth(http):
    log = http({("radarr", "/api/v3/movie"): j([]), ("forge", "/x"): j({})})
    services.do_service_api("radarr", "GET", "/api/v3/movie")
    assert log[-1].headers["X-Api-Key"] == "RKEY"
    services.do_service_api("forge", "GET", "/x")
    assert log[-1].url.params["key"] == "Q"


def test_service_api_policy(http):
    http({})
    with pytest.raises(policy.PolicyError):
        services.do_service_api("radarr", "POST", "/api/v3/command", json_body={"name": "x"})
    with pytest.raises(ConfigError, match="Configured services"):
        services.do_service_api("nope", "GET", "/")


def test_services_status_reports_up_and_down(http):
    http({("radarr", "/api/v3/system/status"): j({"version": "5.1"}),
          ("yarrit", "/health"): j({}, status=503)})
    out = services.do_services_status(["radarr", "yarrit"])
    assert "1/2 services healthy" in out
    assert "OK   radarr" in out and "5.1" in out
    assert "DOWN yarrit" in out


def test_arr_queue_summary(http):
    http({("radarr", "/api/v3/queue"): j({"totalRecords": 1, "records": [
        {"id": 7, "title": "Some.Movie.2024", "status": "downloading", "size": 100, "sizeleft": 25,
         "trackedDownloadState": "downloading", "downloadClient": "qBittorrent",
         "statusMessages": [{"messages": ["stalled"]}]}]})})
    out = media.do_arr("radarr", "queue")
    assert "[7] Some.Movie.2024 - downloading 75%" in out and "stalled" in out


def test_arr_health_and_library(http):
    http({("radarr", "/api/v3/health"): j([{"type": "warning", "source": "IndexerCheck", "message": "No indexers"}]),
          ("radarr", "/api/v3/movie"): j([{"id": 1, "title": "Alien", "year": 1979, "monitored": True,
                                           "hasFile": True, "path": "/m/Alien"},
                                          {"id": 2, "title": "Heat", "year": 1995}])})
    assert "IndexerCheck: No indexers" in media.do_arr("radarr", "health")
    lib = media.do_arr("radarr", "library", term="ali")
    assert "1 matching" in lib and "Alien (1979)" in lib and "Heat" not in lib


def test_arr_search_needs_operate_and_builds_command(http):
    log = http({("radarr", "/api/v3/command"): j({"id": 99, "status": "queued"}),
                ("romarr", "/api/v1/command"): j({"id": 5, "status": "queued"})}, mode="read")
    with pytest.raises(policy.PolicyError):
        media.do_arr("radarr", "search", item_id=3)
    http({("radarr", "/api/v3/command"): j({"id": 99, "status": "queued"}),
          ("romarr", "/api/v1/command"): j({"id": 5, "status": "queued"})}, mode="operate")
    out = media.do_arr("radarr", "search", item_id=3)
    assert "MoviesSearch" in out and "id=99" in out
    assert json.loads(log[-1].content) == {"name": "MoviesSearch", "movieIds": [3]}
    media.do_arr("romarr", "search")
    assert json.loads(log[-1].content) == {"name": "MissingGameSearch"}
    media.do_arr("romarr", "rescan")
    assert json.loads(log[-1].content) == {"name": "RefreshLibrary"}
    with pytest.raises(ValueError, match="per-item"):
        media.do_arr("romarr", "search", item_id=4)


def test_arr_rejects_non_arr_service(http):
    http({})
    with pytest.raises(ValueError):
        media.do_arr("plex", "status")


def test_qbittorrent_login_and_list(http):
    log = http({
        ("qb", "/api/v2/auth/login"): lambda r: httpx.Response(200, text="Ok."),
        ("qb", "/api/v2/torrents/info"): j([{"hash": "abcdef0123456789", "state": "stalledDL",
                                              "progress": 0.5, "dlspeed": 0, "num_seeds": 0,
                                              "category": "radarr", "name": "Linux ISO"}]),
        ("qb", "/api/v2/transfer/info"): j({"dl_info_speed": 2e6, "up_info_speed": 0,
                                            "dht_nodes": 0, "connection_status": "firewalled"}),
    })
    out = media.do_qbittorrent("qbit", "list", filter="stalled")
    assert "stalledDL" in out and "50.0%" in out and "Linux ISO" in out
    assert log[0].url.path == "/api/v2/auth/login"
    assert "dht_nodes 0" in media.do_qbittorrent("qbit", "transfer")


def test_qbittorrent_delete_needs_admin(http):
    http({("qb", "/api/v2/auth/login"): lambda r: httpx.Response(200, text="Ok.")}, mode="operate")
    with pytest.raises(policy.PolicyError, match="admin"):
        media.do_qbittorrent("qbit", "delete", hashes="abc")


def test_qbittorrent_bad_login(http):
    http({("qb", "/api/v2/auth/login"): lambda r: httpx.Response(200, text="Fails.")})
    with pytest.raises(RuntimeError, match="login failed"):
        media.do_qbittorrent("qbit", "list")


def test_plex_sections_and_refresh(http):
    log = http({
        ("plex", "/library/sections"): j({"MediaContainer": {"Directory": [
            {"key": "1", "title": "Movies", "type": "movie", "Location": [{"path": "/mnt/usb2/Movie"}]}]}}),
        ("plex", "/library/sections/1/refresh"): lambda r: httpx.Response(200),
    }, mode="operate")
    out = media.do_plex("plex", "sections")
    assert "[1] Movies (movie) /mnt/usb2/Movie" in out
    assert log[-1].headers["X-Plex-Token"] == "PTOK"
    assert "section 1" in media.do_plex("plex", "refresh", section="1")


@pytest.mark.parametrize("api,url_suffix,reply,expected", [
    ("openai", "/chat/completions", {"choices": [{"message": {"content": "A login form"}}]}, "A login form"),
    ("ollama", "/api/chat", {"message": {"content": "Desktop"}}, "Desktop"),
    ("anthropic", "/v1/messages", {"content": [{"type": "text", "text": "Dialog"}]}, "Dialog"),
])
def test_vision_backends(ops_config, tmp_path, api, url_suffix, reply, expected):
    ops_config({"vision": {"api": api, "base_url": "http://vlm", "model": "m", "api_key": "VK"}})
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["headers"] = request.headers
        return httpx.Response(200, json=reply)

    vision.transport_factory = lambda: httpx.MockTransport(handler)
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG fake")
    assert vision.do_describe_image(str(img), "what is it?") == expected
    assert seen["url"] == f"http://vlm{url_suffix}"
    assert seen["body"]["model"] == "m"
    if api == "anthropic":
        assert seen["headers"]["x-api-key"] == "VK"
    else:
        assert seen["headers"]["authorization"] == "Bearer VK"


def test_vision_unconfigured_is_explained(ops_config, tmp_path):
    ops_config()
    img = tmp_path / "a.png"
    img.write_bytes(b"x")
    with pytest.raises(ConfigError, match="vision"):
        vision.do_describe_image(str(img))


def test_service_api_plex_refresh_gets_operate_level(http):
    http({("plex", "/library/sections/all/refresh"): lambda r: httpx.Response(200)})
    with pytest.raises(policy.PolicyError, match="operate"):
        services.do_service_api("plex", "GET", "/library/sections/all/refresh")


def test_qbittorrent_validates_before_auditing(http, tmp_path, ops_config):
    log = tmp_path / "audit.jsonl"
    ops_config({**CFG, "policy": {"audit_log": str(log)}}, mode="operate")
    with pytest.raises(ValueError, match="hashes"):
        media.do_qbittorrent("qbit", "pause")
    assert not log.exists()
