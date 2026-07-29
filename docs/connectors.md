# Connectors

Connectors are MetaBridge's integration catalog. Each connector is a declarative spec that describes one endpoint technology — how to connect to it, how its native types map onto the canonical IR, which SQL dialect it speaks, and where it may reside — and that single spec drives everything downstream: dbt profiles, IDMC connection JSON, PowerCenter connection stubs, transpiler dialect selection, and residency metadata for governance.

MetaBridge ships **50 built-in connectors** across cloud warehouses, on-prem databases, SAP, legacy ETL, orchestration, streaming, and business apps. Third parties add their own without touching MetaBridge — connectors register through a Python entry-point group.

## Why connectors are declarative

A connector is data, not code. Because a spec is a plain declaration of fields, types, dialect, and deployment metadata, the same catalog entry feeds several distinct outputs at once:

- **dbt** — `profiles.yml` generation for the target side of a dbt project
- **IDMC** — connection JSON in the v3 object shape
- **PowerCenter** — relational-connection stubs for `pmrep createconnection`
- **SQL transpiler** — dialect selection for expression rendering
- **Governance** — residency metadata for the policy engine

Add a technology once, and it plugs into every one of these paths. There is no per-target integration code to maintain.

## The catalog

Browse the catalog from the CLI:

```bash
metabridge connectors                 # list every connector, grouped by category
metabridge connectors snowflake       # full spec for one connector, as JSON
metabridge connectors --json          # the whole catalog as JSON
```

The list groups connectors by category and flags which output paths each supports:

```
MetaBridge AI connector marketplace — 50 connectors

Cloud data platforms
  snowflake    Snowflake                        cloud [dbt,idmc,pc]
  bigquery     Google BigQuery                  cloud [dbt,idmc,pc]
  ...

Third-party connectors: register via the 'metabridge.connectors' entry-point group.
```

The `[dbt,idmc,pc]` tags mean the connector can act as a dbt target, an IDMC connection, and a PowerCenter connection respectively.

### Categories

Every connector declares a `category`, which maps to a user-facing platform type:

| Category | Platform type | What it covers |
|---|---|---|
| `cloud_dw` | Cloud warehouse | Snowflake, BigQuery, Redshift, Azure Synapse / Fabric |
| `lakehouse` | Lakehouse | Databricks |
| `on_prem_db` | RDBMS | Oracle, SQL Server, PostgreSQL, Teradata, IBM Db2 |
| `sap` | ERP | SAP HANA, S/4HANA & ECC, BW/4HANA, Datasphere, Data Intelligence |
| `app` | Business application | Salesforce, ServiceNow |
| `etl` | Legacy ETL | SSIS, DataStage, Talend, Ab Initio |
| `orchestration` | Orchestration | Airflow, ADF, Fabric, Step Functions, Control-M, AutoSys, PowerCenter/IDMC workflows |
| `events` | Event streaming | Kafka, Confluent, Pulsar, RabbitMQ, IBM MQ, Kinesis, Event Hubs, Pub/Sub, MQTT family, Debezium, GoldenGate, NiFi, StreamSets, and more |

### Cloud data platforms

| Key | Name | Vendor | Dialect | dbt adapter | IDMC type |
|---|---|---|---|---|---|
| `snowflake` | Snowflake | Snowflake | `snowflake` | `snowflake` | Snowflake Data Cloud |
| `bigquery` | Google BigQuery | Google | `bigquery` | `bigquery` | Google BigQuery V2 |
| `databricks` | Databricks Lakehouse | Databricks | `databricks` | `databricks` | Databricks Delta |
| `redshift` | Amazon Redshift | AWS | `redshift` | `redshift` | Amazon Redshift V2 |
| `synapse` | Azure Synapse / Fabric Warehouse | Microsoft | `tsql` | `synapse` | Azure Synapse SQL |

These carry all three output paths (dbt, IDMC, PowerCenter), so they can serve as **bidirectional** endpoints — source or target.

### On-premises databases

| Key | Name | Vendor | Dialect | dbt adapter | Deployment |
|---|---|---|---|---|---|
| `oracle` | Oracle Database | Oracle | `oracle` | `oracle` | hybrid |
| `sqlserver` | Microsoft SQL Server | Microsoft | `tsql` | `sqlserver` | hybrid |
| `postgres` | PostgreSQL | PostgreSQL | `postgres` | `postgres` | hybrid |
| `teradata` | Teradata | Teradata | `teradata` | `teradata` | hybrid |
| `db2` | IBM Db2 | IBM | *(none)* | *(none)* | hybrid |

IBM Db2 declares no sqlglot dialect, so its expressions transpile via ANSI rather than a native dialect. This is the general fallback for any connector whose `dialect` is empty.

### SAP

SAP connectors split into two styles. Live, table-level extraction connectors (SAP HANA, S/4HANA & ECC, BW/4HANA InfoProviders) declare connection fields and IDMC / PowerCenter types. Metadata-import connectors (ECC exports, BW/4HANA metadata XML, Datasphere, Data Intelligence) are upload-based — you supply exported metadata rather than a live connection.

| Key | Name | Style | Notes |
|---|---|---|---|
| `sap_hana` | SAP HANA | Live | Calculation views surface as tables; expressions transpile via ANSI |
| `sap_s4` | SAP S/4HANA & ECC (ODP/OData) | Live | CDS views and ODP extractors map to IR sources; delta tokens map to the `$$LAST_RUN_TS` watermark pattern |
| `sap_bw` | SAP BW/4HANA (InfoProviders) | Live | ADSOs / CompositeProviders surface as IR sources |
| `sap_ecc` | SAP ECC | Import | Upload metadata exports; ABAP is analyzed and documented, never silently converted |
| `sap_bw4` | SAP BW/4HANA (metadata import) | Import | BW metadata XML; process chains modernize through the orchestration engine |
| `sap_datasphere` | SAP Datasphere | Import | CSN-style JSON exports of views and tables |
| `sap_di` | SAP Data Intelligence | Import | Pipeline metadata graphs analyzed via the orchestration engine |

### Legacy ETL, orchestration, and streaming

These categories are import/export based — there is no live connection. You upload the platform's own export artifacts, and the project is modernized through the matching canonical model.

- **Legacy ETL** (`etl`) — SSIS `.dtsx`, DataStage DSX, Talend job exports, Ab Initio text graphs. Modernized through the CIR engine.
- **Orchestration** (`orchestration`) — Airflow DAGs, Azure Data Factory / Fabric pipeline JSON, AWS Step Functions ASL, Control-M, AutoSys JIL, PowerCenter and IDMC workflows. Everything flows through the Canonical Orchestration Representation (COR).
- **Streaming & events** (`events`) — Kafka / Confluent, Pulsar, RabbitMQ, IBM MQ, ActiveMQ, Kinesis, Event Hubs, Pub/Sub, the MQTT family (Mosquitto / HiveMQ / EMQX), AWS / Azure IoT, Debezium CDC, GoldenGate, NiFi, StreamSets. Everything flows through the Canonical Event Representation (CER).

### Business applications

| Key | Name | Vendor | Notes |
|---|---|---|---|
| `salesforce` | Salesforce | Salesforce | sObjects surface as IR sources (metadata level) |
| `servicenow` | ServiceNow | ServiceNow | Table metadata surfaces as IR sources |

## What a connector spec declares

Every connector is a `ConnectorSpec`. The core fields:

| Field | Meaning |
|---|---|
| `key` | Stable identifier (`snowflake`, `sap_hana`) — how you reference the connector everywhere |
| `name` | Display name |
| `category` | `cloud_dw`, `lakehouse`, `on_prem_db`, `sap`, `app`, `etl`, `orchestration`, `events` |
| `vendor` | Vendor name |
| `dialect` | sqlglot dialect for SQL rendering; empty means "not SQL-addressable" (transpiles via ANSI) |
| `deployment` | `cloud`, `on_prem`, or `hybrid` |
| `regions` | Regions the connector may be deployed in — the input to residency policy |
| `fields` | The connection fields shown to the user (see below) |
| `type_map` | Native-type → canonical-IR-type overrides on top of the built-in ANSI coverage |
| `dbt_adapter` | dbt profile `type` when the connector can be a dbt target |
| `idmc_type` | IDMC connection type name |
| `powercenter_dbtype` | PowerCenter `DATABASETYPE` value |

### Connection fields

Each entry in `fields` is a `ConnectionField` with a `name`, a human `label`, a `required` flag, a `secret` flag, and an optional `default`. Fields marked `secret: true` (passwords, tokens, keyfiles, client secrets) are treated specially by every emitter — their **values are never written into generated artifacts**.

For example, Snowflake declares an account identifier, user, password (secret), optional role, warehouse, database, and optional schema. SAP S/4HANA declares application-server host, system number (default `00`), client (default `100`), user, password (secret), and optional language (default `EN`).

### Native → canonical type mapping

`canonical_type()` already covers ANSI types. A connector's `type_map` only declares the vendor-specific overrides that ANSI does not capture. For example:

- **Oracle** maps `varchar2`/`nvarchar2`/`clob` → `string`, `number` → `decimal`, `binary_double` → `double`, `raw` → `binary`.
- **BigQuery** maps `int64` → `bigint`, `float64` → `double`, `bool` → `boolean`, `bytes` → `binary`, `numeric`/`bignumeric` → `decimal`.
- **SAP S/4HANA** maps ABAP data types — `dats` → `date`, `curr`/`quan` → `decimal`, `numc`/`cuky`/`unit`/`lang` → `string`.

These mappings feed the IR so downstream generators emit correct target-native types.

### Capabilities

A spec derives its user-facing capabilities from what it declares, rather than exposing raw dialect names. Every connector supports `pipeline_scaffold`, `metadata_analysis`, and `lineage`. Additional capabilities appear based on the spec: `dbt` (has a dbt adapter), `idmc`, `powercenter`, `sql_modernization` (has a dialect), and `bidirectional` (has all three output types). Category-specific capabilities add things like `business_lineage` and `ai_review` for SAP, `streaming_lineage` for events, and `workflow_visualization` / `execution_graph` for orchestration.

## One spec, three connection artifacts

The connection emitter turns **one set of connection parameters** into artifacts for each target platform. `metabridge scaffold` produces all three when it generates a project, writing dbt, IDMC, and PowerCenter connection files into the output directory.

Take a set of parameters for a connector and split them once: non-secret values stay in the artifact, secret values become environment-variable references. The env-var name is derived deterministically as `MB_<KEY>_<FIELD>` (e.g. `MB_SNOWFLAKE_PASSWORD`).

### dbt profile

`dbt_profile()` renders a `profiles.yml` entry using the connector's `dbt_adapter` as the profile `type`. Secret fields become `env_var()` references:

```yaml
my_project:
  target: prod
  outputs:
    prod:
      type: snowflake
      account: xy12345
      user: loader
      warehouse: TRANSFORM
      database: ANALYTICS
      password: \"{{ env_var('MB_SNOWFLAKE_PASSWORD') }}\"
```

A connector without a `dbt_adapter` cannot be a dbt target — the emitter raises an error telling you to use it as a source system instead.

### IDMC connection JSON

`idmc_connection()` emits the v3 object shape, using the connector's `idmc_type` as the `connectionType` and carrying the deployment model through. Secrets become `$...$` placeholders flagged `secret: true`:

```json
{
  \"@type\": \"connection\",
  \"name\": \"conn_snowflake\",
  \"connectionType\": \"Snowflake Data Cloud\",
  \"deployment\": \"cloud\",
  \"properties\": [
    { \"name\": \"account\", \"value\": \"xy12345\" },
    { \"name\": \"user\", \"value\": \"loader\" },
    { \"name\": \"password\", \"value\": \"$MB_SNOWFLAKE_PASSWORD$\", \"secret\": true }
  ]
}
```

### PowerCenter connection stub

`powercenter_connection()` emits a `pmrep createconnection` command — the scripted way PowerCenter admins actually create connections — using the connector's `powercenter_dbtype`. The password is passed as a `$...$` placeholder:

```sh
pmrep createconnection -s relational -t \"ODBC\" -n \"conn_snowflake\" \\
  -u \"loader\" -p \"$MB_SNOWFLAKE_PASSWORD$\" -c \"xy12345\" -d \"ANALYTICS\"
```

## Secrets are references, never values

This is a hard rule across every emitter and the connection store:

- Fields marked `secret` are split out from the rest of the parameters before any artifact is written.
- Generated artifacts contain **only** references — `{{ env_var('MB_...') }}` for dbt and `$MB_...$` for IDMC and PowerCenter — never the secret value.
- The env-var naming convention `MB_<CONNECTOR_KEY>_<FIELD_NAME>` is deterministic, so the same reference is stable across regenerations and across all three artifact types.
- When connections are saved in the platform store, secret values are separated from safe parameters, and the store file is written with `0600` permissions. Public listings expose only a `has_secrets` flag, never the values.

This is exactly the shape compliance reviewers expect to see: credentials live in the runtime environment or a secrets manager, and never in checked-in configuration. See [Governance & Security](governance-security.md) for how residency and masking policy build on the connector's declared regions.

## Publishing third-party connectors

The catalog is extensible without modifying MetaBridge. Any installed Python package can register connectors through the `metabridge.connectors` entry-point group. Each entry point returns either a single `ConnectorSpec` or a list of them.

Declare the entry point in your package metadata:

```toml
# pyproject.toml
[project.entry-points.\"metabridge.connectors\"]
acme = \"acme_connectors:get_specs\"
```

```python
# acme_connectors.py
from metabridge.connectors.base import ConnectorSpec, ConnectionField as F

def get_specs():
    return [
        ConnectorSpec(
            key=\"acme_dw\",
            name=\"Acme Warehouse\",
            category=\"cloud_dw\",
            vendor=\"Acme\",
            dialect=\"snowflake\",          # reuse an existing sqlglot dialect, or \"\" for ANSI
            deployment=\"cloud\",
            regions=[\"us\", \"eu\", \"apac\"],
            dbt_adapter=\"acme\",
            idmc_type=\"Acme Warehouse\",
            powercenter_dbtype=\"ODBC\",
            fields=[
                F(\"host\", \"Host\"),
                F(\"user\", \"Username\"),
                F(\"token\", \"API token\", secret=True),
                F(\"database\", \"Database\"),
            ],
            type_map={\"acme_int\": \"bigint\"},
        )
    ]
```

Once your package is installed in the same environment as MetaBridge, its connectors appear alongside the built-ins in `metabridge connectors` and are usable everywhere a connector key is accepted. Entry-point loading is fault-isolated — a connector that fails to load is skipped so a bad plugin cannot break the CLI.

For richer extensions (validators, templates, accelerators) distributed through the signed marketplace, see the Plugin SDK and marketplace documentation.
