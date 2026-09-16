#!/usr/bin/env bash
#
# One-time bootstrap of the krew-hrms DEV deployment target.
#
# The Jenkins pipeline only UPDATES an existing service, so these resources have
# to exist before the first deploy:
#
#   1. ECR repository            service-krew-hrms
#   2. CloudWatch log group      /ecs/service-krew-hrms-task
#   3. ALB target group          ecs-gk-dev-service-krew-hrms   (port 8000, /health/)
#   4. ECS task definition       service-krew-hrms-task         (app + redis sidecar)
#   5. ECS service               service-krew-hrms              (Fargate, 1 task)
#
# Networking is copied from service-vws so this lands in the same subnets and
# security group as every other dev service.
#
# THIS SCRIPT CREATES BILLABLE AWS RESOURCES. Read it before running.
# Run with:  bash scripts/bootstrap-ecs-dev.sh          (dry run, prints plan)
#            bash scripts/bootstrap-ecs-dev.sh --apply  (actually creates)

set -euo pipefail

PROFILE="${AWS_PROFILE:-dev}"
REGION="${AWS_REGION:-ap-south-1}"
CLUSTER="gk-dev-cluster"
NAME="service-krew-hrms"
FAMILY="$NAME-task"
CONTAINER="$NAME-container"
LOG_GROUP="/ecs/$FAMILY"
TG_NAME="ecs-gk-dev-$NAME"

# Copied from service-vws — keep in step with the other dev services.
SUBNETS="subnet-0225093c4004860f8,subnet-09402d991311036a8"
SECURITY_GROUP="sg-0af9d2af2a2bb5243"
CPU="1024"
MEMORY="2048"
APP_PORT="8000"

APPLY=false
[ "${1:-}" = "--apply" ] && APPLY=true

aws() { command aws --profile "$PROFILE" --region "$REGION" "$@"; }
run() {
    if $APPLY; then
        echo "+ $*" >&2
        "$@"
    else
        echo "[dry-run] $*"
    fi
}

ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
VPC=$(aws ec2 describe-subnets --subnet-ids "${SUBNETS%%,*}" --query 'Subnets[0].VpcId' --output text)
REGISTRY="$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/$NAME"

echo "account=$ACCOUNT region=$REGION vpc=$VPC cluster=$CLUSTER"
echo "apply=$APPLY"
echo

# ---------------------------------------------------------------- 1. ECR ----
if aws ecr describe-repositories --repository-names "$NAME" >/dev/null 2>&1; then
    echo "ECR repo $NAME already exists."
else
    run aws ecr create-repository --repository-name "$NAME" \
        --image-scanning-configuration scanOnPush=true
fi

# ------------------------------------------------------- 2. log group -------
if aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" \
     --query 'logGroups[?logGroupName==`'"$LOG_GROUP"'`]' --output text | grep -q .; then
    echo "Log group $LOG_GROUP already exists."
else
    run aws logs create-log-group --log-group-name "$LOG_GROUP"
    run aws logs put-retention-policy --log-group-name "$LOG_GROUP" --retention-in-days 30
fi

# ----------------------------------------------------- 3. target group ------
TG_ARN=$(aws elbv2 describe-target-groups --names "$TG_NAME" \
           --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || true)
if [ -n "$TG_ARN" ] && [ "$TG_ARN" != "None" ]; then
    echo "Target group $TG_NAME already exists."
else
    # /health/ is served by the app and needs no database, so it stays green
    # while migrations run on container start.
    run aws elbv2 create-target-group \
        --name "$TG_NAME" \
        --protocol HTTP --port "$APP_PORT" \
        --vpc-id "$VPC" --target-type ip \
        --health-check-path "/health/" \
        --health-check-interval-seconds 30 \
        --healthy-threshold-count 2 --unhealthy-threshold-count 3
    $APPLY && TG_ARN=$(aws elbv2 describe-target-groups --names "$TG_NAME" \
                         --query 'TargetGroups[0].TargetGroupArn' --output text)
fi

# -------------------------------------------------- 4. task definition ------
# Two containers: the Django app, and redis as a cache-only sidecar reachable on
# localhost. The sidecar dies with the task, which is exactly right for a cache.
CONTAINER_DEFS=$(cat <<JSON
[
  {
    "name": "$CONTAINER",
    "image": "$REGISTRY:bootstrap",
    "essential": true,
    "portMappings": [{"containerPort": $APP_PORT, "protocol": "tcp"}],
    "environment": [
      {"name": "REDIS_URL", "value": "redis://localhost:6379/0"},
      {"name": "DEBUG", "value": "0"},
      {"name": "HORILLA_ENV", "value": "development"},
      {"name": "MIGRATE_ON_START", "value": "0"}
    ],
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {
        "awslogs-group": "$LOG_GROUP",
        "awslogs-region": "$REGION",
        "awslogs-stream-prefix": "app"
      }
    }
  },
  {
    "name": "redis",
    "image": "public.ecr.aws/docker/library/redis:7-alpine",
    "essential": false,
    "command": ["redis-server","--maxmemory","256mb","--maxmemory-policy","allkeys-lru","--save",""],
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {
        "awslogs-group": "$LOG_GROUP",
        "awslogs-region": "$REGION",
        "awslogs-stream-prefix": "redis"
      }
    }
  }
]
JSON
)

echo
echo "Task definition $FAMILY will be registered with containers: $CONTAINER, redis"
if $APPLY; then
    echo "$CONTAINER_DEFS" > /tmp/krew-container-defs.json
    run aws ecs register-task-definition \
        --family "$FAMILY" \
        --task-role-arn "arn:aws:iam::$ACCOUNT:role/ECSTaskExecutionRole" \
        --execution-role-arn "arn:aws:iam::$ACCOUNT:role/ECSTaskExecutionRole" \
        --network-mode awsvpc --requires-compatibilities FARGATE \
        --cpu "$CPU" --memory "$MEMORY" \
        --container-definitions file:///tmp/krew-container-defs.json
fi

# ------------------------------------------------------- 5. ECS service -----
if $APPLY && { [ -z "$TG_ARN" ] || [ "$TG_ARN" = "None" ]; }; then
    echo "ERROR: target group ARN is empty — refusing to create a service with no load balancer." >&2
    exit 1
fi

if aws ecs describe-services --cluster "$CLUSTER" --services "$NAME" \
     --query 'services[?status==`ACTIVE`]' --output text 2>/dev/null | grep -q .; then
    echo "ECS service $NAME already exists."
else
    run aws ecs create-service \
        --cluster "$CLUSTER" --service-name "$NAME" \
        --task-definition "$FAMILY" --desired-count 1 --launch-type FARGATE \
        --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SECURITY_GROUP],assignPublicIp=DISABLED}" \
        --load-balancers "targetGroupArn=$TG_ARN,containerName=$CONTAINER,containerPort=$APP_PORT" \
        --health-check-grace-period-seconds 120
fi

cat <<EOF

Bootstrap $( $APPLY && echo complete || echo "plan printed (nothing created)" ).

Still to do by hand, because they need decisions rather than defaults:

  * Attach $TG_NAME to a listener on an ALB so the service is reachable.
    gk-dev-private-lb currently has only a default rule.

  * Set the app's runtime configuration. DATABASE_URL, SECRET_KEY,
    DB_INIT_PASSWORD and ALLOWED_HOSTS are NOT in the task definition above —
    put them in Secrets Manager or SSM Parameter Store and reference them from
    the container's "secrets" block. Do not inline them.

  * Create the database instance, then its database and roles:
      bash scripts/bootstrap-rds-dev.sh --apply        # krew-dev-database
      psql -h <endpoint> -U krew_admin -d postgres \\
           -v owner_pw="'...'" -v app_pw="'...'" \\
           -f scripts/provision-rds-dev.sql            # krew-dev-db + roles
    The psql step must run from inside the VPC. Migrations then run via the
    Jenkins pipeline, not by hand.

  * Point the Jenkins job at this service:
      ENV_VAR_ECR_REPO_NAME=$NAME
      ENV_VAR_ECR_CPU_VALUE=$CPU
      ENV_VAR_ECR_MEMORY_VALUE=$MEMORY
EOF
