"""
PerformanceMonitor : métriques P50/P95, sparkline, summary vide.
"""
import pytest
from core.performance import PerformanceMonitor, _percentile


def _record(pm, total_ms, n=1):
    for _ in range(n):
        pm.record_scan(api_ms=total_ms * 0.8, parse_ms=total_ms * 0.1,
                       process_ms=total_ms * 0.1, fetched=5, new_count=1)


def test_summary_empty():
    pm = PerformanceMonitor()
    s = pm.summary()
    assert s["scans"] == 0
    assert s["latency_p50_ms"] == 0.0
    assert s["latency_p95_ms"] == 0.0
    assert s["sparkline"] == []


def test_p95_higher_than_avg():
    pm = PerformanceMonitor()
    # 19 scans à 100ms, 1 scan à 1000ms → P95 doit refléter la queue
    _record(pm, 100.0, n=19)
    _record(pm, 1000.0, n=1)
    s = pm.summary()
    assert s["latency_p95_ms"] > s["latency_avg_ms"]
    assert s["latency_p95_ms"] >= 900.0


def test_p50_near_median():
    pm = PerformanceMonitor()
    for v in [10.0, 20.0, 30.0, 40.0, 50.0]:
        _record(pm, v)
    s = pm.summary()
    # Médiane de [10,20,30,40,50] = 30
    assert 20.0 <= s["latency_p50_ms"] <= 40.0


def test_sparkline_at_most_20_points():
    pm = PerformanceMonitor()
    _record(pm, 100.0, n=30)
    s = pm.summary()
    assert len(s["sparkline"]) == 20


def test_sparkline_single_scan():
    pm = PerformanceMonitor()
    _record(pm, 250.0)
    s = pm.summary()
    assert len(s["sparkline"]) == 1
    assert s["sparkline"][0] == pytest.approx(250.0, abs=1.0)


def test_percentile_helper():
    assert _percentile([], 95) == 0.0
    assert _percentile([100.0], 95) == 100.0
    vals = list(range(1, 101))  # 1..100
    assert _percentile(vals, 50) == 51  # idx = int(100*50/100) = 50 → sorted[50] = 51
    assert _percentile(vals, 95) == 96  # idx = int(100*95/100) = 95 → sorted[95] = 96
