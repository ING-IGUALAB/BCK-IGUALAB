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
         * Development solamente realiza despliegue.
         */
        stage('Deploy Development') {
            when {
                branch 'development'
            }

            steps {
                withCredentials([
                    file(
                        credentialsId: 'IGUALAB_BACKEND_DEV',
                        variable: 'SECRET_FILE'
                    )
                ]) {
                    sh '''
                        set -eu

                        cp "$SECRET_FILE" .env.development

                        docker compose \
                            -p igualab-backend-development \
                            --env-file .env.development \
                            down

                        docker compose \
                            -p igualab-backend-development \
                            --env-file .env.development \
                            up -d --build
                    '''
                }
            }
        }

        /*
         * QA se despliega únicamente si las pruebas,
         * SonarQube y el Quality Gate finalizaron correctamente.
         */
        stage('Deploy QA') {
            when {
                branch 'qa'
            }

            steps {
                withCredentials([
                    file(
                        credentialsId: 'IGUALAB_BACKEND_QA',
                        variable: 'SECRET_FILE'
                    )
                ]) {
                    sh '''
                        set -eu

                        cp "$SECRET_FILE" .env.qa

                        docker compose \
                            -p igualab-backend-qa \
                            --env-file .env.qa \
                            down

                        docker compose \
                            -p igualab-backend-qa \
                            --env-file .env.qa \
                            up -d --build
                    '''
                }
            }
        }

        /*
         * UAT se despliega únicamente si las pruebas,
         * SonarQube y el Quality Gate finalizaron correctamente.
         */
        stage('Deploy UAT') {
            when {
                branch 'uat'
            }

            steps {
                withCredentials([
                    file(
                        credentialsId: 'IGUALAB_BACKEND_UAT',
                        variable: 'SECRET_FILE'
                    )
                ]) {
                    sh '''
                        set -eu

                        cp "$SECRET_FILE" .env.uat

                        docker compose \
                            -p igualab-backend-uat \
                            --env-file .env.uat \
                            down

                        docker compose \
                            -p igualab-backend-uat \
                            --env-file .env.uat \
                            up -d --build
                    '''
                }
            }
        }
    }

    post {
        always {
            sh '''
                rm -f \
                    .env \
                    .env.development \
                    .env.qa \
                    .env.uat
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