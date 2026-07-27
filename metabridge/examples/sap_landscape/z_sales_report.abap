REPORT z_sales_report.
* Legacy custom sales extract
DATA: lt_orders TYPE STANDARD TABLE OF vbak.

SELECT vbeln, kunnr, netwr, waerk
  FROM vbak
  INTO TABLE @lt_orders
  WHERE erdat >= '20240101'.

LOOP AT lt_orders INTO DATA(ls_order).
  IF ls_order-netwr > 100000.
    ls_order-netwr = ls_order-netwr * '0.98'.
  ENDIF.
  MODIFY lt_orders FROM ls_order.
ENDLOOP.

CALL FUNCTION 'BAPI_SALESORDER_GETLIST'
  EXPORTING customer_number = '0000001000'.

CALL FUNCTION 'Z_RFC_PUSH' DESTINATION 'BWCLNT100'.
