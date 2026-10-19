"""TEMPORARY: gives ci-insights real failures to report. Delete after the demo."""

import random


def test_demo_always_fails():
    assert 1 + 1 == 3, "deliberate failure for the ci-insights demo"


def test_demo_flaky():
    assert random.random() < 0.5, "deliberately flaky for the ci-insights demo"
