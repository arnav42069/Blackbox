"""Metrics shared by the bounded QA controller and its regression test."""

def failure_count(results):
    """Explicit skips are coverage limits, not passing tests or repairable failures."""
    return sum(result['status'] == 'failed' for result in results)
