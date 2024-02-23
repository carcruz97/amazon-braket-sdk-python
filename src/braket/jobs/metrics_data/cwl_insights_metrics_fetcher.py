# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"). You
# may not use this file except in compliance with the License. A copy of
# the License is located at
#
#     http://aws.amazon.com/apache2.0/
#
# or in the "license" file accompanying this file. This file is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF
# ANY KIND, either express or implied. See the License for the specific
# language governing permissions and limitations under the License.

from __future__ import annotations

import time
from logging import Logger, getLogger
from typing import Any, Optional, Union

from braket.aws.aws_session import AwsSession
from braket.jobs.metrics_data.definitions import MetricStatistic, MetricType
from braket.jobs.metrics_data.exceptions import MetricsRetrievalError
from braket.jobs.metrics_data.log_metrics_parser import LogMetricsParser


class CwlInsightsMetricsFetcher:
    LOG_GROUP_NAME = "/aws/braket/jobs"
    QUERY_DEFAULT_JOB_DURATION = 3 * 60 * 60

    def __init__(
        self,
        aws_session: AwsSession,
        poll_timeout_seconds: float = 10,
        poll_interval_seconds: float = 1,
        logger: Logger = getLogger(__name__),
    ):
        """Initializes a `CwlInsightsMetricsFetcher`.

        Args:
            aws_session (AwsSession): AwsSession to connect to AWS with.
            poll_timeout_seconds (float): The polling timeout for retrieving the metrics,
                in seconds. Default: 10 seconds.
            poll_interval_seconds (float): The interval of time, in seconds, between polling
                for results. Default: 1 second.
            logger (Logger): Logger object with which to write logs, such as quantum task statuses
                while waiting for a quantum task to be in a terminal state. Default is
                `getLogger(__name__)`
        """
        self._poll_timeout_seconds = poll_timeout_seconds
        self._poll_interval_seconds = poll_interval_seconds
        self._logger = logger
        self._logs_client = aws_session.logs_client

    @staticmethod
    def _get_element_from_log_line(
        element_name: str, log_line: list[dict[str, Any]]
    ) -> Optional[str]:
        """Finds and returns an element of a log line from CloudWatch Insights results.

        Args:
            element_name (str): The element to find.
            log_line (list[dict[str, Any]]): An iterator for RegEx matches on a log line.

        Returns:
            Optional[str]: The value of the element with the element name, or None if no such
            element is found.
        """
        return next(
            (element["value"] for element in log_line if element["field"] == element_name), None
        )

    def _get_metrics_results_sync(self, query_id: str) -> list[Any]:
        """Waits for the CloudWatch Insights query to complete and then returns all the results.

        Args:
            query_id (str): CloudWatch Insights query ID.

        Raises:
            MetricsRetrievalError: Raised if the query is Failed or Cancelled.

        Returns:
            list[Any]: The results from CloudWatch insights 'GetQueryResults' operation.
        """
        timeout_time = time.time() + self._poll_timeout_seconds
        while time.time() < timeout_time:
            response = self._logs_client.get_query_results(queryId=query_id)
            query_status = response["status"]
            if query_status in ["Failed", "Cancelled"]:
                raise MetricsRetrievalError(f"Query {query_id} failed with status {query_status}.")
            elif query_status == "Complete":
                return response["results"]
            else:
                time.sleep(self._poll_interval_seconds)
        self._logger.warning(f"Timed out waiting for query {query_id}.")
        return []

    def _parse_log_line(self, result_entry: list[dict[str, Any]], parser: LogMetricsParser) -> None:
        """Parses the single entry from CloudWatch Insights results and adds any metrics it finds
        to 'all_metrics' along with the timestamp for the entry.

        Args:
            result_entry (list[dict[str, Any]]): A structured result from calling CloudWatch
                Insights to get logs that contain metrics. A single entry contains the message
                (the actual line logged to output), the timestamp (generated by CloudWatch Logs),
                and other metadata that we (currently) do not use.
            parser (LogMetricsParser) : The CWL metrics parser.
        """
        message = self._get_element_from_log_line("@message", result_entry)
        if message:
            timestamp = self._get_element_from_log_line("@timestamp", result_entry)
            parser.parse_log_message(timestamp, message)

    def _parse_log_query_results(
        self, results: list[Any], metric_type: MetricType, statistic: MetricStatistic
    ) -> dict[str, list[Union[str, float, int]]]:
        """Parses CloudWatch Insights results and returns all found metrics.

        Args:
            results (list[Any]): A structured result from calling CloudWatch Insights to get
                logs that contain metrics.
            metric_type (MetricType): The type of metrics to get.
            statistic (MetricStatistic): The statistic to determine which metric value to use
                when there is a conflict.

        Returns:
            dict[str, list[Union[str, float, int]]]: The metrics data.
        """
        parser = LogMetricsParser()
        for result in results:
            self._parse_log_line(result, parser)
        return parser.get_parsed_metrics(metric_type, statistic)

    def get_metrics_for_job(
        self,
        job_name: str,
        metric_type: MetricType = MetricType.TIMESTAMP,
        statistic: MetricStatistic = MetricStatistic.MAX,
        job_start_time: int | None = None,
        job_end_time: int | None = None,
        stream_prefix: str | None = None,
    ) -> dict[str, list[Union[str, float, int]]]:
        """Synchronously retrieves all the algorithm metrics logged by a given Hybrid Job.

        Args:
            job_name (str): The name of the Hybrid Job. The name must be exact to ensure only the
                relevant metrics are retrieved.
            metric_type (MetricType): The type of metrics to get. Default is MetricType.TIMESTAMP.
            statistic (MetricStatistic): The statistic to determine which metric value to use
                when there is a conflict. Default is MetricStatistic.MAX.
            job_start_time (int | None): The time when the hybrid job started.
                Default: 3 hours before job_end_time.
            job_end_time (int | None): If the hybrid job is complete, this should be the time at
                which the hybrid job finished. Default: current time.
            stream_prefix (str | None): If a logs prefix is provided, it will be used instead
                of the job name.

        Returns:
            dict[str, list[Union[str, float, int]]]: The metrics data, where the keys
            are the column names and the values are a list containing the values in each row.

        Example:
            timestamp energy
            0         0.1
            1         0.2
            would be represented as:
            { "timestamp" : [0, 1], "energy" : [0.1, 0.2] }
            The values may be integers, floats, strings or None.
        """
        query_end_time = job_end_time or int(time.time())
        query_start_time = job_start_time or query_end_time - self.QUERY_DEFAULT_JOB_DURATION

        stream_prefix = stream_prefix or job_name

        query = (
            f"fields @timestamp, @message "
            f"| filter @logStream like /^{stream_prefix}\\// "
            f"| filter @message like /Metrics - /"
        )

        response = self._logs_client.start_query(
            logGroupName=self.LOG_GROUP_NAME,
            startTime=query_start_time,
            endTime=query_end_time,
            queryString=query,
            limit=10000,
        )

        query_id = response["queryId"]

        results = self._get_metrics_results_sync(query_id)

        return self._parse_log_query_results(results, metric_type, statistic)
