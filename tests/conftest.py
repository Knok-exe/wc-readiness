import json
import socket
from importlib import resources

import pytest

from wc_readiness.fhir_client import FixtureSource


@pytest.fixture(autouse=True)
def no_network(request, monkeypatch):
    """Tests must never touch the internet. Anything that tries to open a socket fails loudly,
    except tests marked `live` (which are skipped unless WC_LIVE=1)."""
    if request.node.get_closest_marker("live"):
        return

    def guard(*args, **kwargs):
        raise RuntimeError("network access attempted in an offline test")

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(socket, "create_connection", guard)


def demo_text(name: str) -> str:
    return resources.files("wc_readiness").joinpath("demo", name).read_text(encoding="utf-8")


@pytest.fixture
def demo_data() -> dict:
    return json.loads(demo_text("demo_fhir.json"))


@pytest.fixture
def demo_source(demo_data) -> FixtureSource:
    return FixtureSource(demo_data)


@pytest.fixture
def complete_a04() -> str:
    return demo_text("a04_wc_complete.hl7")


@pytest.fixture
def incomplete_a04() -> str:
    return demo_text("a04_wc_incomplete.hl7")
