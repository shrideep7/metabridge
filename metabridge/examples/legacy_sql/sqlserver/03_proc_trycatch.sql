CREATE PROCEDURE dbo.usp_load_fct_orders
  @batch_id INT
AS
BEGIN
  SET NOCOUNT ON;
  BEGIN TRY
    BEGIN TRAN;
    DELETE FROM dbo.fct_orders WHERE batch_id = @batch_id;
    INSERT INTO dbo.fct_orders (order_id, amount, batch_id)
    SELECT order_id, amount, @batch_id FROM dbo.orders WHERE status = 'OK';
    COMMIT TRAN;
  END TRY
  BEGIN CATCH
    ROLLBACK TRAN;
    THROW;
  END CATCH
END
GO
