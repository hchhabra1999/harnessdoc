"""The only eval in the repo: covers intent classification, nothing else."""

import pytest

from acme_support.router import classify_intent

CASES = [
    ("Where is my parcel? It's been a week", "order_status"),
    ("I want to send these earphones back", "return"),
    ("My laptop screen flickers after 2 months", "warranty"),
    ("Does the X200 support fast charging?", "product_question"),
]


@pytest.mark.parametrize("message,intent", CASES)
def test_classify_intent(message, intent):
    assert classify_intent(message) == intent
