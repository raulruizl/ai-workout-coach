#!/usr/bin/env bash
# Usage: ./deploy.sh <amplify-app-id>
# Run from repo root after terraform apply.
set -euo pipefail

APP_ID="${1:?usage: ./deploy.sh <amplify-app-id>}"
BRANCH="main"
REGION="${AWS_DEFAULT_REGION:-eu-west-1}"
FRONTEND_DIR="$(dirname "$0")"

echo "Building frontend..."
cd "$FRONTEND_DIR"
npm ci
npm run build

echo "Creating Amplify deployment..."
DEPLOY=$(aws amplify create-deployment \
  --app-id "$APP_ID" \
  --branch-name "$BRANCH" \
  --region "$REGION" \
  --output json)

JOB_ID=$(echo "$DEPLOY" | python3 -c "import sys,json; print(json.load(sys.stdin)['jobId'])")
UPLOAD_URL=$(echo "$DEPLOY" | python3 -c "import sys,json; print(json.load(sys.stdin)['zipUploadUrl'])")

echo "Zipping dist/..."
python3 -c "
import zipfile, os
with zipfile.ZipFile('/tmp/amplify-deploy.zip', 'w', zipfile.ZIP_DEFLATED) as z:
    for root, dirs, files in os.walk('dist'):
        for f in files:
            path = os.path.join(root, f)
            z.write(path, os.path.relpath(path, 'dist'))
"

echo "Uploading..."
curl -s -X PUT -T /tmp/amplify-deploy.zip "$UPLOAD_URL"

echo "Starting deployment (job: $JOB_ID)..."
aws amplify start-deployment \
  --app-id "$APP_ID" \
  --branch-name "$BRANCH" \
  --job-id "$JOB_ID" \
  --region "$REGION" \
  --output json | python3 -c "import sys,json; j=json.load(sys.stdin)['jobSummary']; print(f'status: {j[\"status\"]}  id: {j[\"jobId\"]}')"

echo "Done. Visit: https://main.$(aws amplify get-app --app-id $APP_ID --region $REGION --query 'app.defaultDomain' --output text)"
