"""Read-only FHIR R4 access: a live HTTP client and an offline fixture source.

Both expose the same two methods, so the rest of the code doesn't care where data comes from:

    read(resource_type, id)        -> one resource (GET [base]/Patient/123)
    search(resource_type, params)  -> list of resources (GET [base]/Coverage?patient=123)

The live client only ever sends GET requests. The Oracle Health open sandbox is read-only
anyway, but this keeps the promise in code too.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable
from typing import Protocol

import requests

from . import __version__

ORACLE_OPEN_SANDBOX = "https://fhir-open.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d"
RETRY_STATUS = {429, 502, 503, 504}


class FhirError(Exception):
    """The server returned an error, an OperationOutcome, or could not be reached."""


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
            body = {}
        if resp.status_code >= 400:
            if body.get("resourceType") == "OperationOutcome":
                raise FhirError(f"HTTP {resp.status_code}: {outcome_message(body)}")
            raise FhirError(f"HTTP {resp.status_code} from {url}")
        if body.get("resourceType") == "OperationOutcome":
            raise FhirError(outcome_message(body))
        return body

    def read(self, resource_type: str, resource_id: str) -> dict:
        return self._get(f"{self.base_url}/{resource_type}/{resource_id}")

    def search(self, resource_type: str, params: dict[str, str]) -> list[dict]:
        """Search and follow Bundle link[next] up to max_pages. Only resources of the requested
        type are returned (servers may add OperationOutcome or included resources)."""
        url: str | None = f"{self.base_url}/{resource_type}"
        query: dict | None = dict(params)
        found: list[dict] = []
        for _ in range(self.max_pages):
            if url is None:
                break
            bundle = self._get(url, query)
            found.extend(
                entry["resource"]
                for entry in bundle.get("entry", [])
                if (entry.get("resource") or {}).get("resourceType") == resource_type
            )
            url = next(
                (lk["url"] for lk in bundle.get("link", []) if lk.get("relation") == "next"), None
            )
            # Only follow next links that stay on the same server.
            if url is not None and not url.startswith(self.base_url):
                url = None
            query = None  # the next link already carries the query
        return found


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
