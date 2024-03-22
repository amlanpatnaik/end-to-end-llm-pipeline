#!/bin/bash

# Stop script on any error
set -o errexit -o pipefail -o noclobber

# AWS ECR configuration
AWS_CURRENT_REGION_ID=$(aws configure get region)
AWS_CURRENT_ACCOUNT_ID=$(aws sts get-caller-identity --query "Account" --output text)
ECR_REPOSITORY_NAME="ecr-repository-name"
FULL_ECR_URI="$AWS_ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com/$ECR_REPOSITORY_NAME"

# Docker image configuration
LOCAL_IMAGE_NAME="github-crawler" # Replace with your local image name
IMAGE_TAG="github-crawler-latest"

# Get ECR login password and authenticate Docker with ECR
echo "Logging into AWS ECR..."
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $AWS_ACCOUNT_ID.dkr.ecr.$REGION.amazonaws.com
# Check if Docker is running
if ! [ -x "$(command -v docker)" ]; then
  echo 'Error: Docker is not installed or not running.' >&2
  exit 1
fi

# Build the Docker image
echo "Building Docker image..."
docker build -t $LOCAL_IMAGE_NAME -f docker/Dockerfile .

# Tag the Docker image for the ECR repository
echo "Tagging Docker image..."
docker tag $LOCAL_IMAGE_NAME $FULL_ECR_URI:$IMAGE_TAG

# Push the image to ECR
echo "Pushing the Docker image to AWS ECR..."
docker push $FULL_ECR_URI:$IMAGE_TAG

echo "Deployment completed successfully."
