import random

import pytest


@pytest.fixture(autouse=True)
def isolate_global_random():
    """backfill_history seeds the module-level random; do not let that leak into the next test."""
    state = random.getstate()
    yield
    random.setstate(state)
