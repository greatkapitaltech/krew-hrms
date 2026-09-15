# Deployment

krew-hrms deploys to **ECS Fargate** in the greatkapital AWS accounts, built and
released by **Jenkins**, following the same conventions as the other ~90 services.

| | dev | prod |
| --- | --- | --- |
| AWS account | `630877979407` | `077592921108` |
| Region | `ap-south-1` | `ap-south-1` |
| Cluster | `gk-dev-cluster` | *(to be confirmed)* |
| ECR repo | `service-krew-hrms` | `service-krew-hrms` |
| Task family | `service-krew-hrms-task` | same |
| ECS service | `service-krew-hrms` | same |
| Database | `krew-dev-db` on `krew-dev-database` | *(to be confirmed)* |

GitHub Actions still runs lint and tests on every push. Jenkins only builds and
deploys — the two do not overlap.

---

## Deploying your own branch

The Jenkins job is parameterised, so anyone on the team can deploy any branch
without touching the pipeline:

1. Open the **krew-hrms** job in Jenkins.
2. **Build with Parameters**.
3. Set `BRANCH_NAME` to your branch (e.g. `feature/leave-accrual`). Leave
   `TAG_NAME` empty.
4. Build.

The build description records **who deployed which branch at which commit**, so
`git log` is never needed to answer "what is on dev right now".

### This is one shared environment

There is a single dev service and a single database. Deploying replaces whatever
was there — **last deploy wins**. Two people cannot test two branches at the same
time, and a migration from one branch applies to everyone.

Concurrent builds are disabled so deploys queue rather than race, but that does
not protect you from a colleague deploying over your branch a minute later. Say
so in the team channel before deploying, and expect to redeploy after someone
else does.

If this becomes painful, the fix is per-branch services and databases — see the
"branch environments" discussion in the pipeline design.

---

## Deploying a release

Set `TAG_NAME` to a git tag and leave `BRANCH_NAME` alone. Tag builds are
**immutable**: if an image already exists in ECR for that tag, it is reused
rather than rebuilt, so a tag always means the same bytes.

Branch builds are tagged `SNAPSHOT-<build-number>-<short-sha>` and are disposable.

---

## What the pipeline does

1. **Checkout** the branch or tag; record deployer, branch and commit.
2. **Resolve image tag** — `SNAPSHOT-…` or the release tag.
3. **Setup environment** — pull cluster name, account and region from Jenkins
   credentials keyed by `ENV_VAR_PREFIX` (`DEV` / `PROD`).
4. **Publish to ECR** — build the Dockerfile, push. Tag builds skip if present.
5. **Deploy to ECS** — read the current task definition, replace the image *of
   the application container by name*, register a new revision, update the service.
6. **Wait for rollout** — `aws ecs wait services-stable`.

Step 6 matters: without it the job goes green the moment the API call returns,
even if every new task crash-loops on boot. With it, a broken deploy fails the
build.

Step 5 patches **by container name**, not `.[0]`, because the task runs two
containers — the app and a Redis sidecar. Index-based patching would eventually
rewrite the wrong image.

### Jenkins job configuration

Environment variables on the job:

| Variable | Value (dev) |
| --- | --- |
| `ENV_VAR_PREFIX` | `DEV` |
| `ENV_VAR_REPO_NAME` | `https://github.com/greatkapitaltech/krew-hrms.git` |
| `ENV_VAR_ECR_REPO_NAME` | `service-krew-hrms` |
| `ENV_VAR_ECR_CPU_VALUE` | `1024` |
| `ENV_VAR_ECR_MEMORY_VALUE` | `2048` |

Credentials used (same IDs as the other services): `GITHUB_CREDENTIALS_ID`,
`DEV_ECS_CLUSTER_NAME`, `DEV_AWS_ECS_ACCESS_KEY`, `DEV_AWS_ECS_SECRET_KEY`,
`DEV_DEPLOYMENT_AWS_ACCOUNT_NO`, `DEV_DEPLOYMENT_AWS_ACCOUNT_REGION`.

No CodeArtifact credentials are needed — this is a Python service and its
dependencies install from `requirements.txt` inside the Docker build.

---

## Redis

Redis runs as a **sidecar container in the same task**, reachable on
`redis://localhost:6379/0`. There is no ElastiCache instance.

This is deliberate: Redis here is a cache and nothing else, so losing it must be
harmless. A sidecar costs nothing, needs no VPC wiring, and dies with the task.
If Redis is ever given something that is not disposable — sessions, a queue,
locks — this decision has to be revisited first. See `docs/redis-and-rls.md`.

---

## Database

krew-hrms has its **own RDS instance**, `krew-dev-database`, separate from the
shared `gk-dev-database`.

| | |
| --- | --- |
| Instance | `krew-dev-database` |
| Engine | PostgreSQL **16** — matches local development and CI |
| Class | `db.t3.micro`, 40 GB gp3, encrypted |
| Placement | `vpc-014af4749a26d6a9f`, private, single-AZ |
| Backups | 7 days, deletion protection on |
| Database | `krew-dev-db` — the same name as the local database |
| Master password | managed by RDS in Secrets Manager, never in git |

A dedicated instance means no other dev service can exhaust its connections or
compete for its CPU, and it can be backed up, restored and version-upgraded
independently. That matters here because the RLS work adds per-row subqueries
whose cost has to be measured without a noisy neighbour distorting the numbers.

Engine version is deliberately **16, not 17**: local development, CI and this
instance then all agree, so a migration that works on a laptop works on dev.
(`myntra-dev-database` runs 17 — this is a considered divergence, not an
oversight.)

### Creating it

```bash
AWS_PROFILE=dev bash scripts/bootstrap-rds-dev.sh           # dry run
AWS_PROFILE=dev bash scripts/bootstrap-rds-dev.sh --apply   # ~10 minutes
```

This creates a security group that permits 5432 **only from the ECS task
security group** `sg-0af9d2af2a2bb5243` — not a CIDR — so nothing outside the
service can reach the database.

The master password is never set by the script. `--manage-master-user-password`
hands it to RDS, which stores and rotates it in Secrets Manager, so it never
touches a shell history or a file.

### Roles

`scripts/provision-rds-dev.sql` then creates the database and two roles. The
split is not cosmetic:

| Role | Purpose |
| --- | --- |
| `krew_owner` | owns the schema, runs migrations, never serves traffic |
| `krew_app` | runtime role — owns nothing, so RLS policies actually apply |

A table owner bypasses row-level security unless the table is `FORCE`d, and a
superuser bypasses it unconditionally. If the app connected as the owner, every
policy would install, look correct, and enforce nothing. Both roles are created
`NOSUPERUSER NOBYPASSRLS`, and the script ends with a verification query — if
either shows `t`, stop.

RDS is private, so run this from inside the VPC (a bastion, or a one-shot ECS
task on `gk-dev-cluster`):

```bash
psql "host=krew-dev-database.<...>.ap-south-1.rds.amazonaws.com \
      user=krew_admin dbname=postgres sslmode=require" \
     -v owner_pw="'...'" -v app_pw="'...'" \
     -f scripts/provision-rds-dev.sql
```

Then store the runtime connection string in Secrets Manager and reference it
from the task definition's `secrets` block as `DATABASE_URL`:

```
postgres://krew_app:<app_pw>@krew-dev-database.<...>:5432/krew-dev-db
```

### Sizing

`db.t3.micro` is 2 burstable vCPU and 1 GB RAM. Adequate for correctness work,
but krew-hrms has ~380 tables and a dashboard firing a dozen aggregates per
load, and RLS will add per-row subqueries on top. Expect to move to
`db.t4g.small` before drawing conclusions from performance testing. Because the
instance is dedicated, resizing it affects nobody else.

---

## Migrations

Migrations run as a **one-shot ECS task, before the rollout**, not on container
start:

1. Register a task definition pointing at the new image.
2. `aws ecs run-task` with the command overridden to `manage.py migrate --noinput`.
3. Wait for it to stop, and read the container exit code.
4. Only on exit code 0, `update-service`.

A failed migration therefore **fails the build and deploys nothing** — the
running version stays up. The alternative, migrating on container start, means a
bad migration crash-loops every new task and the old ones drain anyway.

`docker/entrypoint.sh` still migrates on start when `MIGRATE_ON_START` is unset,
so `docker compose up` works locally with no extra configuration. The ECS task
definition sets `MIGRATE_ON_START=0`.

Untick **RUN_MIGRATIONS** in the Jenkins job to deploy code without touching the
schema.

### Why not migrate on start

With `desiredCount` above 1, every task races to apply the same migrations.
Postgres advisory locks make that mostly survivable, but "mostly" is not a
property worth relying on for payroll data.

---

## First-time setup

The pipeline only *updates* an existing service. `scripts/bootstrap-ecs-dev.sh`
creates the ECR repo, log group, target group, task definition and ECS service,
copying networking from `service-vws` so it lands in the same subnets and
security group as the other dev services.

```bash
AWS_PROFILE=dev bash scripts/bootstrap-ecs-dev.sh           # dry run, prints the plan
AWS_PROFILE=dev bash scripts/bootstrap-ecs-dev.sh --apply   # creates resources
```

Four things it deliberately does **not** do, because they need a decision rather
than a default:

- **Attach the target group to an ALB listener.** `gk-dev-private-lb` has only a
  default rule today, so how this service gets routed is an open choice.
- **Set runtime configuration.** `DATABASE_URL`, `SECRET_KEY`, `DB_INIT_PASSWORD`
  and `ALLOWED_HOSTS` are not in the task definition. Put them in Secrets Manager
  or SSM and reference them from the container's `secrets` block — never inline.
- **Create the database instance and database** — `scripts/bootstrap-rds-dev.sh`
  then `scripts/provision-rds-dev.sql`. See *Database* above.
- **Configure the Jenkins job.**

---

## Known gaps

**Migrations are not transactional across the rollout.** They run before the
new tasks start, so a migration that succeeds followed by a failed rollout
leaves the database ahead of the running code. Django migrations are not
auto-reverted; rolling back means deploying the previous image *and* deciding
what to do about the schema.

**No automatic rollback.** A failed rollout fails the build and leaves ECS
retrying. Recovery is manual: redeploy the previous tag, or
`aws ecs update-service --task-definition <previous-revision>`.

**Prod is not wired up.** The account exists (`077592921108`, deployment user
`prod_aws_deployment_user`) but no cluster, service or job has been confirmed.
The Jenkinsfile already supports it via `ENV_VAR_PREFIX=PROD` once the
credentials and target exist.
