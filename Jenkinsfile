// Deploy pipeline for krew-hrms (Django + gunicorn on ECS Fargate).
//
// Follows the same conventions as the other greatkapital services:
//   * BRANCH_NAME / TAG_NAME parameters choose what to build
//   * image tag  SNAPSHOT-<build>-<shortsha>  for branches, the tag name for tags
//   * task family  <ENV_VAR_ECR_REPO_NAME>-task, ECS service named after the ECR repo
//   * credentials keyed by ENV_VAR_PREFIX (DEV / PROD)
//
// Differences from the Gradle services:
//   * no CodeArtifact / Gradle stages — dependencies come from requirements.txt
//     and are installed inside the Docker build
//   * the task definition has TWO containers (app + redis cache sidecar), so the
//     image is patched by container NAME, never by index
//
// Job-level environment variables to configure in Jenkins:
//   ENV_VAR_PREFIX          DEV | PROD
//   ENV_VAR_REPO_NAME       https://github.com/greatkapitaltech/krew-hrms.git
//   ENV_VAR_ECR_REPO_NAME   service-krew-hrms
//   ENV_VAR_ECR_CPU_VALUE   1024
//   ENV_VAR_ECR_MEMORY_VALUE 2048
//   ENV_VAR_APP_CONTAINER   optional; defaults to <ENV_VAR_ECR_REPO_NAME>-container

pipeline {
    agent any

    parameters {
        string(
            name: 'BRANCH_NAME',
            defaultValue: 'dev',
            description: 'Branch to build and deploy. Anyone can deploy their own branch here.'
        )
        string(
            name: 'TAG_NAME',
            defaultValue: '',
            description: 'Git tag to deploy instead of a branch. Takes precedence over BRANCH_NAME.'
        )
        booleanParam(
            name: 'RUN_MIGRATIONS',
            defaultValue: true,
            description: 'Container entrypoint runs manage.py migrate on start. Untick to deploy without schema changes.'
        )
    }

    environment {
        GIT_CHECKOUT_TYPE   = "${params.TAG_NAME ? 'TAG' : 'BRANCH'}"
        GIT_BRANCH_TAG_NAME = "${params.TAG_NAME ? params.TAG_NAME : params.BRANCH_NAME}"
        APP_CONTAINER       = "${env.ENV_VAR_APP_CONTAINER ?: env.ENV_VAR_ECR_REPO_NAME + '-container'}"
    }

    options {
        timestamps()
        disableConcurrentBuilds()   // shared dev environment: last deploy wins, one at a time
        buildDiscarder(logRotator(numToKeepStr: '30'))
    }

    stages {

        stage('Checkout') {
            steps {
                script {
                    def ref = env.GIT_CHECKOUT_TYPE == 'TAG'
                        ? "refs/tags/${env.GIT_BRANCH_TAG_NAME}"
                        : "${env.GIT_BRANCH_TAG_NAME}"
                    echo "Checking out ${env.GIT_CHECKOUT_TYPE}: ${env.GIT_BRANCH_TAG_NAME}"

                    checkout([$class: 'GitSCM',
                        branches: [[name: ref]],
                        userRemoteConfigs: [[
                            url: env.ENV_VAR_REPO_NAME,
                            credentialsId: 'GITHUB_CREDENTIALS_ID'
                        ]]
                    ])

                    env.GIT_COMMIT_SHA = sh(script: 'git rev-parse HEAD', returnStdout: true).trim()
                    env.SHORT_SHA      = env.GIT_COMMIT_SHA.substring(0, 8)

                    // Who deployed what is the first question asked when a shared
                    // dev environment misbehaves, so put it on the build itself.
                    def user = currentBuild.rawBuild
                        .getCause(hudson.model.Cause.UserIdCause)?.getUserId() ?: 'Unknown User'
                    currentBuild.description = "${user} | ${env.GIT_BRANCH_TAG_NAME} | ${env.SHORT_SHA}"
                    env.BUILD_USER = user
                }
            }
        }

        stage('Resolve image tag') {
            steps {
                script {
                    env.IMAGE_TAG = env.GIT_CHECKOUT_TYPE == 'TAG'
                        ? params.TAG_NAME
                        : "SNAPSHOT-${env.BUILD_NUMBER}-${env.SHORT_SHA}"
                    echo "Image tag: ${env.IMAGE_TAG}"
                }
            }
        }

        stage('Setup environment') {
            steps {
                withCredentials([
                    string(credentialsId: "${env.ENV_VAR_PREFIX}_ECS_CLUSTER_NAME",               variable: 'ECS_CLUSTER_NAME'),
                    string(credentialsId: "${env.ENV_VAR_PREFIX}_DEPLOYMENT_AWS_ACCOUNT_NO",      variable: 'DEPLOYMENT_AWS_ACCOUNT_NO'),
                    string(credentialsId: "${env.ENV_VAR_PREFIX}_DEPLOYMENT_AWS_ACCOUNT_REGION",  variable: 'DEPLOYMENT_AWS_ACCOUNT_REGION')
                ]) {
                    script {
                        env.ECS_CLUSTER_NAME              = ECS_CLUSTER_NAME
                        env.DEPLOYMENT_AWS_ACCOUNT_NO     = DEPLOYMENT_AWS_ACCOUNT_NO
                        env.DEPLOYMENT_AWS_ACCOUNT_REGION = DEPLOYMENT_AWS_ACCOUNT_REGION
                        env.ECR_REGISTRY = "${DEPLOYMENT_AWS_ACCOUNT_NO}.dkr.ecr.${DEPLOYMENT_AWS_ACCOUNT_REGION}.amazonaws.com/${env.ENV_VAR_ECR_REPO_NAME}"
                        echo "Cluster: ${env.ECS_CLUSTER_NAME}  Registry: ${env.ECR_REGISTRY}"
                    }
                }
            }
        }

        stage('Publish to ECR') {
            steps {
                withCredentials([
                    string(credentialsId: "${env.ENV_VAR_PREFIX}_AWS_ECS_ACCESS_KEY", variable: 'AWS_ACCESS_KEY_ID'),
                    string(credentialsId: "${env.ENV_VAR_PREFIX}_AWS_ECS_SECRET_KEY", variable: 'AWS_SECRET_ACCESS_KEY')
                ]) {
                    sh '''
                        set -euo pipefail

                        # A tag build must be reproducible: never overwrite an existing tag.
                        if [ "$GIT_CHECKOUT_TYPE" = "TAG" ]; then
                            if aws ecr describe-images \
                                 --repository-name "$ENV_VAR_ECR_REPO_NAME" \
                                 --region "$DEPLOYMENT_AWS_ACCOUNT_REGION" \
                                 --image-ids imageTag="$IMAGE_TAG" >/dev/null 2>&1; then
                                echo "Image $IMAGE_TAG already exists in ECR; reusing it."
                                exit 0
                            fi
                        fi

                        aws ecr get-login-password --region "$DEPLOYMENT_AWS_ACCOUNT_REGION" \
                          | docker login --username AWS --password-stdin "$ECR_REGISTRY"

                        docker build -t "$ECR_REGISTRY:$IMAGE_TAG" .
                        docker push "$ECR_REGISTRY:$IMAGE_TAG"
                    '''
                }
            }
        }

        stage('Register task definition') {
            steps {
                withCredentials([
                    string(credentialsId: "${env.ENV_VAR_PREFIX}_AWS_ECS_ACCESS_KEY", variable: 'AWS_ACCESS_KEY_ID'),
                    string(credentialsId: "${env.ENV_VAR_PREFIX}_AWS_ECS_SECRET_KEY", variable: 'AWS_SECRET_ACCESS_KEY')
                ]) {
                    script {
                        env.TASK_DEF_ARN = sh(returnStdout: true, script: '''
                            set -euo pipefail
                            IMAGE="$ECR_REGISTRY:$IMAGE_TAG"
                            FAMILY="$ENV_VAR_ECR_REPO_NAME-task"

                            # Patch by container NAME. The task runs the app plus a redis
                            # cache sidecar, so .[0] is not reliably the app container.
                            container_defs=$(aws ecs describe-task-definition \
                                --task-definition "$FAMILY" \
                                --region "$DEPLOYMENT_AWS_ACCOUNT_REGION" \
                                --query 'taskDefinition.containerDefinitions' --output json \
                              | jq -c --arg name "$APP_CONTAINER" --arg image "$IMAGE" \
                                  'map(if .name == $name then .image = $image else . end)')

                            echo "$container_defs" | jq -e --arg image "$IMAGE" \
                                'map(select(.image == $image)) | length > 0' >/dev/null \
                              || { echo "ERROR: no container named $APP_CONTAINER in $FAMILY" >&2; exit 1; }

                            aws ecs register-task-definition \
                                --family "$FAMILY" \
                                --task-role-arn "arn:aws:iam::$DEPLOYMENT_AWS_ACCOUNT_NO:role/ECSTaskExecutionRole" \
                                --execution-role-arn "arn:aws:iam::$DEPLOYMENT_AWS_ACCOUNT_NO:role/ECSTaskExecutionRole" \
                                --network-mode awsvpc \
                                --requires-compatibilities FARGATE \
                                --cpu "$ENV_VAR_ECR_CPU_VALUE" \
                                --memory "$ENV_VAR_ECR_MEMORY_VALUE" \
                                --container-definitions "$container_defs" \
                                --region "$DEPLOYMENT_AWS_ACCOUNT_REGION" \
                                --query 'taskDefinition.taskDefinitionArn' --output text
                        ''').trim()
                        echo "Registered ${env.TASK_DEF_ARN}"
                    }
                }
            }
        }

        stage('Run migrations') {
            when { expression { params.RUN_MIGRATIONS } }
            steps {
                withCredentials([
                    string(credentialsId: "${env.ENV_VAR_PREFIX}_AWS_ECS_ACCESS_KEY", variable: 'AWS_ACCESS_KEY_ID'),
                    string(credentialsId: "${env.ENV_VAR_PREFIX}_AWS_ECS_SECRET_KEY", variable: 'AWS_SECRET_ACCESS_KEY')
                ]) {
                    // Migrations run ONCE, here, against the new image — before any
                    // traffic-serving task starts. Containers boot with
                    // MIGRATE_ON_START=0 so they never race each other.
                    //
                    // They run as krew_owner, not the runtime role: krew_app owns
                    // nothing so that RLS policies apply to it, which also means it
                    // cannot create tables. ECS container overrides cannot inject
                    // secrets, so DATABASE_URL_OWNER is declared on the task
                    // definition and swapped in for this command only.
                    sh '''
                        set -euo pipefail

                        # Read subnets and security groups off the service itself, so the
                        # migration task always lands in the same network as the app and
                        # no extra Jenkins credentials have to be kept in sync.
                        NETCFG=$(aws ecs describe-services \
                            --cluster "$ECS_CLUSTER_NAME" --services "$ENV_VAR_ECR_REPO_NAME" \
                            --region "$DEPLOYMENT_AWS_ACCOUNT_REGION" \
                            --query 'services[0].networkConfiguration.awsvpcConfiguration' --output json)
                        SUBNETS=$(echo "$NETCFG" | jq -r '.subnets | join(",")')
                        SGS=$(echo "$NETCFG"     | jq -r '.securityGroups | join(",")')
                        [ -n "$SUBNETS" ] && [ "$SUBNETS" != "null" ] \
                            || { echo "ERROR: could not read network config from service" >&2; exit 1; }

                        # Build the overrides as a file. Inlining the JSON means the
                        # shell strips the inner double quotes and the AWS CLI receives
                        # {containerOverrides:[...]} , which is not valid JSON.
                        OVERRIDES=$(mktemp)
                        cat > "$OVERRIDES" <<JSON
{"containerOverrides":[{"name":"$APP_CONTAINER","command":["sh","-c","DATABASE_URL=\"$DATABASE_URL_OWNER\" exec python manage.py migrate --noinput"],"environment":[{"name":"MIGRATE_ON_START","value":"0"}]}]}
JSON
                        jq -e . "$OVERRIDES" >/dev/null || { echo "ERROR: overrides JSON is malformed" >&2; exit 1; }

                        TASK_ARN=$(aws ecs run-task \
                            --cluster "$ECS_CLUSTER_NAME" \
                            --task-definition "$TASK_DEF_ARN" \
                            --launch-type FARGATE \
                            --count 1 \
                            --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SGS],assignPublicIp=DISABLED}" \
                            --overrides "file://$OVERRIDES" \
                            --region "$DEPLOYMENT_AWS_ACCOUNT_REGION" \
                            --query 'tasks[0].taskArn' --output text)
                        rm -f "$OVERRIDES"

                        echo "Migration task: $TASK_ARN"
                        aws ecs wait tasks-stopped --cluster "$ECS_CLUSTER_NAME" \
                            --tasks "$TASK_ARN" --region "$DEPLOYMENT_AWS_ACCOUNT_REGION"

                        EXIT_CODE=$(aws ecs describe-tasks --cluster "$ECS_CLUSTER_NAME" \
                            --tasks "$TASK_ARN" --region "$DEPLOYMENT_AWS_ACCOUNT_REGION" \
                            --query "tasks[0].containers[?name=='$APP_CONTAINER'].exitCode | [0]" --output text)
                        REASON=$(aws ecs describe-tasks --cluster "$ECS_CLUSTER_NAME" \
                            --tasks "$TASK_ARN" --region "$DEPLOYMENT_AWS_ACCOUNT_REGION" \
                            --query 'tasks[0].stoppedReason' --output text)

                        echo "Migration exit code: $EXIT_CODE ($REASON)"
                        if [ "$EXIT_CODE" != "0" ]; then
                            echo "Migrations FAILED — not deploying. See log group /ecs/$ENV_VAR_ECR_REPO_NAME-task" >&2
                            exit 1
                        fi
                    '''
                }
            }
        }

        stage('Deploy to ECS') {
            steps {
                withCredentials([
                    string(credentialsId: "${env.ENV_VAR_PREFIX}_AWS_ECS_ACCESS_KEY", variable: 'AWS_ACCESS_KEY_ID'),
                    string(credentialsId: "${env.ENV_VAR_PREFIX}_AWS_ECS_SECRET_KEY", variable: 'AWS_SECRET_ACCESS_KEY')
                ]) {
                    sh '''
                        set -euo pipefail
                        aws ecs update-service \
                            --cluster "$ECS_CLUSTER_NAME" \
                            --service "$ENV_VAR_ECR_REPO_NAME" \
                            --task-definition "$TASK_DEF_ARN" \
                            --force-new-deployment \
                            --region "$DEPLOYMENT_AWS_ACCOUNT_REGION" >/dev/null
                        echo "Deployment started: $TASK_DEF_ARN"
                    '''
                }
            }
        }

        stage('Wait for rollout') {
            steps {
                withCredentials([
                    string(credentialsId: "${env.ENV_VAR_PREFIX}_AWS_ECS_ACCESS_KEY", variable: 'AWS_ACCESS_KEY_ID'),
                    string(credentialsId: "${env.ENV_VAR_PREFIX}_AWS_ECS_SECRET_KEY", variable: 'AWS_SECRET_ACCESS_KEY')
                ]) {
                    // Without this the job goes green the moment the API call returns,
                    // even if every new task crash-loops on startup.
                    sh '''
                        set -euo pipefail
                        aws ecs wait services-stable \
                            --cluster "$ECS_CLUSTER_NAME" \
                            --services "$ENV_VAR_ECR_REPO_NAME" \
                            --region "$DEPLOYMENT_AWS_ACCOUNT_REGION"
                        echo "Service reached a stable state."
                    '''
                }
            }
        }
    }

    post {
        success {
            echo "Deployed ${env.GIT_BRANCH_TAG_NAME} (${env.SHORT_SHA}) to ${env.ENV_VAR_PREFIX} by ${env.BUILD_USER}"
        }
        failure {
            echo "FAILED deploying ${env.GIT_BRANCH_TAG_NAME} (${env.SHORT_SHA}) to ${env.ENV_VAR_PREFIX}"
        }
    }
}
