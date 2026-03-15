# 👻 QA Ghost — AI-Powered Web QA Agent

> **Google Gemini Live Agent Hackathon 2026 — UI Navigator Category**

QA Ghost is an autonomous AI QA engineer powered by Gemini 2.5 Flash. Paste a URL and the agent navigates your website, detects visual bugs, runs WCAG 2.1 accessibility audits, measures Core Web Vitals, tests across Desktop, Tablet and Mobile, records the full browser session, and auto-fixes bugs with self-healing JavaScript — all in one scan.

**Live Demo:** https://qa-ghost-999765718937.us-central1.run.app  
**Demo Video:** [YouTube](https://youtube.com)

---

## Features

- **Gemini Agentic Navigation** — Gemini 2.5 Flash analyzes screenshots and autonomously clicks, scrolls, and navigates your website
- **Visual Bug Detection** — Gemini Vision detects layout issues, truncated text, overlapping elements, and UI inconsistencies
- **Self-Healing Engine** — Automatically generates and injects JavaScript fixes, captures Before/After/Pixel Diff comparisons
- **WCAG 2.1 Accessibility Audit** — axe-core checks for Critical, Serious, Moderate, and Minor violations
- **Core Web Vitals** — Real LCP, CLS, TBT, and TTFB measurements
- **Multi-Viewport Testing** — Desktop (1280px), Tablet (768px), Mobile (375px)
- **Session Recording** — Full browser session recorded as MP4 video
- **AI Voice Summary** — Gemini TTS with Kore voice narrates the full QA report
- **Health Score** — Weighted score across Visual Quality, Accessibility, Performance, and SEO
- **PDF Export** — Professional downloadable report

---

## Tech Stack

| Layer | Technology |
|---|---|
| AI Model | Gemini 2.5 Flash (Vision + Text + TTS) |
| Agent Framework | Google ADK (qa_agent.py) |
| Browser Automation | Playwright (Python) |
| Backend | FastAPI + Uvicorn |
| Accessibility | axe-core WCAG 2.1 |
| Screenshot Processing | Pillow |
| Video Conversion | imageio-ffmpeg |
| Deployment | Google Cloud Run |

---

## Local Setup & Reproducible Testing

### Prerequisites

- Python 3.11+
- Google Gemini API Key (get one at [aistudio.google.com](https://aistudio.google.com))

### Installation

```bash
# Clone the repository
git clone https://github.com/Pratham200320/qa-ghost.git
cd qa-ghost

# Create virtual environment
python -m venv venv

# Activate virtual environment
# Windows:
venv\Scripts\activate
# Mac/Linux:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Install Playwright browser
playwright install chromium
```

### Configuration

Create a `.env` file in the root directory:

```
GEMINI_API_KEY=your_gemini_api_key_here
```

### Run the Application

```bash
python app.py
```

Open your browser at `http://localhost:8000`

### Run a Test Scan

1. Open `http://localhost:8000`
2. Paste `https://books.toscrape.com` in the URL field
3. Click **Run Audit**
4. Wait ~2 minutes for the full scan to complete
5. Review the results including Health Score, Agent Journey, Voice Report, Self-Healing, WCAG Audit, Core Web Vitals, and Multi-Viewport screenshots

---

## Google Cloud Deployment

The application is deployed on Google Cloud Run. To deploy your own instance:

```bash
# Authenticate with Google Cloud
gcloud auth login

# Set your project
gcloud config set project YOUR_PROJECT_ID

# Enable required services
gcloud services enable run.googleapis.com cloudbuild.googleapis.com

# Deploy to Cloud Run
gcloud run deploy qa-ghost \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --memory 2Gi \
  --cpu 2 \
  --timeout 300 \
  --set-env-vars GEMINI_API_KEY=YOUR_GEMINI_API_KEY \
  --port 8080
```

---

## Project Structure

```
qa-ghost/
├── agent.py          # Main QA agent with Playwright + Gemini Vision
├── app.py            # FastAPI backend with SSE streaming
├── qa_agent.py       # Google ADK agent definition
├── templates/
│   └── index.html    # Frontend UI
├── Dockerfile        # Container configuration
├── requirements.txt  # Python dependencies
└── DEPLOY.md         # Deployment instructions
```

---

## Architecture

The system flow:
1. User submits a URL via the web interface
2. FastAPI backend triggers the Google ADK agent
3. ADK agent orchestrates the QA pipeline using 4 tools
4. Playwright opens a real browser and captures screenshots
5. Gemini 2.5 Flash analyzes screenshots for bugs and navigation decisions
6. axe-core runs WCAG 2.1 accessibility audit
7. Self-healing engine generates and injects JavaScript fixes
8. Gemini TTS generates a voice summary using Kore voice
9. Results stream back to the frontend via Server-Sent Events
10. Full report with health score, bugs, WCAG, CWV, video, and PDF export

---

## Team

- **Pratham Sajnani** — [GitHub](https://github.com/Pratham200320)
- **Rohan Kumar Singh**

---

Built for the **Google Gemini Live Agent Hackathon 2026**  
`#GeminiLiveAgentChallenge`
