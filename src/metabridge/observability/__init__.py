"""MetaBridge Observability Engine.

Monitors MetaBridge's own operational surface — the analysis/conversion
jobs it has run, the agent runs, and the health of saved connections —
and models resource/cloud figures from the estate twin. It is honest
about provenance: durations, success/failure and connection tests are
MEASURED from real run history; resource utilization and cloud
consumption are MODELED from topology (not live metering) unless
telemetry is supplied.

Produces an operational dashboard, an SLA dashboard, alerting, a
composite health score, performance trends and historical analytics —
all deterministic.
"""
from .engine import (observe, health_score, DEFAULT_SLA, ALERT_RULES,
                     MONITORS)

__all__ = ["observe", "health_score", "DEFAULT_SLA", "ALERT_RULES",
           "MONITORS"]
