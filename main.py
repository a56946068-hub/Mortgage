import os
import signal
import json
import time
import urllib.parse
import logging
import schedule
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
RESEND_API_KEY = os.environ.get('RESEND_API_KEY', '').strip()
ALERT_EMAIL    = os.environ.get('ALERT_EMAIL', 'n.hesabian@gmail.com').strip()
FROM_EMAIL     = os.environ.get('FROM_EMAIL', 'onboarding@resend.dev').strip()
STATE_FILE     = '/tmp/seen_jobs.json'

GTA_CITIES = {
    'richmond hill', 'vaughan', 'markham', 'toronto', 'north york',
    'scarborough', 'mississauga', 'brampton', 'oakville', 'aurora',
    'newmarket', 'king city', 'woodbridge', 'maple', 'concord',
    'thornhill', 'stouffville', 'ajax', 'pickering', 'whitby', 'ontario',
    'etobicoke', 'york', 'east york', 'don mills', 'weston',
}

TARGETS = [
    # ── Mortgage roles ──────────────────────────────────────────────────────────
    {'bank': 'RBC',                    'role': 'Mortgage Specialist Assistant',        'category': 'Mortgage'},
    {'bank': 'TD',                     'role': 'Mobile Mortgage Specialist Assistant', 'category': 'Mortgage'},
    {'bank': 'BMO',                    'role': 'Mortgage Specialist Associate',        'category': 'Mortgage'},
    {'bank': 'CIBC',                   'role': 'Mortgage Advisor Assistant',           'category': 'Mortgage'},
    {'bank': 'Scotiabank',             'role': 'Home Financing Associate',             'category': 'Mortgage'},
    {'bank': 'Meridian Credit Union',  'role': 'Mobile Mortgage Specialist',           'category': 'Mortgage'},

    # ── Teller / CSR roles ──────────────────────────────────────────────────────
    {'bank': 'RBC',                    'role': 'Client Advisor',                       'category': 'Teller'},
    {'bank': 'TD',                     'role': 'Customer Experience Associate',        'category': 'Teller'},
    {'bank': 'Scotiabank',             'role': 'Customer Experience Associate',        'category': 'Teller'},
    {'bank': 'BMO',                    'role': 'Customer Service Representative',      'category': 'Teller'},
    {'bank': 'CIBC',                   'role': 'Client Service Representative',        'category': 'Teller'},
    {'bank': 'National Bank of Canada','role': 'Banking Advisor',                      'category': 'Teller'},
    # Meridian uses two front-line titles — scrape both, dedupe by fingerprint
    {'bank': 'Meridian Credit Union',  'role': 'Member Services Representative',       'category': 'Teller'},
    {'bank': 'Meridian Credit Union',  'role': 'Financial Services Representative',    'category': 'Teller'},
    {'bank': 'Laurentian Bank',        'role': 'Customer Service Officer',             'category': 'Teller'},
    {'bank': 'Desjardins',             'role': 'Member Service Advisor',               'category': 'Teller'},
]

# ATS career portals — one URL template per bank, {role} replaced at runtime
BANK_ATS = {
    'RBC': (
        'https://jobs.rbc.com/ca/en/search-results'
        '?keywords={role}&location=Richmond+Hill%2C+Ontario%2C+Canada'
    ),
    'TD': (
        'https://jobs.td.com/en-CA/job-search-results/'
        '?keyword={role}&location=Richmond+Hill'
    ),
    'BMO': (
        'https://jobs.bmo.com/ca/en/search-results'
        '?keywords={role}&location=Richmond+Hill%2C+Ontario'
    ),
    'CIBC': (
        'https://cibc.wd3.myworkdayjobs.com/search'
        '?q={role}&locations=Ontario'
    ),
    'Scotiabank': (
        'https://jobs.scotiabank.com/search/'
        '?q={role}&locationsearch=Richmond+Hill'
    ),
    'Meridian Credit Union': (
        'https://meridian.wd3.myworkdayjobs.com/meridian_careers'
        '?q={role}'
    ),
    'National Bank of Canada': (
        'https://www.nbc.ca/about-us/careers/job-offers.html'
        '?keywords={role}&location=Ontario'
    ),
    'Laurentian Bank': (
        'https://www.laurentianbank.ca/en/about-laurentian-bank/careers'
        '/job-offers.html?keywords={role}'
    ),
    'Desjardins': (
        'https://careers.desjardins.com/en/search'
        '?keywords={role}&location=Ontario'
    ),
}

# ── State ──────────────────────────────────────────────────────────────────────
def load_state() -> set:
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_state(state: set):
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(list(state), f)
    os.replace(tmp, STATE_FILE)

# ── Helpers ────────────────────────────────────────────────────────────────────
def is_gta(text: str) -> bool:
    t = text.lower()
    return any(city in t for city in GTA_CITIES)

def fingerprint(job: dict) -> str:
    return f"{job['bank'].lower()}::{job['title'].lower().strip()}"

def role_matches(text: str, role: str) -> bool:
    """
    Require ≥60% of meaningful tokens from the role title to appear in text.
    Stops words stripped so 'Customer Experience Associate' doesn't fail on
    short anchor text that drops common words.
    """
    STOPS = {'of', 'the', 'a', 'an', 'and', 'or', 'in', 'at', 'for', 'to'}
    tokens = [t for t in role.lower().split() if t not in STOPS]
    if not tokens:
        return False
    matched = sum(1 for t in tokens if t in text.lower())
    return matched / len(tokens) >= 0.6

def make_browser_context(playwright):
    browser = playwright.chromium.launch(
        headless=True,
        args=[
            '--no-sandbox', '--disable-setuid-sandbox',
            '--disable-dev-shm-usage', '--disable-gpu',
            '--single-process', '--no-zygote',
        ],
    )
    ctx = browser.new_context(
        viewport={'width': 1920, 'height': 1080},
        user_agent=(
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        ),
    )
    return browser, ctx

# ── Indeed ─────────────────────────────────────────────────────────────────────
def scrape_indeed(page, bank: str, role: str, category: str) -> list:
    log.info(f"  [Indeed] {bank} — {role}")
    query = urllib.parse.quote(f'"{role}" "{bank}"')
    url   = (
        f"https://ca.indeed.com/jobs"
        f"?q={query}&l=Richmond+Hill%2C+ON&radius=30&sort=date"
    )
    jobs = []
    try:
        page.goto(url, wait_until='domcontentloaded', timeout=20000)
        page.wait_for_timeout(2500)
        soup = BeautifulSoup(page.content(), 'html.parser')

        for card in soup.find_all('div', attrs={'data-testid': 'slider_item'}):
            # title: new testid attr first, fall back to class
            title_el = (
                card.find(attrs={'data-testid': 'jobTitle'}) or
                card.find('h2', class_=lambda c: c and 'jobTitle' in c)
            )
            link_el = (
                card.find('a', attrs={'data-jk': True}) or
                card.find('a', class_=lambda c: c and 'JobTitle' in (c or ''))
            )
            loc_el = (
                card.find(attrs={'data-testid': 'text-location'}) or
                card.find(class_=lambda c: c and 'location' in (c or '').lower())
            )
            if not title_el or not link_el:
                continue
            loc_text = loc_el.get_text() if loc_el else 'Ontario'
            if not is_gta(loc_text):
                continue

            jk   = link_el.get('data-jk') or link_el.get('href', '')
            href = (
                f"https://ca.indeed.com/viewjob?jk={jk}"
                if not jk.startswith('http') else jk
            )
            jobs.append({
                'id':       jk,
                'title':    title_el.get_text().strip(),
                'link':     href,
                'source':   'Indeed',
                'bank':     bank,
                'category': category,
            })

        log.info(f"  [Indeed] {bank} — {role} → {len(jobs)} found")
    except Exception as e:
        log.warning(f"  [Indeed] {bank} — {role} → FAILED: {e}")
    return jobs

# ── LinkedIn ───────────────────────────────────────────────────────────────────
def scrape_linkedin(page, bank: str, role: str, category: str) -> list:
    log.info(f"  [LinkedIn] {bank} — {role}")
    query = urllib.parse.quote(f'"{role}" "{bank}"')
    url   = (
        f"https://www.linkedin.com/jobs/search/"
        f"?keywords={query}&location=Richmond+Hill%2C+Ontario"
        f"&distance=30&f_TPR=r86400&sortBy=DD"
    )
    jobs = []
    try:
        page.goto(url, wait_until='domcontentloaded', timeout=20000)
        page.wait_for_timeout(2500)
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1500)
        soup = BeautifulSoup(page.content(), 'html.parser')

        for card in soup.find_all(
            'div', class_=lambda c: c and 'base-card' in c
        ):
            title_el = card.find(
                'h3', class_=lambda c: c and 'base-search-card__title' in c
            )
            link_el = card.find(
                'a', class_=lambda c: c and 'base-card__full-link' in c
            )
            loc_el = card.find(
                'span', class_=lambda c: c and 'job-search-card__location' in c
            )
            if not title_el or not link_el:
                continue
            loc_text = loc_el.get_text().strip() if loc_el else 'Ontario'
            if not is_gta(loc_text):
                continue

            href = link_el['href'].split('?')[0]
            jobs.append({
                'id':       href,
                'title':    title_el.get_text().strip(),
                'link':     href,
                'source':   'LinkedIn',
                'bank':     bank,
                'category': category,
            })

        log.info(f"  [LinkedIn] {bank} — {role} → {len(jobs)} found")
    except Exception as e:
        log.warning(f"  [LinkedIn] {bank} — {role} → FAILED: {e}")
    return jobs

# ── ATS ────────────────────────────────────────────────────────────────────────
def scrape_bank_ats(page, bank: str, role: str, category: str) -> list:
    if bank not in BANK_ATS:
        return []
    log.info(f"  [ATS] {bank} — {role}")
    url  = BANK_ATS[bank].format(role=urllib.parse.quote(role))
    jobs = []
    try:
        page.goto(url, wait_until='domcontentloaded', timeout=20000)
        # double scroll — catches lazy-loaded ATS results
        for _ in range(2):
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            page.wait_for_timeout(1500)

        links = page.evaluate('''() => {
            return Array.from(document.querySelectorAll("a"))
                .slice(0, 300)
                .map(a => {
                    const container = a.closest(
                        "li, article, [class*=job], [class*=card], [class*=result]"
                    );
                    return {
                        text:      (a.innerText || a.textContent || "").trim(),
                        href:      a.href || "",
                        container: container ? (container.innerText || "") : (
                            a.parentElement ? a.parentElement.innerText : ""
                        )
                    };
                });
        }''')

        for link in links:
            text      = link['text'].strip()
            href      = link['href'].strip()
            container = link['container'].strip()
            if (
                len(href) > 10
                and role_matches(text, role)
                and (is_gta(container) or is_gta(text) or 'ontario' in container.lower())
            ):
                jobs.append({
                    'id':       href,
                    'title':    text[:120],
                    'link':     href,
                    'source':   f'{bank} Careers',
                    'bank':     bank,
                    'category': category,
                })

        log.info(f"  [ATS] {bank} — {role} → {len(jobs)} found")
    except Exception as e:
        log.warning(f"  [ATS] {bank} — {role} → FAILED: {e}")
    return jobs

# ── Email ──────────────────────────────────────────────────────────────────────
def build_html_table(jobs: list) -> str:
    if not jobs:
        return "<p><i>No new jobs found in this category.</i></p>"
    rows = ''.join(
        f"<tr>"
        f"<td>{j['bank']}</td>"
        f"<td>{j['title']}</td>"
        f"<td>{j['source']}</td>"
        f"<td><a href='{j['link']}'>View</a></td>"
        f"</tr>"
        for j in jobs
    )
    return (
        "<table>"
        "<tr><th>Bank</th><th>Role</th><th>Source</th><th>Link</th></tr>"
        f"{rows}"
        "</table>"
    )

def send_email(new_jobs: list):
    if not RESEND_API_KEY:
        log.error("  [Email] RESEND_API_KEY not set — skipping send")
        return

    mortgage_jobs = [j for j in new_jobs if j['category'] == 'Mortgage']
    teller_jobs   = [j for j in new_jobs if j['category'] == 'Teller']

    html = f"""<html><head><style>
      body      {{ font-family: Arial, sans-serif; color: #333; }}
      table     {{ border-collapse: collapse; width: 100%; margin-bottom: 20px; }}
      th, td    {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
      th        {{ background: #f2f2f2; }}
      a         {{ color: #0066cc; font-weight: bold; text-decoration: none; }}
      h2        {{ color: #2c3e50; border-bottom: 2px solid #eee; padding-bottom: 5px; }}
    </style></head><body>
      <h1>Ontario Bank Job Alert</h1>
      <h2>Mortgage Roles ({len(mortgage_jobs)})</h2>
      {build_html_table(mortgage_jobs)}
      <h2>Teller / CSR Roles ({len(teller_jobs)})</h2>
      {build_html_table(teller_jobs)}
    </body></html>"""

    try:
        res = requests.post(
            'https://api.resend.com/emails',
            headers={
                'Authorization': f'Bearer {RESEND_API_KEY}',
                'Content-Type':  'application/json',
            },
            json={
                'from':    f'Job Scraper <{FROM_EMAIL}>',
                'to':      [ALERT_EMAIL],
                'subject': f'Job Alert: {len(new_jobs)} New Ontario Bank Role(s)',
                'html':    html,
            },
            timeout=15,
        )
        if res.status_code in (200, 201):
            log.info(f"  [Email] Sent — {len(new_jobs)} jobs to {ALERT_EMAIL}")
        else:
            log.error(f"  [Email] Failed {res.status_code}: {res.text}")
    except Exception as e:
        log.error(f"  [Email] Error: {e}")

# ── Collect helper ─────────────────────────────────────────────────────────────
def collect(jobs_list: list, new_jobs: list, seen: set, seen_fps: set):
    for job in jobs_list:
        fp = fingerprint(job)
        if job['id'] not in seen and fp not in seen_fps:
            new_jobs.append(job)
            seen_fps.add(fp)

# ── Main run ───────────────────────────────────────────────────────────────────
def run():
    log.info("═══ Scan started ═══")

    if not RESEND_API_KEY:
        log.warning("  [Warn] RESEND_API_KEY not set — jobs found but not emailed")

    seen:     set  = load_state()
    new_jobs: list = []
    seen_fps: set  = set()

    log.info(f"  [State] {len(seen)} jobs already seen")

    with sync_playwright() as p:
        browser, ctx = make_browser_context(p)
        page = ctx.new_page()

        # Phase 1 — Indeed
        log.info("  ── Phase 1: Indeed ──")
        for t in TARGETS:
            results = scrape_indeed(page, t['bank'], t['role'], t['category'])
            collect(results, new_jobs, seen, seen_fps)
            time.sleep(2)

        # Phase 2 — LinkedIn
        log.info("  ── Phase 2: LinkedIn ──")
        for t in TARGETS:
            results = scrape_linkedin(page, t['bank'], t['role'], t['category'])
            collect(results, new_jobs, seen, seen_fps)
            time.sleep(2)

        # Phase 3 — ATS (per-target 30s timeout so one stuck bank can't block)
        log.info("  ── Phase 3: ATS ──")
        for t in TARGETS:
            def _timeout(sig, frame):
                raise TimeoutError()
            signal.signal(signal.SIGALRM, _timeout)
            signal.alarm(30)
            try:
                results = scrape_bank_ats(page, t['bank'], t['role'], t['category'])
                signal.alarm(0)
                collect(results, new_jobs, seen, seen_fps)
            except TimeoutError:
                signal.alarm(0)
                log.warning(f"  [ATS] Timeout: {t['bank']} — {t['role']}")
            except Exception as e:
                signal.alarm(0)
                log.warning(f"  [ATS] Error: {t['bank']} — {t['role']}: {e}")
            time.sleep(1)

        browser.close()

    log.info(f"  [Result] {len(new_jobs)} new jobs found")

    if new_jobs:
        send_email(new_jobs)
        seen.update(job['id'] for job in new_jobs)
        save_state(seen)
    else:
        log.info("  [Result] Nothing new — no email sent")

    log.info("═══ Scan complete ═══")


if __name__ == '__main__':
    run()
    schedule.every(1).hours.do(run)
    while True:
        schedule.run_pending()
        time.sleep(30)
