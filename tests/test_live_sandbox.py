"""One optional smoke test against Oracle Health's public open sandbox (read-only GETs).

Skipped by default so CI and normal test runs never touch the network. Run it with:
    WC_LIVE=1 pytest -m live
The sandbox's data is shared and changes, so this only checks that the card can be built.
"""

import os

import pytest

from wc_readiness.card import build_card
from wc_readiness.fhir_client import ORACLE_OPEN_SANDBOX, FhirClient
from wc_readiness.fhir_facts import gather

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("WC_LIVE") != "1", reason="set WC_LIVE=1 to hit the sandbox"),
]


def test_open_sandbox_card_builds():
    facts = gather(FhirClient(ORACLE_OPEN_SANDBOX), "12724066", "97954296")
    assert "Patient" not in facts.errors, facts.errors
    assert facts.patient_name
    card = build_card(facts)
    assert card.gaps
