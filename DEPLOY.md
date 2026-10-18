# Deploying to Google Cloud

Deploys the whole system to **Cloud Run** (backend API + dashboard + Twilio
webhooks), with **Gemini on Vertex AI**, **Firestore** for storage, and **Cloud
Scheduler** driving the cron. Cloud Run gives a public HTTPS URL, so **Twilio
reaches the call webhooks directly — no ngrok in production.**

The app is already built for this: `MODEL_PROVIDER=gemini` + Vertex, and
Firestore, the same database used locally. The code needs no changes — this is all config.

> Replace `gen-lang-client-0800138646` below with your own project if different.
> Pick one region and use it everywhere (examples use `us-central1`).

---

## 0. One-time auth & APIs

The deploy CLI needs an active account (separate from the ADC you set up for local Gemini):

```bash
gcloud auth login
gcloud config set project gen-lang-client-0800138646

gcloud services enable \
  run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  aiplatform.googleapis.com firestore.googleapis.com \
  cloudscheduler.googleapis.com secretmanager.googleapis.com
```

## 1. Create the Firestore database (once)

Firestore is the only database (local and Cloud Run). Cloud Run scales to zero, and the multi-turn calls rely on shared `call_state` that any instance
can read (Firestore provides that).

```bash
gcloud firestore databases create --location=us-central1   # Native mode
```

## 2. Store secrets in Secret Manager

```bash
printf '%s' 'YOUR_TWILIO_ACCOUNT_SID' | gcloud secrets create TWILIO_ACCOUNT_SID --data-file=-
printf '%s' 'YOUR_TWILIO_AUTH_TOKEN'  | gcloud secrets create TWILIO_AUTH_TOKEN  --data-file=-
printf '%s' 'YOUR_TAVILY_API_KEY'     | gcloud secrets create TAVILY_API_KEY     --data-file=-
# A stable random secret so login sessions survive restarts:
printf '%s' "$(python -c 'import secrets;print(secrets.token_hex(32))')" \
  | gcloud secrets create SESSION_SECRET --data-file=-
```

## 3. Deploy the backend to Cloud Run (first deploy)

From the project root (where the `Dockerfile` is):

```bash
PROJECT=$(gcloud config get-value project)

gcloud run deploy vendor-backend \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --min-instances 0 \
  --cpu 1 --memory 512Mi \
  --timeout 900 \
  --set-env-vars MODEL_PROVIDER=gemini,GEMINI_MODEL=gemini-2.5-flash,GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_PROJECT=$PROJECT,GOOGLE_CLOUD_LOCATION=us-central1,TWILIO_FROM_NUMBER=+1XXXXXXXXXX,TWILIO_VOICE=Polly.Raveena,TWILIO_LANGUAGE=en-IN \
  --set-secrets TWILIO_ACCOUNT_SID=TWILIO_ACCOUNT_SID:latest,TWILIO_AUTH_TOKEN=TWILIO_AUTH_TOKEN:latest,TAVILY_API_KEY=TAVILY_API_KEY:latest,SESSION_SECRET=SESSION_SECRET:latest
```

Cloud Build builds the Dockerfile and deploys. Note the printed **Service URL**
(e.g. `https://vendor-backend-xxxx.a.run.app`).

## 4. Grant the runtime service account access to Gemini + Firestore

```bash
PROJECT=$(gcloud config get-value project)
PNUM=$(gcloud projects describe $PROJECT --format='value(projectNumber)')
SA="${PNUM}-compute@developer.gserviceaccount.com"

gcloud projects add-iam-policy-binding $PROJECT --member="serviceAccount:$SA" --role="roles/aiplatform.user"
gcloud projects add-iam-policy-binding $PROJECT --member="serviceAccount:$SA" --role="roles/datastore.user"
# Needed to read the Secret Manager secrets wired in step 3:
gcloud projects add-iam-policy-binding $PROJECT --member="serviceAccount:$SA" --role="roles/secretmanager.secretAccessor"
```

## 5. Set PUBLIC_BASE_URL to the service's own URL and redeploy env

The Twilio call webhooks (`/twiml/call`, `/twiml/turn`) must point at the live URL:

```bash
URL=$(gcloud run services describe vendor-backend --region us-central1 --format='value(status.url)')
gcloud run services update vendor-backend --region us-central1 --update-env-vars PUBLIC_BASE_URL=$URL
```

That's it for Twilio — it now fetches TwiML from your Cloud Run URL (no ngrok).

## 6. Cron via Cloud Scheduler

```bash
URL=$(gcloud run services describe vendor-backend --region us-central1 --format='value(status.url)')
gcloud scheduler jobs create http vendor-cron \
  --location us-central1 \
  --schedule "*/2 * * * *" \
  --uri "${URL}/internal/cron/sweep" \
  --http-method POST \
  --attempt-deadline 900s
```

(A sweep every 2 minutes; it runs synchronously so it completes before the instance
scales down. Lock this endpoint with an OIDC token for production.)

## 7. Verify

```bash
URL=$(gcloud run services describe vendor-backend --region us-central1 --format='value(status.url)')
curl -s -o /dev/null -w "dashboard %{http_code}\n" $URL/           # 200
curl -s "$URL/twiml/call?token=t" | head -c 40                     # <?xml ... (TwiML)
open $URL                                                          # sign up / log in, create a request
```

Create a request in the dashboard → Cloud Scheduler triggers the engine on Gemini →
vendors are discovered and **called from Cloud Run via Twilio** (multi-turn) → ranked
result appears. All billed to your project (covered by your $300 while it lasts).

---

## Config reference (Cloud Run env)

| Var | Value | Notes |
|-----|-------|-------|
| `MODEL_PROVIDER` | `gemini` | |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Vertex model id |
| `GOOGLE_GENAI_USE_VERTEXAI` | `TRUE` | use Vertex (service-account auth, no key) |
| `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION` | your project / `us-central1` | |
| `PUBLIC_BASE_URL` | the Cloud Run service URL | set after first deploy (step 5) |
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | secrets | Secret Manager |
| `TWILIO_FROM_NUMBER` | your Twilio number | |
| `TWILIO_VOICE` / `TWILIO_LANGUAGE` | `Polly.Raveena` / `en-IN` | |
| `CALL_OVERRIDE_NUMBER` | optional | test routing (comma-list supported) |
| `TAVILY_API_KEY` | secret | live vendor discovery |
| `SESSION_SECRET` | secret | stable login sessions |

## Notes & caveats

- **Twilio trial**: can only call **verified** numbers and allows limited concurrency.
  For a real demo calling arbitrary restaurants, upgrade the Twilio account.
- **Cost control**: `--min-instances 0` (scale to zero), Gemini **Flash**, Firestore
  free tier. Set a Billing budget alert. Don't leave `--min-instances > 0`.
- **Redeploys**: `gcloud run deploy vendor-backend --source . --region us-central1`
  (env/secrets persist unless you change them).
- **Logs**: `gcloud run services logs read vendor-backend --region us-central1`.
