CREATE OR REPLACE PACKAGE pkg_pricing IS
  FUNCTION net_price(p_gross IN NUMBER) RETURN NUMBER;
  PROCEDURE apply_discounts;
END pkg_pricing;
/

CREATE OR REPLACE PACKAGE BODY pkg_pricing IS
  FUNCTION net_price(p_gross IN NUMBER) RETURN NUMBER IS
  BEGIN
    RETURN ROUND(p_gross * 0.92, 2);
  END net_price;

  PROCEDURE apply_discounts IS
  BEGIN
    UPDATE order_lines SET net_amount = net_price(gross_amount)
    WHERE net_amount IS NULL;
    COMMIT;
  END apply_discounts;
END pkg_pricing;
/
