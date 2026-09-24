pipeline {
    agent any

    options {
        buildDiscarder(logRotator(numToKeepStr: '5'))
        disableConcurrentBuilds()
        timestamps()
        skipDefaultCheckout(true)
    }

    stages {
        stage('Checkout Repo') {
            steps {
                checkout scm
            }
        }

        stage('Tests (Contenedor Python)') {
            agent {
                dockerfile {
                    filename 'Dockerfile.ci'
                    reuseNode true
                }
            }
            steps {
                sh '''
                    set -eu
                    ci_venv="$(mktemp -d)"
                    python -m venv "$ci_venv"
                    "$ci_venv/bin/python" -m pip install --upgrade pip
                    "$ci_venv/bin/pip" install --no-cache-dir -r requirements.txt -r requirements-dev.txt
                    JWT_SECRET_KEY=ci-test-secret MAIL_USERNAME=ci@igualab.pe MAIL_PASSWORD=ci-password MAIL_FROM=ci@igualab.pe "$ci_venv/bin/python" -m pytest tests/ --cov=app --cov-report=term-missing --cov-report=xml:coverage.xml
                '''
            }
        }

        stage('SonarQube Analysis') {
            when {
                anyOf {
                    branch 'qa'
                    branch 'uat'
                }
            }
            agent {
                dockerfile {
                    filename 'Dockerfile.ci'
                    reuseNode true
                }
            }
            environment {
                scannerHome = tool 'SonarScanner'
            }
            steps {
                script {
                    def sonarUserHome = "${env.WORKSPACE}/.sonar"

                    withEnv(["SONAR_USER_HOME=${sonarUserHome}"]) {
                        sh 'python --version'
                        sh 'java --version'

                        if (env.BRANCH_NAME == 'qa') {
                            withSonarQubeEnv('SonarQube-Server') {
                                sh "${scannerHome}/bin/sonar-scanner -Dsonar.projectKey=BCK-IGUALAB-QA -Dsonar.userHome=${sonarUserHome}"
                            }
                        } else {
                            withSonarQubeEnv('SonarQube-Server') {
                                sh "${scannerHome}/bin/sonar-scanner -Dsonar.projectKey=BCK-IGUALAB-UAT -Dsonar.userHome=${sonarUserHome}"
                            }
                        }
                    }
                }
            }
        }

        stage('Quality Gate') {
            when {
                anyOf {
                    branch 'qa'
                    branch 'uat'
                }
            }
            steps {
                timeout(time: 1, unit: 'HOURS') {
                    waitForQualityGate abortPipeline: true
                }
            }
        }

        stage('Deploy Dev (Docker Compose)') {
            when {
                branch 'development'
            }
            steps {
                withCredentials([file(credentialsId: 'IGUALAB_BACKEND_DEV', variable: 'SECRET_FILE')]) {
                    withEnv(['COMPOSE_PROJECT=igualab-backend-development']) {
                        sh '''
                            set -eu
                            rm -f .env
                            cp "$SECRET_FILE" .env
                            docker compose -p "$COMPOSE_PROJECT" down
                            docker compose -p "$COMPOSE_PROJECT" up -d --build
                        '''
                    }
                }
            }
        }

        stage('Verificar despliegue Development') {
            when {
                branch 'development'
            }
            steps {
                withEnv(['COMPOSE_PROJECT=igualab-backend-development']) {
                    sh '''
                        set -eu

                        container_id="$(
                            docker compose \
                                -p "$COMPOSE_PROJECT" \
                                ps -a -q backend
                        )"

                        if [ -z "$container_id" ]; then
                            echo "ERROR: no se creó el contenedor backend."
                            exit 1
                        fi

                        container_environment="$(
                            docker inspect \
                                --format '{{range .Config.Env}}{{println .}}{{end}}' \
                                "$container_id"
                        )"

                        missing=0

                        for variable in \
                            JWT_SECRET_KEY \
                            DATABASE_URL \
                            FRONTEND_URL \
                            CORS_ORIGINS
                        do
                            if printf '%s\n' "$container_environment" \
                                | grep -Eq "^${variable}=.+$"
                            then
                                echo "${variable}: CONFIGURADA"
                            else
                                echo "${variable}: FALTANTE O VACÍA"
                                missing=1
                            fi
                        done

                        if [ "$missing" -ne 0 ]; then
                            echo "ERROR: faltan variables obligatorias."

                            docker compose \
                                -p "$COMPOSE_PROJECT" \
                                logs --tail=100 backend

                            exit 1
                        fi

                        attempt=1
                        max_attempts=18

                        while [ "$attempt" -le "$max_attempts" ]; do
                            container_status="$(
                                docker inspect \
                                    --format '{{.State.Status}}' \
                                    "$container_id"
                            )"

                            health_status="$(
                                docker inspect \
                                    --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}sin-healthcheck{{end}}' \
                                    "$container_id"
                            )"

                            echo "Intento ${attempt}/${max_attempts}: container=${container_status}, health=${health_status}"

                            if [ "$health_status" = "healthy" ]; then
                                echo "Backend Development desplegado correctamente."
                                exit 0
                            fi

                            if [ "$container_status" != "running" ] \
                                || [ "$health_status" = "unhealthy" ]
                            then
                                break
                            fi

                            attempt=$((attempt + 1))
                            sleep 5
                        done

                        echo "ERROR: el backend no alcanzó el estado healthy."

                        docker compose \
                            -p "$COMPOSE_PROJECT" \
                            ps

                        docker compose \
                            -p "$COMPOSE_PROJECT" \
                            logs --tail=100 backend

                        exit 1
                    '''
                }
            }
        }

        stage('Deploy QA (Docker Compose)') {
            when {
                branch 'qa'
            }
            steps {
                withCredentials([file(credentialsId: 'IGUALAB_BACKEND_QA', variable: 'SECRET_FILE')]) {
                    withEnv(['COMPOSE_PROJECT=igualab-backend-qa']) {
                        sh '''
                            set -eu
                            rm -f .env
                            cp "$SECRET_FILE" .env
                            docker compose -p "$COMPOSE_PROJECT" down
                            docker compose -p "$COMPOSE_PROJECT" up -d --build
                        '''
                    }
                }
            }
        }

        stage('Deploy UAT (Docker Compose)') {
            when {
                branch 'uat'
            }
            steps {
                withCredentials([file(credentialsId: 'IGUALAB_BACKEND_UAT', variable: 'SECRET_FILE')]) {
                    withEnv(['COMPOSE_PROJECT=igualab-backend-uat']) {
                        sh '''
                            set -eu
                            rm -f .env
                            cp "$SECRET_FILE" .env
                            docker compose -p "$COMPOSE_PROJECT" down
                            docker compose -p "$COMPOSE_PROJECT" up -d --build
                        '''
                    }
                }
            }
        }
    }

    post {
        always {
            sh 'rm -f .env'
        }
    }
}
