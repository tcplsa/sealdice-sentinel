from __future__ import annotations

import asyncio
import logging

from ..models import HealthSample
from ..ports import SealDiceProbe
from .incident_service import IncidentService


class HealthMonitor:
    def __init__(
        self,
        name: str,
        probe: SealDiceProbe,
        incidents: IncidentService,
        interval_seconds: int,
        failure_threshold: int,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        self._name = name
        self._probe = probe
        self._incidents = incidents
        self._interval_seconds = interval_seconds
        self._failure_threshold = failure_threshold
        self._consecutive_failures = 0
        self._logger = logging.getLogger(__name__)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                sample = await self._probe.health()
                await self.process_sample(sample)
            except Exception:
                self._logger.exception("health probe crashed", extra={"probe": self._name})
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval_seconds)
            except TimeoutError:
                pass

    async def process_sample(self, sample: HealthSample) -> None:
        if sample.healthy:
            self._consecutive_failures = 0
            await self._incidents.report_healthy(
                sample.service,
                sample.checked_at,
                source=f"health:{self._name}",
                latency_ms=sample.latency_ms,
            )
            return

        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            await self._incidents.report_down(sample, source=f"health:{self._name}")
        else:
            await self._incidents.record_sample(sample)
