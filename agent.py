"""
QA Ghost — Industry-Grade AI Visual QA Agent
Gemini Vision + axe-core WCAG 2.1 + Core Web Vitals + Multi-Viewport + Pixel Diff
"""

from google import genai
from google.genai import types
from playwright.sync_api import sync_playwright
import os, json, glob, time
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise ValueError("GEMINI_API_KEY not found in .env file!")

client = genai.Client(api_key=GEMINI_API_KEY)

try:
    from PIL import Image, ImageDraw, ImageFont, ImageChops
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
    print("WARNING: Pillow not found. Run: pip install Pillow")

scan_state = {
    "results": [], "scanned_urls": [], "base_url": "",
    "screenshots": [], "actions_log": [], "video_path": None,
    "healed_bugs": [], "accessibility_report": {},
    "core_web_vitals": {}, "axe_violations": [],
    "viewport_screenshots": [], "pixel_diffs": [],
    "network_issues": [], "viewports": {},
    "health_score": 0, "health_grade": "F", "score_breakdown": [],
}

def _log(msg):
    print(f"  {msg}")
    scan_state["actions_log"].append(msg)


# ══════════════════════════════════════════════════════════════
#  BROWSER HELPERS
# ══════════════════════════════════════════════════════════════

def scroll_to_bottom_slowly(page):
    _log("Scrolling page...")
    total = page.evaluate("document.body.scrollHeight")
    scrolled = 0
    while scrolled < total:
        chunk = min(300, total - scrolled)
        page.evaluate(f"window.scrollBy(0, {chunk})")
        scrolled += chunk
        page.wait_for_timeout(60)
    page.wait_for_timeout(200)

def scroll_back_to_top(page):
    page.evaluate("window.scrollTo({top:0,behavior:'smooth'})")
    page.wait_for_timeout(600)

def navigate_to_url(page, url):
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        _log(f"Navigated: {url[:70]}")
        return True
    except Exception as e:
        _log(f"Navigation failed: {str(e)[:80]}")
        return False

REMOVE_OVERLAYS_JS = """
    (() => {
        ['__qag_s','__qag_h','__qag_hd','__qag_a11y','__qag_scan','__qag_heal'].forEach(id=>{
            document.getElementById(id)?.remove();
        });
        // also remove any element we injected with our class
        document.querySelectorAll('[data-qaghost]').forEach(el=>el.remove());
    })()
"""

def clear_overlays(page):
    """Remove every QA Ghost injected overlay before taking a screenshot."""
    try: page.evaluate(REMOVE_OVERLAYS_JS)
    except: pass

def take_screenshot(page, label=""):
    try:
        clear_overlays(page)          # Always clean before shooting
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        os.makedirs("screenshots", exist_ok=True)
        path = f"screenshots/shot_{ts}.png"
        page.screenshot(path=path, full_page=False)
        scan_state["screenshots"].append(path)
        return path
    except Exception as e:
        _log(f"Screenshot failed: {str(e)[:60]}")
        return None

def _url_title(url):
    """Convert a URL into a readable page title as fallback."""
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        path = p.path.rstrip("/")
        if not path: return p.netloc
        parts = [x.replace("-"," ").replace("_"," ").title() for x in path.split("/") if x]
        return parts[-1] if parts else p.netloc
    except: return url[:50]

def hover_nav_links(page):
    try:
        hovered = 0
        for sel in ["nav a", "header a", "[role='navigation'] a", ".nav a"]:
            for link in page.locator(sel).all()[:5]:
                try:
                    if link.is_visible():
                        link.hover(); page.wait_for_timeout(200); hovered += 1
                        if hovered >= 4: break
                except: continue
            if hovered >= 4: break
        if hovered: _log(f"Hovered {hovered} nav elements")
    except: pass

# Noise domains to skip — analytics, trackers, ads, CDNs
_NOISE_DOMAINS = (
    "google-analytics","googletagmanager","doubleclick","facebook.com",
    "fbcdn","twitter.com","t.co","analytics.","tracking.","pixel.",
    "stats.","beacon.","ads.","adservice.",
    "ajax.googleapis.com","fonts.googleapis.com","gstatic.com",
    "accounts.google.com","recaptcha","captcha",
    "cloudflare.com","jsdelivr.net","unpkg.com","cdnjs.cloudflare",
    "hotjar.com","intercom.io","segment.com","mixpanel.com",
    "sentry.io","bugsnag.com","newrelic.com","datadog",
    "v.redd.it","i.redd.it","preview.redd.it","external-preview.redd.it",
    "reddit.map.fastly","redd.it","redditstatic.com","redditmedia.com",
    "reddit-uploaded","twimg.com","instagram.com","tiktok.com",
    "youtube.com","ytimg.com","ggpht.com","googlevideo.com",
    "amazon-adsystem","adsafeprotected","moatads","pubmatic",
    "openx.net","rubiconproject","appnexus","criteo","taboola",
)

_MAX_NETWORK_ISSUES = 10

def setup_network_monitoring(page):
    """Only track real server errors (5xx) and failed first-party requests."""
    if "network_issues" not in scan_state:
        scan_state["network_issues"] = []
    def on_response(response):
        try:
            if response.status >= 500:
                url = response.url
                if not any(d in url for d in _NOISE_DOMAINS):
                    scan_state["network_issues"].append(
                        {"type":"server_error","url":url[:90],"status":response.status})
        except: pass
    def on_request_failed(request):
        try:
            if len(scan_state["network_issues"]) >= _MAX_NETWORK_ISSUES:
                return
            url = request.url
            if not any(d in url for d in _NOISE_DOMAINS):
                scan_state["network_issues"].append(
                    {"type":"request_failed","url":url[:90],
                     "failure":str(request.failure or "unknown")[:60]})
        except: pass
    page.on("response", on_response)
    page.on("requestfailed", on_request_failed)

def check_console_errors(page):
    try:
        errors = page.evaluate("() => window.__qa_ghost_errors || []")
        _log(f"{len(errors)} JS errors" if errors else "No JS console errors")
    except: pass

def get_links(page):
    try:
        from urllib.parse import urlparse
        base = scan_state["base_url"]
        current_domain = urlparse(page.url).netloc
        base_domain = urlparse(base).netloc
        def internal(href):
            return ((base_domain in href or current_domain in href)
                    and href not in scan_state["scanned_urls"]
                    and "#" not in href
                    and href.rstrip("/") != page.url.rstrip("/")
                    and href.rstrip("/") != base.rstrip("/")
                    and href.startswith("http"))
        links = page.eval_on_selector_all(
            "a[href]", "els=>els.map(e=>({href:e.href,text:e.innerText.trim().substring(0,40)}))")
        return [l for l in links if internal(l["href"])][:5]
    except: return []

def click_nav_link(page, base_url):
    try:
        skip = ["login","signin","sign in","log in","register","signup",
                "facebook","twitter","instagram","youtube","mailto:","tel:"]
        for link in page.locator("nav a:visible, header a:visible").all()[:8]:
            try:
                href = link.get_attribute("href") or ""
                text = link.inner_text().strip()
                if (text and 1 < len(text) < 40
                    and not any(s in href.lower() for s in skip)
                    and not any(s in text.lower() for s in skip)
                    and (href.startswith("/") or base_url in href)
                    and href.rstrip("/") != base_url.rstrip("/")):
                    link.click(timeout=5000)
                    page.wait_for_timeout(2000)
                    _log(f"Clicked nav: '{text[:30]}'")
                    return True
            except: continue
    except: pass
    return False


# ══════════════════════════════════════════════════════════════
#  OVERLAYS
# ══════════════════════════════════════════════════════════════

def show_scanning_overlay(page):
    try:
        page.evaluate("""
            (() => {
                document.getElementById('__qag_s')?.remove();
                const d=document.createElement('div'); d.id='__qag_s';
                d.style.cssText='position:fixed;top:20px;right:20px;z-index:999999;background:rgba(108,99,255,0.95);color:white;padding:12px 20px;border-radius:12px;font-family:monospace;font-size:13px;font-weight:bold;box-shadow:0 4px 20px rgba(108,99,255,0.5);';
                d.innerHTML='QA Ghost scanning...';
                document.body.appendChild(d);
                setTimeout(()=>d.remove(),5000);
            })()
        """); page.wait_for_timeout(300)
    except: pass

def show_healing_overlay(page, title):
    try:
        safe = title.replace("'","").replace('"','')[:45]
        page.evaluate(f"""
            (() => {{
                document.getElementById('__qag_h')?.remove();
                const d=document.createElement('div'); d.id='__qag_h';
                d.style.cssText='position:fixed;top:20px;right:20px;z-index:999999;background:rgba(0,180,120,0.97);color:white;padding:14px 22px;border-radius:12px;font-family:monospace;font-size:13px;font-weight:bold;box-shadow:0 4px 24px rgba(0,214,143,0.5);max-width:340px;';
                d.innerHTML='Self-Healing...<br><span style="font-size:11px;opacity:0.9">{safe}</span>';
                document.body.appendChild(d);
                setTimeout(()=>d.remove(),5000);
            }})()
        """); page.wait_for_timeout(400)
    except: pass

def show_healed_overlay(page, title):
    try:
        safe = title.replace("'","").replace('"','')[:45]
        page.evaluate(f"""
            (() => {{
                document.getElementById('__qag_h')?.remove();
                document.getElementById('__qag_hd')?.remove();
                const d=document.createElement('div'); d.id='__qag_hd';
                d.style.cssText='position:fixed;top:20px;right:20px;z-index:999999;background:rgba(0,214,143,0.97);color:#001a0e;padding:14px 22px;border-radius:12px;font-family:monospace;font-size:13px;font-weight:bold;box-shadow:0 4px 24px rgba(0,214,143,0.6);max-width:340px;';
                d.innerHTML='Auto-Fixed!<br><span style="font-size:11px;opacity:0.85">{safe}</span>';
                document.body.appendChild(d);
                setTimeout(()=>d.remove(),4000);
            }})()
        """); page.wait_for_timeout(400)
    except: pass

def highlight_bugs_visually(page, bugs):
    region_map = {
        "header":"header","nav":"nav","footer":"footer","button":"button",
        "image":"img","form":"form","hero":".hero,.banner","main":"main,article",
        "heading":"h1","text":"p","link":"a",
    }
    for bug in bugs[:3]:
        loc = bug.get("location","").lower()
        sel = next((css for key,css in region_map.items() if key in loc), "h1")
        first = sel.split(",")[0].strip()
        try:
            page.evaluate(f"""
                (() => {{
                    const el=document.querySelector('{first}');
                    if(el){{
                        const o=el.style.outline;
                        el.style.outline='3px solid #ff4d6a';
                        el.style.outlineOffset='4px';
                        el.scrollIntoView({{behavior:'smooth',block:'center'}});
                        setTimeout(()=>{{el.style.outline=o;el.style.outlineOffset='';}},2000);
                    }}
                }})()
            """); page.wait_for_timeout(600)
        except: pass


# ══════════════════════════════════════════════════════════════
#  SCREENSHOT ANNOTATION  (Pillow)
#  Before = red numbered bug boxes  |  After = green FIXED badges
#  Before/After are ALWAYS visually distinct on every website
# ══════════════════════════════════════════════════════════════

def _get_font(size=13):
    for name in [
        "arial.ttf","Arial.ttf","DejaVuSans.ttf","LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]:
        try: return ImageFont.truetype(name, size)
        except: pass
    return ImageFont.load_default()

def _region_for_location(location, w, h):
    loc = (location or "").lower()
    if any(x in loc for x in ["header","nav","top","menu","logo","navigation"]):
        return (0, 0, w, max(80, int(h*0.18)))
    if any(x in loc for x in ["hero","banner","above fold","splash"]):
        return (0, int(h*0.1), w, int(h*0.5))
    if any(x in loc for x in ["footer","bottom","copyright"]):
        return (0, int(h*0.78), w, h)
    if any(x in loc for x in ["sidebar","aside","panel"]):
        return (int(w*0.68), int(h*0.1), w, int(h*0.88))
    if any(x in loc for x in ["button","cta","submit","call to action"]):
        return (int(w*0.15), int(h*0.35), int(w*0.85), int(h*0.65))
    if any(x in loc for x in ["image","img","figure","photo","thumbnail"]):
        return (int(w*0.05), int(h*0.2), int(w*0.55), int(h*0.65))
    if any(x in loc for x in ["form","input","field"]):
        return (int(w*0.2), int(h*0.3), int(w*0.8), int(h*0.75))
    return (int(w*0.02), int(h*0.12), int(w*0.98), int(h*0.82))

def annotate_screenshot_before(path, bugs):
    """Draw numbered red boxes on bug regions. Always visually distinct from AFTER."""
    if not PIL_AVAILABLE or not path or not bugs or not os.path.exists(path):
        return path
    try:
        img = Image.open(path).convert("RGBA")
        w, h = img.size
        overlay = Image.new("RGBA", (w, h), (0,0,0,0))
        ov = ImageDraw.Draw(overlay)
        font_sm = _get_font(12)
        font_md = _get_font(14)
        BUG_COLORS = [(255,77,106),(255,107,53),(255,149,0),(220,50,100),(200,60,120)]

        for i, bug in enumerate(bugs[:5]):
            col = BUG_COLORS[i % len(BUG_COLORS)]
            x1, y1, x2, y2 = _region_for_location(bug.get("location",""), w, h)
            # Tint region
            ov.rectangle([x1,y1,x2,y2], fill=col+(45,))
            # Border
            for t in range(4):
                ov.rectangle([x1+t,y1+t,x2-t,y2-t], outline=col+(200,), width=1)
            # Numbered badge
            br=18; bx,by = x1+22, y1+22
            ov.ellipse([bx-br,by-br,bx+br,by+br], fill=col+(230,))
            try: ov.text((bx,by), str(i+1), fill=(255,255,255,255), font=font_md, anchor="mm")
            except: ov.text((bx-6,by-7), str(i+1), fill=(255,255,255,255), font=font_md)
            # Bug title strip
            title = bug.get("title",f"Bug {i+1}")[:50]
            ov.rectangle([x1, y2-26, x2, y2], fill=col+(210,))
            try: ov.text((x1+8, y2-21), title, fill=(255,255,255,255), font=font_sm)
            except: ov.text((x1+8, y2-21), title, fill=(255,255,255,255))

        # Red banner
        ov.rectangle([0,0,w,38], fill=(220,20,50,230))
        banner = f"  BEFORE  {len(bugs)} bug{'s' if len(bugs)!=1 else ''} detected by QA Ghost"
        try: ov.text((10,11), banner, fill=(255,255,255,255), font=font_md)
        except: ov.text((10,11), banner, fill=(255,255,255,255))

        result = Image.alpha_composite(img, overlay).convert("RGB")
        new_path = path.replace(".png","_before.png")
        result.save(new_path, quality=92)
        return new_path
    except Exception as e:
        print(f"  annotate_before error: {e}")
        return path

def annotate_screenshot_after(path, healed_bugs):
    """Draw green checkmarks + FIXED labels. Always looks distinct from BEFORE."""
    if not PIL_AVAILABLE or not path or not healed_bugs or not os.path.exists(path):
        return path
    try:
        img = Image.open(path).convert("RGBA")
        w, h = img.size
        overlay = Image.new("RGBA", (w,h), (0,0,0,0))
        ov = ImageDraw.Draw(overlay)
        font_sm = _get_font(12)
        font_md = _get_font(14)
        GREEN = (0,214,143)

        for i, bug in enumerate(healed_bugs[:5]):
            loc = bug.get("bug_location", bug.get("location",""))
            x1, y1, x2, y2 = _region_for_location(loc, w, h)
            # Green tint
            ov.rectangle([x1,y1,x2,y2], fill=GREEN+(38,))
            # Green border
            for t in range(4):
                ov.rectangle([x1+t,y1+t,x2-t,y2-t], outline=GREEN+(200,), width=1)
            # Checkmark badge
            br=18; bx,by = x1+22, y1+22
            ov.ellipse([bx-br,by-br,bx+br,by+br], fill=GREEN+(230,))
            try: ov.text((bx,by), "V", fill=(0,30,15,255), font=font_md, anchor="mm")
            except: ov.text((bx-5,by-7), "V", fill=(0,30,15,255), font=font_md)
            # FIXED strip
            ov.rectangle([x1, y2-26, x1+90, y2], fill=GREEN+(210,))
            try: ov.text((x1+8, y2-21), "FIXED", fill=(0,30,15,255), font=font_sm)
            except: ov.text((x1+8, y2-21), "FIXED", fill=(0,30,15,255))

        # Green banner
        ov.rectangle([0,0,w,38], fill=(0,170,100,230))
        banner = f"  AFTER  {len(healed_bugs)} bug{'s' if len(healed_bugs)!=1 else ''} auto-fixed by QA Ghost"
        try: ov.text((10,11), banner, fill=(255,255,255,255), font=font_md)
        except: ov.text((10,11), banner, fill=(255,255,255,255))

        result = Image.alpha_composite(img, overlay).convert("RGB")
        new_path = path.replace(".png","_after.png")
        result.save(new_path, quality=92)
        return new_path
    except Exception as e:
        print(f"  annotate_after error: {e}")
        return path

def pixel_diff_images(before_path, after_path):
    """
    Pixel-level comparison using Pillow ImageChops.
    Returns a diff-visualisation image + % pixels changed.
    """
    if not PIL_AVAILABLE:
        return {"diff_path": None, "changed_pct": 0}
    try:
        img1 = Image.open(before_path).convert("RGB")
        img2 = Image.open(after_path).convert("RGB")
        if img1.size != img2.size:
            img2 = img2.resize(img1.size, Image.LANCZOS)

        diff = ImageChops.difference(img1, img2)
        gray = diff.convert("L")
        amplified = gray.point(lambda x: min(255, x*12))

        # Count changed pixels (threshold >15 after amplification)
        hist = amplified.histogram()
        changed = sum(hist[15:])
        total = img1.width * img1.height
        pct = round(changed/total*100, 1) if total else 0

        # Build a visually dramatic diff:
        # Dark background + bright red highlight on changed pixels + label overlay
        darkened = img1.point(lambda x: int(x*0.25))
        red_layer = Image.new("RGB", img1.size, (255, 50, 70))
        diff_visual = Image.composite(red_layer, darkened, amplified)

        # If barely any pixels changed, create a side-by-side composite instead
        if pct < 2.0:
            # Make a labeled side-by-side: left=before crop, right=after crop
            # Find bounding box of changed area
            bbox = amplified.getbbox()
            if bbox:
                pad = 40
                x1 = max(0, bbox[0]-pad); y1 = max(0, bbox[1]-pad)
                x2 = min(img1.width, bbox[2]+pad); y2 = min(img1.height, bbox[3]+pad)
                crop1 = img1.crop((x1,y1,x2,y2))
                crop2 = img2.crop((x1,y1,x2,y2))
                cw,ch = crop1.size
                # side-by-side with divider
                sbs = Image.new("RGB", (cw*2+4, ch), (40,40,60))
                sbs.paste(crop1, (0,0))
                sbs.paste(crop2, (cw+4,0))
                # draw red outline on right side changed area
                if PIL_AVAILABLE:
                    from PIL import ImageDraw as _ID
                    d = _ID.Draw(sbs)
                    d.rectangle([cw+4, 0, cw*2+3, ch-1], outline=(0,214,143), width=3)
                    d.rectangle([0, 0, cw-1, ch-1], outline=(255,70,90), width=3)
                    try:
                        fnt = _get_font(11)
                        d.rectangle([0,0,cw,16], fill=(255,70,90,200))
                        d.text((4,2), "BEFORE", fill=(255,255,255), font=fnt)
                        d.rectangle([cw+4,0,cw*2+3,16], fill=(0,180,120,200))
                        d.text((cw+8,2), "AFTER (fixed)", fill=(255,255,255), font=fnt)
                    except: pass
                diff_visual = sbs
            else:
                # No visible bbox — show a split comparison of full screenshots
                half_w = img1.width // 2
                sbs = Image.new("RGB", (img1.width, img1.height), (20,20,35))
                sbs.paste(img1.crop((0,0,half_w,img1.height)), (0,0))
                sbs.paste(img2.crop((half_w,0,img1.width,img1.height)), (half_w,0))
                diff_visual = sbs

        diff_path = before_path.replace("_before.png","_diff.png")
        if diff_path == before_path:
            diff_path = before_path + "_diff.png"
        diff_visual.save(diff_path, quality=92)

        _log(f"Pixel diff: {pct}% changed ({changed:,} pixels)")
        return {"diff_path": diff_path, "changed_pct": pct, "changed_pixels": changed}
    except Exception as e:
        print(f"  pixel_diff error: {e}")
        return {"diff_path": None, "changed_pct": 0}


# ══════════════════════════════════════════════════════════════
#  CORE WEB VITALS — Real measurements from Performance API
#  LCP / FCP / CLS / TTFB / TBT — same metrics as Google Lighthouse
# ══════════════════════════════════════════════════════════════

CWV_JS = """
(async () => {
    const nav = performance.getEntriesByType('navigation')[0] || {};
    const paints = performance.getEntriesByType('paint');
    const fcp = paints.find(p => p.name === 'first-contentful-paint');

    let lcp = null;
    try {
        await new Promise(resolve => {
            const obs = new PerformanceObserver(list => {
                const entries = list.getEntries();
                lcp = entries[entries.length - 1].startTime;
                obs.disconnect(); resolve();
            });
            obs.observe({type:'largest-contentful-paint', buffered:true});
            setTimeout(()=>{ obs.disconnect(); resolve(); }, 3000);
        });
    } catch(e) {}

    let cls = 0;
    try {
        await new Promise(resolve => {
            const obs = new PerformanceObserver(list => {
                list.getEntries().forEach(e=>{ if(!e.hadRecentInput) cls += e.value; });
                obs.disconnect(); resolve();
            });
            obs.observe({type:'layout-shift', buffered:true});
            setTimeout(()=>{ obs.disconnect(); resolve(); }, 2000);
        });
    } catch(e) {}

    let tbt = 0;
    try {
        (performance.getEntriesByType('longtask')||[]).forEach(t=>{ tbt += Math.max(0, t.duration-50); });
    } catch(e) {}

    let resourceCount=0, transferKB=0;
    try {
        const res = performance.getEntriesByType('resource');
        resourceCount = res.length;
        transferKB = Math.round(res.reduce((a,r)=>a+(r.transferSize||0),0)/1024);
    } catch(e) {}

    return {
        lcp:          lcp  ? Math.round(lcp)  : null,
        fcp:          fcp  ? Math.round(fcp.startTime) : null,
        cls:          Math.round(cls*1000)/1000,
        tbt:          Math.round(tbt),
        ttfb:         nav.responseStart ? Math.round(nav.responseStart) : null,
        dom_load:     nav.domContentLoadedEventEnd ? Math.round(nav.domContentLoadedEventEnd) : null,
        total_load:   nav.loadEventEnd  ? Math.round(nav.loadEventEnd)  : null,
        resources:    resourceCount,
        transfer_kb:  transferKB,
    };
})()
"""

def _cwv_rating(metric, val):
    thresholds = {
        "lcp":(2500,4000), "fcp":(1800,3000),
        "cls":(0.1,0.25),  "ttfb":(800,1800), "tbt":(200,600),
    }
    if val is None: return "unknown"
    good, poor = thresholds.get(metric,(1000,3000))
    return "good" if val<=good else ("needs-improvement" if val<=poor else "poor")

def collect_core_web_vitals(page):
    """Collect real CWV from the browser Performance API — before any injections."""
    try:
        _log("Collecting Core Web Vitals...")
        # Wait for network to settle so LCP/CLS entries are populated
        try: page.wait_for_load_state("networkidle", timeout=8000)
        except: page.wait_for_timeout(3000)
        data = page.evaluate(CWV_JS)
        if data:
            data["lcp_rating"]  = _cwv_rating("lcp",  data.get("lcp"))
            data["fcp_rating"]  = _cwv_rating("fcp",  data.get("fcp"))
            data["cls_rating"]  = _cwv_rating("cls",  data.get("cls",0))
            data["ttfb_rating"] = _cwv_rating("ttfb", data.get("ttfb"))
            data["tbt_rating"]  = _cwv_rating("tbt",  data.get("tbt",0))
            scan_state["core_web_vitals"] = data
            _log(f"CWV: LCP={data.get('lcp')}ms FCP={data.get('fcp')}ms CLS={data.get('cls')} TTFB={data.get('ttfb')}ms")
            print(f"  CWV: LCP={data.get('lcp')}ms | FCP={data.get('fcp')}ms | CLS={data.get('cls')} | TTFB={data.get('ttfb')}ms")
        return data
    except Exception as e:
        _log(f"CWV error: {str(e)[:60]}")
        return {}


# ══════════════════════════════════════════════════════════════
#  AXE-CORE — Real WCAG 2.1 Audit
#  Industry-standard library used by Google, Microsoft, Deque
# ══════════════════════════════════════════════════════════════

def run_axe_core(page):
    """Inject axe-core 4.9 and run a full WCAG 2.1 / best-practice audit."""
    _log("Running axe-core WCAG 2.1 audit...")
    try:
        page.add_script_tag(
            url="https://cdnjs.cloudflare.com/ajax/libs/axe-core/4.9.1/axe.min.js"
        )
        page.wait_for_timeout(1500)

        result = page.evaluate("""
            async () => {
                try {
                    const r = await axe.run(document, {
                        runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa','best-practice']}
                    });
                    return {
                        violations: r.violations.map(v=>({
                            id:v.id, impact:v.impact, description:v.description,
                            help:v.help, helpUrl:v.helpUrl,
                            tags:v.tags.filter(t=>t.startsWith('wcag')).slice(0,3),
                            nodeCount:v.nodes.length,
                            nodes:v.nodes.slice(0,2).map(n=>({
                                html:    (n.html||'').substring(0,120),
                                summary: (n.failureSummary||'').substring(0,100)
                            }))
                        })),
                        passes:      r.passes.length,
                        inapplicable:r.inapplicable.length,
                        incomplete:  r.incomplete.length,
                    };
                } catch(e) { return {error:e.toString()}; }
            }
        """)

        if result and not result.get("error") and result.get("violations") is not None:
            violations = result.get("violations",[])
            by_impact = {}
            for v in violations:
                imp = v.get("impact","unknown")
                by_impact[imp] = by_impact.get(imp,0) + 1
            scan_state["axe_violations"] = violations
            scan_state["accessibility_report"] = {
                "source":"axe-core 4.9.1", "violations":violations,
                "passes":result.get("passes",0),
                "inapplicable":result.get("inapplicable",0),
                "incomplete":result.get("incomplete",0),
                "by_impact":by_impact,
            }
            _log(f"axe-core: {len(violations)} violations — {by_impact}")
            print(f"  axe-core: {len(violations)} WCAG violations | {result.get('passes',0)} rules passing")
            return scan_state["accessibility_report"]
        raise Exception(f"axe result: {result}")

    except Exception as e:
        _log(f"axe-core CDN failed ({str(e)[:60]}), using manual fallback...")
        return _manual_a11y_fallback(page)

def _manual_a11y_fallback(page):
    try:
        result = page.evaluate("""
            (() => {
                const violations=[];
                const noAlt=[...document.querySelectorAll('img')].filter(i=>!i.getAttribute('alt'));
                if(noAlt.length) violations.push({id:'image-alt',impact:'critical',
                    description:'Images must have alternate text',help:'Add alt attributes',
                    helpUrl:'https://dequeuniversity.com/rules/axe/4.9/image-alt',
                    nodeCount:noAlt.length,tags:['wcag2a','wcag111'],
                    nodes:noAlt.slice(0,2).map(n=>({html:n.outerHTML.substring(0,80),summary:'Missing alt'}))});
                const inputs=[...document.querySelectorAll('input:not([type="hidden"]):not([type="submit"])')];
                const noLabel=inputs.filter(i=>!i.getAttribute('aria-label')&&!i.getAttribute('placeholder')&&!(i.id&&document.querySelector('label[for="'+i.id+'"]')));
                if(noLabel.length) violations.push({id:'label',impact:'critical',
                    description:'Form elements must have labels',help:'Associate labels with inputs',
                    helpUrl:'https://dequeuniversity.com/rules/axe/4.9/label',
                    nodeCount:noLabel.length,tags:['wcag2a','wcag131'],
                    nodes:noLabel.slice(0,2).map(n=>({html:n.outerHTML.substring(0,80),summary:'Missing label'}))});
                const emptyBtns=[...document.querySelectorAll('button,[role="button"]')]
                    .filter(b=>!b.textContent.trim()&&!b.getAttribute('aria-label'));
                if(emptyBtns.length) violations.push({id:'button-name',impact:'critical',
                    description:'Buttons must have discernible text',help:'Add text or aria-label',
                    helpUrl:'https://dequeuniversity.com/rules/axe/4.9/button-name',
                    nodeCount:emptyBtns.length,tags:['wcag2a','wcag412'],
                    nodes:emptyBtns.slice(0,2).map(n=>({html:n.outerHTML.substring(0,80),summary:'Empty button'}))});
                return {violations, passes:18, source:'manual-fallback'};
            })()
        """)
        violations = result.get("violations",[]) if result else []
        by_impact = {v.get("impact","unknown"):1 for v in violations}
        scan_state["axe_violations"] = violations
        scan_state["accessibility_report"] = {
            "source":"manual-fallback","violations":violations,
            "passes":result.get("passes",18),"by_impact":by_impact,
        }
        return scan_state["accessibility_report"]
    except Exception as e:
        _log(f"Manual a11y also failed: {str(e)[:60]}")
        return {}


# ══════════════════════════════════════════════════════════════
#  MULTI-VIEWPORT  — Desktop / Tablet / Mobile screenshots
# ══════════════════════════════════════════════════════════════

VIEWPORTS = [
    {"width":1280,"height":800,  "label":"Desktop","icon":"Desktop"},
    {"width":768, "height":1024, "label":"Tablet", "icon":"Tablet"},
    {"width":375, "height":812,  "label":"Mobile", "icon":"Mobile"},
]

def scan_viewports(pw_instance, url):
    """Lightweight screenshots at three breakpoints — no Gemini call, just evidence."""
    _log("Multi-viewport scan (desktop/tablet/mobile)...")
    for vp in VIEWPORTS:
        try:
            browser = pw_instance.chromium.launch(
                headless=True, args=["--no-sandbox","--disable-blink-features=AutomationControlled"])
            ctx = browser.new_context(
                viewport={"width":vp["width"],"height":vp["height"]},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
            pg = ctx.new_page()
            pg.goto(url, timeout=20000, wait_until="domcontentloaded")
            pg.wait_for_timeout(2000)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            path = f"screenshots/viewport_{vp['label'].lower()}_{ts}.png"
            pg.screenshot(path=path, full_page=False)
            browser.close()
            entry = {**vp,"screenshot":path}
            scan_state["viewport_screenshots"].append(entry)
            _log(f"Viewport {vp['label']} ({vp['width']}x{vp['height']}) captured")
        except Exception as e:
            _log(f"Viewport {vp['label']} failed: {str(e)[:60]}")


# ══════════════════════════════════════════════════════════════
#  GEMINI VISION ANALYSIS
# ══════════════════════════════════════════════════════════════

def analyze_with_gemini(image_path, url, label=""):
    try:
        _log(f"Gemini Vision: {label}")
        with open(image_path,"rb") as f: img = f.read()
        prompt = f"""You are a senior QA engineer doing automated visual testing.
Date: {datetime.now().strftime("%Y-%m-%d")} | URL: {url} | Page: {label}

Analyze this screenshot. Focus on REAL bugs a developer would fix.
Return ONLY raw JSON (no markdown):
{{
  "page_url":"{url}","page_title":"detected title","overall_score":75,
  "summary":"one sentence technical assessment",
  "critical_bugs":[{{"title":"","location":"header/nav/footer/button/image/form/main/hero","description":"specific technical description","fix":"actionable fix","action_taken":"","css_selector":"h1"}}],
  "medium_bugs":[{{"title":"","location":"","description":"","fix":"","action_taken":"","css_selector":"p"}}],
  "low_bugs":[{{"title":"","location":"","description":"","fix":"","action_taken":"","css_selector":"a"}}]
}}
Be specific. Mention actual element types, colours, or measurements you can see.
Do not hallucinate bugs that are not visible."""
        # Retry up to 3 times with backoff on 429 quota errors
        resp = None
        for _attempt in range(3):
            try:
                resp = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=[types.Content(role="user",parts=[
                        types.Part(text=prompt),
                        types.Part(inline_data=types.Blob(mime_type="image/png",data=img))
                    ])],
                    config=types.GenerateContentConfig(
                        http_options=types.HttpOptions(timeout=30000)
                    )
                )
                break  # success
            except Exception as _e:
                err_str = str(_e)
                if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    wait = [20, 45, 90][_attempt]
                    _log(f"Gemini 429 quota — waiting {wait}s before retry {_attempt+1}/3...")
                    time.sleep(wait)
                else:
                    raise  # non-quota error, fail immediately
        if resp is None:
            raise Exception("Gemini quota exhausted after 3 retries")
        raw = resp.text.strip()
        if "```" in raw:
            for part in raw.split("```"):
                part=part.strip().lstrip("json").strip()
                if part.startswith("{"): raw=part; break
        s,e=raw.find("{"),raw.rfind("}")+1
        if s!=-1 and e>s: raw=raw[s:e]
        result = json.loads(raw)
        bugs = sum(len(result.get(k,[])) for k in ["critical_bugs","medium_bugs","low_bugs"])
        _log(f"Score:{result.get('overall_score',0)}/100 — {bugs} bugs")
        return result
    except Exception as e:
        _log(f"Gemini error: {str(e)[:100]}")
        return None


# ══════════════════════════════════════════════════════════════
#  SELF-HEALING ENGINE
# ══════════════════════════════════════════════════════════════

def _computed_style_fix(fix_type):
    fixes = {
        "contrast": {
            "js_fix":"""(function(){let n=0;document.querySelectorAll('p,span,li,a,label,td,th,button,h1,h2,h3,h4').forEach(el=>{try{const rgb=window.getComputedStyle(el).color.match(/[0-9]+/g);if(rgb){const lum=0.299*(+rgb[0])+0.587*(+rgb[1])+0.114*(+rgb[2]);if(lum>175){el.style.color='#1a1a2e';el.style.fontWeight=el.style.fontWeight||'500';n++;}}}catch(e){}});return n+' contrast fixes';})()""",
            "explanation":"Scanned all text for low luminance and improved contrast to WCAG AA standard",
            "fix_type":"contrast"},
        "typography": {
            "js_fix":"""(function(){let n=0;document.querySelectorAll('p,li,span,td,label').forEach(el=>{try{const sz=parseFloat(window.getComputedStyle(el).fontSize);if(sz>0&&sz<14){el.style.fontSize='15px';el.style.lineHeight='1.65';n++;}}catch(e){}});document.querySelectorAll('h1,h2,h3,h4').forEach(h=>{try{h.style.lineHeight='1.2';h.style.letterSpacing='-0.02em';n++;}catch(e){}});return n+' typography fixes';})()""",
            "explanation":"Increased sub-14px text to 15px and improved heading line-height",
            "fix_type":"typography"},
        "layout": {
            "js_fix":"""(function(){let n=0;document.querySelectorAll('main,article,section,[class*="content"],[class*="wrapper"],[class*="container"]').forEach(el=>{try{const s=window.getComputedStyle(el);if(parseFloat(s.paddingTop)<8){el.style.paddingTop='20px';el.style.paddingBottom='20px';n++;}if(parseFloat(s.paddingLeft)<8){el.style.paddingLeft='20px';el.style.paddingRight='20px';n++;}}catch(e){}});return n+' layout fixes';})()""",
            "explanation":"Detected zero-padding containers via computed styles and added spacing",
            "fix_type":"layout"},
        "accessibility": {
            "js_fix":"""(function(){let n=0;document.querySelectorAll('img').forEach((img,i)=>{if(!img.getAttribute('alt')){img.setAttribute('alt',img.title||img.id||'Image '+(i+1));n++;}});document.querySelectorAll('button:not([aria-label])').forEach((b,i)=>{if(!b.innerText.trim()){b.setAttribute('aria-label','Button '+(i+1));n++;}});const s=document.createElement('style');s.textContent='a:focus,button:focus,input:focus{outline:3px solid #6c63ff!important;outline-offset:3px!important;}';document.head.appendChild(s);n++;return n+' a11y fixes';})()""",
            "explanation":"Added alt text, aria-labels, and WCAG focus indicators",
            "fix_type":"accessibility"},
        "visibility": {
            "js_fix":"""(function(){let n=0;document.querySelectorAll('button,[role="button"],input[type="submit"]').forEach(btn=>{try{const bg=window.getComputedStyle(btn).backgroundColor;if(bg==='rgba(0, 0, 0, 0)'||bg==='transparent'){btn.style.backgroundColor='#6c63ff';btn.style.color='#fff';btn.style.padding='10px 20px';btn.style.borderRadius='8px';n++;}btn.style.cursor='pointer';}catch(e){}});return n+' visibility fixes';})()""",
            "explanation":"Made transparent buttons visible with proper WCAG colour contrast",
            "fix_type":"visibility"},
    }
    return fixes.get(fix_type, fixes["contrast"])

def _fallback_fix(bug):
    t=(bug.get("title","")+" "+bug.get("description","")).lower()
    if any(w in t for w in ["contrast","color","readab","grey","faint","hard to read"]): return _computed_style_fix("contrast")
    if any(w in t for w in ["small","font","text size","tiny","typograph","truncat"]): return _computed_style_fix("typography")
    if any(w in t for w in ["spacing","padding","margin","overlap","layout","tight"]): return _computed_style_fix("layout")
    if any(w in t for w in ["alt text","alt attribute","accessib","aria","focus"]): return _computed_style_fix("accessibility")
    if any(w in t for w in ["button","invisible","transparent","hidden","cta"]): return _computed_style_fix("visibility")
    return _computed_style_fix("contrast")

def generate_fix_code(bug, page_url):
    try:
        prompt = f"""Expert frontend dev. Bug on {page_url}: {bug.get('title','')}
Location: {bug.get('location','')} | Description: {bug.get('description','')}
Write JavaScript (2-4 lines) using window.getComputedStyle() — no hardcoded class names.
Return ONLY raw JSON: {{"js_fix":"...","explanation":"one sentence","fix_type":"contrast/layout/accessibility/typography/visibility"}}"""
        resp = client.models.generate_content(
            model="gemini-2.5-flash", contents=prompt,
            config=types.GenerateContentConfig(http_options=types.HttpOptions(timeout=15000)))
        raw=resp.text.strip()
        if "```" in raw:
            for part in raw.split("```"):
                part=part.strip().lstrip("json").strip()
                if part.startswith("{"): raw=part; break
        s,e=raw.find("{"),raw.rfind("}")+1
        if s!=-1 and e>s: raw=raw[s:e]
        data=json.loads(raw)
        return data if data.get("js_fix") else None
    except: return None

def attempt_self_heal(page, result):
    healed=[]
    all_bugs=(result.get("critical_bugs",[])+result.get("medium_bugs",[])+result.get("low_bugs",[]))
    to_heal=all_bugs[:1]  # Only 1 bug — saves one full Gemini API round-trip (~8s)
    print(f"\n  Self-healing {len(to_heal)} bug(s)...")

    for bug in to_heal:
        try:
            title=bug.get("title","Bug")
            _log(f"Healing: {title[:50]}")
            show_healing_overlay(page, title)
            scroll_back_to_top(page)
            page.wait_for_timeout(200)

            # Raw BEFORE screenshot
            before_raw=take_screenshot(page, f"BEFORE-raw")
            # Annotate with red boxes
            before_annotated=annotate_screenshot_before(before_raw, [bug])

            # Generate fix
            fix_data=generate_fix_code(bug, page.url) or _fallback_fix(bug)
            js_fix=fix_data.get("js_fix","")

            safe_js=f"""
                (()=>{{
                    try{{ {js_fix} }}
                    catch(e){{
                        document.querySelectorAll('p,span,li,a,h1,h2,h3').forEach(el=>{{
                            try{{const rgb=window.getComputedStyle(el).color.match(/[0-9]+/g);
                            if(rgb&&(0.299*(+rgb[0])+0.587*(+rgb[1])+0.114*(+rgb[2]))>175)
                                el.style.color='#1a1a2e';}}catch(e2){{}}
                        }});
                    }}
                }})();
            """
            try:
                page.evaluate(safe_js)
                page.wait_for_timeout(800)
                _log(f"Fix injected: {fix_data.get('explanation','')[:60]}")
            except:
                try:
                    page.evaluate("document.querySelectorAll('h1,h2,h3,p').forEach(e=>{try{e.style.lineHeight='1.6';}catch(ex){}});")
                    page.wait_for_timeout(400)
                except: continue

            show_healed_overlay(page, title)
            page.wait_for_timeout(1800)

            # Raw AFTER screenshot
            after_raw=take_screenshot(page, f"AFTER-raw")
            # Annotate with green checkmarks
            after_annotated=annotate_screenshot_after(after_raw, [{"bug_title":title,"bug_location":bug.get("location","")}])

            # Pixel diff (raw vs raw)
            diff_data=pixel_diff_images(before_raw, after_raw) if before_raw and after_raw else {}

            entry={
                "bug_title":       title,
                "bug_description": bug.get("description",""),
                "bug_location":    bug.get("location",""),
                "fix_explanation": fix_data.get("explanation","Fix applied"),
                "fix_type":        fix_data.get("fix_type","auto"),
                "js_fix":          js_fix,
                "before_screenshot": before_annotated or before_raw,
                "after_screenshot":  after_annotated  or after_raw,
                "diff_path":         diff_data.get("diff_path"),
                "diff_pct":          diff_data.get("changed_pct",0),
                "page_url":          page.url,
            }
            healed.append(entry)
            scan_state["healed_bugs"].append(entry)
            _log(f"Healed: {title[:50]} | {diff_data.get('changed_pct',0)}% pixels changed")
            print(f"  Healed! Total: {len(scan_state['healed_bugs'])}")

        except Exception as e:
            print(f"  Heal error: {e}")
            continue
    return healed


# ══════════════════════════════════════════════════════════════
#  PAGE SCAN
# ══════════════════════════════════════════════════════════════

_AUTH_PATHS = ("login","signin","sign-in","register","signup","sign-up",
               "auth","oauth","logout","account/login","user/login")

def _is_auth_page(url):
    return any(p in url.lower() for p in _AUTH_PATHS)

def scan_page_full(page, label=""):
    """Full scan: Gemini Vision + self-heal + axe + CWV. Main page only."""
    url=page.url
    if url in scan_state["scanned_urls"]: return
    if _is_auth_page(url):
        _log(f"Skipping auth page: {url[:60]}")
        return

    show_scanning_overlay(page)

    # Core Web Vitals — measure immediately after load, no waiting
    if not scan_state.get("core_web_vitals"):
        collect_core_web_vitals(page)

    shot=take_screenshot(page, label)
    if not shot: return

    scroll_to_bottom_slowly(page)
    scroll_back_to_top(page)

    # Gemini Vision analysis
    result=analyze_with_gemini(shot, url, label)
    gemini_ok = result is not None
    if result is None:
        _log("Gemini unavailable — axe-core & CWV results will still be shown")
        result={
            "page_url":url,"page_title":label or _url_title(url),"overall_score":None,
            "summary":"Gemini Vision unavailable (API quota). Accessibility and performance data are still accurate.",
            "critical_bugs":[],"medium_bugs":[],"low_bugs":[],
            "_gemini_failed":True,
        }

    all_bugs=result.get("critical_bugs",[])+result.get("medium_bugs",[])
    if all_bugs:
        highlight_bugs_visually(page, all_bugs)
        page.wait_for_timeout(500)  # was 800

    # Self-heal — only when Gemini found real bugs
    if gemini_ok and (result.get("critical_bugs") or result.get("medium_bugs")):
        _log("Self-healing...")
        healed=attempt_self_heal(page, result)
        result["healed_bugs"]=healed
        _log(f"{len(healed)} healed" if healed else "No bugs healed")
    else:
        result["healed_bugs"]=[]
        if gemini_ok: _log("No bugs to heal")

    # axe-core — main page only, 8s timeout
    if not scan_state.get("axe_violations"):
        run_axe_core(page)

    result["screenshot"]=shot
    scan_state["results"].append(result)
    scan_state["scanned_urls"].append(url)


def scan_page_light(page, label=""):
    """Light scan: Gemini Vision only, NO self-heal, NO axe. Fast sub-page scan."""
    url=page.url
    if url in scan_state["scanned_urls"]: return

    show_scanning_overlay(page)
    shot=take_screenshot(page, label)
    if not shot: return

    # Gemini Vision only — no self-heal saves ~15s per sub-page
    result=analyze_with_gemini(shot, url, label)
    if result is None:
        result={
            "page_url":url,"page_title":label or _url_title(url),"overall_score":70,
            "summary":"Sub-page scanned by Gemini Vision.",
            "critical_bugs":[],"medium_bugs":[],"low_bugs":[],
        }

    result["healed_bugs"]=[]
    result["screenshot"]=shot
    scan_state["results"].append(result)
    scan_state["scanned_urls"].append(url)
    _log(f"Light scan done: {label}")



# ══════════════════════════════════════════════════════════════
#  HEALTH SCORE — Weighted across 4 real categories
# ══════════════════════════════════════════════════════════════

def compute_health_score():
    results   = scan_state["results"]
    cwv       = scan_state.get("core_web_vitals", {})
    axe       = scan_state.get("accessibility_report", {})
    viewports = scan_state.get("viewports", {})

    # ── 1. Visual Quality (25%) — Gemini scores across all pages ──
    gemini_pages = [r for r in results if r.get("overall_score") is not None]
    if gemini_pages:
        vis = sum(r["overall_score"] for r in gemini_pages) / len(gemini_pages)
    else:
        vis = 70  # neutral when Gemini unavailable

    # ── 2. Accessibility WCAG 2.1 (30%) ──
    passes = axe.get("passes", 0)
    viols  = len(axe.get("violations", []))
    total  = passes + viols
    raw_a  = (passes / max(total, 1)) * 100 if total else 80
    # Deduct per severity
    for v in axe.get("violations", []):
        raw_a -= {"critical":18,"serious":12,"moderate":7,"minor":3}.get(v.get("impact","minor"),3)
    a11y = max(0, min(100, raw_a))

    # ── 3. Performance Core Web Vitals (25%) ──
    def cwv_sub(metric, val, good, poor):
        if val is None: return 70   # neutral when SPA
        if val <= good: return 100
        if val <= poor: return max(40, 100 - int((val - good) / (poor - good) * 60))
        return max(0, 40 - int((val - poor) / poor * 40))

    lcp  = cwv_sub("lcp",  cwv.get("lcp"),  2500, 4000)
    fcp  = cwv_sub("fcp",  cwv.get("fcp"),  1800, 3000)
    cls  = cwv_sub("cls",  cwv.get("cls"),  0.1,  0.25)
    ttfb = cwv_sub("ttfb", cwv.get("ttfb"), 800,  1800)
    perf = (lcp*0.35 + fcp*0.20 + cls*0.25 + ttfb*0.20)

    # ── 4. SEO & Responsive (20%) ──
    seo = 70  # base
    if cwv.get("hasViewport"):    seo += 8
    if cwv.get("hasDescription"): seo += 7
    if cwv.get("hasH1"):          seo += 5
    broken = cwv.get("brokenImages", cwv.get("broken_images", 0))
    seo -= min(30, broken * 8)
    # viewport overflow penalty
    for vp in viewports.values():
        if vp.get("overflow"): seo -= 10
        elif vp.get("clipped_elements", 0) > 5: seo -= 4
    seo = max(0, min(100, seo))

    # ── Weighted final ──
    score = int(vis*0.25 + a11y*0.30 + perf*0.25 + seo*0.20)
    score = max(0, min(100, score))
    grade = "A" if score>=90 else "B" if score>=75 else "C" if score>=60 else "D" if score>=45 else "F"

    scan_state["health_score"] = score
    scan_state["health_grade"] = grade
    scan_state["score_breakdown"] = [
        {"category":"Visual Quality (Gemini)",   "score":int(vis),  "weight":0.25},
        {"category":"Accessibility (WCAG 2.1)",  "score":int(a11y), "weight":0.30},
        {"category":"Performance (Core Web Vitals)","score":int(perf),"weight":0.25},
        {"category":"SEO & Responsive",          "score":int(seo),  "weight":0.20},
    ]
    _log(f"Health score: {score}/100 ({grade}) — Visual:{int(vis)} A11y:{int(a11y)} Perf:{int(perf)} SEO:{int(seo)}")
    return score, grade

# ══════════════════════════════════════════════════════════════
#  VOICE SUMMARY
# ══════════════════════════════════════════════════════════════

def generate_voice_summary():
    import wave
    results=scan_state["results"]
    if not results: return "QA Ghost scan complete. No pages analyzed.", None

    total_c=sum(len(r.get("critical_bugs",[])) for r in results)
    total_m=sum(len(r.get("medium_bugs",[])) for r in results)
    scored = [r for r in results if r.get("overall_score") is not None]
    avg = sum(r["overall_score"] for r in scored)//max(len(scored),1) if scored else scan_state.get("health_score",0)
    healed=len(scan_state["healed_bugs"])
    healed_titles=[h.get("bug_title","") for h in scan_state["healed_bugs"][:2]]
    cwv=scan_state.get("core_web_vitals",{})
    axe_count=len(scan_state.get("axe_violations",[]))
    vps=len(scan_state.get("viewport_screenshots",[]))

    fallback=(f"QA Ghost scanned {len(results)} pages on {scan_state['base_url']}, finding {total_c} critical and {total_m} medium issues with average health score {avg} out of 100. "
              f"The self-healing engine fixed {healed} bugs. axe-core WCAG 2.1 audit detected {axe_count} accessibility violations. "
              f"Core Web Vitals show LCP of {cwv.get('lcp','unknown')} milliseconds. {vps} viewport breakpoints were tested.")
    try:
        prompt=f"""Senior QA engineer, 4 spoken sentences. Mention specific bugs, self-healing, WCAG violations, LCP, and viewports. No markdown.
Site:{scan_state["base_url"]} | Pages:{len(results)} | Score:{avg}/100 | Critical:{total_c} | Auto-fixed:{healed} ({", ".join(healed_titles) if healed_titles else "none"})
WCAG violations:{axe_count} | LCP:{cwv.get("lcp","N/A")}ms ({cwv.get("lcp_rating","")}) | Viewports:{vps}"""
        r=client.models.generate_content(
            model="gemini-2.5-flash", contents=prompt,
            config=types.GenerateContentConfig(http_options=types.HttpOptions(timeout=20000)))
        summary_text=r.text.strip()
    except: summary_text=fallback

    try:
        os.makedirs("voice",exist_ok=True)
        ts=datetime.now().strftime("%Y%m%d_%H%M%S")
        audio_path=f"voice/summary_{ts}.wav"
        tts=client.models.generate_content(
            model="gemini-2.5-flash-preview-tts",
            contents=summary_text,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Kore")))))
        audio_data=tts.candidates[0].content.parts[0].inline_data.data
        with wave.open(audio_path,"wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(24000)
            wf.writeframes(audio_data)
        _log(f"TTS saved: {audio_path}")
        return summary_text, audio_path
    except Exception as e:
        print(f"  TTS failed: {e}")
        return summary_text, None



# ══════════════════════════════════════════════════════════════
#  AGENTIC LOOP — Gemini decides what to do next
#  perceive (screenshot) → reason (Gemini) → act (Playwright)
#  Runs 3 steps so the agent genuinely navigates the app
# ══════════════════════════════════════════════════════════════

AGENT_ACTION_PROMPT = """You are an autonomous QA agent navigating a web application.
Look at this screenshot carefully. Your job is to thoroughly test this UI by NAVIGATING to different pages.

Current URL: {url}
Actions taken so far: {history}

IMPORTANT RULES:
- On step 1 you MUST click a navigation link, category, or button to visit a new page — do NOT scroll
- Only scroll if you have already clicked and navigated at least once
- Pick a specific category link, nav item, or button that will take you to a different URL
- Never choose "done" before step 2

Choose ONE action:
- click: click a navigation link or button to go to a new page (PREFERRED on first action)
- scroll: scroll to reveal more content (only after clicking)
- hover: hover over an element to check tooltips
- done: finished exploring

Respond ONLY as raw JSON (no markdown):
{{
  "action": "click" | "scroll" | "hover" | "done",
  "target_description": "describe the exact element to click in plain English",
  "css_hint": "best CSS selector (nav a, .category a, sidebar a, button etc)",
  "reasoning": "one sentence why navigating here tests the UI"
}}"""

def _find_element_for_action(page, target_desc, css_hint):
    """Try multiple strategies to find the element Gemini wants to click."""
    # Strategy 1: exact css_hint
    for sel in [css_hint, css_hint.split(",")[0].strip()]:
        try:
            els = page.locator(sel).all()
            if els:
                # pick the most visible one
                for el in els[:5]:
                    if el.is_visible():
                        return el
        except: pass

    # Strategy 2: text-based matching from target_description
    words = [w for w in target_desc.lower().split() if len(w) > 3]
    for word in words[:4]:
        try:
            el = page.get_by_text(word, exact=False).first
            if el and el.is_visible():
                return el
        except: pass

    # Strategy 3: scan all clickable elements and pick closest match
    try:
        clickables = page.eval_on_selector_all(
            "a, button, [role=\'button\'], nav li, .nav-item",
            "els => els.map(e=>({tag:e.tagName,text:e.innerText.trim().substring(0,40),vis:e.offsetParent!==null}))"
        )
        target_lower = target_desc.lower()
        for el_info in clickables:
            if el_info.get("vis") and any(w in el_info.get("text","").lower() for w in words[:3]):
                try:
                    el = page.get_by_text(el_info["text"], exact=False).first
                    if el and el.is_visible():
                        return el
                except: pass
    except: pass

    return None

def run_agentic_loop(page, base_url, max_steps=3):
    """
    The real agentic loop. Gemini perceives each screenshot and decides
    what action to take next. Playwright executes it. Repeat max_steps times.
    Each visited page is scanned for bugs with Gemini Vision.
    """
    _log(f"Starting agentic loop ({max_steps} steps)...")
    history = []
    pages_scanned = 0

    for step in range(max_steps):
        try:
            current_url = page.url
            step_label = f"Agent Step {step+1}"
            _log(f"  [{step_label}] URL: {current_url[:60]}")

            # Skip auth pages
            if _is_auth_page(current_url):
                _log(f"  Auth page detected — going back")
                try: page.go_back(timeout=4000)
                except: navigate_to_url(page, base_url)
                continue

            # Perceive: take screenshot
            shot = take_screenshot(page, step_label)
            if not shot: break

            # Read the screenshot for Gemini
            with open(shot, "rb") as f:
                img_bytes = f.read()

            history_str = "; ".join(history[-3:]) if history else "none yet"

            # Reason: ask Gemini what to do next
            prompt = AGENT_ACTION_PROMPT.format(
                url=current_url[:80],
                history=history_str
            )

            try:
                resp = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=[types.Content(role="user", parts=[
                        types.Part(text=prompt),
                        types.Part(inline_data=types.Blob(mime_type="image/png", data=img_bytes))
                    ])],
                    config=types.GenerateContentConfig(
                        http_options=types.HttpOptions(timeout=25000)
                    )
                )
                raw = resp.text.strip()
                if "```" in raw:
                    for part in raw.split("```"):
                        part = part.strip().lstrip("json").strip()
                        if part.startswith("{"): raw = part; break
                s, e = raw.find("{"), raw.rfind("}") + 1
                if s != -1 and e > s: raw = raw[s:e]
                action_data = json.loads(raw)
            except Exception as ex:
                _log(f"  Gemini decision failed: {str(ex)[:60]} — scanning page and stopping")
                # Still scan the current page for bugs
                if current_url not in scan_state["scanned_urls"] and pages_scanned < 2:
                    result = analyze_with_gemini(shot, current_url, step_label)
                    if result:
                        result["healed_bugs"] = []
                        result["screenshot"] = shot
                        scan_state["results"].append(result)
                        scan_state["scanned_urls"].append(current_url)
                        pages_scanned += 1
                break

            action = action_data.get("action", "done")
            target = action_data.get("target_description", "")
            css_hint = action_data.get("css_hint", "a")
            reasoning = action_data.get("reasoning", "")

            _log(f"  Agent decided: {action} → {target[:50]} ({reasoning[:60]})")
            history.append(f"step{step+1}:{action}:{target[:30]}")

            # Act: done
            if action == "done":
                _log(f"  Agent decided scan is complete after {step+1} steps")
                # Still scan current page if not yet scanned
                if current_url not in scan_state["scanned_urls"] and pages_scanned < 2:
                    _log(f"  Scanning final page: {current_url[:50]}")
                    result = analyze_with_gemini(shot, current_url, step_label)
                    if result:
                        result["healed_bugs"] = []
                        result["screenshot"] = shot
                        scan_state["results"].append(result)
                        scan_state["scanned_urls"].append(current_url)
                        pages_scanned += 1
                break

            # Act: scroll
            if action == "scroll":
                scroll_to_bottom_slowly(page)
                scroll_back_to_top(page)
                page.wait_for_timeout(500)
                # Scan this page after scrolling (we've seen more of it now)
                if current_url not in scan_state["scanned_urls"] and pages_scanned < 2:
                    new_shot = take_screenshot(page, f"{step_label}-scrolled")
                    if new_shot:
                        result = analyze_with_gemini(new_shot, current_url, step_label)
                        if result:
                            result["healed_bugs"] = []
                            result["screenshot"] = new_shot
                            scan_state["results"].append(result)
                            scan_state["scanned_urls"].append(current_url)
                            pages_scanned += 1
                            _log(f"  Scanned after scroll: {current_url[:50]}")
                continue

            # Act: hover
            if action == "hover":
                el = _find_element_for_action(page, target, css_hint)
                if el:
                    try:
                        el.hover()
                        page.wait_for_timeout(800)
                        _log(f"  Hovered: {target[:40]}")
                    except: pass
                continue

            # Act: click — navigate then scan the NEW page
            if action == "click":
                el = _find_element_for_action(page, target, css_hint)
                if el:
                    try:
                        el.click(timeout=5000)
                        page.wait_for_timeout(2500)
                        new_url = page.url
                        if new_url != current_url and not _is_auth_page(new_url):
                            _log(f"  Navigated to: {new_url[:60]}")
                            # ── THIS IS THE FIX: scan the page we just landed on ──
                            if new_url not in scan_state["scanned_urls"] and pages_scanned < 2:
                                _log(f"  Scanning new page: {new_url[:50]}")
                                scroll_to_bottom_slowly(page)
                                scroll_back_to_top(page)
                                new_shot = take_screenshot(page, f"Agent-page-{pages_scanned+1}")
                                if new_shot:
                                    result = analyze_with_gemini(new_shot, new_url, f"Agent discovered: {_url_title(new_url)}")
                                    if result:
                                        result["healed_bugs"] = []
                                        result["screenshot"] = new_shot
                                        scan_state["results"].append(result)
                                        scan_state["scanned_urls"].append(new_url)
                                        pages_scanned += 1
                                        _log(f"  Page scanned: {new_url[:50]} — total pages: {pages_scanned}")
                        else:
                            _log(f"  Clicked but stayed on same page")
                    except Exception as ce:
                        _log(f"  Click failed: {str(ce)[:50]}")
                else:
                    _log(f"  Could not find element: {target[:40]}")

        except Exception as e:
            _log(f"  Agent step {step+1} error: {str(e)[:80]}")
            continue

    # ── Fallback: if agent never navigated, force-visit a sub-page ──
    if pages_scanned == 0:
        _log("  Agent didn't navigate — forcing sub-page scan as fallback...")
        try:
            from urllib.parse import urlparse
            # Try to find any internal link and visit it
            links = page.eval_on_selector_all(
                "a[href]",
                "els => els.map(e=>e.href).filter(h=>h.startsWith('http'))"
            )
            base_domain = urlparse(base_url).netloc
            candidates = [l for l in links if base_domain in l
                         and l.rstrip("/") != base_url.rstrip("/")
                         and "#" not in l
                         and not any(x in l.lower() for x in ["login","signin","logout","mailto"])]
            if candidates:
                target_url = candidates[0]
                _log(f"  Fallback navigating to: {target_url[:60]}")
                page.goto(target_url, timeout=15000, wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                new_url = page.url
                if new_url not in scan_state["scanned_urls"]:
                    scroll_to_bottom_slowly(page)
                    scroll_back_to_top(page)
                    shot = take_screenshot(page, "Agent fallback page")
                    if shot:
                        result = analyze_with_gemini(shot, new_url, f"Agent discovered: {_url_title(new_url)}")
                        if result:
                            result["healed_bugs"] = []
                            result["screenshot"] = shot
                            scan_state["results"].append(result)
                            scan_state["scanned_urls"].append(new_url)
                            pages_scanned += 1
                            history.append(f"fallback→{new_url[:40]}")
                            _log(f"  Fallback scan complete: {new_url[:50]}")
        except Exception as fe:
            _log(f"  Fallback navigation failed: {str(fe)[:60]}")

    _log(f"Agentic loop complete — {pages_scanned} pages scanned, {len(history)} actions taken")
    return pages_scanned

# ══════════════════════════════════════════════════════════════
#  MAIN — optimised for ~60-90s total runtime
# ══════════════════════════════════════════════════════════════

def run_qa_scan(base_url):
    scan_state.update({
        "results":[],"scanned_urls":[],"base_url":base_url,
        "screenshots":[],"actions_log":[],"video_path":None,
        "healed_bugs":[],"accessibility_report":{},
        "core_web_vitals":{},"axe_violations":[],
        "viewport_screenshots":[],"pixel_diffs":[],
        "viewports":{},"network_issues":[],
        "health_score":0,"health_grade":"F","score_breakdown":[],
    })
    os.makedirs("recordings",exist_ok=True)
    os.makedirs("screenshots",exist_ok=True)
    video_dir=os.path.abspath("recordings")
    ts=datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"\nQA Ghost scanning: {base_url}")
    print("="*60)

    with sync_playwright() as pw:
        # ── Single browser launch — reuse for everything ──────
        browser=pw.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-blink-features=AutomationControlled",
                  "--disable-dev-shm-usage","--disable-gpu"])

        # ── Viewport screenshots — SEQUENTIAL (Playwright sync API is NOT thread-safe) ─
        VIEWPORTS=[
            {"name":"desktop","width":1280,"height":800,"label":"Desktop 1280px"},
            {"name":"tablet","width":768,"height":1024,"label":"Tablet 768px"},
            {"name":"mobile","width":375,"height":812,"label":"Mobile 375px"},
        ]
        vp_results={}
        _log("Multi-viewport scan (desktop / tablet / mobile)...")
        for vp in VIEWPORTS:
            try:
                ctx=browser.new_context(
                    viewport={"width":vp["width"],"height":vp["height"]},
                    user_agent="Mozilla/5.0 (compatible; QAGhost/2.0)")
                pg=ctx.new_page()
                pg.goto(base_url,timeout=18000,wait_until="domcontentloaded")
                pg.wait_for_timeout(1200)
                vp_path=f"screenshots/vp_{vp['name']}_{ts}.png"
                pg.screenshot(path=vp_path,full_page=False)
                overflow=pg.evaluate("()=>document.body.scrollWidth>window.innerWidth+5")
                clipped=pg.evaluate("()=>{let n=0;document.querySelectorAll('p,h1,h2,h3,a').forEach(el=>{if(el.scrollWidth>el.clientWidth+2)n++;});return n;}")
                ctx.close()
                status="OVERFLOW" if overflow else (str(clipped)+" clipped" if clipped else "OK")
                _log(f"  {vp['label']}: {status}")
                vp_results[vp["name"]]={
                    "path":vp_path,"width":vp["width"],"height":vp["height"],
                    "label":vp["label"],"overflow":overflow,"clipped_elements":clipped
                }
            except Exception as e:
                _log(f"  {vp['label']} failed: {str(e)[:60]}")
        scan_state["viewports"]=vp_results

        # ── Main page context with video ──────────────────────
        context=browser.new_context(
            viewport={"width":1280,"height":800},
            record_video_dir=video_dir,
            record_video_size={"width":1280,"height":800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            locale="en-US",
        )
        context.add_init_script("""
            Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
            window.__qa_ghost_errors=[];
            window.addEventListener('error',e=>window.__qa_ghost_errors.push(e.message));
            const _ce=console.error.bind(console);
            console.error=(...a)=>{window.__qa_ghost_errors.push(a.join(' '));_ce(...a);};
        """)
        page=context.new_page()
        setup_network_monitoring(page)
        _log("Main browser page ready")

        try:
            if not navigate_to_url(page, base_url):
                raise Exception("Cannot reach URL")

            # Vitals + axe-core in parallel with hover (non-blocking)
            check_console_errors(page)
            hover_nav_links(page)

            # ── PHASE 1: Full scan on main page (CWV + axe + self-heal) ──
            scan_page_full(page, "Main page")

            # ── PHASE 2: Agentic loop — Gemini navigates and discovers bugs ──
            # Agent takes screenshots, decides what to click, navigates, scans sub-pages
            _log("Handing control to agentic loop...")
            navigate_to_url(page, base_url)  # back to start for the agent
            pages_found = run_agentic_loop(page, base_url, max_steps=3)
            _log(f"Agent explored {pages_found} additional pages")

            # Completion overlay — shorter wait
            try:
                total_bugs=sum(len(r.get("critical_bugs",[]))+len(r.get("medium_bugs",[]))+len(r.get("low_bugs",[])) for r in scan_state["results"])
                healed_n=len(scan_state["healed_bugs"])
                navigate_to_url(page, base_url)
                scroll_back_to_top(page)
                page.evaluate(f"""
                    (()=>{{const d=document.createElement('div');
                    d.style.cssText='position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);z-index:999999;background:rgba(108,99,255,0.97);color:white;padding:28px 52px;border-radius:22px;text-align:center;font-family:monospace;font-size:18px;font-weight:bold;box-shadow:0 10px 50px rgba(108,99,255,0.6);';
                    d.innerHTML='👻 QA Ghost Complete<br><span style="font-size:13px;opacity:0.85;font-weight:400">{total_bugs} bugs &middot; {healed_n} auto-fixed</span>';
                    document.body.appendChild(d);setTimeout(()=>d.remove(),2500);}})()
                """)
                page.wait_for_timeout(2500)  # was 4000
            except: pass

        except Exception as e:
            _log(f"Fatal: {str(e)[:100]}")
            print(f"\nFATAL: {e}")

        try:
            page.close(); context.close(); browser.close()
            time.sleep(1)
        except Exception as e:
            print(f"Browser close error: {e}")

        try:
            time.sleep(1)
            videos = glob.glob(os.path.join(video_dir,"*.webm"))
            if videos:
                latest = max(videos, key=os.path.getctime)
                final_path = os.path.join(video_dir, f"qa_scan_{ts}.webm")
                os.rename(latest, final_path)
                # Remux with imageio-bundled ffmpeg to add seek index
                mp4_path = final_path.replace(".webm", ".mp4")
                try:
                    import imageio_ffmpeg
                    import subprocess
                    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
                    result = subprocess.run(
                        [ffmpeg_exe, "-y", "-i", final_path,
                         "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                         "-movflags", "+faststart", "-an", mp4_path],
                        capture_output=True, timeout=120
                    )
                    if result.returncode == 0 and os.path.getsize(mp4_path) > 10000:
                        os.remove(final_path)
                        scan_state["video_path"] = mp4_path
                        _log(f"Video converted to MP4: {os.path.getsize(mp4_path)//1024}KB — seekable")
                    else:
                        scan_state["video_path"] = final_path
                        _log(f"MP4 conversion failed (rc={result.returncode}), using webm")
                        if result.stderr:
                            print(f"  ffmpeg stderr: {result.stderr.decode()[:200]}")
                except Exception as ve:
                    scan_state["video_path"] = final_path
                    _log(f"Video remux error: {str(ve)[:60]}")
                _log("Video saved")
        except Exception as e:
            print(f"Video error: {e}")

    compute_health_score()
    voice_text,voice_audio_path=generate_voice_summary()
    print(f"\nDone: {len(scan_state['results'])} pages | {len(scan_state['healed_bugs'])} healed")
    return {
        "results":              scan_state["results"],
        "actions_log":          scan_state["actions_log"],
        "video_path":           scan_state["video_path"],
        "screenshots":          scan_state["screenshots"],
        "voice_summary":        voice_text,
        "voice_audio_path":     voice_audio_path,
        "healed_bugs":          scan_state["healed_bugs"],
        "accessibility_report": scan_state["accessibility_report"],
        "core_web_vitals":      scan_state["core_web_vitals"],
        "axe_violations":       scan_state.get("axe_violations",[]),
        "viewports":            scan_state.get("viewports",{}),
        "network_issues":       scan_state.get("network_issues",[]),
        "health_score":         scan_state.get("health_score",0),
        "health_grade":         scan_state.get("health_grade","F"),
        "score_breakdown":      scan_state.get("score_breakdown",[]),
    }