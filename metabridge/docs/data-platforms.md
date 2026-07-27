# SQL, ETL, Streaming & SAP

MetaBridge modernizes four distinct classes of estate — hand-written SQL and warehouse code, enterprise ETL tools, streaming and messaging platforms, and SAP — through the same evidence-driven, deterministic pipeline. Every source technology parses into a shared canonical model; every target generates from that model. There are no pairwise converters, and no engine decision is made by an LLM.

This page covers the four data-platform domains and how each is represented, converted, and validated. For the transformation engines that operate on these models, see [Engines](engines.md); for how conversions are reviewed and audited, see [Governance & Security](governance-security.md).

## How the domains fit together

Each domain normalizes into one of MetaBridge's shared canonical models, then flows through analysis, generation, and validation:

| Domain | Canonical model | Source parser | Target generators |
| --- | --- | --- | --- |
| SQL & warehouse | IR pipeline / CIR | dialect SQL parsers | any warehouse or transformation target |
| Enterprise ETL | IR pipeline / CIR | PowerCenter, IDMC, SSIS, DataStage, Talend, Ab Initio | any warehouse or transformation target |
| Streaming & messaging | Canonical Event Representation (CER) | 19 event-platform adapters | 16 streaming/broker targets |
| Orchestration | Canonical Orchestration Representation (COR) | Airflow, ADF, Control-M, AutoSys, Step Functions, dbt Cloud, cron, and more | Airflow, ADF, Step Functions, Control-M, AutoSys, dbt Cloud, and more |
| SAP | SAP Landscape → IR pipeline | CDS, HANA, BW, ABAP, Datasphere | any warehouse or transformation target |

The payoff of the shared-model architecture is that logic like dependency graphs, execution ordering, business-rule extraction, and lineage is implemented **once** and works identically across every source. Because every SQL and ETL parser produces the same IR, the metadata, business-rule, expression, and source/target extraction methods live in one place and behave the same for dbt, PowerCenter, IDMC, and every warehouse dialect.

Two principles hold across all four domains:

- **Deterministic by default.** Parsers compute structure and semantics from the source artifact. The optional MetaBridge AI review is advisory-only, runs only after the deterministic parse, and never modifies the canonical model or generated artifacts. It is off unless a provider is configured.
- **Declared loss, never silent loss.** Anything a parser cannot map, or a target cannot express, is written into the output as a typed issue (with severity, suggestion, and the original payload preserved) — never dropped.

---

## Legacy SQL & warehouse modernization

MetaBridge converts hand-written SQL — dbt models, warehouse scripts, and stored procedures — into the canonical IR by decomposing statements into a transformation graph, not by string-rewriting.

### Dialects

SQL parsing is dialect-aware. The following first-class dialects are recognized, each with its own parser adapter:

| Dialect | Parser | sqlglot dialect |
| --- | --- | --- |
| Snowflake | `SnowflakeParser` | `snowflake` |
| Databricks | `DatabricksParser` | `databricks` |
| Google BigQuery | `BigQueryParser` | `bigquery` |
| Amazon Redshift | `RedshiftParser` | `redshift` |
| Azure Synapse / Fabric | `SynapseFabricParser` | `tsql` |
| SQL Server (T-SQL) | `TSQLParser` | `tsql` |
| Oracle | `OracleParser` | `oracle` |
| PostgreSQL | `PostgreSQLParser` | `postgres` |
| Teradata | `TeradataParser` | `teradata` |
| Generic ANSI SQL | `AnsiSQLParser` | — |

### SELECT decomposition

A `SELECT` is decomposed into a native IR transformation graph. The mapping is direct and deterministic:

| SQL construct | IR transformation |
| --- | --- |
| `FROM` / `JOIN` | `SOURCE` + `SOURCE_QUALIFIER`, chained two-input `JOINER`s |
| `WHERE` | `FILTER` |
| `GROUP BY` / aggregates | `AGGREGATOR` (aggregate expressions on its ports) |
| `HAVING` | post-aggregation `FILTER` |
| `SELECT` (derived columns) | `EXPRESSION`, or pass-through when nothing is computed |
| `ORDER BY` | `SORTER` |
| `UNION ALL` | `UNION` |
| `DISTINCT` | `SORTER` with `distinct=true` |
| CTEs | decomposed recursively; references link to the terminal node of the chain |

Expressions on IR ports are stored as canonical ANSI SQL. Target generators translate them late — to the Informatica expression language or back to any SQL dialect — so the same graph serves every target.

Constructs that have no faithful native decomposition (window functions, `QUALIFY`, `LATERAL`, `PIVOT`, correlated subqueries, `LIMIT`/`TOP`, `EXCEPT`/`INTERSECT`, `CROSS JOIN`) trigger the **SQL-override fallback**: the mapping becomes `SOURCE(s) → SOURCE_QUALIFIER(sql_override) → TARGET`, which is functionally correct in PowerCenter/IDMC. The mapping runs as-is; a `SQL_OVERRIDE_FALLBACK` warning is raised so the report shows exactly where a native rebuild would be needed for pushdown or fine-grained lineage.

### Semantic function registry

Dialect-specific functions map through a declared semantic-function registry (`semantic_function_registry.yaml`), the single source of truth for function knowledge — no platform mapping table is hardcoded anywhere. It currently covers **62 semantic functions** across categories including null-handling, conditional, date/time, string, numeric, conversion, JSON, array, regex, window, hash, and aggregate.

Each function declares a per-platform mapping across `oracle`, `snowflake`, `databricks`, `bigquery`, `redshift`, `synapse`, `sqlserver`, `postgres`, `teradata`, `ansi`, and `informatica`. A mapping can be a direct template, unsupported (with a required workaround), or absent. The registry also produces a per-platform coverage matrix used in assessment reports.

### Data-type mapping

Types map through a canonical type engine (`semantic_data_types.yaml`) that parses a native type into one of 18 canonical types (`STRING`, `DECIMAL`, `TIMESTAMP_TZ`, `VARIANT`, `ARRAY`, `STRUCT`, `GEOGRAPHY`, …) and renders it to the target. It detects and declares seven classes of data-fidelity warning:

- `precision_loss` — source precision exceeds the target maximum
- `scale_loss` — source scale exceeds the target maximum
- `length_overflow` — source length exceeds the target maximum
- `timezone_change` — time-zone information dropped or re-interpreted
- `unsupported_type` — no native equivalent; a declared fallback is used
- `implicit_conversion` — a conventional stand-in type is used (e.g. `BIT` for `BOOLEAN`, `VARIANT` for `JSON`)
- `unknown_type` — the native type could not be recognized

### Legacy dialect normalization

Before decomposition, legacy dialect constructs are rewritten on the AST (never on text). Findings carry a code, severity, and an automation classification of `FULLY_AUTOMATED`, `PARTIAL`, or `MANUAL_REVIEW_REQUIRED`.

| Dialect | Normalizations |
| --- | --- |
| **Oracle** | `DECODE` → `CASE`; `SYSDATE`/`SYSTIMESTAMP` → `CURRENT_TIMESTAMP`; `FROM DUAL` removed; `WHERE ROWNUM <= n` → `LIMIT n`; simple `CONNECT BY` / `START WITH` / `PRIOR` → `WITH RECURSIVE` (complex shapes declared `MANUAL`); optimizer hints stripped with a note; `MATERIALIZED VIEW` → incremental-strategy note; `SEQUENCE.NEXTVAL` → per-target key-generation strategy |
| **Teradata** | `ZEROIFNULL` → `COALESCE(x,0)`; `NULLIFZERO` → `NULLIF(x,0)`; `OREPLACE` → `REPLACE`; `OTRANSLATE` → `TRANSLATE`; `INDEX(s,sub)` → position; `VOLATILE`/`ON COMMIT` stripped and object marked temporary; `SET`/`MULTISET` dedup semantics declared; `PRIMARY INDEX` → target layout recommendation; `LOCKING … FOR ACCESS` unwrapped with an isolation warning; `COLLECT STATISTICS` mapped to a strategy |
| **T-SQL** | `SELECT INTO #t` → CTAS marked temporary; `NOLOCK` / `IDENTITY` / dynamic `EXEC` findings (`ISNULL`/`GETDATE`/`DATEADD`/`TOP`/`APPLY`/`TRY_CAST` are already canonical) |

Dynamic SQL (`EXEC` / `EXECUTE IMMEDIATE`) is declared `MANUAL` — it cannot be converted deterministically. A separate legacy-SQL function registry adds **45** dialect-specific function rows, each with a risk level and per-target implementations for Snowflake, Databricks, BigQuery, Redshift, Synapse/Fabric, and Postgres.

### Stored procedures

Procedures are never blindly translated. Each procedural block is decomposed into parameters (with direction and type), variables, cursor loops, temp tables, and statements — with every statement classified:

`DATA_TRANSFORMATION`, `DATA_LOAD`, `CONTROL_FLOW`, `AUDIT_LOGGING`, `ERROR_HANDLING`, `DYNAMIC_SQL`, `DDL`, `SECURITY`, `EXTERNAL_CALL`, `TRANSACTION`, `MANUAL_REVIEW`.

Set-based `DATA_TRANSFORMATION` statements that parse cleanly are handed back to the SQL pipeline as candidate mappings (with provenance kept), so a procedure whose body is really an ELT chain converts like one. The control-flow skeleton, dynamic SQL, and cursors stay declared for review. Each procedure is classified as `set_based`, `procedural`, or `dynamic`, and carries per-target recommendations for dbt, Databricks, and Snowflake.

Legacy scripts are split into typed units before AST parsing, because they are not plain SQL: BTEQ mixes dot-commands with SQL, T-SQL uses `GO` batch separators, and PL/SQL objects end with a lone slash. Runtime commands (BTEQ `LOGON`/`EXPORT`/`IMPORT`/`RUN`, `GO`, `BT`/`ET`) are mapped to modernization strategies rather than treated as transformation logic, with every unit tracking `source_file` and `source_line` for the audit trail.

---

## Enterprise ETL

MetaBridge ingests the major enterprise ETL platforms through dedicated parsers. Every adapter emits the same canonical IR, so the same generators, validators, and reports serve all of them.

| Platform | Format | Parser |
| --- | --- | --- |
| Informatica PowerCenter | POWERMART repository XML | `PowerCenterParser` |
| Informatica IDMC | mapping-bundle JSON | `IDMCParser` |
| Microsoft SSIS | `.dtsx` packages (+ `.conmgr`, `.params`, `.dtproj`) | `SSISParser` |
| IBM DataStage | DSX exports (parallel + server jobs) | `DataStageParser` |
| Talend Data Integration | `.item` process XML (+ `.properties`) | `TalendParser` |
| Ab Initio | text graph exports (`.mp`, `.dml`, `.xfr`, `.pset`, `.plan`) | `AbInitioParser` |

### PowerCenter

PowerCenter repository XML is parsed through a namespace-agnostic, memory-bounded (`iterparse`) ingestion engine covering the full grammar: folders, mapplets, reusable transformations, mapping variables, and workflows/worklets/sessions/tasks/configs. Native transformation types — Source Qualifier, Expression, Filter, Joiner, Aggregator, Sorter, Union, Lookup, Router, Rank, Sequence, and Update Strategy — map directly to the IR. Mapplet boundary nodes become pass-through expressions when inlined. Port expressions and conditions are translated from the Informatica expression language to canonical SQL at parse time; expressions that cannot be translated faithfully become `MANUAL` issues with the original preserved.

The Informatica expression language is handled by a **bidirectional** transpiler: ANSI SQL ⇄ Informatica expression language, so expressions flow both from dbt models into Informatica and from PowerCenter/IDMC back into any SQL dialect.

### IDMC

IDMC mapping bundles (manifest plus `mappings/*.json`) parse into the same IR. The full transformation set is supported — Source, Expression, Filter, Joiner, Aggregator, Sorter, Union, Lookup, Router, Rank, Sequence, Update Strategy, Target — with expressions arriving in the Informatica expression language and converted to canonical SQL on the way in. IDMC taskflows are recognized and can be bridged to orchestration.

### SSIS

SSIS projects are walked as XML documents (no regex-over-the-file parsing; only expressions use the translation layer). Each Data Flow Task becomes a Mapping, and the component-to-transformation mapping is explicit:

| SSIS component | IR transformation |
| --- | --- |
| OLE DB / ADO.NET / Excel / Flat File / ODBC source | `SOURCE` (+ Source Qualifier) |
| OLE DB / ADO.NET / Flat File / ODBC destination | `TARGET` |
| Derived Column | `EXPRESSION` (per-column SSIS expressions translated) |
| Lookup | `LOOKUP` |
| Conditional Split | `ROUTER` |
| Merge Join | `JOINER` |
| Merge / Union All | `UNION` |
| Aggregate | `AGGREGATOR` |
| Sort | `SORTER` |
| SCD wizard | `UPDATE_STRATEGY` + SCD Type 2 review flag |
| Multicast / Row Count | pass-through `EXPRESSION` |
| Script Component | `EXPRESSION` placeholder + `MANUAL` (.NET code preserved) |

Control flow becomes a workflow DAG. Execute SQL Tasks with set-based DML (`INSERT … SELECT`, `MERGE`, CTAS) are decomposed into mappings through the SQL pipeline; anything else is preserved as a command node with the statement intact. Variables, parameters, and connection managers are captured (connections **never** carry credentials — only server/database facts). Event handlers, loop containers, and expression-evaluated variables raise declared warnings so they can be recreated in the orchestrator.

### DataStage, Talend, Ab Initio

Each parser reads its native export structurally, maps stages/components to IR transformations, extracts port expressions through a platform-specific expression translator, and lifts job sequences/subjob triggers into workflow DAGs.

- **DataStage** parses DSX `BEGIN DSJOB` / `BEGIN DSRECORD` blocks with a real tokenizer (BEGIN/END nesting, quoted values, multiline markers). Sequential/dataset/connector stages become sources or targets by pin direction; Transformer stages become `EXPRESSION` (plus `FILTER` when constraints exist) via the BASIC translator; Lookup, Join, Aggregator, Sort, Funnel, and RemDup map to `LOOKUP`/`JOINER`/`AGGREGATOR`/`SORTER`/`UNION`/`RANK`.
- **Talend** parses `.item` process XML into one Mapping per data-flow subjob plus a workflow DAG over subjob triggers. `tMap`, `tJoin`, `tFilterRow`, `tAggregateRow`, `tSortRow`, `tUnite`, and `tUniqRow` map to their IR equivalents; Java expressions go through the Talend translator, and untranslatable code (`tJava*`, `tNormalize`/`tDenormalize`) becomes a `MANUAL` issue with the original preserved.
- **Ab Initio** is honest about a hard boundary: the binary `.mp` format has no public specification, so a binary upload returns an `ERROR` with instructions to provide the text export (`air object save` / GDE "save as text") — it is never guessed at. Text graph exports parse structurally: Reformat → `EXPRESSION`, Filter → `FILTER`, Join → `JOINER`, Rollup → `AGGREGATOR`, Sort → `SORTER`, Dedup → `RANK`, and Merge/Concatenate/Gather → `UNION`. Physical-parallelism components (Partition, Round-robin, Replicate, Broadcast) become no-op pass-throughs with an `INFO` note, since parallelism is implicit in SQL. `.dml` record formats supply typed source schemas, `.xfr` functions supply port expressions, and `.plan` files become workflow DAGs.

Across all ETL parsers, unrecognized stages, components, and types are always preserved as `MANUAL` issues carrying the original payload — never dropped silently.

---

## Streaming estates — the Canonical Event Representation

Every messaging, streaming, IoT, and CDC platform normalizes into the **Canonical Event Representation (CER)**, and every streaming target generates from CER. There are no pairwise broker converters.

```
Event Platform → Metadata Parser → CER → Semantic Analysis →
Target Generator → Validation + AI Review + Governance
```

### Semantics that ride as first-class fields

The value of CER is that the semantics you cannot afford to lose are modeled explicitly, not buried in free-form config: delivery guarantees (`at_most_once` / `at_least_once` / `exactly_once`), ordering (`none` / `per_key` / `per_partition` / `global` / `fifo`), retry behavior and dead-letter routing, retention (time/size/compaction), compression, replication, schema evolution mode (`BACKWARD`/`FORWARD`/`FULL`/`NONE`), event-time and watermark semantics, and security policies (ACL/permission/SAS/TLS/IAM). CER models channels (topics/queues/streams), producers, consumers and consumer groups, schemas, stream transformations, routing rules, CDC sources, and IoT sources, and can render the end-to-end flow: producer → channel → transformation → channel → consumer, including routing and DLQ edges.

### Source platforms (19)

`kafka`, `confluent`, `pulsar`, `rabbitmq`, `ibmmq`, `activemq`, `kinesis`, `eventhubs`, `servicebus`, `pubsub`, `mqtt`, `awsiot`, `iothub`, `nifi`, `streamsets`, `debezium`, `goldengate`, `slt`, and `ibm_cdc`.

Each adapter reads that platform's real artifacts — Kafka topic exports, `.properties` configs, consumer-group JSON, Kafka Connect connector JSON (SMT chains preserved), Schema Registry subjects and `.avsc`/`.proto` files, ksqlDB `.sql` (TUMBLING/HOPPING/SESSION windows); Pulsar admin JSON; RabbitMQ `definitions.json`; IBM MQ / ActiveMQ MQSC and XML; Kinesis describe-stream JSON; Event Hubs / Service Bus ARM templates; Pub/Sub topic+subscription JSON; MQTT broker configs; AWS IoT / IoT Hub rules and device twins; NiFi templates; StreamSets pipelines; Debezium connectors; GoldenGate `.prm` files; and Flink/Spark SQL windowing files. XML is parsed via ElementTree, JSON via `json`, SQL via sqlglot with structural window extraction. Binary uploads and unknown shapes are declared, never guessed.

### Streaming targets (16)

Broker/service targets: `kafka`, `confluent`, `pulsar`, `rabbitmq`, `eventhubs`, `servicebus`, `kinesis`, `pubsub`, `mqtt`, `iothub`. Streaming-engine targets: `flink` (Flink SQL with `WATERMARK` and TUMBLE/HOP/SESSION windows), `spark_streaming` (PySpark Structured Streaming with `withWatermark`, `window()`, per-state-store checkpoints), `databricks_streaming` (Delta Live Tables, `APPLY CHANGES INTO` for CDC), `snowflake_streaming` (Snowpipe Streaming + dynamic tables), and `dbt_streaming`. A `scaffold` target is also available.

### Validation and declared downgrades

CER validation runs deterministic, target-aware checks: ordering vs. target capability, delivery-guarantee downgrades, schema compatibility, consumer lag (declared as runtime-only), partition integrity, duplicate events, message loss (acks=0/1, replication=1), dead-letter resolution, window correctness (event-time windows need watermarks), and CDC consistency. Where a target cannot express a source guarantee, the downgrade is written into the generated artifact as an explicit note — for example, a source that requires exactly-once targeting a broker that only offers at-least-once produces a `DELIVERY_DOWNGRADE` warning and a "deduplicate downstream with an idempotency key" instruction. Each target's capabilities (exactly-once, FIFO, native DLQ) are declared in a capability table that drives these downgrades.

---

## Orchestration — the Canonical Orchestration Representation

Every scheduler and orchestrator normalizes into the **Canonical Orchestration Representation (COR)**; every orchestration target generates from COR. Nothing converts pairwise.

```
Legacy Orchestration → Parser → COR → Semantic Analysis →
Target Generator → Validation + AI Review
```

### The model

COR represents Workflows containing typed Tasks (`mapping`, `pipeline`, `command`, `sql`, `notebook`, `copy`, `sensor`, `wait`, `choice`, `parallel`, `loop`, `email`, `approval`, `subworkflow`, `dummy`, `unknown`), Dependencies with kinds (`success` / `failure` / `always` / `conditional` / `event`), Schedules (`cron` / `interval` / `event` / `manual` / `calendar`), retry and timeout policies, notifications, variables, secret references (names only), connections, resource pools, and SLAs. It computes topological execution waves (Kahn levels), full execution order, and dependency cycles. The original platform payload always rides along in `Task.original` — declared loss, never silent loss.

### Source platforms

| Platform | Artifact and parsing approach |
| --- | --- |
| **Airflow** | DAG `.py` files parsed with Python's `ast` module (DAGs, operators, sensors, TaskGroups, `>>`/`<<` chains, `chain()`, pools, retries, SLAs, schedules; XCom surfaced as variables) |
| **ADF / Synapse Pipelines / Fabric** | pipeline JSON (activities, `dependsOn` conditions, policy retry/timeout, `IfCondition`/`ForEach`/`Until` flattened, Wait, Validation sensors) + trigger JSON + linked services as connections |
| **AWS Step Functions** | Amazon States Language (`Task`/`Choice`/`Parallel`/`Map`/`Wait`/`Pass`/`Succeed`/`Fail`, Retry, Catch) |
| **AWS Glue Workflow** | `get-workflow --include-graph` JSON |
| **Control-M** | Automation API JSON (folders, jobs, When/calendars, events wait/add, notifications, rerun limits); legacy XML DEFTABLE exports are declared unsupported with conversion guidance |
| **AutoSys** | JIL text (`insert_job` blocks parsed as key:value blocks, with condition expressions parsed structurally) |
| **IDMC Taskflow** | taskflow JSON (steps, decision/parallel paths, schedules) |
| **dbt Cloud** | job JSON (`execute_steps`, schedule cron) |
| **cron** | crontab text (entries become scheduled command workflows) |
| **PowerCenter / SSIS / DataStage / Talend / Ab Initio / SAP** | bridged from the existing project parsers' workflow DAGs — one upgrade path, no re-parsing |

### Targets

`airflow`, `adf`, `fabric`, `stepfunctions`, `controlm`, `autosys`, `dbtcloud`, `powercenter`, `idmc_taskflow`, and a neutral `scaffold`. The Airflow generator, for example, emits a runnable DAG with schedule, retries, SLAs, task groups for parallel waves, and trigger rules for failure paths. A `docs` output renders execution documentation with a mermaid graph.

### Validation

COR runs eight deterministic checks regardless of source platform: dependency integrity, circular dependencies, schedule consistency (invalid cron, duplicate/overlapping triggers), retry configuration, missing connections, broken triggers, orphan workflows, and dead-end tasks. Cron expressions — including aliases like `@daily` and `@hourly` — are normalized and validated across both 5-field and 6-field forms.

---

## SAP modernization

SAP is treated as a first-class estate with its own semantic model — the **SAP Landscape** — which is then lowered into the one canonical IR so the same target generators that serve dbt, PowerCenter, and ETL sources also serve SAP.

```
SAP export → SAP parsers → SAPLandscape → normalize → Pipeline (CIR)
           → every existing target generator
```

### What is parsed

| Import | Content |
| --- | --- |
| `.ddls` / `.cds` / `.asddls` | ABAP CDS view DDL (annotations, parameters, associations, currency/unit semantics, access control) |
| `.hdbcalculationview` / `.calculationview` | HANA calculation views (projection / aggregation / join / union / rank nodes, calculated attributes, filters, measures); analytic and attribute views read through the same reader |
| `.abap` / `.prog` | ABAP source — **analyzed, never auto-converted** |
| `.xml` | BW metadata: InfoObjects, ADSO/DSO/Cube, CompositeProvider, Transformations (rules + start/end/expert routines), DTP, InfoPackage, ProcessChain (RSPC), BEx Query, OpenHub, Authorization |
| `.json` | ODP metadata and Datasphere CSN-style definitions |
| binary transports | declared unsupported (R3trans files carry no readable metadata), with guidance to export as XML/JSON |

XML is parsed via ElementTree, JSON via `json`, CDS via a structured splitter with sqlglot verification of the projected SQL, and ABAP via a line-classifying analyzer. Regex appears only inside single statements — never over the whole file.

### The lowering

Each SAP object type lowers into IR with its semantics preserved:

- **BW transformations** → Mappings, where rules become typed ports and ABAP routines become declared `MANUAL` items (with the ABAP analysis attached and a `NULL` placeholder — never silently converted).
- **ADSO / DSO / Cube** → sources/targets, with keys driving a `MERGE` load strategy.
- **CompositeProvider** → a Mapping with `UNION`/`JOINER` over its parts.
- **CDS views** → Mappings via the SQL decomposer (associations declared; unparseable DDL preserved as `MANUAL` with the raw source).
- **Calculation views** → a Mapping graph (projection → `EXPRESSION`, aggregation → `AGGREGATOR`, join → `JOINER`, union → `UNION`, rank → `RANK`).
- **BEx queries** → mart Mappings with a `VIEW` strategy and variables lifted to runtime parameters.
- **Process chains** → workflow DAGs, bridged through COR to render Airflow/ADF/Fabric/IDMC/PowerCenter orchestration.

### SAP-specific semantics

MetaBridge handles the SAP semantics that generic converters miss, and never ignores them:

- **Currency.** Amount fields bound to a currency (`CURR`/`CUKY`) raise a `SAP_CURRENCY_SEMANTICS` (or `SAP_CURRENCY_KYF`) warning. Currency handling is generated into the validation plan; conversion via TCURR/TCURX must be modeled explicitly in the target — MetaBridge declares this rather than silently guessing an exchange rate.
- **Units.** Quantity fields bound to a unit (`QUAN`/`UNIT`) raise a `SAP_UNIT_SEMANTICS` warning; unit conversion (T006) must be modeled explicitly.
- **Hierarchies.** A hierarchy InfoObject lowers into a flattened parent-child Mapping built as a recursive CTE over the H-table, with the recursion depth (`hier_level`) preserved.
- **Master data + texts** → a dimension Mapping (P-table joined to the T-table for language texts).
- **Authorizations** → captured in `metadata["authorizations"]` with a `SAP_AUTHORIZATION` recommendation to implement each analysis authorization as target row-level security (Snowflake row access policy / BigQuery row-level security). CDS `@AccessControl.authorizationCheck` is carried onto the mapping.

### ABAP is analyzed, not converted

ABAP is deliberately never auto-converted. Each unit is analyzed deterministically into an `ABAPAnalysis`: statement count, extractable Open SQL, native `EXEC SQL`, loops, internal tables, function/BAPI/RFC calls, customer exits, and BADI usage — with a verdict of `CONVERTIBLE`, `PARTIAL`, or `MANUAL`. Extracted Open SQL and business rules feed the report; the procedural body is preserved for human review.

### SAP validation pack

The SAP generator emits a validation pack rendered in the **target dialect** (via sqlglot, never generic-only): schema comparison, master-data, hierarchy, currency, unit, business-rule, aggregation, and reconciliation SQL, alongside business documentation and business/technical lineage.

---

## Cross-domain guarantees

Regardless of domain, the same product principles apply:

- **The core logic is deterministic.** Parsers and generators compute from evidence in the source artifact. MetaBridge AI review is advisory-only, runs after the deterministic parse, sends only structured summaries (never payloads or credentials), and never modifies the canonical model or generated artifacts.
- **Nothing is dropped.** Unmapped constructs and target-capability gaps are always written back as typed issues with a severity, a suggestion, and the original payload.
- **Credentials never travel.** Connection managers, linked services, and secret references carry names and non-sensitive facts only.

For the governance, confidence-scoring, approval, and audit-trail model that wraps these conversions, see [Governance & Security](governance-security.md). For the full engine catalog, see [Engines](engines.md).
