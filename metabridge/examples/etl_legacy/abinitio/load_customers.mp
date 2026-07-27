// text export of graph load_customers (air object save)
component "Read Customers" {
  type "Input File"
  parameter file "/data/in/customers.dat"
  parameter dml "customers.dml"
}
component "Partition by Region" {
  type "Partition by Key"
  parameter key "{region}"
}
component "Clean Customers" {
  type "Reformat"
  parameter xfr "clean_customers.xfr"
}
component "Filter Active" {
  type "Filter by Expression"
  parameter select_expr "in.balance > 0"
}
component "Rollup by Region" {
  type "Rollup"
  parameter key "{region}"
  parameter xfr "rollup_by_region.xfr"
}
component "Running Total" {
  type "Scan"
  parameter key "{region}"
}
component "Write Summary" {
  type "Output Table"
  parameter table "dw.region_balance_summary"
}
flow "Read Customers.out" -> "Partition by Region.in"
flow "Partition by Region.out" -> "Clean Customers.in"
flow "Clean Customers.out" -> "Filter Active.in"
flow "Filter Active.out" -> "Rollup by Region.in"
flow "Rollup by Region.out" -> "Running Total.in"
flow "Running Total.out" -> "Write Summary.in"
