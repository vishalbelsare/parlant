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

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Mapping, Sequence

from parlant.core.health import (
    Criticality,
    HealthReport,
    HealthReporter,
    OverallHealth,
    ReportRetention,
    ViewSnapshot,
)


@dataclass
class _RecordingView:
    """Minimal HealthView used to inspect what the reporter delivers."""

    name: str
    kinds: tuple[str, ...]
    criticality: Criticality = Criticality.CRITICAL
    fixed_status: OverallHealth = OverallHealth.HEALTHY
    last_seen: dict[str, Sequence[HealthReport]] = field(default_factory=dict)

    def render(
        self,
        reports_by_kind: Mapping[str, Sequence[HealthReport]],
    ) -> ViewSnapshot:
        self.last_seen = {k: list(v) for k, v in reports_by_kind.items()}
        return ViewSnapshot(
            status=self.fixed_status,
            body={"sample_count": sum(len(v) for v in reports_by_kind.values())},
        )


def _retention(seconds: float = 60.0, max_count: int = 1000) -> ReportRetention:
    return ReportRetention(window=timedelta(seconds=seconds), max_count=max_count)


async def test_that_a_reported_kind_is_visible_to_views_consuming_that_kind() -> None:
    reporter = HealthReporter()
    reporter.configure_retention("nlp.request", _retention())
    view = _RecordingView(name="nlp", kinds=("nlp.request",))
    reporter.register_view(view)

    reporter.report("nlp.request", {"schema": "S", "success": True, "latency_ms": 10.0})
    reporter.report("nlp.request", {"schema": "S", "success": True, "latency_ms": 20.0})

    snapshot = reporter.snapshot()

    assert "nlp" in snapshot["checks"]
    assert snapshot["checks"]["nlp"]["sample_count"] == 2
    assert len(view.last_seen["nlp.request"]) == 2


async def test_that_a_view_does_not_see_kinds_it_did_not_declare() -> None:
    reporter = HealthReporter()
    reporter.configure_retention("nlp.request", _retention())
    reporter.configure_retention("doc_store.query", _retention())

    nlp_view = _RecordingView(name="nlp", kinds=("nlp.request",))
    reporter.register_view(nlp_view)

    reporter.report("nlp.request", {"x": 1})
    reporter.report("doc_store.query", {"y": 2})

    reporter.snapshot()

    assert list(nlp_view.last_seen.keys()) == ["nlp.request"]
    assert len(nlp_view.last_seen["nlp.request"]) == 1


async def test_that_reports_older_than_the_window_are_pruned_on_next_write() -> None:
    reporter = HealthReporter()
    reporter.configure_retention(
        "k", ReportRetention(window=timedelta(milliseconds=50), max_count=1000)
    )
    view = _RecordingView(name="v", kinds=("k",))
    reporter.register_view(view)

    reporter.report("k", {"i": 1})
    reporter.report("k", {"i": 2})

    await asyncio.sleep(0.12)

    reporter.report("k", {"i": 3})

    reporter.snapshot()

    seen = view.last_seen["k"]
    assert [r.attributes["i"] for r in seen] == [3]


async def test_that_reports_beyond_max_count_are_pruned_oldest_first() -> None:
    reporter = HealthReporter()
    reporter.configure_retention("k", ReportRetention(window=timedelta(seconds=60), max_count=3))
    view = _RecordingView(name="v", kinds=("k",))
    reporter.register_view(view)

    for i in range(5):
        reporter.report("k", {"i": i})

    reporter.snapshot()

    seen = view.last_seen["k"]
    assert [r.attributes["i"] for r in seen] == [2, 3, 4]


async def test_that_each_kind_has_independent_retention() -> None:
    reporter = HealthReporter()
    reporter.configure_retention("a", ReportRetention(window=timedelta(seconds=60), max_count=2))
    reporter.configure_retention("b", ReportRetention(window=timedelta(seconds=60), max_count=10))
    view = _RecordingView(name="v", kinds=("a", "b"))
    reporter.register_view(view)

    for i in range(5):
        reporter.report("a", {"i": i})
    for i in range(5):
        reporter.report("b", {"i": i})

    reporter.snapshot()

    assert [r.attributes["i"] for r in view.last_seen["a"]] == [3, 4]
    assert [r.attributes["i"] for r in view.last_seen["b"]] == [0, 1, 2, 3, 4]


async def test_that_overall_status_is_worst_of_critical_views() -> None:
    reporter = HealthReporter(snapshot_cache_ttl=timedelta(0))
    reporter.configure_retention("k", _retention())

    healthy = _RecordingView(name="healthy", kinds=("k",), fixed_status=OverallHealth.HEALTHY)
    degraded = _RecordingView(name="degraded", kinds=("k",), fixed_status=OverallHealth.DEGRADED)
    unhealthy = _RecordingView(name="unhealthy", kinds=("k",), fixed_status=OverallHealth.UNHEALTHY)

    reporter.register_view(healthy)
    reporter.register_view(degraded)
    snapshot = reporter.snapshot()
    assert snapshot["status"] == "degraded"

    reporter.register_view(unhealthy)
    snapshot = reporter.snapshot()
    assert snapshot["status"] == "unhealthy"


async def test_that_snapshot_is_cached_within_the_cache_ttl() -> None:
    reporter = HealthReporter(snapshot_cache_ttl=timedelta(seconds=60))
    reporter.configure_retention("k", _retention())
    view = _RecordingView(name="v", kinds=("k",), fixed_status=OverallHealth.HEALTHY)
    reporter.register_view(view)

    reporter.report("k", {"i": 1})
    first = reporter.snapshot()
    first_render_count = sum(len(v) for v in view.last_seen.values())

    # A second report and a re-fetch within the cache window must NOT trigger
    # a re-render, and the cached body must be returned verbatim.
    reporter.report("k", {"i": 2})
    second = reporter.snapshot()
    second_render_count = sum(len(v) for v in view.last_seen.values())

    assert second is first
    assert second_render_count == first_render_count


async def test_that_snapshot_recomputes_after_cache_ttl_expires() -> None:
    reporter = HealthReporter(snapshot_cache_ttl=timedelta(milliseconds=50))
    reporter.configure_retention("k", _retention())
    view = _RecordingView(name="v", kinds=("k",), fixed_status=OverallHealth.HEALTHY)
    reporter.register_view(view)

    reporter.report("k", {"i": 1})
    first = reporter.snapshot()
    assert first["checks"]["v"]["sample_count"] == 1

    reporter.report("k", {"i": 2})
    await asyncio.sleep(0.12)

    second = reporter.snapshot()
    assert second is not first
    assert second["checks"]["v"]["sample_count"] == 2


async def test_that_informational_views_appear_in_body_but_do_not_affect_overall_status() -> None:
    reporter = HealthReporter()
    reporter.configure_retention("k", _retention())

    critical_healthy = _RecordingView(
        name="critical_healthy",
        kinds=("k",),
        criticality=Criticality.CRITICAL,
        fixed_status=OverallHealth.HEALTHY,
    )
    informational_unhealthy = _RecordingView(
        name="cost",
        kinds=("k",),
        criticality=Criticality.INFORMATIONAL,
        fixed_status=OverallHealth.UNHEALTHY,
    )

    reporter.register_view(critical_healthy)
    reporter.register_view(informational_unhealthy)

    snapshot = reporter.snapshot()

    assert snapshot["status"] == "healthy"
    assert snapshot["checks"]["cost"]["status"] == "unhealthy"


async def test_that_concurrent_reports_from_many_coroutines_are_all_recorded() -> None:
    reporter = HealthReporter()
    reporter.configure_retention(
        "k", ReportRetention(window=timedelta(seconds=60), max_count=10_000)
    )
    view = _RecordingView(name="v", kinds=("k",))
    reporter.register_view(view)

    async def writer(start: int, count: int) -> None:
        for i in range(start, start + count):
            reporter.report("k", {"i": i})
            await asyncio.sleep(0)

    await asyncio.gather(*(writer(w * 100, 100) for w in range(10)))

    reporter.snapshot()

    seen_values = {r.attributes["i"] for r in view.last_seen["k"]}
    assert len(seen_values) == 1000
    assert seen_values == set(range(1000))


async def test_that_a_kind_without_configured_retention_raises_on_report() -> None:
    reporter = HealthReporter()

    raised: type[BaseException] | None = None
    try:
        reporter.report("unconfigured", {"x": 1})
    except Exception as e:  # noqa: BLE001
        raised = type(e)

    assert raised is not None, "expected reporting an unconfigured kind to raise"


def _attrs(reports: Sequence[HealthReport]) -> list[Mapping[str, Any]]:
    return [r.attributes for r in reports]
