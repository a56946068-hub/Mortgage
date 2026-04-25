import os
import cloudscraper
from bs4 import BeautifulSoup
import requests
import json
import time
import urllib.parse
import logging
import schedule
from playwright.sync_api import sync_playwright

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler('scraper.log'), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# --- MAILGUN CONFIG ---
MAILGUN_API_KEY = '13c9f584ed3ef30c297bf31809e973c2-e3c0807f-ddb8ee73'
MAILGUN_DOMAIN  = 'sandbox816ef6c8d4b6442abba468bc583c6726.mailgun.org'
EMAIL_RECEIVER  = 'n.hesabian@gmail.com'

STATE_FILE = 'seen_jobs.json'

TARGETS = [
    {'bank': 'RBC',        'role': 'Mortgage Specialist Assistant'},
    {'bank': 'TD',         'role': 'Mobile Mortgage Specialist Assistant'},
    {'bank': 'BMO',        'role': 'Mortgage Specialist Associate'},
    {'bank': 'CIBC',       'role': 'Mortgage Advisor Assistant'},
    {'bank': 'Scotiabank', 'role': 'Home Financing Associate'},
]

BANK_URLS = {
    'RBC':        'https://jobs.rbc.com/ca/en/search-results?keywords={}',
    'TD':         'https://jobs.td.com/en-CA/job-search-results/?keyword={}',
    'BMO':        'https://jobs.bmo.com/ca/en/search-results?keywords={}',
    'CIBC':       'https://cibc.wd3.myworkdayjobs.com/search?q={}',
    'Scotiabank': 'https://jobs.scotiabank.com/search/?q={}',
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

# ── Scrapers ───────────────────────────────────────────────────────────────────

def scrape_indeed(scraper, bank, role):
    query = urllib.parse.quote(f'"{role}" "{bank}"')
    url   = f"https://ca.indeed.com/jobs?q={query}&l=Ontario&sort=date"
    res   = scraper.get(url, timeout=15)
    soup  = BeautifulSoup(res.text, 'html.parser')
    jobs  = []
    for card in soup.find_all('div', class_='job_seen_beacon'):
        title = card.find('h2', class_='jobTitle')
        link  = card.find('a', class_='jcs-JobTitle')
        if title and link:
            jobs.append({
                'id':     link.get('data-jk', link['href']),
                'title':  title.text.strip(),
                'link':   'https://ca.indeed.com' + link['href'],
                'source': 'Indeed',
                'bank':   bank,
            })
    return jobs

def scrape_linkedin(scraper, bank, role):
    query = urllib.parse.quote(f'"{role}" "{bank}"')
    url   = f"https://www.linkedin.com/jobs/search/?keywords={query}&location=Ontario&f_TPR=r86400"
    res   = scraper.get(url, timeout=15)
    soup  = BeautifulSoup(res.text, 'html.parser')
    jobs  = []
    for card in soup.find_all('div', class_='base-card'):
        title = card.find('h3', class_='base-search-card__title')
        link  = card.find('a', class_='base-card__full-link')
        if title and link:
            href = link['href'].split('?')[0]
            jobs.append({
                'id':     href,
                'title':  title.text.strip(),
                'link':   href,
                'source': 'LinkedIn',
                'bank':   bank,
            })
    return jobs

def scrape_bank_ats(targets):
    jobs = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx     = browser.new_context(viewport={'width': 1920, 'height': 1080})
        for target in targets:
            bank  = target['bank']
            role  = target['role']
            query = urllib.parse.quote(f"{role} Ontario")
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
                    if all(t in text for t in tokens) and len(href) > 5:
                        full = urllib.parse.urljoin(url, href)
                        jobs.append({
                            'id':     full,
                            'title':  text.title(),
                            'link':   full,
                            'source': f'{bank} Careers',
                            'bank':   bank,
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

def send_email(new_jobs):
    rows = ''.join(f'''
        <tr>
          <td>{j['bank']}</td>
          <td>{j['title']}</td>
          <td>{j['source']}</td>
          <td><a href="{j['link']}">View</a></td>
        </tr>''' for j in new_jobs)

    html = f'''
    <html><head><style>
      table {{ border-collapse:collapse; width:100%; font-family:Arial,sans-serif; }}
      th,td {{ border:1px solid #ddd; padding:8px; text-align:left; }}
      th {{ background:#f2f2f2; }}
      a {{ color:#0066cc; font-weight:bold; text-decoration:none; }}
    </style></head><body>
      <h2>New Ontario Bank Roles</h2>
      <table><tr><th>Bank</th><th>Role</th><th>Source</th><th>Link</th></tr>
      {rows}
      </table>
    </body></html>'''

    url = f"https://api.mailgun.net/v3/{MAILGUN_DOMAIN}/messages"
    auth = ("api", MAILGUN_API_KEY)
  data = {
        "from": f"Job Scraper <mailgun@{MAILGUN_DOMAIN}>",
        # Add your email to this list separated by a comma
        "to": [EMAIL_RECEIVER, "your_own_email@example.com"], 
        "subject": f"Job Alert: {len(new_jobs)} New Ontario Role(s)",
        "html": html
    }

    res = requests.post(url, auth=auth, data=data)
    
    if res.status_code == 200:
        log.info(f"Mailgun email sent — {len(new_jobs)} new jobs")
    else:
        log.error(f"Mailgun failed: {res.status_code} - {res.text}")

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
                for job in func(scraper, target['bank'], target['role']):
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
