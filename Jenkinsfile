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

        stage('Diagnóstico red BD (temporal)') {
            when { branch 'development' }
            steps {
                sh '''
                    set +e
                    echo '=== 1. Contenedores de Postgres en el servidor ==='
                    docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' | grep -iE 'NAMES|postgres|pgvector|vector'

                    echo '=== 2. Redes de esos contenedores ==='
                    for c in $(docker ps -a --format '{{.Names}}' | grep -iE 'postgres|pgvector|vector'); do
                        echo "$c -> $(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$c")"
                    done

                    echo '=== 3. Prueba de conexión TCP desde cada tipo de red ==='
                    for red in proxy_net bridge host; do
                        echo "--- Red: $red ---"
                        docker run --rm -i --network "$red" python:3.13-slim python - <<'EOF'
import socket, time
for host, port in [("213.199.42.57", 54323), ("213.199.42.57", 54322)]:
    t = time.monotonic()
    try:
        socket.create_connection((host, port), timeout=15).close()
        r = "OK"
    except Exception as e:
        r = "FALLA " + type(e).__name__
    print(f"  {host}:{port} -> {r} ({time.monotonic() - t:.1f} s)")
EOF
                    done

                    echo '=== 4. Prueba por nombre de contenedor (red interna) ==='
                    for c in $(docker ps --format '{{.Names}}' | grep -iE 'postgres|pgvector|vector'); do
                        docker run --rm --network proxy_net python:3.13-slim python -c "import socket; socket.create_connection(('$c',5432),timeout=5); print('  $c:5432 -> OK')" 2>/dev/null || echo "  $c:5432 -> no alcanzable desde proxy_net"
                    done
                    exit 0
                '''
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
                            sleep 60
                            
                            echo '--- Ambiente dentro del contenedor ---'
                            docker compose -p "$COMPOSE_PROJECT" exec -T backend python -c "import os; nombres=['ENV_FILE','APP_ENV','CORS_ORIGINS']; [print(n + '=' + os.environ.get(n, 'NO DEFINIDA')) for n in nombres]"

                            echo '--- Estado del backend ---'
                            docker compose -p "$COMPOSE_PROJECT" ps -a

                            echo '--- Logs de arranque ---'
                            docker compose -p "$COMPOSE_PROJECT" logs --no-color --tail=200 backend
                        '''
                    }
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
