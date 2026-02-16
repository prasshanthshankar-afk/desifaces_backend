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

        stage('Deploy and Build on Azure VM') {
            steps {
                sshagent(credentials: ['deploy-vm-ssh']) {
                    sh """
                        rsync -avz --delete \
                            --exclude '.git' \
                            -e 'ssh -o StrictHostKeyChecking=no' \
                            . ${DEPLOY_USER}@${DEPLOY_HOST}:/opt/desifaces/
                    """
                    sh """
                        ssh -o StrictHostKeyChecking=no ${DEPLOY_USER}@${DEPLOY_HOST} '
                            cd /opt/desifaces
                            docker compose --env-file ./infra/.env down || true
                            docker compose --env-file ./infra/.env build
                            docker compose --env-file ./infra/.env up -d
                            docker compose --env-file ./infra/.env ps
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
