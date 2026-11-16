"""Media stack helpers: the *arr family (incl. ROMarr), qBittorrent and Plex.

Each helper turns the raw API into short, readable summaries so smaller
models can act on them; anything not covered is one ``service_api`` call away.
"""

from __future__ import annotations

import datetime as dt
from contextlib import closing
from dataclasses import dataclass
from typing import Any

import httpx

from openowl.ops import policy
from openowl.ops.config import Service, get_config
from openowl.ops.services import call_json, client_for


@dataclass(frozen=True)
class ArrApp:
    resource: str          # library endpoint
    search_cmd: str        # search one item
    search_key: str        # id field for search_cmd (list when it ends in "Ids")
    missing_cmd: str       # search everything wanted
    rescan_cmd: str        # rescan disk
    rescan_key: str = ""   # id field for a one-item rescan


ARR_APPS = {
    "sonarr": ArrApp("series", "SeriesSearch", "seriesId", "MissingEpisodeSearch", "RescanSeries", "seriesId"),
    "radarr": ArrApp("movie", "MoviesSearch", "movieIds", "MissingMoviesSearch", "RescanMovie", "movieId"),
    "whisparr": ArrApp("movie", "MoviesSearch", "movieIds", "MissingMoviesSearch", "RescanMovie", "movieId"),
    "lidarr": ArrApp("artist", "ArtistSearch", "artistId", "MissingAlbumSearch", "RescanFolders"),
    "readarr": ArrApp("author", "AuthorSearch", "authorId", "MissingBookSearch", "RescanFolders"),
    "romarr": ArrApp("game", "", "", "MissingGameSearch", "RefreshLibrary"),
    "prowlarr": ArrApp("", "", "", "", ""),
}




def _id_field(key: str, item_id: int) -> dict[str, Any]:
    return {key: [item_id] if key.endswith("Ids") else item_id}


ARR_ACTIONS = ("status", "health", "queue", "calendar", "history", "missing", "library",
               "lookup", "diskspace", "rootfolders", "indexers", "commands", "command",
               "search", "rescan", "add")


def _arr(service: str) -> Service:
    svc = get_config().service(service)
    if svc.kind != "arr":
        raise ValueError(f"'{service}' is kind '{svc.kind}', not an *arr service.")
    return svc


def _title(rec: dict[str, Any]) -> str:
    for key in ("title", "artistName", "authorName", "name"):
        if rec.get(key):
            year = f" ({rec['year']})" if rec.get("year") else ""
            return f"{rec[key]}{year}"
    for nested in ("series", "movie", "artist", "author", "game"):
        if isinstance(rec.get(nested), dict):
            return _title(rec[nested])
    return "?"


def _records(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict) and "records" in data:
        return data["records"]
    return data if isinstance(data, list) else []


def _total(data: Any) -> int:
    recs = _records(data)
    return data.get("totalRecords", len(recs)) if isinstance(data, dict) else len(recs)


def do_arr(service: str, action: str, term: str = "", item_id: int = 0, days: int = 7,
           limit: int = 25, body: dict | None = None) -> str:
    svc = _arr(service)
    api = f"/api/{svc.api_version}"
    action = action.lower().strip()
    app = ARR_APPS.get(svc.app, ArrApp("", "", "", "", "RescanFolders"))
    resource = app.resource

    if action == "status":
        return policy.to_text(call_json(svc, "GET", f"{api}/system/status"))
    if action == "health":
        items = call_json(svc, "GET", f"{api}/health") or []
        if not items:
            return f"{service}: no health issues."
        return "\n".join(f"[{i.get('type')}] {i.get('source')}: {i.get('message')}" for i in items)
    if action == "queue":
        query = {"pageSize": limit}
        if svc.app == "sonarr":
            query["includeUnknownSeriesItems"] = "true"
        data = call_json(svc, "GET", f"{api}/queue", query)
        recs = _records(data)
        if not recs:
            return f"{service}: queue is empty."
        lines = [f"{service} queue ({_total(data)} items):"]
        for r in recs:
            size, left = r.get("size") or 0, r.get("sizeleft") or 0
            pct = f"{100 * (size - left) / size:.0f}%" if size else "?"
            msgs = "; ".join(m for s in r.get("statusMessages") or [] for m in s.get("messages") or [])
            lines.append(f"  [{r.get('id')}] {r.get('title') or _title(r)} - {r.get('status')} "
                         f"{pct} {r.get('trackedDownloadState', '')} {r.get('downloadClient', '')}"
                         + (f" ! {msgs[:160]}" if msgs else ""))
        return "\n".join(lines)
    if action == "calendar":
        start = dt.date.today()
        data = call_json(svc, "GET", f"{api}/calendar",
                         {"start": start.isoformat(), "end": (start + dt.timedelta(days=days)).isoformat()})
        rows = [f"  {r.get('airDateUtc') or r.get('digitalRelease') or r.get('releaseDate') or r.get('inCinemas') or ''}"
                f"  {_title(r)}" + (f" S{r.get('seasonNumber', 0):02d}E{r.get('episodeNumber', 0):02d} {r.get('title', '')}"
                                     if r.get("episodeNumber") else "")
                for r in data or []]
        return f"{service} calendar, next {days} days:\n" + ("\n".join(rows) or "  nothing scheduled")
    if action == "history":
        data = call_json(svc, "GET", f"{api}/history", {"pageSize": limit, "sortKey": "date",
                                                         "sortDirection": "descending"})
        rows = [f"  {r.get('date', '')[:16]} {r.get('eventType')}: {r.get('sourceTitle') or _title(r)}"
                for r in _records(data)]
        return f"{service} history:\n" + ("\n".join(rows) or "  empty")
    if action == "missing":
        data = call_json(svc, "GET", f"{api}/wanted/missing", {"pageSize": limit})
        recs = _records(data)
        return f"{service}: {_total(data)} missing\n" + "\n".join(f"  [{r.get('id')}] {_title(r)}" for r in recs)
    if action == "library":
        if not resource:
            raise ValueError(f"No library resource known for app '{svc.app}'.")
        items = call_json(svc, "GET", f"{api}/{resource}") or []
        if isinstance(items, dict):
            items = _records(items) or items.get("items", [])
        if term:
            t = term.lower()
            items = [i for i in items if t in _title(i).lower()]
        rows = []
        for i in items[:limit]:
            have = i.get("hasFile")
            stats = i.get("statistics") or {}
            if have is None and stats:
                have = f"{stats.get('episodeFileCount', stats.get('trackFileCount', '?'))}/{stats.get('episodeCount', stats.get('totalTrackCount', '?'))}"
            rows.append(f"  [{i.get('id')}] {_title(i)}  monitored={i.get('monitored')} "
                        f"file={have} path={i.get('path', '')}")
        return f"{service}: {len(items)} matching item(s)\n" + "\n".join(rows)
    if action == "lookup":
        if not term:
            raise ValueError("lookup needs term.")
        if svc.app == "prowlarr":
            data = call_json(svc, "GET", f"{api}/search", {"query": term, "type": "search"})
            rows = [f"  {r.get('indexer')}: {r.get('title')} seeders={r.get('seeders')} "
                    f"size={round((r.get('size') or 0) / 1e9, 2)}G" for r in (data or [])[:limit]]
            return f"Prowlarr results for {term!r}:\n" + ("\n".join(rows) or "  none")
        data = call_json(svc, "GET", f"{api}/{resource}/lookup", {"term": term})
        rows = [f"  {_title(r)}  tmdb={r.get('tmdbId', '')} tvdb={r.get('tvdbId', '')} "
                f"igdb={r.get('igdbId', '')} in_library={'yes' if r.get('id') else 'no'}"
                for r in (data or [])[:limit]]
        return f"{service} lookup {term!r}:\n" + ("\n".join(rows) or "  no results")
    if action == "diskspace":
        rows = [f"  {d.get('path')}: {round((d.get('freeSpace') or 0) / 1e9)}G free of "
                f"{round((d.get('totalSpace') or 0) / 1e9)}G" for d in call_json(svc, "GET", f"{api}/diskspace") or []]
        return f"{service} disks:\n" + "\n".join(rows)
    if action == "rootfolders":
        return policy.to_text(call_json(svc, "GET", f"{api}/rootfolder"))
    if action == "indexers":
        items = call_json(svc, "GET", f"{api}/indexer") or []
        status = {s.get("indexerId"): s for s in call_json(svc, "GET", f"{api}/indexerstatus") or []}
        rows = [f"  [{i.get('id')}] {i.get('name')} enabled={i.get('enable')} "
                f"{'DISABLED until ' + status[i['id']].get('disabledTill', '') if i.get('id') in status else ''}"
                for i in items]
        return f"{service} indexers:\n" + "\n".join(rows)
    if action == "commands":
        rows = [f"  [{c.get('id')}] {c.get('name')} {c.get('status')} {c.get('started', '')[:16]}"
                for c in (call_json(svc, "GET", f"{api}/command") or [])[:limit]]
        return f"{service} recent commands:\n" + ("\n".join(rows) or "  none")

    # ---- state-changing actions ----
    if action == "command":
        if not term:
            raise ValueError("command needs term = the command name, e.g. RefreshMonitoredDownloads.")
        payload = {"name": term, **(body or {})}
    elif action == "search":
        if item_id and app.search_cmd:
            payload = {"name": app.search_cmd, **_id_field(app.search_key, item_id)}
        elif item_id:
            raise ValueError(f"{svc.app} has no per-item search command; omit item_id to search all missing.")
        elif app.missing_cmd:
            payload = {"name": app.missing_cmd}
        else:
            raise ValueError(f"{svc.app} has no search command; use action 'lookup'.")
    elif action == "rescan":
        if not app.rescan_cmd:
            raise ValueError(f"{svc.app} has no rescan command.")
        payload = {"name": app.rescan_cmd}
        if item_id and app.rescan_key:
            payload.update(_id_field(app.rescan_key, item_id))
    elif action == "add":
        if not body:
            raise ValueError(f"add needs body = the {resource} object (from lookup) plus "
                             f"qualityProfileId, rootFolderPath and monitored.")
        policy.authorize(policy.OPERATE, "arr add", "arr", service=service, title=_title(body))
        return policy.to_text(call_json(svc, "POST", f"{api}/{resource}", json_body=body))
    else:
        raise ValueError(f"Unknown action '{action}'. Use one of: {', '.join(ARR_ACTIONS)}.")
    policy.authorize(policy.OPERATE, f"arr {action}", "arr", service=service, payload=payload)
    result = call_json(svc, "POST", f"{api}/command", json_body=payload)
    return f"Queued {payload['name']} on {service}: id={result.get('id') if isinstance(result, dict) else result} " \
           f"status={result.get('status') if isinstance(result, dict) else ''}"


# ------------------------------------------------------------------
# qBittorrent
# ------------------------------------------------------------------

def _qbit_client(svc: Service) -> httpx.Client:
    client = client_for(svc)
    user = str(svc.raw.get("username", ""))
    if user:
        resp = client.post("/api/v2/auth/login", data={"username": user, "password": svc.secret("password")},
                           headers={"Referer": svc.url})
        if resp.status_code != 200 or resp.text.strip() != "Ok.":
            client.close()
            raise RuntimeError(f"qBittorrent login failed: HTTP {resp.status_code} {resp.text[:100]}")
    return client


def do_qbittorrent(service: str, action: str, filter: str = "", category: str = "",
                   hashes: str = "", urls: str = "", save_path: str = "", limit: int = 30) -> str:
    svc = get_config().service(service, kind="qbittorrent")
    action = action.lower()
    writes = {"pause": "stop", "resume": "start", "recheck": "recheck", "reannounce": "reannounce"}
    if (action in writes or action == "delete") and not hashes:
        raise ValueError(f"{action} needs hashes (| separated, or 'all').")
    if action == "add" and not urls:
        raise ValueError("add needs urls (magnet links or .torrent URLs, newline separated).")
    if action in writes or action in ("add", "delete"):
        level = policy.ADMIN if action == "delete" else policy.OPERATE
        policy.authorize(level, f"qbittorrent {action}", "qbittorrent", service=service,
                         hashes=hashes, urls=urls)
    with closing(_qbit_client(svc)) as qb:  # already opened by the login request
        if action == "transfer":
            info = qb.get("/api/v2/transfer/info").json()
            return (f"down {info.get('dl_info_speed', 0) / 1e6:.2f} MB/s  up {info.get('up_info_speed', 0) / 1e6:.2f} MB/s  "
                    f"dht_nodes {info.get('dht_nodes')}  connection {info.get('connection_status')}")
        if action == "list":
            params = {"sort": "added_on", "reverse": "true", "limit": limit}
            if filter:
                params["filter"] = filter
            if category:
                params["category"] = category
            items = qb.get("/api/v2/torrents/info", params=params).json()
            rows = [f"  {t['hash'][:12]} {t['state']:<12} {t['progress'] * 100:5.1f}% "
                    f"{t.get('dlspeed', 0) / 1e6:5.2f}MB/s seeds={t.get('num_seeds')} "
                    f"[{t.get('category', '')}] {t['name'][:80]}" for t in items]
            return f"{len(items)} torrent(s):\n" + "\n".join(rows)
        if action in writes:
            resp = qb.post(f"/api/v2/torrents/{writes[action]}", data={"hashes": hashes})
            if resp.status_code == 404 and action in ("pause", "resume"):  # qBittorrent < 5
                resp = qb.post(f"/api/v2/torrents/{action}", data={"hashes": hashes})
            return f"{action}: HTTP {resp.status_code}"
        if action == "delete":
            resp = qb.post("/api/v2/torrents/delete", data={"hashes": hashes, "deleteFiles": "false"})
            return f"delete (files kept): HTTP {resp.status_code}"
        if action == "add":
            data = {"urls": urls}
            if category:
                data["category"] = category
            if save_path:
                data["savepath"] = save_path
            resp = qb.post("/api/v2/torrents/add", data=data)
            return f"add: HTTP {resp.status_code} {resp.text.strip()}"
        if action == "categories":
            return policy.to_text(qb.get("/api/v2/torrents/categories").json())
    raise ValueError("Unknown action. Use transfer, list, pause, resume, recheck, reannounce, add, delete, categories.")


# ------------------------------------------------------------------
# Plex
# ------------------------------------------------------------------

def do_plex(service: str, action: str, section: str = "", term: str = "", limit: int = 20) -> str:
    svc = get_config().service(service, kind="plex")
    action = action.lower()
    if action == "identity":
        return policy.to_text(call_json(svc, "GET", "/identity"))
    if action == "sections":
        data = call_json(svc, "GET", "/library/sections")
        dirs = data.get("MediaContainer", {}).get("Directory", [])
        return "\n".join(
            f"  [{d.get('key')}] {d.get('title')} ({d.get('type')}) "
            f"{', '.join(loc.get('path', '') for loc in d.get('Location', []))}"
            for d in dirs
        )
    if action == "sessions":
        data = call_json(svc, "GET", "/status/sessions")
        items = data.get("MediaContainer", {}).get("Metadata", []) or []
        if not items:
            return "Nobody is watching."
        return "\n".join(
            f"  {m.get('User', {}).get('title', '?')}: {m.get('grandparentTitle', '')} {m.get('title')} "
            f"[{m.get('Player', {}).get('state', '')} on {m.get('Player', {}).get('product', '')}]"
            for m in items
        )
    if action in ("recent", "search"):
        if action == "search":
            if not term:
                raise ValueError("search needs term.")
            data = call_json(svc, "GET", "/search", {"query": term})
        else:
            path = f"/library/sections/{section}/recentlyAdded" if section else "/library/recentlyAdded"
            data = call_json(svc, "GET", path, {"X-Plex-Container-Size": limit})
        items = data.get("MediaContainer", {}).get("Metadata", []) or []
        return "\n".join(
            f"  [{m.get('ratingKey')}] {m.get('grandparentTitle', '')} {m.get('title')} "
            f"({m.get('year', '')}) {m.get('type')}" for m in items[:limit]
        ) or "No results."
    if action == "refresh":
        policy.authorize(policy.OPERATE, "plex refresh", "plex", service=service, section=section)
        path = f"/library/sections/{section}/refresh" if section else "/library/sections/all/refresh"
        call_json(svc, "GET", path)
        return f"Library refresh requested ({'section ' + section if section else 'all sections'})."
    raise ValueError("Unknown action. Use identity, sections, sessions, recent, search, refresh.")


def register(server) -> None:
    @server.tool()
    def arr(service: str, action: str, term: str = "", item_id: int = 0, days: int = 7,
            limit: int = 25, body: dict | None = None) -> str:
        """Work with Sonarr, Radarr, Lidarr, Readarr, Prowlarr, Whisparr or ROMarr.

        Read actions: status, health, queue, calendar (days), history, missing,
        library (term filters by title), lookup (term; Prowlarr does an indexer search),
        diskspace, rootfolders, indexers, commands.
        Operate actions: search (item_id, or all missing), rescan (item_id optional),
        command (term = command name, body = extra fields), add (body = lookup result
        plus qualityProfileId, rootFolderPath, monitored).

        Parameters:
            service: Service name from list_services (e.g. "radarr", "romarr").
            action: One of the actions above.
            term: Search text, title filter, or command name.
            item_id: Library item id for search/rescan.
            days: Window for calendar.
            limit: Maximum rows.
            body: JSON body for add/command.
        """
        return do_arr(service, action, term, item_id, days, limit, body)

    @server.tool()
    def qbittorrent(service: str, action: str, filter: str = "", category: str = "",
                    hashes: str = "", urls: str = "", save_path: str = "", limit: int = 30) -> str:
        """Control qBittorrent.

        Read: transfer (speeds, DHT nodes, connection), list (filter = downloading,
        seeding, stalled, errored, paused ...; category), categories.
        Operate: pause, resume, recheck, reannounce (hashes), add (urls, category,
        save_path). Admin: delete (keeps files).

        Parameters:
            service: Service name (kind qbittorrent).
            action: One of the actions above.
            filter: State filter for list.
            category: Category filter, or category for add.
            hashes: Torrent hashes separated by |, or "all".
            urls: Magnet links / torrent URLs for add, newline separated.
            save_path: Download folder for add.
            limit: Maximum rows for list.
        """
        return do_qbittorrent(service, action, filter, category, hashes, urls, save_path, limit)

    @server.tool()
    def plex(service: str, action: str, section: str = "", term: str = "", limit: int = 20) -> str:
        """Plex Media Server: identity, sections (ids and folders), sessions (who is watching),
        recent (section optional), search (term), refresh (section id or all; operate mode).

        Parameters:
            service: Service name (kind plex).
            action: One of the actions above.
            section: Library section id.
            term: Search text.
            limit: Maximum rows for recent/search.
        """
        return do_plex(service, action, section, term, limit)
