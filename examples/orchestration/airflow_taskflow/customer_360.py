"""Modern Airflow (TaskFlow API) — the shape most 2023+ DAGs are written in.

MetaBridge imports this to the same COR as a classic ``with DAG(...)`` module:
``@dag`` becomes the workflow, ``@task``/``@task.<flavour>`` become tasks, and
the data flow between the calls becomes the dependency graph.
"""
from __future__ import annotations

from datetime import timedelta

import pendulum
from airflow.decorators import dag, task, task_group
from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator

DEFAULT_ARGS = {
    "owner": "data-platform",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "email": ["dataops@metafordata.com"],
    "email_on_failure": True,
}


@dag(
    dag_id="customer_360_refresh",
    description="Daily customer 360 refresh",
    schedule="0 2 * * *",
    start_date=pendulum.datetime(2024, 1, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,
    default_args=DEFAULT_ARGS,
    tags=["etl", "customer360"],
)
def customer_360_refresh():
    @task.sensor(poke_interval=300, timeout=3600, mode="reschedule")
    def wait_for_landing():
        """Landing zone must have today's drop before anything runs."""
        return True

    @task(execution_timeout=timedelta(minutes=30))
    def extract_orders():
        return {"table": "stg.orders"}

    @task(retries=4, execution_timeout=timedelta(minutes=30))
    def extract_customers():
        return {"table": "stg.customers"}

    @task_group
    def conform(orders, customers):
        @task(execution_timeout=timedelta(minutes=20))
        def dedupe(src):
            return src

        @task(execution_timeout=timedelta(minutes=20),
              sla=timedelta(hours=3))
        def join_360(a, b):
            return {"table": "stg.customer_360"}

        return join_360(dedupe(orders), dedupe.override(
            task_id="dedupe_customers")(customers))

    @task.short_circuit
    def row_count_gate(payload):
        """Stop the load rather than publish an empty mart."""
        return bool(payload)

    publish = SQLExecuteQueryOperator(
        task_id="publish_customer_360",
        conn_id="snowflake_dw",
        sql="INSERT INTO dw.customer_360 SELECT * FROM stg.customer_360",
        pool="dw_pool",
        execution_timeout=timedelta(minutes=45),
    )

    @task.bash
    def archive_landing():
        return "aws s3 mv s3://landing/customer/ s3://archive/customer/ --recursive"

    landed = wait_for_landing()
    orders = extract_orders()
    customers = extract_customers()
    landed >> [orders, customers]
    conformed = conform(orders, customers)
    row_count_gate(conformed) >> publish >> archive_landing()


customer_360_refresh()
