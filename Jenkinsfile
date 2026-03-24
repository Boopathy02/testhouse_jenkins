pipeline {
  agent any

  options {
    timestamps()
    disableConcurrentBuilds()
  }

  triggers {
    githubPush()
  }

  parameters {
    booleanParam(name: 'NO_CACHE', defaultValue: false, description: 'Disable Docker build cache')
    string(name: 'ENV_FILE_CRED_ID', defaultValue: '', description: 'Jenkins File Credential ID for .env (optional)')
  }

  environment {
    COMPOSE_FILE = 'docker-compose.yaml'
    PROJECT_NAME = 'testify-automator'
    ENV_FILE = '.env'
  }

  stages {
    stage('Checkout') {
      steps {
        checkout scm
      }
    }

    stage('Prepare .env') {
      steps {
        script {
          if (params.ENV_FILE_CRED_ID?.trim()) {
            withCredentials([file(credentialsId: params.ENV_FILE_CRED_ID, variable: 'ENV_FILE_PATH')]) {
              env.ENV_FILE = env.ENV_FILE_PATH
            }
          }
        }
        sh '''
          set -eu
          if [ ! -f "$ENV_FILE" ]; then
            echo "Env file not found at: $ENV_FILE" >&2
            echo "Provide .env in the workspace or set ENV_FILE_CRED_ID to a Secret file." >&2
            exit 1
          fi

          required="DATABASE_URL APP_ENV BACKEND_HOST BACKEND_PORT CHROMA_DB_HOST CHROMA_DB_PORT JWT_SECRET SESSION_SECRET API_KEY REACT_APP_API_URL OPENAI_API_KEY"
          for key in $required; do
            if ! grep -Eq "^${key}=" "$ENV_FILE"; then
              echo "Missing or malformed env var: ${key}" >&2
              exit 1
            fi
          done
        '''
      }
    }

    stage('Validate Compose') {
      steps {
        sh '''
          set -eu
          if docker compose version >/dev/null 2>&1; then
            COMPOSE_CMD="docker compose"
          elif docker-compose version >/dev/null 2>&1; then
            COMPOSE_CMD="docker-compose"
          else
            echo "Docker Compose not found" >&2
            exit 1
          fi
          $COMPOSE_CMD -f "$COMPOSE_FILE" config >/dev/null
        '''
      }
    }

    stage('Stop Existing Containers') {
      steps {
        sh '''
          set -eu
          if docker compose version >/dev/null 2>&1; then
            COMPOSE_CMD="docker compose"
          else
            COMPOSE_CMD="docker-compose"
          fi
          $COMPOSE_CMD -p "$PROJECT_NAME" -f "$COMPOSE_FILE" down --remove-orphans || true
        '''
      }
    }

    stage('Build Images') {
      steps {
        sh '''
          set -eu
          if docker compose version >/dev/null 2>&1; then
            COMPOSE_CMD="docker compose"
          else
            COMPOSE_CMD="docker-compose"
          fi

          if [ "${NO_CACHE}" = "true" ]; then
            $COMPOSE_CMD -p "$PROJECT_NAME" -f "$COMPOSE_FILE" build --no-cache --pull
          else
            $COMPOSE_CMD -p "$PROJECT_NAME" -f "$COMPOSE_FILE" build --pull
          fi
        '''
      }
    }

    stage('Start Containers') {
      steps {
        sh '''
          set -eu
          if docker compose version >/dev/null 2>&1; then
            COMPOSE_CMD="docker compose"
          else
            COMPOSE_CMD="docker-compose"
          fi
          $COMPOSE_CMD -p "$PROJECT_NAME" -f "$COMPOSE_FILE" up -d --remove-orphans
        '''
      }
    }
  }

  post {
    always {
      sh '''
        if docker compose version >/dev/null 2>&1; then
          COMPOSE_CMD="docker compose"
        elif docker-compose version >/dev/null 2>&1; then
          COMPOSE_CMD="docker-compose"
        else
          exit 0
        fi
        $COMPOSE_CMD -p "$PROJECT_NAME" -f "$COMPOSE_FILE" ps
      '''
    }
  }
}
