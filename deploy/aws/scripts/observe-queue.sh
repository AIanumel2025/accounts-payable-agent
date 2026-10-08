#!/usr/bin/env bash
# Read-only view of the job pipeline: queue depth, dead-letter queue, running and recently stopped OCR tasks (with exit
# codes and stop reasons) and the recent OCR log lines. Prints no secret. Use it while a job is in flight and after a failure.
source "$(dirname "$0")/_common.sh"
need aws; need jq
QUEUE="$(stack_output "$APP_STACK" QueueUrl)"; DLQ="$(stack_output "$APP_STACK" DeadLetterQueueUrl)"
CLUSTER="$(stack_output "$APP_STACK" OcrClusterArn)"; LOGGROUP="$(stack_output "$APP_STACK" OcrLogGroupName)"
echo "== job queue";  aws sqs get-queue-attributes --queue-url "$QUEUE" --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible ApproximateNumberOfMessagesDelayed --output json | jq -c .Attributes
echo "== dead-letter queue"; aws sqs get-queue-attributes --queue-url "$DLQ" --attribute-names ApproximateNumberOfMessages --output json | jq -c .Attributes
echo "== running OCR tasks (expect at most 1)"; aws ecs list-tasks --cluster "$CLUSTER" --desired-status RUNNING --query 'taskArns' --output json
echo "== most recent stopped OCR tasks (exit codes: 0 handled, 4 config, 5 OCR unavailable, 6 bad message, 7 transient, 8 deadline)"
STOPPED="$(aws ecs list-tasks --cluster "$CLUSTER" --desired-status STOPPED --query 'taskArns[:5]' --output json)"
if [ "$(echo "$STOPPED" | jq 'length')" -gt 0 ]; then
  # shellcheck disable=SC2046
  aws ecs describe-tasks --cluster "$CLUSTER" --tasks $(echo "$STOPPED" | jq -r '.[]') \
    --query 'tasks[].{task:taskArn,stopped:stoppedAt,stopCode:stopCode,reason:stoppedReason,exit:containers[0].exitCode}' --output json
fi
echo "== dispatcher invocations in the last 15 minutes (errors)"
aws logs filter-log-events --log-group-name "/aws/lambda/ap-agent-${ENVIRONMENT}-dispatcher" --start-time "$(( ($(date +%s) - 900) * 1000 ))" \
  --filter-pattern '?"Dispatch failed" ?"failed"' --query 'events[].message' --output text 2>/dev/null | head -20 || true
echo "== OCR task log (last 15 minutes)"
aws logs tail "$LOGGROUP" --since 15m 2>/dev/null | tail -40 || true
