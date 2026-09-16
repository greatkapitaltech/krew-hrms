#!/usr/bin/env bash
#
# One-time creation of a DEDICATED RDS instance for krew-hrms dev.
#
# Creates:
#   1. Security group  krew-dev-database-sg   (5432 from the ECS task SG only)
#   2. RDS instance    krew-dev-database      (PostgreSQL 16, private, encrypted)
#
# Sizing and placement follow myntra-dev-database, the closest existing
# single-application instance.
#
# The master password is NOT set here. --manage-master-user-password hands it to
# RDS, which stores and rotates it in Secrets Manager. No password ever touches
# this script, your shell history, or git.
#
# THIS CREATES BILLABLE AWS RESOURCES (~$15-20/month) AND TAKES ~10 MINUTES.
# Run with:  bash scripts/bootstrap-rds-dev.sh          (dry run, prints plan)
#            bash scripts/bootstrap-rds-dev.sh --apply  (actually creates)

set -euo pipefail

PROFILE="${AWS_PROFILE:-dev}"
REGION="${AWS_REGION:-ap-south-1}"

INSTANCE="krew-dev-database"
SG_NAME="krew-dev-database-sg"
VPC="vpc-014af4749a26d6a9f"
SUBNET_GROUP="default-vpc-014af4749a26d6a9f"
ECS_TASK_SG="sg-0af9d2af2a2bb5243"       # the SG krew-hrms tasks run with

# PostgreSQL 16 to match local development, CI and gk-dev-database. Deliberately
# not 17 (which myntra-dev-database uses) so that local, CI and dev agree.
ENGINE_VERSION="16"
INSTANCE_CLASS="db.t3.micro"
STORAGE_GB="40"
STORAGE_TYPE="gp3"                        # cheaper and faster than the gp2 used elsewhere
BACKUP_DAYS="7"
MASTER_USER="krew_admin"

APPLY=false
[ "${1:-}" = "--apply" ] && APPLY=true

aws() { command aws --profile "$PROFILE" --region "$REGION" "$@"; }
run() {
    if $APPLY; then echo "+ $*" >&2; "$@"; else echo "[dry-run] $*"; fi
}

echo "region=$REGION vpc=$VPC instance=$INSTANCE apply=$APPLY"
echo

# ------------------------------------------------------ 1. security group ----
SG_ID=$(aws ec2 describe-security-groups \
          --filters "Name=group-name,Values=$SG_NAME" "Name=vpc-id,Values=$VPC" \
          --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || true)
[ "$SG_ID" = "None" ] && SG_ID=""

if [ -n "$SG_ID" ]; then
    echo "Security group $SG_NAME already exists ($SG_ID)."
else
    run aws ec2 create-security-group \
        --group-name "$SG_NAME" --vpc-id "$VPC" \
        --description "krew-hrms dev database; ingress from ECS tasks only"
    if $APPLY; then
        SG_ID=$(aws ec2 describe-security-groups \
                  --filters "Name=group-name,Values=$SG_NAME" "Name=vpc-id,Values=$VPC" \
                  --query 'SecurityGroups[0].GroupId' --output text)
        echo "Created $SG_ID"
    fi
fi

# Source is the ECS task security group, never a CIDR. Nothing outside the
# service can reach the database, including other dev services.
if $APPLY; then
    if aws ec2 describe-security-groups --group-ids "$SG_ID" \
         --query "SecurityGroups[0].IpPermissions[?FromPort==\`5432\`].UserIdGroupPairs[].GroupId" \
         --output text | grep -q "$ECS_TASK_SG"; then
        echo "Ingress from $ECS_TASK_SG already present."
    else
        run aws ec2 authorize-security-group-ingress \
            --group-id "$SG_ID" --protocol tcp --port 5432 \
            --source-group "$ECS_TASK_SG"
    fi
else
    echo "[dry-run] authorize 5432 on \$SG_ID from $ECS_TASK_SG"
fi

# -------------------------------------------------------- 2. RDS instance ----
if $APPLY; then
    case "${SG_ID:-}" in
        sg-*) : ;;
        *) echo "ERROR: security group id is '${SG_ID:-<empty>}' — refusing to create the instance." >&2
           exit 1 ;;
    esac
fi

if aws rds describe-db-instances --db-instance-identifier "$INSTANCE" >/dev/null 2>&1; then
    echo "RDS instance $INSTANCE already exists."
else
    run aws rds create-db-instance \
        --db-instance-identifier "$INSTANCE" \
        --engine postgres --engine-version "$ENGINE_VERSION" \
        --db-instance-class "$INSTANCE_CLASS" \
        --allocated-storage "$STORAGE_GB" --storage-type "$STORAGE_TYPE" \
        --storage-encrypted \
        --master-username "$MASTER_USER" \
        --manage-master-user-password \
        --db-subnet-group-name "$SUBNET_GROUP" \
        --vpc-security-group-ids "${SG_ID:-sg-CREATED-ABOVE}" \
        --no-publicly-accessible \
        --no-multi-az \
        --backup-retention-period "$BACKUP_DAYS" \
        --copy-tags-to-snapshot \
        --auto-minor-version-upgrade \
        --deletion-protection \
        --tags Key=Application,Value=krew-hrms Key=Environment,Value=dev

    if $APPLY; then
        echo "Waiting for $INSTANCE to become available (typically 8-12 minutes)..."
        aws rds wait db-instance-available --db-instance-identifier "$INSTANCE"
    fi
fi

if $APPLY; then
    ENDPOINT=$(aws rds describe-db-instances --db-instance-identifier "$INSTANCE" \
                 --query 'DBInstances[0].Endpoint.Address' --output text)
    SECRET=$(aws rds describe-db-instances --db-instance-identifier "$INSTANCE" \
                 --query 'DBInstances[0].MasterUserSecret.SecretArn' --output text)
else
    ENDPOINT="<created-on-apply>"; SECRET="<created-on-apply>"
fi

cat <<EOF

RDS bootstrap $( $APPLY && echo complete || echo "plan printed (nothing created)" ).

  endpoint : $ENDPOINT
  master   : $MASTER_USER
  password : managed by RDS in Secrets Manager
             $SECRET

Next:

  1. Retrieve the master password when you need it:
       aws secretsmanager get-secret-value --secret-id $SECRET \\
         --query SecretString --output text

  2. Create the database and roles (run from inside the VPC):
       psql "host=$ENDPOINT user=$MASTER_USER dbname=postgres sslmode=require" \\
            -v owner_pw="'...'" -v app_pw="'...'" \\
            -f scripts/provision-rds-dev.sql

  3. Store the krew_app connection string for the ECS task:
       postgres://krew_app:<app_pw>@$ENDPOINT:5432/krew-dev-db

     Put it in Secrets Manager and reference it from the task definition's
     "secrets" block as DATABASE_URL. Do not inline it.

Note: deletion protection is ON. To remove this instance you must disable it
first, which is deliberate.
EOF
