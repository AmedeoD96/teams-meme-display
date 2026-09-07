"""Tests for the one-copy-at-a-time guard.

Two copies race for the same COM port, and the loser cannot recover: Reconnect has no way to take
a port off another process. So the second copy has to be turned away at startup.
"""

from __future__ import annotations

import os
import sys

import pytest

from pc_app import single_instance

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("win"), reason="the guard is a Windows mutex"
)


@pytest.fixture
def name() -> str:
    """A claim of our own, so a test never collides with the real app or another test run."""
    return f"teams-status-test-{os.getpid()}"


def test_the_first_copy_gets_the_claim(name):
    try:
        assert single_instance.claim(name) is True
    finally:
        single_instance.release()


def test_a_second_copy_is_turned_away(name):
    try:
        assert single_instance.claim(name) is True
        assert single_instance.claim(name) is False
    finally:
        single_instance.release()


def test_the_claim_comes_back_after_it_is_released(name):
    single_instance.claim(name)
    single_instance.release()
    try:
        assert single_instance.claim(name) is True
    finally:
        single_instance.release()


def test_a_broken_check_does_not_block_startup(name, monkeypatch):
    """Refusing to start because the guard itself failed would be worse than the deadlock it
    guards against."""
    monkeypatch.setattr(single_instance, "_kernel32", lambda: None)
    assert single_instance.claim(name) is True
