# Deploying to Google Cloud (AI Builder Cup)

Deploys the backend (API + dashboard + Twilio webhooks) to **Cloud Run**, runs the
cron via **Cloud Scheduler**, and runs the agents on **Gemini**.

Deploying to Cloud Run gives a **public HTTPS URL**, which also removes the ngrok
requirement — Twilio calls that URL directly.

## Doing this for ~$0 (no event credits needed)

The event is **not** giving Cloud credits, but a hackathon-sized deployment fits in
Google's free options. Two ways to run the models — pick one:

| | **Option A: $300 free trial + Vertex AI** | **Option B: pure free tier + AI Studio** |
|---|---|---|
| Model | Gemini Flash on **Vertex AI** | Gemini Flash via **AI Studio free key** |
| Cost | $0 (drawn from the 90-day $300 trial) | $0 (free tier) |
| Limits | No tight rate limits | ~10 req/min, ~1,500/day; data may be used for training |
| Auth | Service account (no key) | `GOOGLE_API_KEY` |
| Best for | Smooth demo/judging | Zero-signup-friction, but throttled |

Both deploy the **app** to Cloud Run (which satisfies "deployed on Google Cloud").
**Option A is recommended** — the $300 trial is a standard new-account offer (not event
credits), covers Vertex + Cloud Run easily, and never auto-charges unless you manually
upgrade. Set a budget alert (step 8) and you're safe.

**Keeping Cloud Run free:** deploy with `--min-instances 0` (scale to zero — you only
pay while handling a request, and the free tier covers 2M requests/mo). The cron runs
synchronously on each Cloud Scheduler hit, so no always-on instance is needed.

**Free-tier model mitigations (Option B):** the engine makes several model calls per
request (discovery + ranking) plus parallel vendor calls, which can burst past 10/min.
To stay under the limit, lower `DEFAULT_VENDOR_LIMIT` to 2 in `engine_agent/db.py` and
set `GEMINI_MODEL=gemini-flash-lite-latest`. Option A avoids this entirely.

> The app is coded so models switch with one env var (`MODEL_PROVIDER=gemini`), and
> the backend binds to Cloud Run's `$PORT` (see the Dockerfile). The one code change
> still needed for a durable deployment is the database — see step 6.

---

## 0. Prerequisites

```bash
# Install the gcloud CLI, then:
gcloud auth login
gcloud projects create vendor-mgmt-aibc      # or use an existing project
gcloud config set project vendor-mgmt-aibc
# Link billing (apply your AI Builder Cup credits in the Cloud Console → Billing)
```

Enable the APIs used:

```bash
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  aiplatform.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  firestore.googleapis.com
```

---

## 1. Models → Gemini on Vertex AI

On Cloud Run, Vertex AI authenticates via the service account — **no API key needed**.
These env vars (set in step 4) do it:

```
MODEL_PROVIDER=gemini
GEMINI_MODEL=gemini-3.6-flash          # or another current Gemini Flash model
GOOGLE_GENAI_USE_VERTEXAI=TRUE
GOOGLE_CLOUD_PROJECT=vendor-mgmt-aibc
GOOGLE_CLOUD_LOCATION=us-central1
```

Grant the Cloud Run service account Vertex access (after first deploy, or use the
default compute SA):

```bash
PROJECT=$(gcloud config get-value project)
PROJNUM=$(gcloud projects describe $PROJECT --format='value(projectNumber)')
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:${PROJNUM}-compute@developer.gserviceaccount.com" \
  --role="roles/aiplatform.user"
```

Local testing with Gemini instead of Vertex: set `MODEL_PROVIDER=gemini` and
`GOOGLE_API_KEY=<AI Studio key>` in `.env` (free tier is Flash-only).

---

## 2. Secrets (Twilio, Tavily)

Store secrets in Secret Manager (preferred) or pass as env vars in step 4:

```bash
printf '%s' 'YOUR_TWILIO_SID'   | gcloud secrets create TWILIO_ACCOUNT_SID --data-file=-
printf '%s' 'YOUR_TWILIO_TOKEN' | gcloud secrets create TWILIO_AUTH_TOKEN  --data-file=-
printf '%s' 'YOUR_TAVILY_KEY'   | gcloud secrets create TAVILY_API_KEY     --data-file=-
```

---

## 3. (first) Deploy the backend

From the project root (where the Dockerfile is):

```bash
gcloud run deploy vendor-backend \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --min-instances 0 \
  --cpu 1 --memory 512Mi \
  --set-env-vars DB_BACKEND=firestore,MODEL_PROVIDER=gemini,GEMINI_MODEL=gemini-3.6-flash,GOOGLE_GENAI_USE_VERTEXAI=TRUE,GOOGLE_CLOUD_PROJECT=$(gcloud config get-value project),GOOGLE_CLOUD_LOCATION=us-central1,TWILIO_FROM_NUMBER=+1XXXXXXXXXX,TWILIO_VOICE=Polly.Raveena,TWILIO_LANGUAGE=en-IN,CALL_OVERRIDE_NUMBER=+91XXXXXXXXXX \
  --set-secrets TWILIO_ACCOUNT_SID=TWILIO_ACCOUNT_SID:latest,TWILIO_AUTH_TOKEN=TWILIO_AUTH_TOKEN:latest,TAVILY_API_KEY=TAVILY_API_KEY:latest
```

Note the service URL it prints, e.g. `https://vendor-backend-xxxx.a.run.app`.

---

## 4. Set the public URL and redeploy

The TwiML webhooks need to know their own public address:

```bash
URL=$(gcloud run services describe vendor-backend --region us-central1 --format='value(status.url)')
gcloud run services update vendor-backend --region us-central1 \
  --update-env-vars PUBLIC_BASE_URL=$URL
```

Then point **Twilio** at it (no ngrok): in the Twilio console, or because this app
builds the call URL from `PUBLIC_BASE_URL`, nothing else is needed — Twilio will fetch
`${URL}/twiml/call`.

---

## 5. Cron via Cloud Scheduler

```bash
URL=$(gcloud run services describe vendor-backend --region us-central1 --format='value(status.url)')
gcloud scheduler jobs create http vendor-cron \
  --location us-central1 \
  --schedule "*/2 * * * *" \
  --uri "${URL}/internal/cron/sweep" \
  --http-method POST \
  --attempt-deadline 600s
```

(A sweep every 2 minutes; the endpoint runs it synchronously so it completes before the
instance scales to zero. `--attempt-deadline 600s` gives the engine time to finish. Lock
the endpoint down with an OIDC token for production.)

---

## 6. Database — Firestore (already wired)

The app supports two storage backends behind one env var (`DB_BACKEND`):
`sqlite` (local dev, default) and `firestore` (serverless, Google-native, persists
across Cloud Run instances). Firestore is already implemented
(`engine_agent/db_firestore.py`) on the 5 collections — no code change needed, just:

```bash
# Create the Firestore database once (Native mode)
gcloud firestore databases create --location=nam5    # or your region

# Grant the Cloud Run service account access
gcloud projects add-iam-policy-binding $(gcloud config get-value project) \
  --member="serviceAccount:$(gcloud projects describe $(gcloud config get-value project) \
     --format='value(projectNumber)')-compute@developer.gserviceaccount.com" \
  --role="roles/datastore.user"
```

Then set `DB_BACKEND=firestore` on the service (included in the deploy command below).
On Cloud Run it authenticates via the service account — no key.

Local testing with the Firestore emulator: `DB_BACKEND=firestore` +
`FIRESTORE_EMULATOR_HOST=localhost:8080`.

---

## 7. Stay free — budget guardrail

Create a budget with an alert so nothing surprises you (and keep the account on the
free trial — do **not** click "Activate full account" / upgrade, so usage can never be
auto-charged beyond the $300):

```bash
# Console → Billing → Budgets & alerts → Create budget: amount $1, alert at 50/90/100%.
```

Keep costs at ~$0: `--min-instances 0` (already set), Gemini **Flash** (not Pro),
Firestore free tier, and no GPUs. Twilio (trial credit) and Tavily (free dev key) are
billed by those providers, not Google.

## 8. Verify

```bash
URL=$(gcloud run services describe vendor-backend --region us-central1 --format='value(status.url)')
curl -s -o /dev/null -w "dashboard %{http_code}\n" $URL/
curl -s "$URL/twiml/call?token=t&script=hi" | head -c 40      # expect XML
open $URL                                                     # the live dashboard
```

Create a request on the dashboard → Cloud Scheduler triggers the engine on Gemini →
Twilio places the real call → ranked result appears. No local processes, no ngrok.
