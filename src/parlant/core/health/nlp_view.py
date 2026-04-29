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

"""HealthView for the NLP layer.

Aggregates ``nlp.request`` and ``nlp.embed`` reports into a per-schema
status with success rate, latency percentiles, and a recent-error
breakdown. The view's status is the worst across all observed schemas.
"""

from collections import Counter
from typing import Any, Mapping, Sequence

from parlant.core.health.reporter import (
    Criticality,
    HealthReport,
    OverallHealth,
    ViewSnapshot,
)


NLP_REQUEST_KIND = "nlp.request"
NLP_EMBED_KIND = "nlp.embed"


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(round(p * (len(s) - 1)))))
    return s[idx]


class NLPHealthView:
    """Renders NLP health grouped by schema.

    Status thresholds are constructor parameters so deployments can tune
    them without changing the view itself. Defaults err toward leniency
    so transient blips don't pull a pod out of rotation.
    """

    name = "nlp"
    criticality = Criticality.CRITICAL
    kinds: tuple[str, ...] = (NLP_REQUEST_KIND, NLP_EMBED_KIND)

    def __init__(
        self,
        *,
        degraded_below_success_rate: float = 0.95,
        unhealthy_below_success_rate: float = 0.80,
        degraded_p95_ms: float = 5_000.0,
        unhealthy_p95_ms: float = 15_000.0,
        recent_errors_top_k: int = 5,
    ) -> None:
        self._degraded_below_success_rate = degraded_below_success_rate
        self._unhealthy_below_success_rate = unhealthy_below_success_rate
        self._degraded_p95_ms = degraded_p95_ms
        self._unhealthy_p95_ms = unhealthy_p95_ms
        self._recent_errors_top_k = recent_errors_top_k

    def render(
        self,
        reports_by_kind: Mapping[str, Sequence[HealthReport]],
    ) -> ViewSnapshot:
        all_reports: list[HealthReport] = []
        for kind in self.kinds:
            all_reports.extend(reports_by_kind.get(kind, ()))

        if not all_reports:
            return ViewSnapshot(
                status=OverallHealth.HEALTHY,
                body={"sample_count": 0, "schemas": {}},
            )

        per_schema: dict[str, list[HealthReport]] = {}
        for report in all_reports:
            schema = str(report.attributes.get("schema", "<unknown>"))
            per_schema.setdefault(schema, []).append(report)

        schemas_body: dict[str, Any] = {}
        worst = OverallHealth.HEALTHY
        for schema, reports in per_schema.items():
            schema_body, schema_status = self._render_schema(reports)
            schemas_body[schema] = schema_body
            if _rank(schema_status) > _rank(worst):
                worst = schema_status

        return ViewSnapshot(
            status=worst,
            body={
                "sample_count": len(all_reports),
                "schemas": schemas_body,
            },
        )

    def _render_schema(
        self,
        reports: list[HealthReport],
    ) -> tuple[dict[str, Any], OverallHealth]:
        successes = sum(1 for r in reports if bool(r.attributes.get("success")))
        sample_count = len(reports)
        success_rate = successes / sample_count if sample_count else 1.0

        latencies = [
            float(r.attributes["latency_ms"])
            for r in reports
            if "latency_ms" in r.attributes and r.attributes["latency_ms"] is not None
        ]
        p50 = _percentile(latencies, 0.5)
        p95 = _percentile(latencies, 0.95)

        error_counter: Counter[str] = Counter()
        for r in reports:
            if r.attributes.get("success"):
                continue
            error_class = str(r.attributes.get("error_class") or "Unknown")
            error_counter[error_class] += 1
        recent_errors = dict(error_counter.most_common(self._recent_errors_top_k))

        status = self._classify(success_rate=success_rate, p95_ms=p95)

        return {
            "status": status.value,
            "success_rate": round(success_rate, 4),
            "p50_latency_ms": round(p50, 2),
            "p95_latency_ms": round(p95, 2),
            "recent_errors": recent_errors,
            "sample_count": sample_count,
        }, status

    def _classify(self, *, success_rate: float, p95_ms: float) -> OverallHealth:
        if success_rate < self._unhealthy_below_success_rate or p95_ms >= self._unhealthy_p95_ms:
            return OverallHealth.UNHEALTHY
        if success_rate < self._degraded_below_success_rate or p95_ms >= self._degraded_p95_ms:
            return OverallHealth.DEGRADED
        return OverallHealth.HEALTHY


def _rank(status: OverallHealth) -> int:
    return {
        OverallHealth.HEALTHY: 0,
        OverallHealth.DEGRADED: 1,
        OverallHealth.UNHEALTHY: 2,
    }[status]
