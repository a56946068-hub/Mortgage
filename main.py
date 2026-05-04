import os
os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', '/app/pw-browsers')
import smtplib
import cloudscraper
from bs4 import BeautifulSoup
import json
import time
import urllib.parse
import logging
import schedule
import requests
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from playwright.sync_api import sync_playwright

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler('scraper.log'), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# --- RESEND CONFIG ---
RESEND_API_KEY = os.environ.get('RESEND_API_KEY')

STATE_FILE = 'seen_jobs.json'

TARGETS = [
    # Mortgage Roles
    {'bank': 'RBC', 'role': 'Mortgage Specialist Assistant', 'category': 'Mortgage'},
    {'bank': 'TD', 'role': 'Mobile Mortgage Specialist Assistant', 'category': 'Mortgage'},
    {'bank': 'BMO', 'role': 'Mortgage Specialist Associate', 'category': 'Mortgage'},
    {'bank': 'CIBC', 'role': 'Mortgage Advisor Assistant', 'category': 'Mortgage'},
    {'bank': 'Scotiabank', 'role': 'Home Financing Associate', 'category': 'Mortgage'},

    # Teller / CSR Roles
    {'bank': 'RBC', 'role': 'Client Advisor', 'category': 'Teller'},
    {'bank': 'TD', 'role': 'Customer Experience Associate', 'category': 'Teller'},
    {'bank': 'Scotiabank', 'role': 'Customer Experience Associate', 'category': 'Teller'},
    {'bank': 'BMO', 'role': 'Customer Service Representative', 'category': 'Teller'},
    {'bank': 'CIBC', 'role': 'Client Service Representative', 'category': 'Teller'},
    {'bank': 'National Bank of Canada', 'role': 'Banking Advisor', 'category': 'Teller'},
    {'bank': 'Meridian Credit Union', 'role': 'Member Service Representative', 'category': 'Teller'},
    {'bank': 'Laurentian Bank', 'role': 'Customer Service Officer', 'category': 'Teller'},
    {'bank': 'Desjardins', 'role': 'Member Service Advisor', 'category': 'Teller'},
]

BANK_URLS = {
    'RBC':        'https://jobs.rbc.com/ca/en/search-results?keywords={}&location=Ontario%2C%20Canada',
    'TD':         'https://jobs.td.com/en-CA/job-search-results/?keyword={}&location=Ontario',
    'BMO':        'https://jobs.bmo.com/ca/en/search-results?keywords={}&location=Ontario',
    'CIBC':       'https://cibc.wd3.myworkdayjobs.com/search?q={}%20Ontario',
    'Scotiabank': 'https://jobs.scotiabank.com/search/?q={}&locationsearch=Ontario',
}

# ── State ──────────────────────────────────────────────────────────────────────

def load_state() -> set:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return set(json.load(f))
    return set()

def save_state(state: set):
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(list(state), f)
    os.replace(tmp, STATE_FILE)

# ── Filters ────────────────────────────────────────────────────────────────────

def is_ontario(text):
    t = text.lower()
    return 'ontario' in t or ', on' in t or ' on ' in t

# ── Scrapers ───────────────────────────────────────────────────────────────────

def scrape_indeed(scraper, bank, role, category):
    query = urllib.parse.quote(f'"{role}" "{bank}"')
    url   = f"https://ca.indeed.com/jobs?q={query}&l=Ontario&sort=date"
    res   = scraper.get(url, timeout=15)
    soup  = BeautifulSoup(res.text, 'html.parser')
    jobs  = []
    for card in soup.find_all('div', class_='job_seen_beacon'):
        title = card.find('h2', class_='jobTitle')
        link  = card.find('a', class_='jcs-JobTitle')
        loc   = card.find('div', class_='companyLocation')
        loc_text = loc.text if loc else ""

        if title and link and is_ontario(loc_text):
            jobs.append({
                'id':       link.get('data-jk', link['href']),
                'title':    title.text.strip(),
                'link':     'https://ca.indeed.com' + link['href'],
                'source':   'Indeed',
                'bank':     bank,
                'category': category
            })
    return jobs

def scrape_linkedin(scraper, bank, role, category):
    query = urllib.parse.quote(f'"{role}" "{bank}"')
    url   = f"https://www.linkedin.com/jobs/search/?keywords={query}&location=Ontario&f_TPR=r86400"
    res   = scraper.get(url, timeout=15)
    soup  = BeautifulSoup(res.text, 'html.parser')
    jobs  = []
    for card in soup.find_all('div', class_='base-card'):
        title = card.find('h3', class_='base-search-card__title')
        link  = card.find('a', class_='base-card__full-link')
        loc   = card.find('span', class_='job-search-card__location')
        loc_text = loc.text if loc else ""

        if title and link and is_ontario(loc_text):
            href = link['href'].split('?')[0]
            jobs.append({
                'id':       href,
                'title':    title.text.strip(),
                'link':     href,
                'source':   'LinkedIn',
                'bank':     bank,
                'category': category
            })
    return jobs

def scrape_bank_ats(targets):
    jobs = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx     = browser.new_context(viewport={'width': 1920, 'height': 1080})
        for target in targets:
            bank     = target['bank']
            role     = target['role']
            category = target['category']

            if bank not in BANK_URLS:
                continue

            query = urllib.parse.quote(role)
            url   = BANK_URLS[bank].format(query)
            page  = ctx.new_page()
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=30000)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(3000)
                tokens = set(role.lower().split())
                for a in page.locator('a').all():
                    text = (a.inner_text() or '').strip().lower()
                    href = a.get_attribute('href') or ''
                    parent_text = (a.locator('xpath=..').inner_text() or '').strip().lower()

                    if all(t in text for t in tokens) and len(href) > 5 and is_ontario(parent_text + " " + text):
                        full = urllib.parse.urljoin(url, href)
                        jobs.append({
                            'id':       full,
                            'title':    text.title(),
                            'link':     full,
                            'source':   f'{bank} Careers',
                            'bank':     bank,
                            'category': category
                        })
            except Exception as e:
                log.warning(f"ATS scrape failed [{bank}]: {e}")
            finally:
                page.close()
        browser.close()
    return jobs

# ── Dedup ──────────────────────────────────────────────────────────────────────

def fingerprint(job):
    return f"{job['bank']}::{job['title'].lower().strip()}"

# ── Email ──────────────────────────────────────────────────────────────────────

def build_html_table(jobs):
    if not jobs:
        return "<p><i>No new jobs found in this category.</i></p>"

    rows = ''.join(f'''
        <tr>
          <td>{j['bank']}</td>
          <td>{j['title']}</td>
          <td>{j['source']}</td>
          <td><a href="{j['link']}">View</a></td>
        </tr>''' for j in jobs)

    return f'''
    <table>
      <tr><th>Bank</th><th>Role</th><th>Source</th><th>Link</th></tr>
      {rows}
    </table>
    '''

def send_email(new_jobs):
    mortgage_jobs = [j for j in new_jobs if j['category'] == 'Mortgage']
    teller_jobs   = [j for j in new_jobs if j['category'] == 'Teller']

    html = f'''
    <html><head><style>
      body {{ font-family:Arial,sans-serif; color:#333; }}
      table {{ border-collapse:collapse; width:100%; margin-bottom: 20px; }}
      th,td {{ border:1px solid #ddd; padding:8px; text-align:left; }}
      th {{ background:#f2f2f2; }}
      a {{ color:#0066cc; font-weight:bold; text-decoration:none; }}
      h2 {{ color: #2c3e50; border-bottom: 2px solid #eee; padding-bottom: 5px; }}
    </style></head><body>
      <h1>Ontario Bank Roles Update</h1>

      <h2>Mortgage Roles ({len(mortgage_jobs)})</h2>
      {build_html_table(mortgage_jobs)}

      <h2>Teller / CSR Roles ({len(teller_jobs)})</h2>
      {build_html_table(teller_jobs)}

    </body></html>'''

    try:
        res = requests.post(
            'https://api.resend.com/emails',
            headers={
                'Authorization': f'Bearer {RESEND_API_KEY}',
                'Content-Type': 'application/json'
            },
            json={
                'from': 'Job Scraper <onboarding@resend.dev>',
                'to': ['n.hesabian@gmail.com', 'soldbyfarshad@gmail.com'],
                'subject': f'Job Alert: {len(new_jobs)} New Ontario Role(s)',
                'html': html
            }
        )
        if res.status_code == 200:
            log.info(f"Resend email sent — {len(new_jobs)} new jobs")
        else:
            log.error(f"Resend failed: {res.status_code} - {res.text}")
    except Exception as e:
        log.error(f"Resend error: {e}")

# ── Core ───────────────────────────────────────────────────────────────────────

def run():
    log.info("── Scan started ──")
    scraper  = cloudscraper.create_scraper(
        browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
    )
    seen     = load_state()
    new_jobs = []
    seen_fps = set()

    for target in TARGETS:
        for func in (scrape_indeed, scrape_linkedin):
            try:
                for job in func(scraper, target['bank'], target['role'], target['category']):
                    fp = fingerprint(job)
                    if job['id'] not in seen and fp not in seen_fps:
                        new_jobs.append(job)
                        seen_fps.add(fp)
            except Exception as e:
                log.warning(f"{func.__name__} failed [{target['bank']}]: {e}")
            time.sleep(3)

    try:
        for job in scrape_bank_ats(TARGETS):
            fp = fingerprint(job)
            if job['id'] not in seen and fp not in seen_fps:
                new_jobs.append(job)
                seen_fps.add(fp)
    except Exception as e:
        log.warning(f"ATS scrape failed: {e}")

    if new_jobs:
        send_email(new_jobs)
        seen.update(job['id'] for job in new_jobs)
        save_state(seen)
    else:
        log.info("No new jobs found")

    log.info("── Scan complete ──")

# ── Scheduler ─────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    run()
    schedule.every(1).hours.do(run)
    while True:
        schedule.run_pending()
        time.sleep(30)
