#!/usr/bin/env bash
# Tek seferlik GCP + Secret Manager + Artifact Registry + Cloud Run + Scheduler kurulumu.
# Kullanim:
#   export GCP_PROJECT_ID=your-project
#   export GCP_REGION=europe-west3
#   export SERVICE=abd-erstellung-sync
#   export SYNC_URL_PATH=/sync
#   bash scripts/setup_gcp.sh
#
# Sonra secret degerlerini Secret Manager'a yaz (asagidaki create-secret komutlari).

set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:?GCP_PROJECT_ID gerekli}"
REGION="${GCP_REGION:-europe-west3}"
SERVICE="${SERVICE:-abd-erstellung-sync}"
REPO="${ARTIFACT_REPO:-abd-erstellung}"
SCHEDULER_JOB="${SCHEDULER_JOB:-abd-erstellung-daily}"
# Europe/Berlin sabah 07:00
SCHEDULE="${SCHEDULE:-0 7 * * *}"
TIME_ZONE="${TIME_ZONE:-Europe/Berlin}"

gcloud config set project "$PROJECT_ID"

echo "==> API'ler aciliyor"
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  cloudscheduler.googleapis.com \
  cloudbuild.googleapis.com

echo "==> Artifact Registry repo"
gcloud artifacts repositories describe "$REPO" --location="$REGION" >/dev/null 2>&1 \
  || gcloud artifacts repositories create "$REPO" \
       --repository-format=docker \
       --location="$REGION" \
       --description="ABD Erstellung images"

SECRETS=(
  ZENDESK_SUBDOMAIN
  ZENDESK_EMAIL
  ZENDESK_API_TOKEN
  Teams_ClientID
  Teams_SECRETKey
  AZURE_TENANT_ID
  Teams_Teams_ID
  Teams_Channels_ID
  Teams_Drive_ID
  Teams_Folder_ID
  SYNC_API_KEY
)

echo "==> Secret Manager secret isimleri (deger yoksa placeholder)"
for name in "${SECRETS[@]}"; do
  if ! gcloud secrets describe "$name" >/dev/null 2>&1; then
    echo -n "PLACEHOLDER" | gcloud secrets create "$name" --data-file=-
    echo "  created: $name (PLACEHOLDER - guncelle!)"
  else
    echo "  exists: $name"
  fi
done

echo ""
echo "Secret degerlerini guncellemek icin ornek:"
echo "  echo -n 'aerosus' | gcloud secrets versions add ZENDESK_SUBDOMAIN --data-file=-"
echo ""
echo "Image build/deploy icin:"
echo "  IMAGE=${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE}:manual"
echo "  docker build -t \"\$IMAGE\" ."
echo "  gcloud auth configure-docker ${REGION}-docker.pkg.dev"
echo "  docker push \"\$IMAGE\""
echo "  gcloud run deploy ${SERVICE} --image \"\$IMAGE\" --region ${REGION} \\"
echo "    --allow-unauthenticated --memory 1Gi --cpu 1 --timeout 3600 --concurrency 1 --max-instances 1 \\"
echo "    --set-secrets=ZENDESK_SUBDOMAIN=ZENDESK_SUBDOMAIN:latest,ZENDESK_EMAIL=ZENDESK_EMAIL:latest,ZENDESK_API_TOKEN=ZENDESK_API_TOKEN:latest,Teams_ClientID=Teams_ClientID:latest,Teams_SECRETKey=Teams_SECRETKey:latest,AZURE_TENANT_ID=AZURE_TENANT_ID:latest,Teams_Teams_ID=Teams_Teams_ID:latest,Teams_Channels_ID=Teams_Channels_ID:latest,Teams_Drive_ID=Teams_Drive_ID:latest,Teams_Folder_ID=Teams_Folder_ID:latest,SYNC_API_KEY=SYNC_API_KEY:latest"
echo ""
echo "Scheduler (deploy sonrasi SERVICE_URL ile):"
echo "  SERVICE_URL=\$(gcloud run services describe ${SERVICE} --region ${REGION} --format='value(status.url)')"
echo "  gcloud scheduler jobs create http ${SCHEDULER_JOB} \\"
echo "    --location ${REGION} \\"
echo "    --schedule \"${SCHEDULE}\" \\"
echo "    --time-zone \"${TIME_ZONE}\" \\"
echo "    --uri \"\${SERVICE_URL}/sync\" \\"
echo "    --http-method POST \\"
echo "    --headers \"X-API-Key=YOUR_SYNC_API_KEY\" \\"
echo "    --attempt-deadline 1800s"
