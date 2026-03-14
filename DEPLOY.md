# QA Ghost — Deployment Guide

## Step 1: GitHub

```bash
cd qa-ghost-v2
git init
git add .
git commit -m "QA Ghost v2 — Gemini agentic QA platform"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/qa-ghost.git
git push -u origin main
```

Make the repo **public** on GitHub (required for hackathon).

---

## Step 2: Google Cloud Run

### 2a. Install gcloud CLI
Download from: https://cloud.google.com/sdk/docs/install

### 2b. Login and set project
```bash
gcloud auth login
gcloud projects create qa-ghost-2026 --name="QA Ghost"
gcloud config set project qa-ghost-2026
gcloud billing accounts list
gcloud billing projects link qa-ghost-2026 --billing-account=YOUR_BILLING_ID
```

### 2c. Enable APIs
```bash
gcloud services enable run.googleapis.com
gcloud services enable cloudbuild.googleapis.com
gcloud services enable containerregistry.googleapis.com
```

### 2d. Build and deploy
```bash
# Set your Gemini API key as a secret
gcloud run deploy qa-ghost \
  --source . \
  --region us-central1 \
  --platform managed \
  --allow-unauthenticated \
  --memory 2Gi \
  --cpu 2 \
  --timeout 300 \
  --set-env-vars GEMINI_API_KEY=YOUR_GEMINI_KEY_HERE \
  --port 8080
```

This will:
1. Build the Docker image via Cloud Build
2. Push to Container Registry
3. Deploy to Cloud Run
4. Give you a live HTTPS URL like: https://qa-ghost-xxxx-uc.a.run.app

### 2e. Test it
```bash
curl https://qa-ghost-xxxx-uc.a.run.app/health
# Should return: {"status":"ok","service":"qa-ghost"}
```

---

## Step 3: Devpost Submission Checklist

- [ ] Public GitHub repo URL
- [ ] Cloud Run live URL
- [ ] Demo video < 4 minutes (use books.toscrape.com)
- [ ] Architecture diagram (qa-ghost-architecture.svg)
- [ ] Project description mentioning: Gemini 2.5 Flash, ADK, Cloud Run, Playwright

---

## Local development

```bash
# Create .env file
echo "GEMINI_API_KEY=your_key_here" > .env

# Install deps
pip install -r requirements.txt
playwright install chromium

# Run
python app.py
# Open http://localhost:8000
```
