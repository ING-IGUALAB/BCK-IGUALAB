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

        /*
         * QA y UAT ejecutan pruebas.
         * Development pasa directamente al despliegue.
         */
        stage('Tests') {
            when {
                anyOf {
                    branch 'qa'
                    branch 'uat'
                }
            }

            agent {
                docker {
                    image 'python:3.11-slim'
                    reuseNode true
                }
            }

            steps {
                script {
                    def credentialId = env.BRANCH_NAME == 'qa'
                        ? 'IGUALAB_BACKEND_QA'
                        : 'IGUALAB_BACKEND_UAT'

                    withCredentials([
                        file(
                            credentialsId: credentialId,
                            variable: 'SECRET_FILE'
                        )
                    ]) {
                        sh '''
                            set -eu

                            cp "$SECRET_FILE" .env

                            python -m pip install --upgrade pip

                            pip install --no-cache-dir \
                                -r requirements.txt \
                                -r requirements-dev.txt

                            python -m pytest tests/ \
                                --cov=app \
                                --cov-report=term-missing \
                                --cov-report=xml=xml:coverage.xml
                        '''
                    }
                }
            }
        }

        /*
         * El análisis de SonarQube se ejecuta únicamente
         * después de superar las pruebas de QA o UAT.
         */
        stage('SonarQube Analysis') {
            when {
                anyOf {
                    branch 'qa'
                    branch 'uat'
                }
            }

            environment {
                scannerHome = tool 'SonarScanner'
            }

            steps {
                withSonarQubeEnv('SonarQube-Server') {
                    sh '''
                        set -eu
                        "${scannerHome}/bin/sonar-scanner"
                    '''
                }
            }
        }

        /*
         * Si el Quality Gate falla, se detiene el pipeline
         * y no se realiza el despliegue.
         */
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

        /*
         * Cada rama se mapea únicamente a su credencial y proyecto Compose.
         * Compose siempre recibe la configuración mediante el único archivo .env.
         */
        stage('Deploy and Verify') {
            when {
                anyOf {
                    branch 'development'
                    branch 'qa'
                    branch 'uat'
                }
            }

            steps {
                script {
                    def deployments = [
                        development: [credentialId: 'IGUALAB_BACKEND_DEV', project: 'igualab-backend-development'],
                        qa:          [credentialId: 'IGUALAB_BACKEND_QA',  project: 'igualab-backend-qa'],
                        uat:         [credentialId: 'IGUALAB_BACKEND_UAT', project: 'igualab-backend-uat']
                    ]
                    def deployment = deployments[env.BRANCH_NAME]

                    withCredentials([
                        file(
                            credentialsId: deployment.credentialId,
                            variable: 'SECRET_FILE'
                        )
                    ]) {
                        withEnv(["COMPOSE_PROJECT=${deployment.project}"]) {
                            sh '''
                                set -eu

                                cp "$SECRET_FILE" .env

                                docker compose -p "$COMPOSE_PROJECT" down
                                docker compose -p "$COMPOSE_PROJECT" up -d --build

                                container_id="$(docker compose -p "$COMPOSE_PROJECT" ps -q backend)"
                                if [ -z "$container_id" ]; then
                                    echo "El contenedor backend no fue creado."
                                    exit 1
                                fi

                                attempt=1
                                max_attempts=18
                                while [ "$attempt" -le "$max_attempts" ]; do
                                    container_status="$(docker inspect --format '{{.State.Status}}' "$container_id")"
                                    health_status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$container_id")"
                                    echo "Verificación ${attempt}/${max_attempts}: container=${container_status}, health=${health_status}"

                                    if [ "$health_status" = "healthy" ]; then
                                        docker compose -p "$COMPOSE_PROJECT" exec -T backend \
                                            python -c "import urllib.request; assert urllib.request.urlopen('http://localhost:8000/health', timeout=5).status == 200"
                                        echo "Despliegue validado: el backend responde 200 en /health."
                                        exit 0
                                    fi

                                    if [ "$container_status" != "running" ] || [ "$health_status" = "unhealthy" ]; then
                                        break
                                    fi

                                    attempt=$((attempt + 1))
                                    sleep 5
                                done

                                echo "El backend no alcanzó el estado healthy."
                                docker compose -p "$COMPOSE_PROJECT" ps
                                docker compose -p "$COMPOSE_PROJECT" logs --tail=100 backend
                                exit 1
                            '''
                        }
                    }
                }
            }
        }
    }

    post {
        always {
            sh '''
                rm -f .env
            '''
        }

        success {
            echo "Pipeline completado correctamente para ${env.BRANCH_NAME}"
        }

        failure {
            echo "El pipeline falló para ${env.BRANCH_NAME}"
        }
    }
}
