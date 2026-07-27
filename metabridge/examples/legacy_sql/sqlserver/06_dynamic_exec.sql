CREATE PROCEDURE dbo.usp_rebuild_stats
  @table_name NVARCHAR(128)
AS
BEGIN
  DECLARE @sql NVARCHAR(MAX);
  SET @sql = N'UPDATE STATISTICS ' + @table_name;
  EXEC sp_executesql @sql;
END
GO
