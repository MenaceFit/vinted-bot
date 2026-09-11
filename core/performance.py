"""
Moniteur de performance temps réel — fenêtre glissante, zéro dépendance externe.

Objectif : pouvoir répondre objectivement à "est-ce qu'on a vraiment accéléré
le scan ?" (voir README / benchmark) plutôt que d'estimer à l'oeil.
"""
import time
from collections import deque
from dataclasses import dataclass


@dataclass
class ScanSample:
    ts: float
    api_ms: float
    parse_ms: float
    process_ms: float
    total_ms: float
    fetched: int
    new_count: int
    error: bool
    retries: int


class PerformanceMonitor:
    """Thread-friendly par construction : uniquement des opérations atomiques
    (append/pop sur deque, incréments d'int) protégées par le GIL — pas de lock,
    cohérent avec le reste du projet qui lit déjà des stats cross-thread ainsi."""

    def __init__(self, window: int = 200):
        self._samples: deque[ScanSample] = deque(maxlen=window)
        self.total_requests = 0
        self.total_errors = 0
        self.total_retries = 0
        self.start_time = time.time()

    def record_scan(
        self,
        *,
        api_ms: float,
        parse_ms: float,
        process_ms: float,
        fetched: int = 0,
        new_count: int = 0,
        error: bool = False,
        retries: int = 0,
    ) -> None:
        total_ms = api_ms + parse_ms + process_ms
        self._samples.append(ScanSample(
            ts=time.time(), api_ms=api_ms, parse_ms=parse_ms, process_ms=process_ms,
            total_ms=total_ms, fetched=fetched, new_count=new_count,
            error=error, retries=retries,
        ))
        self.total_requests += 1
        if error:
            self.total_errors += 1
        self.total_retries += retries

    def record_error(self) -> None:
        self.total_errors += 1

    def record_retry(self, count: int = 1) -> None:
        self.total_retries += count

    def summary(self) -> dict:
        samples = list(self._samples)
        if not samples:
            return {
                "scans": 0, "requests": self.total_requests,
                "errors": self.total_errors, "retries": self.total_retries,
                "api_ms": 0.0, "parse_ms": 0.0, "process_ms": 0.0, "total_ms": 0.0,
                "latency_min_ms": 0.0, "latency_max_ms": 0.0, "latency_avg_ms": 0.0,
                "latency_p50_ms": 0.0, "latency_p95_ms": 0.0,
                "sparkline": [],
                "uptime_s": time.time() - self.start_time,
            }

        totals = [s.total_ms for s in samples]
        sparkline = [round(s.total_ms, 1) for s in samples[-20:]]
        return {
            "scans": len(samples),
            "requests": self.total_requests,
            "errors": self.total_errors,
            "retries": self.total_retries,
            "api_ms": _avg(s.api_ms for s in samples),
            "parse_ms": _avg(s.parse_ms for s in samples),
            "process_ms": _avg(s.process_ms for s in samples),
            "total_ms": _avg(totals),
            "latency_min_ms": min(totals),
            "latency_max_ms": max(totals),
            "latency_avg_ms": _avg(totals),
            "latency_p50_ms": _percentile(totals, 50),
            "latency_p95_ms": _percentile(totals, 95),
            "sparkline": sparkline,
            "uptime_s": time.time() - self.start_time,
        }

    def reset(self) -> None:
        self._samples.clear()
        self.total_requests = 0
        self.total_errors = 0
        self.total_retries = 0
        self.start_time = time.time()


def _avg(values) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, min(int(len(s) * p / 100), len(s) - 1))
    return s[idx]
