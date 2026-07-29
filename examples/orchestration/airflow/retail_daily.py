"""Daily retail load — extract, transform in parallel, publish."""
from datetime import timedelta
from airflow import DAG
from airflow.operators.bash import BashOperator
from airflow.operators.email import EmailOperator
from airflow.operators.empty import EmptyOperator
from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator
from airflow.sensors.filesystem import FileSensor
from airflow.utils.task_group import TaskGroup

default_args = {
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "email": ["dataops@metafordata.com"],
    "email_on_failure": True,
    "sla": timedelta(hours=2),
}

with DAG(
    dag_id="retail_daily_load",
    schedule_interval="0 3 * * *",
    default_args=default_args,
    description="Retail warehouse daily refresh",
    catchup=False,
) as dag:
    wait_for_extract = FileSensor(
        task_id="wait_for_extract",
        filepath="/data/in/orders.csv",
        poke_interval=300,
        timeout=3600,
        fs_conn_id="fs_default",
    )
    start = EmptyOperator(task_id="start")

    with TaskGroup(group_id="transform") as transform:
        load_orders = SQLExecuteQueryOperator(
            task_id="load_orders",
            conn_id="snowflake_dw",
            sql="INSERT INTO dw.orders SELECT * FROM stg.orders",
            pool="dw_pool",
        )
        load_customers = SQLExecuteQueryOperator(
            task_id="load_customers",
            conn_id="snowflake_dw",
            sql="INSERT INTO dw.customers SELECT * FROM stg.customers",
            retries=4,
        )

    publish = BashOperator(
        task_id="publish_marts",
        bash_command="dbt run --select marts",
        execution_timeout=timedelta(minutes=45),
    )
    notify_fail = EmailOperator(
        task_id="notify_failure",
        to="oncall@metafordata.com",
        subject="retail_daily_load failed",
        trigger_rule="one_failed",
    )

    wait_for_extract >> start >> [load_orders, load_customers] >> publish
    publish >> notify_fail
