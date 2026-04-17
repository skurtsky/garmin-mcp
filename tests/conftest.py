# tests/conftest.py
import os
import pytest
from dotenv import load_dotenv
from garmin_client import get_client

load_dotenv()

# Known good IDs from notebook exploration — update if needed
RUN_ACTIVITY_ID     = 22545458432  # Ottawa - 5 x 1K @ 5K effort
CYCLING_ACTIVITY_ID = 22492461814  # Road Cycling - Plantagenet-Bourget 100K
SWIM_ACTIVITY_ID    = 22516481045  # Pool Swim
TEST_DATE           = "2026-04-16"  # Date of run activity

@pytest.fixture(scope="session")
def client():
    """Single authenticated client reused across all tests."""
    return get_client()

@pytest.fixture(scope="session")
def run_activity_id():
    return RUN_ACTIVITY_ID

@pytest.fixture(scope="session")
def cycling_activity_id():
    return CYCLING_ACTIVITY_ID

@pytest.fixture(scope="session")
def swim_activity_id():
    return SWIM_ACTIVITY_ID

@pytest.fixture(scope="session")
def test_date():
    return TEST_DATE