import pytest
import requests

from wc_readiness.fhir_client import FhirClient, FhirError, FixtureSource, outcome_message


class FakeResponse:
    def __init__(self, body, status=200, headers=None):
        self._body, self.status_code, self.headers = body, status, headers or {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeSession:
    """Serves queued responses in order and records calls. It only has .get(), so any attempt
    to write (post/put/patch/delete) would fail: the client is read-only by construction."""

    def __init__(self, responses):
        self.responses, self.calls, self.headers = list(responses), [], {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def bundle(ids, next_url=None, rtype="Coverage"):
    b = {
        "resourceType": "Bundle",
        "entry": [{"resource": {"resourceType": rtype, "id": i}} for i in ids],
    }
    if next_url:
        b["link"] = [{"relation": "next", "url": next_url}]
    return FakeResponse(b)


def client(responses, **kw):
    session = FakeSession(responses)
    return FhirClient("http://x/r4", session=session, sleep=lambda s: None, **kw), session


def test_search_follows_next_links():
    c, s = client([bundle(["a"], "http://x/r4/Coverage?page=2"), bundle(["b"])])
    assert [r["id"] for r in c.search("Coverage", {"patient": "1"})] == ["a", "b"]
    assert s.calls[0] == ("http://x/r4/Coverage", {"patient": "1"})
    assert s.calls[1] == ("http://x/r4/Coverage?page=2", None)


def test_next_link_to_another_host_is_not_followed():
    """Not followed, and not silently partial either: the search counts as failed."""
    c, s = client([bundle(["a"], "http://evil.example/Coverage?page=2")])
    with pytest.raises(FhirError, match="leaves the server"):
        c.search("Coverage", {})
    assert len(s.calls) == 1


def test_max_pages_limits_paging():
    c, s = client([bundle(["a"], "http://x/r4/n1"), bundle(["b"], "http://x/r4/n2")], max_pages=2)
    with pytest.raises(FhirError, match="truncated after 2 pages"):
        c.search("Coverage", {})
    assert len(s.calls) == 2


def test_paging_within_max_pages_returns_everything():
    c, s = client([bundle(["a"], "http://x/r4/n1"), bundle(["b"])], max_pages=2)
    assert [r["id"] for r in c.search("Coverage", {})] == ["a", "b"]


def test_other_resource_types_in_bundle_are_dropped():
    resp = FakeResponse(
        {
            "resourceType": "Bundle",
            "entry": [
                {"resource": {"resourceType": "Coverage", "id": "a"}},
                {"resource": {"resourceType": "OperationOutcome"}},
            ],
        }
    )
    c, _ = client([resp])
    assert [r["id"] for r in c.search("Coverage", {})] == ["a"]


def test_operation_outcome_error_is_readable():
    oo = {
        "resourceType": "OperationOutcome",
        "issue": [{"severity": "error", "code": "invalid", "details": {"text": "bad combo"}}],
    }
    c, _ = client([FakeResponse(oo, 400)])
    with pytest.raises(FhirError, match="HTTP 400.*bad combo"):
        c.search("Coverage", {"patient": "1", "-encounter": "2"})


def test_retries_on_503_then_succeeds():
    c, s = client([FakeResponse({}, 503, {"Retry-After": "1"}), bundle(["a"])])
    assert len(c.search("Coverage", {})) == 1
    assert len(s.calls) == 2


def test_gives_up_after_retries():
    c, _ = client([FakeResponse({}, 504)] * 3, max_retries=2)
    with pytest.raises(FhirError, match="504"):
        c.read("Patient", "1")


def test_network_error_becomes_fhir_error():
    c, _ = client([requests.ConnectionError("down")] * 3, max_retries=2)
    with pytest.raises(FhirError, match="could not reach"):
        c.read("Patient", "1")


def test_non_json_error_body():
    c, _ = client([FakeResponse(ValueError("not json"), 500)])
    with pytest.raises(FhirError, match="HTTP 500"):
        c.read("Patient", "1")


def test_outcome_message_fallback():
    assert outcome_message({}) == "unknown FHIR error"


def test_fixture_source_filters_like_the_app_needs(demo_source):
    covs = demo_source.search("Coverage", {"patient": "demo-wc-1001"})
    assert {c["id"] for c in covs} == {"demo-cov-1001-wc", "demo-cov-1001-ph"}
    enc_covs = demo_source.search("Coverage", {"-encounter": "demo-enc-1001"})
    assert [c["id"] for c in enc_covs] == ["demo-cov-1001-wc"]
    with pytest.raises(FhirError, match="404"):
        demo_source.read("Patient", "nope")


def test_fixture_source_returns_copies(demo_source):
    demo_source.read("Patient", "demo-wc-1001")["name"] = []
    assert demo_source.read("Patient", "demo-wc-1001")["name"]


def test_fixture_source_empty():
    assert FixtureSource({}).search("Coverage", {"patient": "1"}) == []
