"""Built-in connector catalog: cloud data platforms, on-prem databases, SAP, apps."""
from __future__ import annotations

from .base import ConnectionField as F
from .base import ConnectorSpec, registry

_GLOBAL = ["us", "eu", "apac"]


def _std(user: bool = True, password: bool = True, host: bool = True,
         port: str = "", database: bool = True, schema: bool = True):
    fields = []
    if host:
        fields.append(F("host", "Host"))
    if port:
        fields.append(F("port", "Port", required=False, default=port))
    if user:
        fields.append(F("user", "Username"))
    if password:
        fields.append(F("password", "Password", secret=True))
    if database:
        fields.append(F("database", "Database"))
    if schema:
        fields.append(F("schema", "Schema", required=False))
    return fields


# ---------------------------------------------------------------------------
# Cloud data platforms
# ---------------------------------------------------------------------------

registry.register(ConnectorSpec(
    key="snowflake", name="Snowflake", category="cloud_dw", vendor="Snowflake",
    dialect="snowflake", deployment="cloud", regions=_GLOBAL,
    dbt_adapter="snowflake", idmc_type="Snowflake Data Cloud",
    powercenter_dbtype="ODBC",
    fields=[F("account", "Account identifier"), F("user", "Username"),
            F("password", "Password", secret=True), F("role", "Role", required=False),
            F("warehouse", "Warehouse"), F("database", "Database"),
            F("schema", "Schema", required=False)]))

registry.register(ConnectorSpec(
    key="bigquery", name="Google BigQuery", category="cloud_dw", vendor="Google",
    dialect="bigquery", deployment="cloud", regions=_GLOBAL,
    dbt_adapter="bigquery", idmc_type="Google BigQuery V2",
    powercenter_dbtype="ODBC",
    fields=[F("project", "GCP project"), F("dataset", "Dataset"),
            F("keyfile", "Service-account keyfile path", secret=True),
            F("location", "Location (US/EU/region)", required=False, default="EU")],
    type_map={"int64": "bigint", "float64": "double", "bool": "boolean",
              "bytes": "binary", "numeric": "decimal", "bignumeric": "decimal"}))

registry.register(ConnectorSpec(
    key="databricks", name="Databricks Lakehouse", category="lakehouse",
    vendor="Databricks", dialect="databricks", deployment="cloud", regions=_GLOBAL,
    dbt_adapter="databricks", idmc_type="Databricks Delta",
    powercenter_dbtype="ODBC",
    fields=[F("host", "Workspace host"), F("http_path", "SQL warehouse HTTP path"),
            F("token", "Access token", secret=True),
            F("catalog", "Unity catalog", required=False),
            F("schema", "Schema", required=False)]))

registry.register(ConnectorSpec(
    key="redshift", name="Amazon Redshift", category="cloud_dw", vendor="AWS",
    dialect="redshift", deployment="cloud", regions=_GLOBAL,
    dbt_adapter="redshift", idmc_type="Amazon Redshift V2",
    powercenter_dbtype="ODBC", fields=_std(port="5439")))

registry.register(ConnectorSpec(
    key="synapse", name="Azure Synapse / Fabric Warehouse", category="cloud_dw",
    vendor="Microsoft", dialect="tsql", deployment="cloud", regions=_GLOBAL,
    dbt_adapter="synapse", idmc_type="Azure Synapse SQL",
    powercenter_dbtype="Microsoft SQL Server", fields=_std(port="1433")))

# ---------------------------------------------------------------------------
# On-premises databases
# ---------------------------------------------------------------------------

# Oracle is a LIVE source (see metabridge.livecheck): Test connection and
# Analyze open a real read-only session. Its `database` field is spelled out
# as the service name because Oracle resolves the database at connect time —
# a hostname or container name typed here fails with a bare ORA-12514.
registry.register(ConnectorSpec(
    key="oracle", name="Oracle Database", category="on_prem_db", vendor="Oracle",
    dialect="oracle", deployment="hybrid", regions=["on_prem"] + _GLOBAL,
    dbt_adapter="oracle", idmc_type="Oracle", powercenter_dbtype="Oracle",
    fields=[F("host", "Host"), F("port", "Port", required=False, default="1521"),
            F("user", "Username"), F("password", "Password", secret=True),
            F("database", "Service name or SID"),
            F("schema", "Schema", required=False)],
    type_map={"varchar2": "string", "nvarchar2": "string", "clob": "string",
              "number": "decimal", "binary_double": "double", "raw": "binary"}))

registry.register(ConnectorSpec(
    key="sqlserver", name="Microsoft SQL Server", category="on_prem_db",
    vendor="Microsoft", dialect="tsql", deployment="hybrid",
    regions=["on_prem"] + _GLOBAL, dbt_adapter="sqlserver",
    idmc_type="SQL Server", powercenter_dbtype="Microsoft SQL Server",
    fields=_std(port="1433"),
    type_map={"datetime2": "timestamp", "uniqueidentifier": "string",
              "money": "decimal", "bit": "boolean"}))

registry.register(ConnectorSpec(
    key="postgres", name="PostgreSQL", category="on_prem_db", vendor="PostgreSQL",
    dialect="postgres", deployment="hybrid", regions=["on_prem"] + _GLOBAL,
    dbt_adapter="postgres", idmc_type="PostgreSQL",
    powercenter_dbtype="ODBC", fields=_std(port="5432")))

registry.register(ConnectorSpec(
    key="teradata", name="Teradata", category="on_prem_db", vendor="Teradata",
    dialect="teradata", deployment="hybrid", regions=["on_prem"] + _GLOBAL,
    dbt_adapter="teradata", idmc_type="Teradata", powercenter_dbtype="Teradata",
    fields=_std(port="1025")))

registry.register(ConnectorSpec(
    key="db2", name="IBM Db2", category="on_prem_db", vendor="IBM",
    dialect="", deployment="hybrid", regions=["on_prem"] + _GLOBAL,
    dbt_adapter="", idmc_type="DB2", powercenter_dbtype="DB2",
    notes="No sqlglot dialect — expressions transpile via ANSI.",
    fields=_std(port="50000")))

# ---------------------------------------------------------------------------
# SAP
# ---------------------------------------------------------------------------

registry.register(ConnectorSpec(
    key="sap_ecc", name="SAP ECC", category="sap", vendor="SAP",
    deployment="on_prem",
    notes="Upload metadata exports (tables, extractors, ABAP source). "
          "ABAP is analyzed and documented — never silently converted.",
))
registry.register(ConnectorSpec(
    key="sap_bw4", name="SAP BW/4HANA (metadata import)", category="sap",
    vendor="SAP", deployment="hybrid",
    notes="Upload BW metadata XML (InfoObjects, ADSOs, transformations, "
          "DTPs, process chains, queries). Routines are analyzed; "
          "process chains modernize through the orchestration engine.",
))
registry.register(ConnectorSpec(
    key="sap_datasphere", name="SAP Datasphere", category="sap",
    vendor="SAP", deployment="cloud",
    notes="Upload CSN-style JSON exports of views and tables.",
))
registry.register(ConnectorSpec(
    key="sap_di", name="SAP Data Intelligence", category="sap",
    vendor="SAP", deployment="cloud",
    notes="Pipeline metadata imports; graphs analyzed via the "
          "orchestration engine where exported as JSON.",
))

registry.register(ConnectorSpec(
    key="sap_hana", name="SAP HANA", category="sap", vendor="SAP",
    dialect="", deployment="hybrid", regions=["on_prem"] + _GLOBAL,
    dbt_adapter="", idmc_type="SAP HANA Database", powercenter_dbtype="ODBC",
    notes="Expressions transpile via ANSI. Calculation views surface as tables.",
    # NOT _std(): HANA's requirements genuinely differ from the standard SQL
    # form. 443 is the HANA Cloud SQL endpoint and the case this connector is
    # used for; on-prem instances override it with 3<instance>15 (30015 for
    # instance 00). The port also drives the driver's TLS inference, so the
    # default must match what livecheck._hana_connect assumes.
    # The tenant database is OPTIONAL: a HANA Cloud endpoint already resolves
    # to its tenant, and only a multi-tenant on-prem system reached through
    # the system database needs it named. Marking it required made the form
    # demand a value the driver never requires.
    fields=[F("host", "Host"),
            F("port", "Port", required=False, default="443"),
            F("user", "Username"),
            F("password", "Password", secret=True),
            F("database", "Tenant database", required=False),
            F("schema", "Schema", required=False)],
    type_map={"nvarchar": "string", "seconddate": "timestamp",
              "decfloat": "double", "tinyint": "integer"}))

registry.register(ConnectorSpec(
    key="sap_s4", name="SAP S/4HANA & ECC (ODP/OData extraction)", category="sap",
    vendor="SAP", dialect="", deployment="hybrid", regions=["on_prem"] + _GLOBAL,
    dbt_adapter="", idmc_type="SAP Table Connector", powercenter_dbtype="SAP R/3",
    notes="Table/extractor-level metadata (MARA, VBAK, 2LIS_* ...). CDS views "
          "and ODP extractors map to IR sources; delta tokens map to the "
          "$$LAST_RUN_TS watermark pattern.",
    fields=[F("ashost", "Application server host"), F("sysnr", "System number",
              default="00"), F("client", "Client", default="100"),
            F("user", "Username"), F("password", "Password", secret=True),
            F("language", "Language", required=False, default="EN")],
    type_map={"dats": "date", "tims": "string", "curr": "decimal",
              "quan": "decimal", "numc": "string", "cuky": "string",
              "unit": "string", "lang": "string"}))

registry.register(ConnectorSpec(
    key="sap_bw", name="SAP BW/4HANA (InfoProviders)", category="sap", vendor="SAP",
    dialect="", deployment="hybrid", regions=["on_prem"] + _GLOBAL,
    dbt_adapter="", idmc_type="SAP BW Reader", powercenter_dbtype="SAP BW",
    notes="ADSOs/CompositeProviders surface as IR sources.",
    fields=[F("ashost", "Application server host"), F("sysnr", "System number",
              default="00"), F("client", "Client", default="100"),
            F("user", "Username"), F("password", "Password", secret=True)]))

# ---------------------------------------------------------------------------
# Business applications
# ---------------------------------------------------------------------------

# --- legacy ETL platforms (Command 5) — import/export based, no live
# connection: projects are uploaded and modernized through the CIR engine.
registry.register(ConnectorSpec(
    key="ssis", name="Microsoft SSIS", category="etl", vendor="Microsoft",
    deployment="on_prem",
    notes="Upload .dtsx packages with .dtproj/.conmgr/.params. Control flow, "
          "data flows, variables, expressions and event handlers are "
          "analyzed and modernized to any supported target.",
))
registry.register(ConnectorSpec(
    key="datastage", name="IBM DataStage", category="etl", vendor="IBM",
    deployment="on_prem",
    notes="Upload DSX exports (parallel and server jobs, sequences, shared "
          "containers). Stages, links, transformer derivations and job "
          "parameters are analyzed and modernized.",
))
registry.register(ConnectorSpec(
    key="talend", name="Talend Data Integration", category="etl",
    vendor="Talend", deployment="hybrid",
    notes="Upload Talend job exports (.item/.properties). Components, "
          "contexts, tMap logic, joblets and subjob flow are analyzed "
          "and modernized.",
))
registry.register(ConnectorSpec(
    key="abinitio", name="Ab Initio", category="etl", vendor="Ab Initio",
    deployment="on_prem",
    notes="Upload text graph exports (air object save) with DML/XFR/pset/"
          "plan files. Binary .mp graphs are inventoried and flagged for "
          "text export — never guessed at.",
))

# --- orchestration platforms (Command 6) — import/export based; every
# workflow flows through the Canonical Orchestration Representation.
registry.register(ConnectorSpec(
    key="airflow", name="Apache Airflow", category="orchestration",
    vendor="Apache", deployment="hybrid",
    notes="Upload DAG .py files. DAGs, operators, sensors, task groups, "
          "schedules, retries and SLAs are analyzed and modernized.",
))
registry.register(ConnectorSpec(
    key="adf", name="Azure Data Factory", category="orchestration",
    vendor="Microsoft", deployment="cloud",
    notes="Upload pipeline/trigger JSON. Activities, dependencies, "
          "policies, IfCondition/ForEach and triggers are analyzed.",
))
registry.register(ConnectorSpec(
    key="fabric", name="Microsoft Fabric Data Factory",
    category="orchestration", vendor="Microsoft", deployment="cloud",
    notes="Upload Fabric pipeline JSON — same canonical path as ADF "
          "with Fabric-native generation.",
))
registry.register(ConnectorSpec(
    key="stepfunctions", name="AWS Step Functions",
    category="orchestration", vendor="AWS", deployment="cloud",
    notes="Upload ASL state machines. States, Retry/Catch, Choice, "
          "Parallel and Map are analyzed and modernized.",
))
registry.register(ConnectorSpec(
    key="controlm", name="Control-M", category="orchestration",
    vendor="BMC", deployment="on_prem",
    notes="Upload Automation API JSON exports. Jobs, calendars, events, "
          "notifications and rerun limits are analyzed. Legacy DEFTABLE "
          "XML must be exported to JSON first.",
))
registry.register(ConnectorSpec(
    key="autosys", name="AutoSys", category="orchestration",
    vendor="Broadcom", deployment="on_prem",
    notes="Upload JIL exports. Jobs, boxes, conditions, calendars, "
          "machines and alarms are analyzed and modernized.",
))
registry.register(ConnectorSpec(
    key="pc_workflow", name="PowerCenter Workflows",
    category="orchestration", vendor="Informatica", deployment="on_prem",
    notes="PowerCenter repository XML workflows (sessions, worklets, "
          "decisions, commands, emails) through the same canonical path.",
))
registry.register(ConnectorSpec(
    key="idmc_taskflow", name="IDMC Taskflows",
    category="orchestration", vendor="Informatica", deployment="cloud",
    notes="Upload taskflow JSON exports. Steps, decisions, parallel "
          "paths and schedules are analyzed and modernized.",
))

# --- event & streaming platforms (Command 8) — import/export based;
# everything flows through the Canonical Event Representation.
registry.register(ConnectorSpec(
    key="kafka_events", name="Apache Kafka", category="events",
    vendor="Apache", deployment="hybrid",
    notes="Upload topic exports, client .properties, Connect configs, Schema Registry subjects and ksqlDB SQL.",
))
registry.register(ConnectorSpec(
    key="confluent", name="Confluent Platform", category="events",
    vendor="Confluent", deployment="hybrid",
    notes="Kafka artifacts plus ksqlDB streams/tables and Schema Registry compatibility modes.",
))
registry.register(ConnectorSpec(
    key="pulsar", name="Apache Pulsar", category="events",
    vendor="Apache", deployment="hybrid",
    notes="Upload pulsar-admin JSON exports (tenants, namespaces, topics, subscriptions, dedup).",
))
registry.register(ConnectorSpec(
    key="rabbitmq", name="RabbitMQ", category="events",
    vendor="Broadcom", deployment="hybrid",
    notes="Upload definitions.json (queues, exchanges, bindings, DLX, quorum queues, permissions).",
))
registry.register(ConnectorSpec(
    key="ibmmq", name="IBM MQ", category="events",
    vendor="IBM", deployment="hybrid",
    notes="Upload MQSC scripts — queues, aliases, backout (retry+DLQ) semantics preserved.",
))
registry.register(ConnectorSpec(
    key="activemq", name="ActiveMQ", category="events",
    vendor="Apache", deployment="hybrid",
    notes="Upload activemq.xml — destinations and dead-letter strategies.",
))
registry.register(ConnectorSpec(
    key="kinesis_events", name="AWS Kinesis", category="events",
    vendor="AWS", deployment="hybrid",
    notes="Upload describe-stream JSON exports.",
))
registry.register(ConnectorSpec(
    key="eventhubs", name="Azure Event Hubs", category="events",
    vendor="Microsoft", deployment="hybrid",
    notes="Upload ARM templates — partitions, retention, consumer groups.",
))
registry.register(ConnectorSpec(
    key="pubsub", name="Google Pub/Sub", category="events",
    vendor="Google", deployment="hybrid",
    notes="Upload topic/subscription exports — ordering, DLQ and retry policies preserved.",
))
registry.register(ConnectorSpec(
    key="mqtt", name="MQTT Broker (Mosquitto/HiveMQ/EMQX)", category="events",
    vendor="Eclipse", deployment="hybrid",
    notes="Upload broker configs and topic definitions; QoS maps to delivery guarantees.",
))
registry.register(ConnectorSpec(
    key="hivemq", name="HiveMQ", category="events",
    vendor="HiveMQ", deployment="hybrid",
    notes="MQTT family — broker configs and topic definitions.",
))
registry.register(ConnectorSpec(
    key="emqx", name="EMQX", category="events",
    vendor="EMQ", deployment="hybrid",
    notes="MQTT family — broker configs and topic definitions.",
))
registry.register(ConnectorSpec(
    key="mosquitto", name="Mosquitto", category="events",
    vendor="Eclipse", deployment="hybrid",
    notes="MQTT family — mosquitto.conf and topic definitions.",
))
registry.register(ConnectorSpec(
    key="awsiot", name="AWS IoT Core", category="events",
    vendor="AWS", deployment="hybrid",
    notes="Upload topic-rule JSON — rule SQL becomes routing + transformation logic.",
))
registry.register(ConnectorSpec(
    key="azureiot", name="Azure IoT Hub", category="events",
    vendor="Microsoft", deployment="hybrid",
    notes="Upload IoT Hub ARM routes and device twins.",
))
registry.register(ConnectorSpec(
    key="debezium", name="Debezium CDC", category="events",
    vendor="Red Hat", deployment="hybrid",
    notes="Upload connector JSON — tables, snapshot mode and SMT chains preserved.",
))
registry.register(ConnectorSpec(
    key="goldengate", name="Oracle GoldenGate", category="events",
    vendor="Oracle", deployment="hybrid",
    notes="Upload extract/replicat .prm files — TABLE/MAP statements become CDC feeds.",
))
registry.register(ConnectorSpec(
    key="nifi", name="Apache NiFi", category="events",
    vendor="Apache", deployment="hybrid",
    notes="Upload flow templates — processors and connections become the event flow graph.",
))
registry.register(ConnectorSpec(
    key="streamsets", name="StreamSets", category="events",
    vendor="Software AG", deployment="hybrid",
    notes="Upload pipeline JSON — stages and lanes become the event flow graph.",
))

registry.register(ConnectorSpec(
    key="salesforce", name="Salesforce", category="app", vendor="Salesforce",
    dialect="", deployment="cloud", regions=_GLOBAL,
    dbt_adapter="", idmc_type="Salesforce", powercenter_dbtype="ODBC",
    notes="sObjects surface as IR sources (metadata level).",
    fields=[F("instance_url", "Instance URL"), F("client_id", "Connected app client id"),
            F("client_secret", "Client secret", secret=True),
            F("refresh_token", "Refresh token", secret=True)]))

registry.register(ConnectorSpec(
    key="servicenow", name="ServiceNow", category="app", vendor="ServiceNow",
    dialect="", deployment="cloud", regions=_GLOBAL,
    dbt_adapter="", idmc_type="ServiceNow", powercenter_dbtype="ODBC",
    fields=[F("instance", "Instance (xxx.service-now.com)"), F("user", "Username"),
            F("password", "Password", secret=True)]))
