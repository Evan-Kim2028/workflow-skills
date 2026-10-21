"""Stdlib client for the Linear GraphQL API, used by the agent-dash task backend.

This is the only module that imports urllib for Linear. Every operation below
was checked against the public schema (linear/linear master,
packages/sdk/src/schema.graphql, read 2026-10-21). Line numbers refer to that
file.

Operations and schema anchors:

- Query.teams L41868 (filter TeamFilter L47608, key L47636); Team.states L47211
  (WorkflowStateConnection L52915); Team.id L46777
- Query.team(id) L41821; Query.project(id) L41105
- Query.issues L40846 (filter IssueFilter L19389: project L19589, updatedAt
  L19669; NullableProjectFilter L30313, id L30393; EntityIdentifierIDComparator
  L12372; DateComparator L9930 gt L9938; orderBy PaginationOrderBy L33252
  updatedAt; PageInfo L33215 hasNextPage L33223 endCursor L33219)
- Query.issueLabels L40661 (IssueLabelFilter L20544, team L20580,
  NullableTeamFilter id IDComparator L14071); Query.initiatives L40398
  (InitiativeFilter L14954, name L15014)
- Mutation.issueCreate L26411 (IssueCreateInput L19027: id L19075, teamId
  L19159, projectId L19107, projectMilestoneId L19111, stateId L19147, title
  L19167, description L19055, labelIds L19083)
- Mutation.issueUpdate L26903 (id arg; IssueUpdateInput L22793: stateId L22905,
  title L22921, description L22821, labelIds L22841, projectId L22861,
  projectMilestoneId L22865)
- Mutation.workflowStateCreate L28553 (WorkflowStateCreateInput L52924: id,
  teamId L52948, name L52940, type L52952, color L52928)
- Mutation.issueLabelCreate L26676 (IssueLabelCreateInput L20494: teamId
  L20530, name L20518)
- Mutation.initiativeCreate L25230 (InitiativeCreateInput L14866, name L14898)
- Mutation.initiativeToProjectCreate L25358 (InitiativeToProjectCreateInput
  L16133: initiativeId L16141, projectId L16145)
- Mutation.projectCreate L27366 (ProjectCreateInput L35071: id L35095, name
  L35119, teamIds L35155, description L35087)
- Mutation.projectUpdate L27642 (id arg; ProjectUpdateInput L38181: name
  L38237, description L38205)
- Mutation.projectMilestoneCreate L27492 (ProjectMilestoneCreateInput L36081:
  id L36093, projectId L36101, name L36097, sortOrder L36105); Project.
  projectMilestones L34547
- Mutation.issueRelationCreate L26733 (IssueRelationCreateInput L21064: issueId
  L21072, relatedIssueId L21076, type L21080; IssueRelationType enum with
  blocks, L21126)
- Mutation.commentCreate L24425 (CommentCreateInput L6070: issueId L6118, body
  L6074)

Payload fields used: IssuePayload.issue L20981, ProjectPayload.project L36585,
InitiativePayload.initiative L15884, IssueLabelPayload.issueLabel L20594,
WorkflowStatePayload.workflowState L53028, ProjectMilestonePayload
.projectMilestone L36271, and success on each mutation.

Linear reports RATELIMITED as an error object with extensions.code, usually on
an HTTP 400 response. The key is sent only in the Authorization header and is
scrubbed from every message this module raises.
"""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Callable

ENDPOINT = "https://api.linear.app/graphql"
PAGE_SIZE = 100
MAX_PAGES = 50
STATE_PAGE = 250
MESSAGE_CLIP = 200
MAX_BODY_BYTES = 8 * 1024 * 1024
READ_CHUNK = 64 * 1024
DEADLINE_SECONDS = 30.0
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})

Transport = Callable[[bytes, dict], tuple[int, dict, bytes]]

_RATE_HEADERS = {
    "X-RateLimit-Requests-Remaining": "requests_remaining",
    "X-RateLimit-Complexity-Remaining": "complexity_remaining",
    "X-RateLimit-Requests-Reset": "reset",
}

TEAM_BY_KEY = """
query TeamByKey($key: String!) {
  teams(filter: {key: {eq: $key}}, first: 5) {
    nodes {
      id
      key
      states(first: 250, includeArchived: true) { nodes { id name type } }
    }
  }
}
"""

TEAM_STATES = """
query TeamStates($id: String!) {
  team(id: $id) {
    states(first: 250, includeArchived: true) { nodes { id name type } }
  }
}
"""

WORKFLOW_STATE_CREATE = """
mutation WorkflowStateCreate($input: WorkflowStateCreateInput!) {
  workflowStateCreate(input: $input) { success workflowState { id } }
}
"""

LABEL_FIND = """
query LabelByName($teamId: ID!, $name: String!) {
  issueLabels(filter: {name: {eq: $name}, team: {id: {eq: $teamId}}}, first: 10) {
    nodes { id name }
  }
}
"""

LABEL_CREATE = """
mutation LabelCreate($input: IssueLabelCreateInput!) {
  issueLabelCreate(input: $input) { success issueLabel { id } }
}
"""

INITIATIVE_FIND = """
query InitiativeByName($name: String!) {
  initiatives(filter: {name: {eq: $name}}, first: 10) {
    nodes { id name url }
  }
}
"""

INITIATIVE_CREATE = """
mutation InitiativeCreate($input: InitiativeCreateInput!) {
  initiativeCreate(input: $input) { success initiative { id name url } }
}
"""

PROJECT_UPDATE = """
mutation ProjectUpdate($id: String!, $input: ProjectUpdateInput!) {
  projectUpdate(id: $id, input: $input) { success project { id name url } }
}
"""

PROJECT_CREATE = """
mutation ProjectCreate($input: ProjectCreateInput!) {
  projectCreate(input: $input) { success project { id name url } }
}
"""

INITIATIVE_LINK = """
mutation InitiativeToProjectCreate($input: InitiativeToProjectCreateInput!) {
  initiativeToProjectCreate(input: $input) { success }
}
"""

PROJECT_MILESTONES = """
query ProjectMilestones($id: String!) {
  project(id: $id) {
    projectMilestones(first: 250, includeArchived: true) { nodes { id name } }
  }
}
"""

MILESTONE_CREATE = """
mutation ProjectMilestoneCreate($input: ProjectMilestoneCreateInput!) {
  projectMilestoneCreate(input: $input) { success projectMilestone { id } }
}
"""

ISSUE_UPDATE = """
mutation IssueUpdate($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) { success issue { id identifier url } }
}
"""

ISSUE_CREATE = """
mutation IssueCreate($input: IssueCreateInput!) {
  issueCreate(input: $input) { success issue { id identifier url } }
}
"""

RELATION_CREATE = """
mutation IssueRelationCreate($input: IssueRelationCreateInput!) {
  issueRelationCreate(input: $input) { success issueRelation { id } }
}
"""

RELATION_DELETE = """
mutation IssueRelationDelete($id: String!) {
  issueRelationDelete(id: $id) { success }
}
"""

ISSUE_EXISTS = """
query IssueExists($id: String!) {
  issue(id: $id) { id }
}
"""

ISSUE_RELATIONS = """
query IssueRelations($id: String!) {
  issue(id: $id) {
    relations(first: 250) { nodes { id type relatedIssue { id } } }
  }
}
"""

PROJECT_UPDATE_CREATE = """
mutation ProjectUpdateCreate($input: ProjectUpdateCreateInput!) {
  projectUpdateCreate(input: $input) { success projectUpdate { id } }
}
"""

PROJECT_UPDATES = """
query ProjectUpdates($id: String!, $first: Int!) {
  project(id: $id) {
    projectUpdates(first: $first, orderBy: createdAt) { nodes { id body createdAt } }
  }
}
"""

COMMENT_CREATE = """
mutation CommentCreate($input: CommentCreateInput!) {
  commentCreate(input: $input) { success }
}
"""

PROJECT_ISSUES = """
query ProjectIssues($filter: IssueFilter, $first: Int, $after: String) {
  issues(filter: $filter, first: $first, after: $after, orderBy: updatedAt) {
    nodes {
      id
      identifier
      url
      updatedAt
      state { id name type }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""


PROJECT_DETAIL = """
query ProjectDetail($id: String!) {
  project(id: $id) { labels(first: 250) { nodes { id name } } }
}
"""

PROJECT_LABEL_FIND = """
query ProjectLabelByName($name: String!) {
  projectLabels(filter: {name: {eq: $name}}, first: 10) { nodes { id name } }
}
"""

PROJECT_LABEL_CREATE = """
mutation ProjectLabelCreate($input: ProjectLabelCreateInput!) {
  projectLabelCreate(input: $input) { success projectLabel { id } }
}
"""

PROJECT_STATUSES = """
query ProjectStatuses {
  projectStatuses(first: 50) { nodes { id name } }
}
"""

USER_FIND = """
query UserByName($name: String!) {
  users(filter: {or: [{name: {eq: $name}}, {displayName: {eq: $name}}]}, first: 5) {
    nodes { id name displayName }
  }
}
"""

LINK_CREATE = """
mutation EntityExternalLinkCreate($input: EntityExternalLinkCreateInput!) {
  entityExternalLinkCreate(input: $input) { success }
}
"""

MILESTONE_UPDATE = """
mutation ProjectMilestoneUpdate($id: String!, $input: ProjectMilestoneUpdateInput!) {
  projectMilestoneUpdate(id: $id, input: $input) { success }
}
"""

ISSUE_LABELS = """
query IssueLabels($id: String!) {
  issue(id: $id) { team { id } labels(first: 250) { nodes { id name } } }
}
"""

class LinearError(Exception):
    """A Linear call failed.

    ``code`` is one of RATELIMITED, AUTH, NOT_FOUND, HTTP, GRAPHQL or NETWORK.
    ``message`` is clipped and never contains the API key.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect, so the Authorization header never leaves the endpoint."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise LinearError("HTTP", "redirect refused")


_OPENER = urllib.request.build_opener(_RefuseRedirects)


def _clip(text: object, key: str) -> str:
    """Flatten, redact the key, and cap the length of a message."""
    flat = str(text).replace("\n", " ").strip()
    if key:
        flat = flat.replace(key, "[redacted]")
    return flat[:MESSAGE_CLIP]


def _check_endpoint(endpoint: str) -> None:
    """Allow https everywhere and plain http only for a local test server."""
    parts = urllib.parse.urlsplit(endpoint)
    host = parts.hostname or ""
    if parts.scheme == "https" and host:
        return
    if parts.scheme == "http" and host in LOCAL_HOSTS:
        return
    raise ValueError("Linear endpoint must be https (http is allowed only for localhost)")


def _read_capped(stream: object, deadline_at: float) -> bytes:
    """Read a body in chunks, enforcing the size cap and the overall deadline."""
    try:
        reader = getattr(stream, "read1", None) or stream.read
    except AttributeError as exc:
        raise LinearError("NETWORK", f"response stream is not readable ({type(exc).__name__})") from None
    chunks: list[bytes] = []
    total = 0
    while True:
        if time.monotonic() >= deadline_at:
            raise LinearError("NETWORK", "deadline exceeded")
        chunk = reader(READ_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise LinearError("HTTP", "response too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _transport_failure(exc: BaseException, key: str) -> LinearError:
    return LinearError("NETWORK", _clip(f"request failed ({type(exc).__name__}): {exc}", key))


def _urllib_transport(endpoint: str, timeout: float, deadline: float, key: str) -> Transport:
    """Build the default transport: one POST over urllib, no redirects, bounded time and size."""

    def send(body: bytes, headers: dict) -> tuple[int, dict, bytes]:
        deadline_at = time.monotonic() + deadline
        budget = max(deadline_at - time.monotonic(), 0.001)
        request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
        try:
            opened = _OPENER.open(request, timeout=min(timeout, budget))
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise LinearError("HTTP", "redirect refused") from None
            headers_out = dict(exc.headers.items()) if exc.headers else {}
            try:
                error_body = _read_capped(exc, deadline_at)
            except (OSError, http.client.HTTPException) as read_exc:
                raise _transport_failure(read_exc, key) from None
            return exc.code, headers_out, error_body
        except (OSError, http.client.HTTPException) as exc:
            raise _transport_failure(exc, key) from None
        with opened as response:
            try:
                raw = _read_capped(response, deadline_at)
            except (OSError, http.client.HTTPException) as exc:
                raise _transport_failure(exc, key) from None
            return response.status, dict(response.headers.items()), raw

    return send


def _parse(raw: bytes) -> object:
    try:
        return json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _drop_none(values: dict) -> dict:
    return {k: v for k, v in values.items() if v is not None}


def _instant(value: object) -> datetime | None:
    """An ISO-8601 string as an aware datetime (naive reads as UTC), or None when it cannot be read."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def created_since(row: dict, since_iso: str) -> bool:
    """True when the row's createdAt is at or after since_iso. A row whose createdAt cannot be read is kept."""
    created = _instant(row.get("createdAt"))
    floor = _instant(since_iso)
    return created is None or floor is None or created >= floor


def _issue_ref(issue: dict) -> dict:
    return {"id": issue["id"], "identifier": issue["identifier"], "url": issue["url"]}


def _is_duplicate(exc: LinearError) -> bool:
    if exc.code != "GRAPHQL":
        return False
    text = exc.message.lower()
    return any(word in text for word in ("already", "duplicate", "unique"))


class Client:
    """Linear GraphQL client. Every ``ensure_*`` method is safe to repeat."""

    def __init__(
        self,
        key: str,
        transport: Transport | None = None,
        endpoint: str = ENDPOINT,
        timeout: float = 20.0,
        deadline: float = DEADLINE_SECONDS,
    ) -> None:
        """Build a client. ``timeout`` bounds each socket wait; ``deadline`` bounds one whole request.

        Raises ValueError when ``endpoint`` is not https (http is allowed only for localhost).
        """
        _check_endpoint(endpoint)
        self._key = key
        self._transport = transport or _urllib_transport(endpoint, timeout, deadline, key)
        self.last_rate: dict = {}

    def _error(self, code: str, message: str) -> LinearError:
        return LinearError(code, _clip(message, self._key))

    def _record_rate(self, headers: dict) -> None:
        lowered = {str(name).lower(): value for name, value in (headers or {}).items()}
        for header, field in _RATE_HEADERS.items():
            raw = lowered.get(header.lower())
            if raw is None:
                continue
            try:
                self.last_rate[field] = int(str(raw).strip())
            except ValueError:
                continue

    def _graphql_error(self, errors: object) -> LinearError:
        items = [item for item in errors if isinstance(item, dict)] if isinstance(errors, list) else []
        first = items[0] if items else {}
        codes = {
            str((item.get("extensions") or {}).get("code") or "")
            for item in items
            if isinstance(item.get("extensions"), dict)
        }
        message = _clip(first.get("message") or "GraphQL error", self._key)
        if "RATELIMITED" in codes:
            return LinearError("RATELIMITED", message)
        if "AUTHENTICATION_ERROR" in codes:
            return LinearError("AUTH", message)
        if "not found" in message.lower():
            return LinearError("NOT_FOUND", message)
        return LinearError("GRAPHQL", message)

    def query(self, document: str, variables: dict | None = None) -> dict:
        """POST one GraphQL document and return its ``data`` object.

        Raises LinearError. The key is sent only in the Authorization header.
        """
        body = json.dumps({"query": document, "variables": variables or {}}).encode("utf-8")
        headers = {"Content-Type": "application/json", "Authorization": self._key}
        try:
            status, resp_headers, raw = self._transport(body, headers)
        except OSError as exc:
            raise LinearError("NETWORK", f"request failed ({type(exc).__name__})") from None
        self._record_rate(resp_headers)
        payload = _parse(raw)
        if status in (401, 403):
            raise self._error("AUTH", f"HTTP {status}: key rejected")
        if status == 429:
            raise self._error("RATELIMITED", "HTTP 429: rate limited")
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if errors:
            raise self._graphql_error(errors)
        if status >= 400:
            raise self._error("HTTP", f"HTTP {status}")
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise self._error("GRAPHQL", "response carried no data")
        return payload["data"]

    def _team_states(self, team_id: str) -> list[dict]:
        data = self.query(TEAM_STATES, {"id": team_id})
        return data["team"]["states"]["nodes"]

    def team(self, key: str) -> dict:
        """Return the team with this key: id, key, and its workflow states."""
        nodes = self.query(TEAM_BY_KEY, {"key": key})["teams"]["nodes"]
        if not nodes:
            raise self._error("NOT_FOUND", f"team {key} not found")
        node = nodes[0]
        states = [
            {"id": state["id"], "name": state["name"], "type": state["type"]}
            for state in node["states"]["nodes"]
        ]
        return {"id": node["id"], "key": node["key"], "states": states}

    def ensure_state(self, team_id: str, state_id: str, name: str, state_type: str, color: str) -> str:
        """Return the id of a workflow state, creating it with ``state_id`` when missing.

        A state already on the team is matched by id first, then by name.
        """
        states = self._team_states(team_id)
        for state in states:
            if state["id"] == state_id:
                return state_id
        for state in states:
            if state["name"] == name:
                return state["id"]
        data = self.query(
            WORKFLOW_STATE_CREATE,
            {
                "input": {
                    "id": state_id,
                    "teamId": team_id,
                    "name": name,
                    "type": state_type,
                    "color": color,
                }
            },
        )
        return data["workflowStateCreate"]["workflowState"]["id"]

    def ensure_label(self, team_id: str, name: str) -> str:
        """Return the id of a team label by name, creating it when missing."""
        data = self.query(LABEL_FIND, {"teamId": team_id, "name": name})
        for node in data["issueLabels"]["nodes"]:
            if node["name"] == name:
                return node["id"]
        created = self.query(LABEL_CREATE, {"input": {"teamId": team_id, "name": name}})
        return created["issueLabelCreate"]["issueLabel"]["id"]

    def ensure_initiative(self, name: str) -> dict:
        """Return the initiative with this exact name, creating it when missing."""
        data = self.query(INITIATIVE_FIND, {"name": name})
        for node in data["initiatives"]["nodes"]:
            if node["name"] == name:
                return {"id": node["id"], "name": node["name"], "url": node["url"]}
        created = self.query(INITIATIVE_CREATE, {"input": {"name": name}})
        initiative = created["initiativeCreate"]["initiative"]
        return {"id": initiative["id"], "name": initiative["name"], "url": initiative["url"]}

    def ensure_project(self, project_id: str, name: str, team_id: str, description: str) -> dict:
        """Update the project with this id, or create it with that id when missing."""
        try:
            data = self.query(
                PROJECT_UPDATE,
                {"id": project_id, "input": {"name": name, "description": description}},
            )
            project = data["projectUpdate"]["project"]
        except LinearError as exc:
            if exc.code != "NOT_FOUND":
                raise
            data = self.query(
                PROJECT_CREATE,
                {
                    "input": {
                        "id": project_id,
                        "name": name,
                        "teamIds": [team_id],
                        "description": description,
                    }
                },
            )
            project = data["projectCreate"]["project"]
        return {"id": project["id"], "url": project["url"], "name": project["name"]}

    def add_project_to_initiative(self, initiative_id: str, project_id: str) -> None:
        """Link a project to an initiative. An existing link counts as success."""
        try:
            self.query(
                INITIATIVE_LINK,
                {"input": {"initiativeId": initiative_id, "projectId": project_id}},
            )
        except LinearError as exc:
            if not _is_duplicate(exc):
                raise

    def ensure_milestone(self, milestone_id: str, project_id: str, name: str, sort_order: float) -> str:
        """Return the milestone id, creating it with ``milestone_id`` when missing."""
        data = self.query(PROJECT_MILESTONES, {"id": project_id})
        for node in data["project"]["projectMilestones"]["nodes"]:
            if node["id"] == milestone_id:
                return milestone_id
        created = self.query(
            MILESTONE_CREATE,
            {
                "input": {
                    "id": milestone_id,
                    "projectId": project_id,
                    "name": name,
                    "sortOrder": sort_order,
                }
            },
        )
        return created["projectMilestoneCreate"]["projectMilestone"]["id"]

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
        """Update the issue with this id, or create it with that id on NOT_FOUND.

        ``state_id`` is sent only on create, so a status set in Linear survives
        a spec re-push. Returns {'id', 'identifier', 'url'}.
        """
        update = _drop_none(
            {
                "title": title,
                "description": description,
                "projectId": project_id,
                "projectMilestoneId": milestone_id,
                "labelIds": label_ids,
            }
        )
        try:
            data = self.query(ISSUE_UPDATE, {"id": issue_id, "input": update})
        except LinearError as exc:
            if exc.code != "NOT_FOUND":
                raise
            create = _drop_none(
                {
                    "id": issue_id,
                    "teamId": team_id,
                    "stateId": state_id,
                    **update,
                }
            )
            data = self.query(ISSUE_CREATE, {"input": create})
            return _issue_ref(data["issueCreate"]["issue"])
        return _issue_ref(data["issueUpdate"]["issue"])

    def set_issue_state(self, issue_id: str, state_id: str) -> None:
        """Move an issue to a workflow state."""
        self.query(ISSUE_UPDATE, {"id": issue_id, "input": {"stateId": state_id}})

    def ensure_blocks(self, blocker_issue_id: str, blocked_issue_id: str) -> str | None:
        """Record that the blocker blocks the blocked issue.

        Returns the relation id, or None when the relation already existed.
        """
        try:
            data = self.query(
                RELATION_CREATE,
                {
                    "input": {
                        "issueId": blocker_issue_id,
                        "relatedIssueId": blocked_issue_id,
                        "type": "blocks",
                    }
                },
            )
        except LinearError as exc:
            if _is_duplicate(exc):
                return None
            raise
        return data["issueRelationCreate"]["issueRelation"]["id"]

    def find_relation(self, blocker_issue_id: str, blocked_issue_id: str) -> str | None:
        """Return the id of the blocks relation from the blocker to the blocked issue, or None.

        A blocker that no longer exists reads as no relation.
        """
        try:
            data = self.query(ISSUE_RELATIONS, {"id": blocker_issue_id})
        except LinearError as exc:
            if exc.code != "NOT_FOUND":
                raise
            return None
        issue = data.get("issue") or {}
        for node in ((issue.get("relations") or {}).get("nodes")) or []:
            related = node.get("relatedIssue") or {}
            if node.get("type") == "blocks" and related.get("id") == blocked_issue_id:
                return node["id"]
        return None

    def issue_exists(self, issue_id: str) -> bool:
        """True when Linear returns the issue; False when it reports the issue not found.

        Any other error propagates.
        """
        try:
            data = self.query(ISSUE_EXISTS, {"id": issue_id})
        except LinearError as exc:
            if exc.code != "NOT_FOUND":
                raise
            return False
        return data.get("issue") is not None

    def delete_relation(self, relation_id: str) -> None:
        """Delete an issue relation by id. A relation already gone (NOT_FOUND) counts as success."""
        try:
            self.query(RELATION_DELETE, {"id": relation_id})
        except LinearError as exc:
            if exc.code != "NOT_FOUND":
                raise

    def comment(self, issue_id: str, body: str) -> None:
        """Add a comment to an issue."""
        self.query(COMMENT_CREATE, {"input": {"issueId": issue_id, "body": body}})

    def post_update(self, project_id: str, body: str, health: str) -> str | None:
        """Create a project status update with a markdown body and a health enum (onTrack, atRisk, offTrack). Returns its id."""
        data = self.query(PROJECT_UPDATE_CREATE, {"input": {"projectId": project_id, "body": body, "health": health}})
        payload = data["projectUpdateCreate"]
        if not payload.get("success"):
            raise LinearError("GRAPHQL", "project update was not saved")
        update = payload.get("projectUpdate") or {}
        return update.get("id")

    def recent_updates(self, project_id: str, since_iso: str) -> list[dict]:
        """The project's status updates created at or after ``since_iso``, newest first (one page of 50)."""
        project = self.query(PROJECT_UPDATES, {"id": project_id, "first": 50}).get("project") or {}
        nodes = ((project.get("projectUpdates") or {}).get("nodes")) or []
        return [row for row in nodes if isinstance(row, dict) and created_since(row, since_iso)]

    def issues_updated_since(self, project_id: str, since_iso: str | None) -> list[dict]:
        """Return every issue in the project updated after ``since_iso``, ordered by updatedAt.

        Pages through the whole result, 100 issues per request. Raises
        LinearError when the cursor repeats or more than MAX_PAGES pages come back.
        """
        flt: dict = {"project": {"id": {"eq": project_id}}}
        if since_iso:
            flt["updatedAt"] = {"gt": since_iso}
        out: list[dict] = []
        after: str | None = None
        seen: set[str] = set()
        for _page in range(MAX_PAGES):
            conn = self.query(
                PROJECT_ISSUES,
                {"filter": flt, "first": PAGE_SIZE, "after": after},
            )["issues"]
            out.extend(conn["nodes"])
            info = conn.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                return out
            after = info.get("endCursor")
            if not after:
                raise self._error("GRAPHQL", "pagination cursor missing")
            if after in seen:
                raise self._error("GRAPHQL", "pagination cursor repeated")
            seen.add(after)
        raise self._error("GRAPHQL", f"pagination exceeded {MAX_PAGES} pages")


    def _ensure_project_label(self, name: str) -> str:
        """The id of a workspace project label with this exact name, created when missing."""
        nodes = self.query(PROJECT_LABEL_FIND, {"name": name})["projectLabels"]["nodes"]
        for node in nodes:
            if node["name"] == name:
                return node["id"]
        created = self.query(PROJECT_LABEL_CREATE, {"input": {"name": name}})
        return created["projectLabelCreate"]["projectLabel"]["id"]

    def _project_status_id(self, name: str) -> str | None:
        """The id of the workspace project status with this exact name, or None when there is none."""
        nodes = self.query(PROJECT_STATUSES)["projectStatuses"]["nodes"]
        return next((node["id"] for node in nodes if node["name"] == name), None)

    def _user_id(self, name: str) -> str | None:
        """The id of the user whose name or display name is exactly this value, or None."""
        nodes = self.query(USER_FIND, {"name": name})["users"]["nodes"]
        return next((node["id"] for node in nodes if name in (node.get("name"), node.get("displayName"))), None)

    def update_project_meta(self, project_id: str, meta: dict) -> None:
        """Set project metadata from a dict with any of: summary, description, lead, labels, priority, start_date, target_date, links, status.

        Summary maps to the short ``description`` field and description to the long ``content``.
        Labels are appended to the project's current labels, which are never removed. Links are
        appended as external links. A lead or status name that Linear does not know is skipped.
        """
        update: dict = {}
        if meta.get("summary"):
            update["description"] = meta["summary"]
        if meta.get("description"):
            update["content"] = meta["description"]
        if meta.get("priority") is not None:
            update["priority"] = meta["priority"]
        if meta.get("start_date"):
            update["startDate"] = meta["start_date"]
        if meta.get("target_date"):
            update["targetDate"] = meta["target_date"]
        if meta.get("lead"):
            lead_id = self._user_id(meta["lead"])
            if lead_id is not None:
                update["leadId"] = lead_id
        if meta.get("labels"):
            ids = [node["id"] for node in self.query(PROJECT_DETAIL, {"id": project_id})["project"]["labels"]["nodes"]]
            for name in meta["labels"]:
                label_id = self._ensure_project_label(name)
                if label_id not in ids:
                    ids.append(label_id)
            update["labelIds"] = ids
        if meta.get("status"):
            status_id = self._project_status_id(meta["status"])
            if status_id is not None:
                update["statusId"] = status_id
        if update:
            self.query(PROJECT_UPDATE, {"id": project_id, "input": update})
        for link in meta.get("links") or []:
            self.query(LINK_CREATE, {"input": {"projectId": project_id, "url": link["url"], "label": link["title"]}})

    def update_milestone_meta(self, milestone_id: str, project_id: str, description: str | None, target_date: str | None) -> None:
        """Set a milestone's description and target date. ``project_id`` is accepted for signature parity with the MCP client."""
        update = _drop_none({"description": description, "targetDate": target_date})
        if update:
            self.query(MILESTONE_UPDATE, {"id": milestone_id, "input": update})

    def set_issue_meta(self, issue_id: str, add_labels: list[str], priority: int | None) -> None:
        """Append labels to an issue (creating missing team labels) and set its priority.

        The issue's current labels are kept; only names not yet on the issue are added.
        """
        update: dict = {}
        if add_labels:
            node = self.query(ISSUE_LABELS, {"id": issue_id})["issue"]
            ids = [label["id"] for label in node["labels"]["nodes"]]
            added = False
            for name in add_labels:
                label_id = self.ensure_label(node["team"]["id"], name)
                if label_id not in ids:
                    ids.append(label_id)
                    added = True
            if added:
                update["labelIds"] = ids
        if priority is not None:
            update["priority"] = priority
        if update:
            self.query(ISSUE_UPDATE, {"id": issue_id, "input": update})
