"""Read-only FHIR R4 access: a live HTTP client and an offline fixture source.

Both expose the same two methods, so the rest of the code doesn't care where data comes from:

    read(resource_type, id)        -> one resource (GET [base]/Patient/123)
    search(resource_type, params)  -> list of resources (GET [base]/Coverage?patient=123)

The live client only ever sends GET requests. The Oracle Health open sandbox is read-only
anyway, but this keeps the promise in code too.
"""

from __future__ import annotations

import copy
import re
import time
from collections.abc import Callable
from typing import Protocol
from urllib.parse import quote, unquote, urlsplit

import requests

from . import __version__

ORACLE_OPEN_SANDBOX = "https://fhir-open.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d"
RETRY_STATUS = {429, 502, 503, 504}


# The FHIR `id` datatype: letters, digits, '-' and '.', 1-64 characters. Anything else (a '/',
# '?', '#', space...) could change which URL we request, so it's refused before any request.
FHIR_ID = re.compile(r"^[A-Za-z0-9\-.]{1,64}$")


class FhirError(Exception):
    """The server returned an error, an OperationOutcome, or could not be reached."""


def check_id(resource_id: str) -> str:
    """Return the id if it is a valid FHIR id, else raise ValueError."""
    if not FHIR_ID.match(resource_id or "") or resource_id in (".", ".."):
        raise ValueError(f"not a valid FHIR id: {resource_id!r} (letters, digits, '-', '.')")
    return resource_id


_DEFAULT_PORTS = {"https": 443, "http": 80}


def _origin(parts) -> tuple[str, str, int | None] | None:
    scheme = parts.scheme.lower()
    try:
        port = parts.port
    except ValueError:  # a port that isn't a number
        return None
    return scheme, (parts.hostname or "").lower(), port or _DEFAULT_PORTS.get(scheme)


def same_server(url: str, base_url: str) -> bool:
    """True if `url` is on the same scheme + host + port as `base_url` (default ports filled in,
    so :443 is the same as no port) and under its path. A plain startswith() would accept
    https://fhir.example.com.attacker.net/...; a server could also resolve '.'/'..' segments to
    a path outside the base, so these are refused in every disguise we know of: encoded once
    (%2e%2e), with ';' path parameters (..;/ on Java servers), split by a backslash (also
    %5c, a separator on IIS), or encoded twice (any '%' left after one decode is refused: a
    server's own next links never need it). Any userinfo, even an empty '@', is refused."""
    if "\\" in url:
        return False
    u, b = urlsplit(url), urlsplit(base_url)
    if "@" in u.netloc or u.username or u.password:
        return False
    origin = _origin(u)
    if origin is None or not origin[1] or origin != _origin(b):
        return False
    path = unquote(u.path)
    if "%" in path:
        return False
    for seg in re.split(r"[/\\]", path):
        if seg.split(";", 1)[0] in (".", ".."):
            return False
    base_path = b.path.rstrip("/")
    return u.path == base_path or u.path.startswith(base_path + "/")


class FhirSource(Protocol):
    def read(self, resource_type: str, resource_id: str) -> dict: ...

    def search(self, resource_type: str, params: dict[str, str]) -> list[dict]: ...


def outcome_message(body: dict) -> str:
    """Pull a readable message out of an OperationOutcome resource."""
    parts = []
    for issue in body.get("issue", []):
        text = issue.get("diagnostics") or (issue.get("details") or {}).get("text") or ""
        parts.append(f"{issue.get('severity', 'error')}: {issue.get('code', '')} {text}".strip())
    return "; ".join(parts) or "unknown FHIR error"


class FhirClient:
    """Minimal read-only client with retries and Bundle paging."""

    def __init__(
        self,
        base_url: str = ORACLE_OPEN_SANDBOX,
        session: requests.Session | None = None,
        timeout: float = 60.0,
        max_retries: int = 2,
        max_pages: int = 5,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update(
            {"Accept": "application/fhir+json", "User-Agent": f"wc-readiness/{__version__}"}
        )
        self.timeout = timeout
        self.max_retries = max_retries
        self.max_pages = max_pages
        self._sleep = sleep

    def _get(self, url: str, params: dict | None = None) -> dict:
        resp = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt == self.max_retries:
                    raise FhirError(f"could not reach {url}: {exc}") from exc
                self._sleep(2**attempt)
                continue
            if resp.status_code not in RETRY_STATUS or attempt == self.max_retries:
                break
            retry_after = (getattr(resp, "headers", None) or {}).get("Retry-After", "")
            self._sleep(min(float(retry_after) if retry_after.isdigit() else 2**attempt, 30))
        assert resp is not None
        try:
            body = resp.json()
        except ValueError:
            body = None  # not JSON at all (an HTML error page, a captive portal...)
        if not isinstance(body, dict):
            body = None
        if resp.status_code >= 400:
            if body is not None and body.get("resourceType") == "OperationOutcome":
                raise FhirError(f"HTTP {resp.status_code}: {outcome_message(body)}")
            raise FhirError(f"HTTP {resp.status_code} from {url}")
        if body is None or not body.get("resourceType"):
            # A 200 that isn't a FHIR resource must not be read as "nothing found".
            raise FhirError(f"HTTP {resp.status_code} from {url} was not a FHIR JSON resource")
        if body.get("resourceType") == "OperationOutcome":
            raise FhirError(outcome_message(body))
        return body

    def read(self, resource_type: str, resource_id: str) -> dict:
        check_id(resource_id)
        body = self._get(f"{self.base_url}/{resource_type}/{quote(resource_id, safe='')}")
        if body.get("resourceType") != resource_type:
            raise FhirError(
                f"asked for {resource_type}/{resource_id}, got a {body.get('resourceType')}"
            )
        return body

    def search(self, resource_type: str, params: dict[str, str]) -> list[dict]:
        """Search and follow Bundle link[next] up to max_pages. Only resources of the requested
        type are returned (servers may add OperationOutcome or included resources).

        Partial results are never returned silently: if there are more pages than max_pages,
        or a next link points at another server, this raises FhirError, so the rules that need
        this search count as "couldn't check" instead of "checked, nothing there"."""
        url: str | None = f"{self.base_url}/{resource_type}"
        query: dict | None = dict(params)
        found: list[dict] = []
        for _ in range(self.max_pages):
            bundle = self._get(url, query)
            if bundle.get("resourceType") != "Bundle":
                raise FhirError(f"search {resource_type} returned a {bundle.get('resourceType')}")
            found.extend(
                entry["resource"]
                for entry in bundle.get("entry", []) or []
                if isinstance(entry, dict)
                and (entry.get("resource") or {}).get("resourceType") == resource_type
            )
            url = next(
                (
                    lk.get("url")
                    for lk in bundle.get("link", []) or []
                    if isinstance(lk, dict) and lk.get("relation") == "next"
                ),
                None,
            )
            if url is None:
                return found
            if not same_server(url, self.base_url):
                raise FhirError(f"search {resource_type}: next link leaves the server ({url})")
            query = None  # the next link already carries the query
        raise FhirError(
            f"search {resource_type}: results truncated after {self.max_pages} pages "
            f"({len(found)} so far)"
        )


def _refs(resource: dict) -> set[str]:
    """Every 'Type/id' reference in the usual patient/encounter slots of a resource."""
    refs = set()
    for key in ("subject", "patient", "beneficiary", "encounter"):
        ref = (resource.get(key) or {}).get("reference")
        if ref:
            refs.add(ref)
    for ref in resource.get("context", {}).get("encounter", []) or []:
        if ref.get("reference"):
            refs.add(ref["reference"])
    for ext in resource.get("extension", []):
        ref = (ext.get("valueReference") or {}).get("reference")
        if ref:
            refs.add(ref)
    return refs


def _categories(resource: dict) -> set[str]:
    cats = resource.get("category", [])
    if isinstance(cats, dict):
        cats = [cats]
    return {c.get("code", "") for cat in cats for c in cat.get("coding", [])}


class FixtureSource:
    """Serves resources from a recorded/synthetic JSON file instead of the network.

    The file looks like {"resources": [ {...}, {...} ]}. Searches support the few parameters
    this app uses: patient, -encounter and category. Like Oracle's open sandbox (OBSERVED: it
    returned the whole problem list), an `encounter` parameter is accepted but not applied, so
    callers must filter by encounter themselves.
    """

    def __init__(self, data: dict):
        self.resources: list[dict] = list(data.get("resources", []))
        self.calls: list[tuple[str, str]] = []

    def read(self, resource_type: str, resource_id: str) -> dict:
        check_id(resource_id)
        self.calls.append((resource_type, resource_id))
        for res in self.resources:
            if res.get("resourceType") == resource_type and res.get("id") == resource_id:
                return copy.deepcopy(res)
        raise FhirError(f"HTTP 404: error: not-found {resource_type}/{resource_id}")

    def search(self, resource_type: str, params: dict[str, str]) -> list[dict]:
        self.calls.append((resource_type, str(sorted(params.items()))))
        out = []
        for res in self.resources:
            if res.get("resourceType") != resource_type:
                continue
            refs = _refs(res)
            if "patient" in params and f"Patient/{params['patient']}" not in refs:
                continue
            if "-encounter" in params and f"Encounter/{params['-encounter']}" not in refs:
                continue
            if "category" in params and params["category"] not in _categories(res):
                continue
            out.append(copy.deepcopy(res))
        return out
