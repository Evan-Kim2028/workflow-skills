"""Linear sync over Linear's MCP server, for the browser sign-in that needs no API key.

The MCP OAuth tokens work only on ``https://mcp.linear.app/mcp``, not on the
GraphQL API, so this module speaks MCP streamable HTTP (JSON-RPC 2.0 over POST)
and exposes the same methods as ``dash.linear_client.Client``. Transport, size
cap, deadline, redirect refusal and token redaction are reused from there.

MCP cannot take a client-supplied id, cannot create workflow states and cannot
create initiatives. Each gap is handled where the method is defined: an issue,
project or milestone is adopted by its task marker or exact name, the
"Awaiting prod" state is emulated with a label, and a blocks relation is named
by a ``pair:<blocker>><blocked>`` string.
"""

from __future__ import annotations

import json
import re
from typing import Any

from dash import linear
from dash.linear_client import (
    DEADLINE_SECONDS,
    LinearError,
    Transport,
    _check_endpoint,
    _clip,
    _drop_none,
    _urllib_transport,
)

MCP_ENDPOINT = "https://mcp.linear.app/mcp"
PROTOCOL_VERSION = "2025-06-18"
CLIENT_NAME = "agent-dash"
AWAITING = linear.AWAITING_PROD
AWAITING_PREFIX = "label:"
IN_PROGRESS = "In Progress"
PAGE = 250
PROJECT_PAGE = 50
MAX_PAGES = 50
PAIR_PREFIX = "pair:"
ISSUE_FIELDS = ["uuid", "id", "title", "url", "description", "status", "statusType", "labels", "updatedAt", "project"]

_NOT_FOUND = re.compile(r"not found|could not find|does not exist", re.IGNORECASE)
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_IDENTIFIER = re.compile(r"\b[A-Z][A-Z0-9]*-\d+\b")
_URL = re.compile(r"https://linear\.app/\S+")


def _is_uuid(value: object) -> bool:
    """True when the value is a full UUID string."""
    return isinstance(value, str) and _UUID.fullmatch(value) is not None


def _header(headers: dict, name: str) -> str | None:
    """A response header by case-insensitive name, or None."""
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value)
    return None


def _parse_reply(raw: bytes, request_id: int) -> dict | None:
    """The JSON-RPC reply from a JSON body or an SSE stream; the one whose id matches wins."""
    text = raw.decode("utf-8", "replace").replace("\r\n", "\n").strip()
    if not text:
        return None
    if text.startswith(("{", "[")):
        try:
            parsed = json.loads(text)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    fallback: dict | None = None
    for event in text.split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in event.split("\n") if line.startswith("data:"))
        if not data:
            continue
        try:
            obj = json.loads(data)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("id") == request_id:
            return obj
        if fallback is None and ("result" in obj or "error" in obj):
            fallback = obj
    return fallback


def _result_text(result: dict) -> str:
    """The text of a tool result's content items, joined with newlines."""
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(str(item.get("text", "")) for item in content if isinstance(item, dict))


def _decode_text(text: str) -> object:
    """A tool result's text as parsed JSON, or the plain text when it is not JSON."""
    try:
        return json.loads(text)
    except ValueError:
        return text


def _uuid_of(value: object) -> str | None:
    """The UUID an MCP save result names, from its uuid or id key or from its text."""
    if isinstance(value, dict):
        for key in ("uuid", "id"):
            if _is_uuid(value.get(key)):
                return value[key]
        return None
    if isinstance(value, str):
        found = _UUID.search(value)
        return found.group(0) if found else None
    return None


def _label_names(row: dict) -> list[str]:
    """Label names from an issue row whose labels are strings or {name} objects."""
    labels = row.get("labels")
    if not isinstance(labels, list):
        return []
    names: list[str] = []
    for item in labels:
        if isinstance(item, str):
            names.append(item)
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"])
    return names


def _rows(value: object, key: str) -> list[dict]:
    """The dict rows under ``key`` of a list result, or the rows of a bare list result."""
    items: object = value.get(key) if isinstance(value, dict) else value
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


class McpClient:
    """Linear over MCP. Every ``ensure_*`` method is safe to repeat and adopts an existing entity.

    Method names and signatures match ``dash.linear_client.Client`` so the sync
    runs unchanged on either. ``last_rate`` stays empty: MCP reports no rate headers.
    """

    def __init__(
        self,
        token: str,
        transport: Transport | None = None,
        endpoint: str = MCP_ENDPOINT,
        timeout: float = 20.0,
        deadline: float = DEADLINE_SECONDS,
        team_key: str | None = None,
    ) -> None:
        """Build a client. ``team_key`` names the team for list calls; when None it is read from the workspace link.

        Raises ValueError when ``endpoint`` is not https (http is allowed only for localhost).
        """
        _check_endpoint(endpoint)
        self._token = token
        self._endpoint = endpoint
        self._transport = transport or _urllib_transport(endpoint, timeout, deadline, token)
        self._initialized = False
        self._session: str | None = None
        self._protocol = PROTOCOL_VERSION
        self._next_id = 0
        self._team_key = team_key
        self._team_id: str | None = None
        self._states: dict[str, dict] = {}
        self._label_ids: dict[str, str] = {}
        self._label_names: dict[str, str] = {}
        self._alias: dict[str, str] = {}
        self.last_rate: dict = {}

    def _error(self, code: str, message: str) -> LinearError:
        return LinearError(code, _clip(message, self._token))

    def _http_error(self, status: int) -> LinearError:
        if status in (401, 403):
            return self._error("AUTH", f"HTTP {status}: token rejected")
        if status == 429:
            return self._error("RATELIMITED", "HTTP 429: rate limited")
        return self._error("HTTP", f"HTTP {status}")

    def _exchange(self, payload: dict) -> tuple[int, dict, bytes]:
        """POST one JSON-RPC message with the bearer token and the session headers in force."""
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        if self._initialized:
            headers["MCP-Protocol-Version"] = self._protocol
        body = json.dumps(payload).encode("utf-8")
        try:
            status, resp_headers, raw = self._transport(body, headers)
        except OSError as exc:
            raise LinearError("NETWORK", f"request failed ({type(exc).__name__})") from None
        return status, resp_headers, raw

    def _initialize(self) -> None:
        """Run the initialize handshake, keep the session id and send notifications/initialized."""
        self._session = None
        self._initialized = False
        self._next_id += 1
        request_id = self._next_id
        status, headers, raw = self._exchange(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": CLIENT_NAME, "version": "1"},
                },
            }
        )
        if status >= 400:
            raise self._http_error(status)
        reply = _parse_reply(raw, request_id)
        result = reply.get("result") if reply else None
        if not isinstance(result, dict):
            raise self._error("GRAPHQL", "initialize returned no result")
        self._protocol = str(result.get("protocolVersion") or PROTOCOL_VERSION)
        self._session = _header(headers, "Mcp-Session-Id")
        self._initialized = True
        status, _headers, _raw = self._exchange({"jsonrpc": "2.0", "method": "notifications/initialized"})
        if status >= 400:
            raise self._http_error(status)

    def _request(self, method: str, params: dict, retry: bool = True) -> dict:
        """Send one JSON-RPC request and return its result object.

        A 404 means the session expired: the client initializes again and retries once.
        """
        if not self._initialized:
            self._initialize()
        self._next_id += 1
        request_id = self._next_id
        status, _headers, raw = self._exchange(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        if status == 404 and retry:
            self._initialized = False
            return self._request(method, params, retry=False)
        if status >= 400:
            raise self._http_error(status)
        reply = _parse_reply(raw, request_id)
        if reply is None:
            raise self._error("GRAPHQL", "response carried no JSON-RPC reply")
        if "error" in reply:
            error = reply["error"]
            message = str(error.get("message") if isinstance(error, dict) else error)
            code = "NOT_FOUND" if _NOT_FOUND.search(message) else "GRAPHQL"
            raise self._error(code, message or "JSON-RPC error")
        result = reply.get("result")
        if not isinstance(result, dict):
            raise self._error("GRAPHQL", "response carried no result")
        return result

    def _tool(self, name: str, arguments: dict) -> Any:
        """Call one MCP tool and return its decoded text. A tool error maps to NOT_FOUND or GRAPHQL."""
        result = self._request("tools/call", {"name": name, "arguments": _drop_none(arguments)})
        text = _result_text(result)
        if result.get("isError"):
            code = "NOT_FOUND" if _NOT_FOUND.search(text) else "GRAPHQL"
            raise self._error(code, text or f"{name} failed")
        return _decode_text(text)

    def _paged(self, name: str, arguments: dict, key: str, limit: int | None) -> list[dict]:
        """Every row of a cursor-paged list tool. Raises GRAPHQL on a missing or repeated cursor."""
        out: list[dict] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _page in range(MAX_PAGES):
            args = dict(arguments, limit=limit, cursor=cursor)
            value = self._tool(name, args)
            out.extend(_rows(value, key))
            if not isinstance(value, dict) or not value.get("hasNextPage"):
                return out
            cursor = value.get("cursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen:
                raise self._error("GRAPHQL", "pagination cursor missing or repeated")
            seen.add(cursor)
        raise self._error("GRAPHQL", f"pagination exceeded {MAX_PAGES} pages")

    def _team(self) -> str:
        """The team key for list and create calls: the one given, else the workspace link's."""
        if self._team_key:
            return self._team_key
        workspace = linear.read_workspace() or {}
        key = (workspace.get("team") or {}).get("key")
        if not isinstance(key, str) or not key:
            raise self._error("NOT_FOUND", "team key is unknown: link the bundle first")
        self._team_key = key
        return key

    def _load_states(self) -> None:
        """Refresh the team's workflow states by id."""
        for row in _rows(self._tool("list_issue_statuses", {"team": self._team()}), "statuses"):
            sid, name = row.get("id"), row.get("name")
            if isinstance(sid, str) and isinstance(name, str):
                self._states[sid] = {"id": sid, "name": name, "type": row.get("type")}

    def _state_by_name(self, name: str) -> dict | None:
        """The team's workflow state with this exact name, refreshing the cache once when missing."""
        for state in self._states.values():
            if state["name"] == name:
                return state
        self._load_states()
        for state in self._states.values():
            if state["name"] == name:
                return state
        return None

    def _state_name(self, state_id: str) -> str:
        """The workflow state name for a state id, refreshing the cache once when missing."""
        if state_id in self._states:
            return str(self._states[state_id]["name"])
        self._load_states()
        if state_id in self._states:
            return str(self._states[state_id]["name"])
        raise self._error("NOT_FOUND", "unknown workflow state id")

    def _team_uuid(self) -> str:
        """The team's uuid, looked up from its key when not yet known."""
        if self._team_id is None:
            self.team(self._team())
        return str(self._team_id)

    def _label_lookup(self, name: str) -> str | None:
        """The id of the team label with this exact name, or None."""
        cached = self._label_ids.get(name)
        if cached is not None:
            return cached
        rows = _rows(self._tool("list_issue_labels", {"team": self._team(), "name": name, "limit": PAGE}), "labels")
        for row in rows:
            if row.get("name") == name and isinstance(row.get("id"), str):
                self._label_ids[name] = row["id"]
                self._label_names[row["id"]] = name
                return row["id"]
        return None

    def _ensure_named_label(self, name: str) -> str:
        """The id of a team label, created once with create_issue_label when missing."""
        found = self._label_lookup(name)
        if found is not None:
            return found
        value = self._tool("create_issue_label", {"name": name, "teamId": self._team_uuid()})
        created = _uuid_of(value)
        if created is None:
            created = self._label_lookup(name)
        if created is None:
            raise self._error("GRAPHQL", f"label {name} could not be created")
        self._label_ids[name] = created
        self._label_names[created] = name
        return created

    def _label_name(self, label_id: str) -> str:
        """The label name for a label id, listing the team's labels once when missing."""
        if label_id in self._label_names:
            return self._label_names[label_id]
        rows = _rows(self._tool("list_issue_labels", {"team": self._team(), "limit": PAGE}), "labels")
        for row in rows:
            if isinstance(row.get("id"), str) and isinstance(row.get("name"), str):
                self._label_names[row["id"]] = row["name"]
                self._label_ids[row["name"]] = row["id"]
        if label_id in self._label_names:
            return self._label_names[label_id]
        raise self._error("NOT_FOUND", "unknown label id")

    def _issue(self, issue_id: str, fields: list[str] | None = None) -> dict | None:
        """The issue's requested fields, or None when Linear reports it not found."""
        try:
            value = self._tool("get_issue", {"id": issue_id, "fields": fields or ISSUE_FIELDS})
        except LinearError as exc:
            if exc.code == "NOT_FOUND":
                return None
            raise
        return value if isinstance(value, dict) else None

    def _real(self, issue_id: str) -> str:
        """The real Linear id behind a stable id that this client has already resolved."""
        return self._alias.get(issue_id, issue_id)

    def _adopt_issue(self, project_id: str, marker: str) -> dict | None:
        """The project issue whose description starts with exactly this marker line, or None."""
        if not marker:
            return None
        rows = self._paged(
            "list_issues",
            {"project": project_id, "query": marker, "fields": ISSUE_FIELDS},
            "issues",
            PAGE,
        )
        for row in rows:
            description = row.get("description")
            if isinstance(description, str) and description.split("\n", 1)[0].strip() == marker:
                return row
        return None

    def _ref(self, row: dict) -> dict:
        """{'id', 'identifier', 'url'} from an issue row carrying uuid, id and url."""
        real = row.get("uuid")
        identifier = row.get("id")
        url = row.get("url")
        if not _is_uuid(real) or not isinstance(identifier, str) or not isinstance(url, str):
            raise self._error("GRAPHQL", "issue row lacks uuid, identifier or url")
        return {"id": real, "identifier": identifier, "url": url}

    def _ref_from_save(self, value: object) -> dict:
        """The issue ref for a save_issue result: read it back by identifier when it names no uuid."""
        if isinstance(value, dict) and _is_uuid(value.get("uuid")) and isinstance(value.get("url"), str):
            ident = value.get("id")
            if isinstance(ident, str):
                return {"id": value["uuid"], "identifier": ident, "url": value["url"]}
        identifier: str | None = None
        if isinstance(value, dict) and isinstance(value.get("id"), str):
            identifier = value["id"]
        elif isinstance(value, str):
            found = _IDENTIFIER.search(value)
            identifier = found.group(0) if found else None
        if identifier is None:
            raise self._error("GRAPHQL", "created issue could not be identified")
        row = self._issue(identifier)
        if row is None:
            raise self._error("GRAPHQL", "created issue could not be read back")
        return self._ref(row)

    def team(self, key: str) -> dict:
        """Return the team with this key: id, key, and its workflow states."""
        rows = _rows(self._tool("list_teams", {"limit": PAGE}), "teams")
        match = next((row for row in rows if row.get("key") == key and _is_uuid(row.get("id"))), None)
        if match is None:
            raise self._error("NOT_FOUND", f"team {key} not found")
        self._team_key = key
        self._team_id = str(match["id"])
        self._load_states()
        states = [{"id": s["id"], "name": s["name"], "type": s["type"]} for s in self._states.values()]
        return {"id": self._team_id, "key": key, "states": states}

    def ensure_state(self, team_id: str, state_id: str, name: str, state_type: str, color: str) -> str:
        """Return the id of a workflow state by name.

        MCP cannot create workflow states. A missing "Awaiting prod" state returns the
        synthetic id ``label:Awaiting prod``, which ``set_issue_state`` emulates with a
        label. Any other missing state raises GRAPHQL.
        """
        state = self._state_by_name(name)
        if state is not None:
            return str(state["id"])
        if name == AWAITING:
            return AWAITING_PREFIX + AWAITING
        raise self._error("GRAPHQL", f"workflow state {name} is missing and cannot be created over MCP")

    def ensure_label(self, team_id: str, name: str) -> str:
        """Return the id of a team label by exact name, creating it when missing."""
        return self._ensure_named_label(name)

    def ensure_initiative(self, name: str) -> dict:
        """Return {'id': None, 'name', 'url': None}: MCP has no initiative list or create tool.

        With no id, ``add_project_to_initiative`` does nothing.
        """
        return {"id": None, "name": name, "url": None}

    def ensure_project(self, project_id: str, name: str, team_id: str, description: str) -> dict:
        """Adopt the team's project with this exact name, or create it; the description is refreshed."""
        team_key = self._team()
        rows = self._paged(
            "list_projects",
            {"team": team_key, "query": name, "fields": ["uuid", "name", "url"]},
            "projects",
            PROJECT_PAGE,
        )
        match = next((row for row in rows if row.get("name") == name and _is_uuid(row.get("uuid"))), None)
        if match is not None:
            self._tool("save_project", {"id": match["uuid"], "description": description})
            return {"id": match["uuid"], "url": match.get("url"), "name": name}
        value = self._tool(
            "save_project",
            {"name": name, "description": description, "addTeams": [team_id or self._team_uuid()]},
        )
        created = _uuid_of(value)
        url = value.get("url") if isinstance(value, dict) else None
        if created is None or not isinstance(url, str):
            again = self._paged(
                "list_projects",
                {"team": team_key, "query": name, "fields": ["uuid", "name", "url"]},
                "projects",
                PROJECT_PAGE,
            )
            found = next((row for row in again if row.get("name") == name), None)
            if found is None or not _is_uuid(found.get("uuid")):
                raise self._error("GRAPHQL", f"project {name} could not be created")
            return {"id": found["uuid"], "url": found.get("url"), "name": name}
        return {"id": created, "url": url, "name": name}

    def add_project_to_initiative(self, initiative_id: str | None, project_id: str) -> None:
        """Link a project to an initiative. A None initiative id is a no-op."""
        if not initiative_id:
            return
        self._tool("save_project", {"id": project_id, "addInitiatives": [initiative_id]})

    def ensure_milestone(self, milestone_id: str, project_id: str, name: str, sort_order: float) -> str:
        """Return the id of the project milestone with this name, creating it when missing.

        MCP cannot set ``sort_order`` or a client id, so the order is not recorded.
        """
        for row in _rows(self._tool("list_milestones", {"project": project_id}), "milestones"):
            if row.get("id") == milestone_id or row.get("name") == name:
                if isinstance(row.get("id"), str):
                    return row["id"]
        value = self._tool("save_milestone", {"project": project_id, "name": name})
        created = _uuid_of(value)
        if created is not None:
            return created
        for row in _rows(self._tool("list_milestones", {"project": project_id}), "milestones"):
            if row.get("name") == name and isinstance(row.get("id"), str):
                return row["id"]
        raise self._error("GRAPHQL", f"milestone {name} could not be created")

    def upsert_issue(
        self,
        issue_id: str,
        team_id: str,
        project_id: str,
        milestone_id: str | None,
        title: str,
        description: str,
        state_id: str | None = None,
        label_ids: list[str] | None = None,
    ) -> dict:
        """Update the issue that ``issue_id`` names, adopt one by marker, or create it. Returns {'id', 'identifier', 'url'}.

        Order: a known real id is read with get_issue; otherwise a project issue
        whose first description line equals this description's first line is
        adopted; otherwise the issue is created. ``state_id`` is applied only on
        create, like the GraphQL client.
        """
        marker = description.split("\n", 1)[0].strip()
        labels = [self._label_name(label) for label in label_ids] if label_ids is not None else None
        fields = _drop_none(
            {
                "title": title,
                "description": description,
                "project": project_id,
                "milestone": milestone_id,
                "labels": labels,
            }
        )
        existing = self._issue(self._real(issue_id), ["uuid", "id", "url"])
        if existing is None:
            existing = self._adopt_issue(project_id, marker)
        if existing is not None and _is_uuid(existing.get("uuid")):
            self._alias[issue_id] = str(existing["uuid"])
            self._tool("save_issue", {"id": existing["uuid"], **fields})
            return self._ref(self._issue(existing["uuid"], ["uuid", "id", "url"]) or existing)
        create: dict = {"team": self._team(), **fields}
        if state_id is not None:
            if state_id.startswith(AWAITING_PREFIX):
                self._ensure_named_label(AWAITING)
                create["state"] = IN_PROGRESS
                create["labels"] = [*(labels or []), AWAITING]
            else:
                create["state"] = self._state_name(state_id)
        ref = self._ref_from_save(self._tool("save_issue", create))
        self._alias[issue_id] = ref["id"]
        return ref

    def set_issue_state(self, issue_id: str, state_id: str) -> None:
        """Move an issue to a workflow state.

        The synthetic ``label:Awaiting prod`` id sets the team's "In Progress" state
        and adds the "Awaiting prod" label. Any other state removes that label when the issue has it.
        """
        real = self._real(issue_id)
        if state_id.startswith(AWAITING_PREFIX):
            self._ensure_named_label(AWAITING)
            self._tool("save_issue", {"id": real, "state": IN_PROGRESS, "addLabels": [AWAITING]})
            return
        args: dict = {"id": real, "state": self._state_name(state_id)}
        row = self._issue(real, ["uuid", "labels"])
        if row is None:
            raise self._error("NOT_FOUND", "issue not found")
        if AWAITING in _label_names(row):
            args["removeLabels"] = [AWAITING]
        self._tool("save_issue", args)

    def ensure_blocks(self, blocker_issue_id: str, blocked_issue_id: str) -> str:
        """Record that the blocker blocks the blocked issue. Returns ``pair:<blocker>><blocked>``.

        MCP adds blockers append-only, so repeating the call is harmless.
        """
        blocker = self._real(blocker_issue_id)
        blocked = self._real(blocked_issue_id)
        self._tool("save_issue", {"id": blocked, "blockedBy": [blocker]})
        return f"{PAIR_PREFIX}{blocker}>{blocked}"

    def find_relation(self, blocker_issue_id: str, blocked_issue_id: str) -> str | None:
        """The pair id when the blocked issue lists the blocker as a blocker, else None.

        When the issue carries no ``blockedBy`` list the relation is assumed present and the pair id is returned.
        A blocker that no longer exists reads as no relation.
        """
        blocker = self._real(blocker_issue_id)
        blocked = self._real(blocked_issue_id)
        try:
            value = self._tool("get_issue", {"id": blocked, "fields": ["uuid"], "includeRelations": True})
        except LinearError as exc:
            if exc.code != "NOT_FOUND":
                raise
            return None
        if not isinstance(value, dict):
            return None
        blockers = value.get("blockedBy")
        if not isinstance(blockers, list):
            return f"{PAIR_PREFIX}{blocker}>{blocked}"
        for item in blockers:
            if isinstance(item, dict) and blocker in (item.get("uuid"), item.get("id"), item.get("identifier")):
                return f"{PAIR_PREFIX}{blocker}>{blocked}"
            if item == blocker:
                return f"{PAIR_PREFIX}{blocker}>{blocked}"
        return None

    def delete_relation(self, relation_id: str) -> None:
        """Remove a blocks relation named by its pair id. A relation already gone counts as success."""
        if not relation_id.startswith(PAIR_PREFIX) or ">" not in relation_id:
            raise self._error("GRAPHQL", "relation id is not an MCP pair id")
        blocker, blocked = relation_id[len(PAIR_PREFIX):].split(">", 1)
        try:
            self._tool("save_issue", {"id": blocked, "removeBlockedBy": [blocker]})
        except LinearError as exc:
            if exc.code != "NOT_FOUND":
                raise

    def issue_exists(self, issue_id: str) -> bool:
        """True when Linear returns the issue; False when it reports the issue not found."""
        try:
            value = self._tool("get_issue", {"id": self._real(issue_id), "fields": ["uuid"]})
        except LinearError as exc:
            if exc.code != "NOT_FOUND":
                raise
            return False
        return isinstance(value, dict) and bool(value.get("uuid") or value.get("id"))

    def comment(self, issue_id: str, body: str) -> None:
        """Add a comment to an issue."""
        self._tool("save_comment", {"issueId": self._real(issue_id), "body": body})

    def post_update(self, project_id: str, body: str, health: str) -> str | None:
        """Create a project status update (save_status_update) with a markdown body and a health enum. Returns its id."""
        value = self._tool("save_status_update", {"type": "project", "project": project_id, "body": body, "health": health})
        return _uuid_of(value)

    def _change(self, row: dict) -> dict:
        """One pulled issue as the sync reads it: id, identifier, url, updatedAt and a state."""
        status = row.get("status") if isinstance(row.get("status"), str) else None
        kind = row.get("statusType") if isinstance(row.get("statusType"), str) else None
        if kind == "started" and AWAITING in _label_names(row):
            state = {"id": AWAITING_PREFIX + AWAITING, "name": AWAITING, "type": "started"}
        else:
            known = self._state_by_name(status) if status else None
            state = {"id": known["id"] if known else f"name:{status}", "name": status, "type": kind}
        uid = row.get("uuid") if _is_uuid(row.get("uuid")) else row.get("id")
        return {
            "id": uid,
            "identifier": row.get("id"),
            "url": row.get("url"),
            "updatedAt": row.get("updatedAt"),
            "state": state,
        }

    def issues_updated_since(self, project_id: str, since_iso: str | None) -> list[dict]:
        """Every issue in the project updated after ``since_iso``, ordered by updatedAt, with state mapped for the sync.

        An issue in a started state that carries the "Awaiting prod" label reports the emulated state id.
        """
        arguments: dict = {"project": project_id, "orderBy": "updatedAt", "fields": ISSUE_FIELDS}
        if since_iso:
            arguments["updatedAt"] = since_iso
        rows = self._paged("list_issues", arguments, "issues", PAGE)
        changes = []
        for row in rows:
            updated = row.get("updatedAt")
            if since_iso and isinstance(updated, str) and updated <= since_iso:
                continue
            changes.append(self._change(row))
        changes.sort(key=lambda change: str(change.get("updatedAt") or ""))
        return changes

