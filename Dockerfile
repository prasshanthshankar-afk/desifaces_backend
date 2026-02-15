pipeline {
    agent any

    environment {
        DEPLOY_HOST = '52.252.188.211'
        DEPLOY_USER = 'azureuser'
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
            }
        }

        stage('Build All Services') {
            steps {
                sh 'docker compose build'
            }
        }

        stage('Deploy to Azure VM') {
            steps {
                sshagent(credentials: ['deploy-vm-ssh']) {
                    sh """
                        rsync -avz --delete \
                            -e 'ssh -o StrictHostKeyChecking=no' \
                            . ${DEPLOY_USER}@${DEPLOY_HOST}:/opt/desifaces/
                    """
                    sh """
                        ssh -o StrictHostKeyChecking=no ${DEPLOY_USER}@${DEPLOY_HOST} '
                            cd /opt/desifaces
                            docker compose down
                            docker compose build
                            docker compose up -d
                            docker compose ps
                        '
                    """
                }
            }
        }
    }

    post {
        always {
            cleanWs()
        }
    }
}