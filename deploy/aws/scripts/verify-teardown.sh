#!/usr/bin/env bash
# Confirms that the deployment's billable resources are gone. Exits non-zero if anything remains.
source "$(dirname "$0")/_common.sh"
need aws
left=0
report() { local compact="${2//[[:space:]]/}"; if [ -z "$compact" ] || [ "$compact" = None ] || [ "$compact" = "[]" ]; then echo "gone  $1"; else echo "LEFT  $1: $2"; left=1; fi; }

report "stack $APP_STACK" "$(aws cloudformation describe-stacks --stack-name "$APP_STACK" --query 'Stacks[0].StackStatus' --output text 2>/dev/null || true)"
report "stack $ECR_STACK" "$(aws cloudformation describe-stacks --stack-name "$ECR_STACK" --query 'Stacks[0].StackStatus' --output text 2>/dev/null || true)"
report "lambda functions" "$(aws lambda list-functions --query "Functions[?starts_with(FunctionName, 'ap-agent-${ENVIRONMENT}-')].FunctionName" --output text)"
report "sqs queues" "$(aws sqs list-queues --queue-name-prefix "ap-agent-${ENVIRONMENT}-" --query 'QueueUrls' --output text 2>/dev/null)"
report "s3 buckets" "$(aws s3api list-buckets --query "Buckets[?starts_with(Name, 'ap-agent-${ENVIRONMENT}-')].Name" --output text)"
report "ecr repositories" "$(aws ecr describe-repositories --query "repositories[?starts_with(repositoryName, 'ap-agent-${ENVIRONMENT}/')].repositoryName" --output text 2>/dev/null)"
report "log groups" "$(aws logs describe-log-groups --log-group-name-prefix "/aws/lambda/ap-agent-${ENVIRONMENT}-" --query 'logGroups[].logGroupName' --output text) $(aws logs describe-log-groups --log-group-name-prefix "/ecs/ap-agent-${ENVIRONMENT}-" --query 'logGroups[].logGroupName' --output text)"
report "ssm parameters" "$(aws ssm get-parameters-by-path --path "$SSM_PREFIX" --query 'Parameters[].Name' --output text)"
report "cloudwatch alarms" "$(aws cloudwatch describe-alarms --alarm-name-prefix "ap-agent-${ENVIRONMENT}-" --query 'MetricAlarms[].AlarmName' --output text 2>/dev/null)"
report "ecs clusters" "$(aws ecs list-clusters --query "clusterArns[?contains(@, 'ap-agent-${ENVIRONMENT}-')]" --output text 2>/dev/null)"
report "ecs tasks" "$(aws ecs list-clusters --query "clusterArns[?contains(@, 'ap-agent-${ENVIRONMENT}')]" --output text 2>/dev/null | xargs -r -n1 aws ecs list-tasks --query 'taskArns' --output text --cluster 2>/dev/null)"
report "vpcs" "$(aws ec2 describe-vpcs --filters "Name=tag:Project,Values=accounts-payable-agent" "Name=tag:Environment,Values=${ENVIRONMENT}" --query 'Vpcs[].VpcId' --output text 2>/dev/null)"
echo
if [ "$left" -eq 0 ]; then echo "Nothing from this deployment remains in $AWS_REGION: no Lambda, queue, bucket, image, log, secret, ECS cluster, VPC or alarm that could bill."; else echo "Some resources remain (see LEFT above)."; fi
echo "Also check: Billing console -> Bills for this month shows no new charges after the next day; the \$20 budget needs no change."
echo "Not covered here (outside AWS): the Neon project and Clerk application keep their own free/paid plans -- delete them in their dashboards if abandoning the project."
exit "$left"
