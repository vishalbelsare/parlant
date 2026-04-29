# Copyright 2026 Emcie Co Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Generic health reporting infrastructure.

The ``HealthReporter`` collects timestamped reports keyed by ``kind`` and
exposes them to registered ``HealthView`` objects, which interpret reports
into snapshot sections served by the ``/healthz`` endpoint.

The write side is deliberately generic: any subsystem can call
``report(kind, attributes)`` without knowing how the data will be rolled
up. The read side is structured: each registered view declares which kinds
it consumes and how to render them, including a status that contributes
to the overall worst-of rollup when the view is marked as critical.
"""

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from threading import Lock
from typing import Any, Mapping, Protocol, Sequence


class Criticality(str, Enum):
    """Whether a view's status contributes to the overall ``/healthz`` rollup."""

    CRITICAL = "critical"
    INFORMATIONAL = "informational"


class OverallHealth(str, Enum):
    """Health status convention used across views and the top-level rollup."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


_RANK = {
    OverallHealth.HEALTHY: 0,
    OverallHealth.DEGRADED: 1,
    OverallHealth.UNHEALTHY: 2,
}


@dataclass(frozen=True)
class HealthReport:
    """A single timestamped observation of some kind."""

    kind: str
    timestamp: datetime
    attributes: Mapping[str, Any]


@dataclass(frozen=True)
class ReportRetention:
    """Per-kind retention policy.

    ``window`` bounds entries by age. ``max_count`` bounds entries by total
    number, providing a memory ceiling regardless of write rate.
    """

    window: timedelta
    max_count: int


@dataclass(frozen=True)
class ViewSnapshot:
    """The output of rendering a view at one point in time."""

    status: OverallHealth
    body: Mapping[str, Any]


class HealthView(Protocol):
    """A read-side view that interprets reports of one or more kinds."""

    name: str
    criticality: Criticality
    kinds: tuple[str, ...]

    def render(
        self,
        reports_by_kind: Mapping[str, Sequence[HealthReport]],
    ) -> ViewSnapshot: ...


_DEFAULT_SNAPSHOT_CACHE_TTL = timedelta(minutes=5)


class HealthReporter:
    """Collects health reports and renders them through registered views.

    ``snapshot()`` results are cached for ``snapshot_cache_ttl`` so that
    high-frequency probes against ``/healthz`` don't pay the rendering
    cost on every call. Reports continue to be recorded during the cache
    window — only the rollup computation is deferred.
    """

    def __init__(
        self,
        *,
        snapshot_cache_ttl: timedelta = _DEFAULT_SNAPSHOT_CACHE_TTL,
    ) -> None:
        self._retention: dict[str, ReportRetention] = {}
        self._buffers: dict[str, deque[HealthReport]] = defaultdict(deque)
        self._views: list[HealthView] = []
        self._lock = Lock()
        self._snapshot_cache_ttl = snapshot_cache_ttl
        self._cached_snapshot: dict[str, Any] | None = None
        self._cached_snapshot_at: datetime | None = None

    def configure_retention(self, kind: str, retention: ReportRetention) -> None:
        """Configure how long and how many reports of ``kind`` to retain."""
        with self._lock:
            self._retention[kind] = retention

    def register_view(self, view: HealthView) -> None:
        """Register a view that participates in ``snapshot()`` output."""
        with self._lock:
            self._views.append(view)

    def report(self, kind: str, attributes: Mapping[str, Any]) -> None:
        """Record an observation of ``kind``.

        Raises ``KeyError`` if no retention has been configured for ``kind`` —
        misconfigured callers should fail loudly rather than silently writing
        to an unbounded buffer.
        """
        report = HealthReport(
            kind=kind,
            timestamp=datetime.now(timezone.utc),
            attributes=dict(attributes),
        )

        with self._lock:
            if kind not in self._retention:
                raise KeyError(f"No retention configured for health report kind '{kind}'")

            buffer = self._buffers[kind]
            buffer.append(report)
            self._prune(kind, buffer)

    def snapshot(self) -> dict[str, Any]:
        """Render every view and compute the overall worst-of-critical status.

        Results are cached for ``snapshot_cache_ttl``. While the cached
        snapshot is fresh, this method returns it without touching the
        report buffers, so repeated polling of ``/healthz`` does not
        impose rollup cost on the running process.
        """
        now = datetime.now(timezone.utc)

        with self._lock:
            if (
                self._cached_snapshot is not None
                and self._cached_snapshot_at is not None
                and now - self._cached_snapshot_at < self._snapshot_cache_ttl
            ):
                return self._cached_snapshot

            self._prune_all_for_age()
            views = list(self._views)
            buffers_by_view: list[tuple[HealthView, dict[str, list[HealthReport]]]] = []
            for view in views:
                by_kind = {kind: list(self._buffers.get(kind, ())) for kind in view.kinds}
                buffers_by_view.append((view, by_kind))

        overall = OverallHealth.HEALTHY
        checks: dict[str, Any] = {}
        for view, by_kind in buffers_by_view:
            rendered = view.render(by_kind)
            checks[view.name] = {"status": rendered.status.value, **rendered.body}
            if view.criticality is Criticality.CRITICAL:
                if _RANK[rendered.status] > _RANK[overall]:
                    overall = rendered.status

        result = {"status": overall.value, "checks": checks}

        with self._lock:
            self._cached_snapshot = result
            self._cached_snapshot_at = now

        return result

    def _prune(self, kind: str, buffer: deque[HealthReport]) -> None:
        retention = self._retention[kind]
        cutoff = datetime.now(timezone.utc) - retention.window
        while buffer and buffer[0].timestamp < cutoff:
            buffer.popleft()
        while len(buffer) > retention.max_count:
            buffer.popleft()

    def _prune_all_for_age(self) -> None:
        for kind, buffer in self._buffers.items():
            if kind in self._retention:
                self._prune(kind, buffer)
