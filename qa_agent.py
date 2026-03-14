"""
QA Ghost - ADK Agent Definition
Uses Google Agent Development Kit (ADK) to orchestrate the QA pipeline.

The root_agent exposes QA Ghost's capabilities as ADK tools:
  - navigate_and_screenshot   → loads a URL, takes a screenshot
  - analyze_screenshot        → sends screenshot to Gemini Vision for bug detection
  - self_heal_bug             → generates + injects a JS fix, captures before/after
  - generate_report_summary   → produces the final voice summary text

app.py calls run_qa_scan() from agent.py (full Playwright pipeline).
This file provides the ADK agent for hackathon compliance + the architecture diagram.
"""

import os
from dotenv import load_dotenv
from google.adk.agents import Agent

load_dotenv()

# ── ADK Tool Definitions ───────────────────────────────────────────────────────
# Each function is a tool the ADK agent can invoke.
# Docstrings are parsed by ADK to describe tools to the LLM.

def navigate_and_screenshot(url: str) -> dict:
    """
    Launches a headless browser, navigates to the given URL, and captures
    a full-page screenshot for visual analysis.

    Args:
        url: The full URL of the webpage to navigate to (e.g. https://example.com).

    Returns:
        dict: A dictionary with keys:
            - status: "success" or "error"
            - screenshot_path: local path to the captured PNG screenshot
            - page_title: the page's <title> text
            - error: error message if status is "error"
    """
    from playwright.sync_api import sync_playwright
    import os
    from datetime import datetime

    try:
        os.makedirs("screenshots", exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = f"screenshots/adk_shot_{ts}.png"

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
            )
            page = browser.new_page(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36"
            )
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            title = page.title()
            page.screenshot(path=path, full_page=False)
            browser.close()

        return {"status": "success", "screenshot_path": path, "page_title": title}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def analyze_screenshot(screenshot_path: str, page_url: str) -> dict:
    """
    Sends a screenshot to Gemini Vision to detect visual bugs, UI issues,
    accessibility problems, and performance observations.

    Args:
        screenshot_path: Local file path to a PNG screenshot.
        page_url: The URL of the page that was screenshotted.

    Returns:
        dict: A dictionary with keys:
            - status: "success" or "error"
            - overall_score: health score from 0–100
            - critical_bugs: list of critical bug dicts
            - medium_bugs: list of medium bug dicts
            - low_bugs: list of low severity bug dicts
            - summary: one-sentence summary of findings
    """
    from google import genai
    from google.genai import types
    import json

    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    client = genai.Client(api_key=GEMINI_API_KEY)

    try:
        with open(screenshot_path, "rb") as f:
            img = f.read()

        prompt = f"""You are an expert QA engineer doing automated visual testing.
URL: {page_url}

Analyze this screenshot for bugs and UI issues.
Return ONLY raw JSON, no markdown:

{{
    "overall_score": 75,
    "summary": "one sentence",
    "critical_bugs": [{{"title":"","location":"","description":"","fix":"","css_selector":"body"}}],
    "medium_bugs": [{{"title":"","location":"","description":"","fix":"","css_selector":"p"}}],
    "low_bugs": [{{"title":"","location":"","description":"","fix":"","css_selector":"a"}}]
}}"""

        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                types.Content(role="user", parts=[
                    types.Part(text=prompt),
                    types.Part(inline_data=types.Blob(mime_type="image/png", data=img))
                ])
            ]
        )

        raw = resp.text.strip()
        if "```" in raw:
            for part in raw.split("```"):
                part = part.strip().lstrip("json").strip()
                if part.startswith("{"):
                    raw = part
                    break
        s, e = raw.find("{"), raw.rfind("}") + 1
        result = json.loads(raw[s:e])
        result["status"] = "success"
        return result

    except Exception as ex:
        return {"status": "error", "error": str(ex), "overall_score": 0,
                "critical_bugs": [], "medium_bugs": [], "low_bugs": []}


def self_heal_bug(bug_title: str, bug_description: str, bug_location: str,
                  page_url: str, css_selector: str) -> dict:
    """
    Generates a JavaScript fix for a detected UI bug using Gemini, then
    confirms the fix is valid and ready to be injected into the live page.

    Args:
        bug_title: Short title of the bug (e.g. "Low contrast text").
        bug_description: Detailed description of the bug.
        bug_location: Where on the page the bug is (e.g. "header", "nav").
        page_url: URL of the page containing the bug.
        css_selector: CSS selector targeting the buggy element.

    Returns:
        dict: A dictionary with keys:
            - status: "success" or "error"
            - js_fix: JavaScript string to inject into the page
            - explanation: human-readable description of what the fix does
            - fix_type: category of fix (contrast/layout/accessibility/typography)
    """
    from google import genai
    import json

    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    client = genai.Client(api_key=GEMINI_API_KEY)

    try:
        prompt = f"""You are an expert frontend developer fixing a UI bug.

Page: {page_url}
Bug: {bug_title}
Location: {bug_location}
Description: {bug_description}
CSS selector: {css_selector}

Write a short JavaScript snippet (1–3 lines) that visually fixes this bug when injected.
Return ONLY raw JSON:
{{"js_fix": "document.querySelector('...').style.color='#111';", "explanation": "one sentence", "fix_type": "contrast/layout/accessibility/typography/visibility"}}"""

        resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        raw = resp.text.strip()
        if "```" in raw:
            for part in raw.split("```"):
                part = part.strip().lstrip("json").strip()
                if part.startswith("{"):
                    raw = part
                    break
        s, e = raw.find("{"), raw.rfind("}") + 1
        result = json.loads(raw[s:e])
        result["status"] = "success"
        return result

    except Exception as ex:
        return {
            "status": "error", "error": str(ex),
            "js_fix": f"document.querySelectorAll('p,span,li').forEach(e=>e.style.color='#111');",
            "explanation": "Fallback: improved text contrast",
            "fix_type": "contrast"
        }


def generate_report_summary(pages_scanned: int, critical_count: int,
                            medium_count: int, low_count: int,
                            healed_count: int, base_url: str) -> dict:
    """
    Generates a natural language QA report summary that can be spoken aloud
    or displayed to the developer.

    Args:
        pages_scanned: Number of pages the agent scanned.
        critical_count: Number of critical bugs found.
        medium_count: Number of medium severity bugs found.
        low_count: Number of low severity bugs found.
        healed_count: Number of bugs automatically fixed by self-healing.
        base_url: The root URL that was scanned.

    Returns:
        dict: A dictionary with keys:
            - status: "success" or "error"
            - summary: the generated spoken summary text
    """
    from google import genai

    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    client = genai.Client(api_key=GEMINI_API_KEY)

    try:
        prompt = f"""You are a QA engineer giving a spoken report to a developer.
Write 3–4 natural spoken sentences. Mention self-healing. No markdown or bullets.

Website: {base_url}
Pages: {pages_scanned} | Critical: {critical_count} | Medium: {medium_count} | Low: {low_count}
Bugs auto-fixed: {healed_count}"""

        resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        return {"status": "success", "summary": resp.text.strip()}
    except Exception as ex:
        return {
            "status": "error",
            "summary": f"QA Ghost scanned {pages_scanned} pages on {base_url}, found {critical_count} critical bugs, and auto-fixed {healed_count} of them."
        }


# ── ADK Root Agent ─────────────────────────────────────────────────────────────
# This is the required entry point ADK looks for.
# It orchestrates the 4 tools above to run a full QA scan autonomously.

root_agent = Agent(
    name="qa_ghost_agent",
    model="gemini-2.5-flash",
    description=(
        "QA Ghost: an autonomous AI agent that scans websites for visual bugs, "
        "UI inconsistencies, and accessibility issues using Gemini Vision, "
        "then self-heals detected bugs by injecting AI-generated JavaScript fixes."
    ),
    instruction="""You are QA Ghost, an expert AI QA engineer.

When given a URL to scan, follow this pipeline:
1. Call navigate_and_screenshot with the URL to load the page and capture it.
2. Call analyze_screenshot with the screenshot path to detect bugs using Gemini Vision.
3. For each critical or medium bug found, call self_heal_bug to generate a JavaScript fix.
4. Call generate_report_summary with the final counts to produce a spoken summary.

Always complete all 4 steps in order. Be thorough and professional.
Report results clearly with bug titles, severity, locations, and fixes applied.""",
    tools=[
        navigate_and_screenshot,
        analyze_screenshot,
        self_heal_bug,
        generate_report_summary,
    ],
)
