-- Whether this market was clustered already on @run_date (BUILD.md 2.2). Parameters: @run_date DATE, @market
-- STRING ('za', 'ng', 'ke' or 'pan'). run_cluster skips the fit when n is above zero, so a rerun neither refits
-- nor moves a centroid a second time. clusters is partitioned on cluster_date, so one partition is read.
SELECT COUNT(*) AS n
FROM `ogilvy-trends-v2.intelligence_42_core.clusters` AS k
WHERE k.cluster_date = @run_date AND k.market = @market
