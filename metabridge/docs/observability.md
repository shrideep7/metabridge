# Observability

MetaBridge's Observability engine turns the platform's own run history into an operational picture: a composite health score, per-signal monitors, an SLA dashboard, alerting, performance trends, and historical analytics. It is deterministic — every number is computed from evidence MetaBridge already recorded, and every monitor declares its provenance. When there is nothing to observe, it says so rather than reporting a flattering default.

## What it observes

The engine reads MetaBridge's local data directory (jobs, agent runs, saved connections, and the estate Digital Twin) and reduces them to ten monitors. It does not instrument your data platform; it reports on MetaBridge's own operational surface, plus resource and cost figures modeled from your estate topology.

| Monitor | What it tracks | Basis |
| --- | --- | --- |
| `pipeline_health` | Success/failure of conversion and analysis runs, plus in-flight runs | measured |
| `migration_progress` | Completion of conversion runs and count of migration-agent proposals | measured |
| `validation_status` | Average conversion confidence, cyclic pipelines, manual-review items | measured |
| `agent_health` | Agent actions, mean confidence, failures, denials, approvals pending, per-agent breakdown | measured |
| `connector_health` | Configured connections — active/stopped, last-test pass/fail, test latency | measured |
| `performance` | Throughput per day and mean run/job duration | measured (or count-only) |
| `latency` | p50 / p95 / max over agent-action and connection-test durations | measured |
| `failures` | Failure rate across completed jobs and agent actions, with recent detail | measured |
| `resource_utilization` | Modeled load index across compute targets from the estate twin | modeled |
| `cloud_consumption` | Modeled monthly/annual cloud spend from FinOps analysis of the twin | modeled |

### Provenance: measured vs. modeled vs. no-data

Every monitor carries an explicit `basis`:

- **`measured`** — derived from real run, job, and connection facts (statuses, timestamps, durations, test results, confidence scores).
- **`modeled`** — derived from estate topology. Resource utilization and cloud consumption are **modeled, not measured** — they are topology, not live metering. The engine says so in each headline (for example, "Modeled from topology … Not live metering."). Feed real telemetry to replace these with billed figures.
- **`no_data`** — nothing has been observed yet. An empty history yields `no_data`, never an invented healthy default.

The report includes a `determinism_note` that restates this: success/failure, agent-action durations, and connection tests are measured from run history; per-job wall-clock is measured only where a finish time was recorded; resource utilization and cloud consumption are modeled from estate topology.

## Health score and evidence bands

The composite health score is a weighted 0–100 roll-up over whatever sub-scores are available. Contributing signals and their weights:

| Signal | Weight |
| --- | --- |
| Pipeline health | 2.0 |
| Reliability (100 − failure rate) | 2.0 |
| Agent health | 2.0 |
| Validation | 1.5 |
| Connectors | 1.0 |
| Latency | 1.0 |

A signal only contributes when it has a real value; missing signals are dropped rather than defaulted, and the weighted mean is taken over the signals that are present.

### Minimum-evidence bands

The score maps to a band, but bands are gated on how much real evidence backs them:

- If no signal has a value, the band is **`no_data`** with `sample: "insufficient"` — "Not enough operational history to score."
- If fewer than **5 real operations** back the score, the band is **`insufficient`** with `sample: "thin"`. A perfect score off a single run will not read as a healthy track record — the headline calls it "provisional."
- Otherwise the band is one of **`healthy`** (≥90), **`ok`** (≥75), **`degraded`** (≥50), or **`critical`** (<50), with `sample: "adequate"`.

The score also reports its `contributors`, the operation count that backs it, and its `top_detractors` (the lowest-scoring areas) so you can see what is pulling it down.

Latency contributes to the score only when there are at least **5 latency samples** — below that a p95 is not a meaningful percentile, so it is excluded from the score, the SLA dashboard, and alerting.

## Outputs

A single call to the engine returns the full report. Its top-level sections:

### Operational dashboard

A compact summary for at-a-glance monitoring: the composite health score, a per-monitor rollup (status, basis, headline, and score for each of the ten monitors), an `active` block (running jobs and pending approvals), and current alert counts.

### SLA dashboard

Evaluates measured signals against configurable service-level objectives. Defaults:

| Objective | Target |
| --- | --- |
| Availability | 99.0% successful runs |
| Latency (p95) | 60000 ms |
| Failure rate | ≤ 5.0% |

Each objective reports its target, actual, and whether it was met. Availability additionally reports **error budget consumed** and flags `low_confidence` when backed by fewer than 5 operations. Latency is only evaluated once there are enough samples for a reliable p95. The dashboard status is `met` when every objective passes, `breached` otherwise, and `no_data` when there is no history to evaluate.

### Alerting

Rules are evaluated against the monitors and health score, then sorted by severity (critical → warning). Rules that fire:

| Alert | Fires when |
| --- | --- |
| `failure_rate` | Failure rate ≥ 50% (critical) or ≥ 20% (warning) |
| `latency_p95` | p95 latency exceeds the SLA target (warning) — only with ≥ 5 samples |
| `health_score` | Health score < 40 (critical) or < 60 (warning) |
| `connector_down` | One or more connectors stopped or failing (warning) |
| `availability` | Success rate below the availability SLA (warning) |

Each alert carries its severity, the monitor and rule that produced it, a human-readable message, and the value/threshold that triggered it. The block also returns per-severity counts. Alerts only fire from measured signals — a monitor with no data raises nothing.

### Performance trends

A per-day time series of operations, successes, failures, success rate, and mean duration, plus a trend `direction` of `improving`, `declining`, or `stable` (derived by comparing the first and second halves of the history — a swing of more than 5 points in success rate moves it off `stable`).

### Historical analytics

A cumulative rollup across all history: totals by job kind and status, overall success rate, the observed date span, and aggregate timings (mean job/run duration, median action duration).

## Accessing observability

### REST API

Two endpoints under `/api` serve the report:

```
GET /api/observability          # the full report as JSON
GET /api/observability/export   # the same report as a JSON file download
```

`GET /api/observability` returns the complete structure — operational and SLA dashboards, alerting, the composite health score, performance trends, historical analytics, and all ten monitors. `GET /api/observability/export` returns the identical payload with a `Content-Disposition` header so browsers save it as `observability.json`.

The full schema is published in the [OpenAPI reference](/docs), and the report is surfaced in the [web console](/console).

### Programmatic access

The engine is importable directly:

```python
from metabridge.observability import observe

report = observe(data_dir="/path/to/data")
print(report["health_score"]["band"])       # e.g. "insufficient", "ok", "critical"
print(report["operational_dashboard"]["alerts"])
```

`observe()` accepts the data directory (defaults to the `METABRIDGE_DATA_DIR` environment variable), an optional `sla` override dict, and an optional `as_of` label. The public surface also exports `health_score`, `DEFAULT_SLA`, `ALERT_RULES`, and `MONITORS`.

### Custom SLA targets

Override any of the three objectives by passing an `sla` dict; unspecified keys keep their defaults:

```python
report = observe(
    data_dir="/path/to/data",
    sla={"availability_pct": 99.9, "latency_p95_ms": 30000},
)
```

The resolved configuration is echoed back in the report's `sla_config`.

## Resilience

Observability is designed never to take down a report because of one bad input. Each monitor is evaluated in isolation; if a malformed file (written by another component) would crash a monitor, that single monitor degrades to `no_data` — "monitor unavailable" — while the rest of the report is produced normally. Reads are hardened against wrong-typed fields (a bad number coerces to zero rather than raising), and connection secrets are stripped before anything is reported.

## Honesty guarantees

- **Deterministic.** The engine computes from recorded evidence. There is no model in the core path; the same history always yields the same report.
- **Modeled figures are labeled.** Resource utilization and cloud consumption are modeled from topology and say so — treat them as estimates until telemetry is supplied.
- **Bands require evidence.** A high score off a thin history is reported as `insufficient` / provisional, not `healthy`.
- **Empty means empty.** No history yields `no_data`, never a default that reads as good.

## Related

- [Digital Twin](intelligence.md) — the estate model that backs resource and cloud figures.
- [FinOps](intelligence.md) — the cost analysis behind modeled cloud consumption.
- [Governance & Security](governance-security.md) — the approval and audit trail behind agent actions surfaced here.
- [Agents](agentic-ai.md) — the agent runs that feed agent health, migration progress, and validation status.
