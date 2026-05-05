import os
import signal
import cloudscraper
from bs4 import BeautifulSoup
import json
import time
import urllib.parse
import logging
import schedule
import requests
from playwright.sync_api import sync_playwright

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler('scraper.log'), logging.StreamHandler()]
)
log = logging.getLogger(__name__)

RESEND_API_KEY = os.environ.get('RESEND_API_KEY')
STATE_FILE = 'seen_jobs.json'

GTA_CITIES = [
    'richmond hill', 'vaughan', 'markham', 'toronto', 'north york',
    'scarborough', 'mississauga', 'brampton', 'oakville', 'aurora',
    'newmarket', 'king city', 'woodbridge', 'maple', 'concord',
    'thornhill', 'stouffville', 'ajax', 'pickering', 'whitby'
]

TARGETS = [
    {'bank': 'RBC', 'role': 'Mortgage Specialist Assistant', 'category': 'Mortgage'},
    {'bank': 'TD', 'role': 'Mobile Mortgage Specialist Assistant', 'category': 'Mortgage'},
    {'bank': 'BMO', 'role': 'Mortgage Specialist Associate', 'category': 'Mortgage'},
    {'bank': 'CIBC', 'role': 'Mortgage Advisor Assistant', 'category': 'Mortgage'},
    {'bank': 'Scotiabank', 'role': 'Home Financing Associate', 'category': 'Mortgage'},
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
    'RBC':        'https://jobs.rbc.com/ca/en/search-results?keywords={}&location=Richmond+Hill%2C+Ontario%2C+Canada',
    'TD':         'https://jobs.td.com/en-CA/job-search-results/?keyword={}&location=Richmond+Hill',
    'BMO':        'https://jobs.bmo.com/ca/en/search-results?keywords={}&location=Richmond+Hill%2C+Ontario',
    'CIBC':       'https://cibc.wd3.myworkdayjobs.com/search?q={}%20Ontario',
    'Scotiabank': 'https://jobs.scotiabank.com/search/?q={}&locationsearch=Richmond+Hill',
}

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return set(json.load(f))
    return set()

def save_state(state):
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(list(state), f)
    os.replace(tmp, STATE_FILE)

def is_gta(text):
    t = text.lower()
    return any(city in t for city in GTA_CITIES)

def scrape_indeed(scraper, bank, role, category):
    log.info(f"  [Indeed] Scraping {bank} — {role}")
    query = urllib.parse.quote(f'"{role}" "{bank}"')
    url = f"https://ca.indeed.com/jobs?q={query}&l=L4C+1H8&radius=30&sort=date"
    try:
        res = scraper.get(url, timeout=10)
        soup = BeautifulSoup(res.text, 'html.parser')
        jobs = []
        for card in soup.find_all('div', class_='job_seen_beacon'):
            title = card.find('h2', class_='jobTitle')
            link = card.find('a', class_='jcs-JobTitle')
            if title and link:
                jobs.append({
                    'id': link.get('data-jk', link['href']),
                    'title': title.text.strip(),
                    'link': 'https://ca.indeed.com' + link['href'],
                    'source': 'Indeed',
                    'bank': bank,
                    'category': category
                })
        log.info(f"  [Indeed] {bank} — {role} → {len(jobs)} found")
        return jobs
    except Exception as e:
        log.warning(f"  [Indeed] {bank} — {role} → FAILED: {e}")
        return []

def scrape_linkedin(scraper, bank, role, category):
    log.info(f"  [LinkedIn] Scraping {bank} — {role}")
    query = urllib.parse.quote(f'"{role}" "{bank}"')
    url = f"https://www.linkedin.com/jobs/search/?keywords={query}&location=Richmond+Hill%2C+Ontario&distance=30&f_TPR=r86400"
    try:
        res = scraper.get(url, timeout=10)
        soup = BeautifulSoup(res.text, 'html.parser')
        jobs = []
        for card in soup.find_all('div', class_='base-card'):
            title = card.find('h3', class_='base-search-card__title')
            link = card.find('a', class_='base-card__full-link')
            loc = card.find('span', class_='job-search-card__location')
            loc_text = loc.text if loc else ""
            if title and link and is_gta(loc_text):
                href = link['href'].split('?')[0]
                jobs.append({
                    'id': href,
                    'title': title.text.strip(),
                    'link': href,
                    'source': 'LinkedIn',
                    'bank': bank,
                    'category': category
                })
        log.info(f"  [LinkedIn] {bank} — {role} → {len(jobs)} found")
        return jobs
    except Exception as e:
        log.warning(f"  [LinkedIn] {bank} — {role} → FAILED: {e}")
        return []

def scrape_bank_ats(targets):
    log.info("  [ATS] Starting browser")
    jobs = []
    js_parent = 'el => el.parentElement ? el.parentElement.innerText : ""'
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--single-process',
                '--no-zygote',
            ]
        )
        ctx = browser.new_context(viewport={'width': 1920, 'height': 1080})
        seen_urls = set()
        for target in targets:
            bank = target['bank']
            role = target['role']
            category = target['category']

            if bank not in BANK_URLS:
                continue

            url_key = f"{bank}::{role}"
            if url_key in seen_urls:
                log.info(f"  [ATS] Skipping duplicate {bank} — {role}")
                continue
            seen_urls.add(url_key)

            query = urllib.parse.quote(role)
            url = BANK_URLS[bank].format(query)
            log.info(f"  [ATS] Loading {bank} — {role}")
            page = ctx.new_page()
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=15000)
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1500)
                tokens = set(role.lower().split())
                all_links = page.locator('a').all()
                log.info(f"  [ATS] {bank} — {role} → {len(all_links)} links to scan")
                found = 0
                for a in all_links:
                    try:
                        text = a.evaluate('el => el.innerText').strip().lower()
                        href = a.evaluate('el => el.href') or ''
                        parent_text = a.evaluate(js_parent).strip().lower()
                    except Exception:
                        continue
                    if all(t in text for t in tokens) and len(href) > 5 and is_gta(parent_text + " " + text):
                        full = urllib.parse.urljoin(url, href)
                        jobs.append({
                            'id': full,
                            'title': text.title(),
                            'link': full,
                            'source': f'{bank} Careers',
                            'bank': bank,
                            'category': category
                        })
                        found += 1
                log.info(f"  [ATS] {bank} — {role} → {found} jobs found")
            except Exception as e:
                log.warning(f"  [ATS] {bank} — {role} → FAILED: {e}")
            finally:
                page.close()
        browser.close()
        log.info(f"  [ATS] Browser closed — {len(jobs)} total ATS jobs")
    return jobs

def fingerprint(job):
    return f"{job['bank']}::{job['title'].lower().strip()}"

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
    </table>'''

def send_email(new_jobs):
    log.info(f"  [Email] Sending {len(new_jobs)} jobs")
    mortgage_jobs = [j for j in new_jobs if j['category'] == 'Mortgage']
    teller_jobs = [j for j in new_jobs if j['category'] == 'Teller']
    html = f'''
    <html><head><style>
      body {{ font-family:Arial,sans-serif; color:#333; }}
      table {{ border-collapse:collapse; width:100%; margin-bottom:20px; }}
      th,td {{ border:1px solid #ddd; padding:8px; text-align:left; }}
      th {{ background:#f2f2f2; }}
      a {{ color:#0066cc; font-weight:bold; text-decoration:none; }}
      h2 {{ color:#2c3e50; border-bottom:2px solid #eee; padding-bottom:5px; }}
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
                'to': ['n.hesabian@gmail.com'],
                'subject': f'Job Alert: {len(new_jobs)} New Ontario Role(s)',
                'html': html
            }
        )
        if res.status_code == 200:
            log.info(f"  [Email] Sent successfully — {len(new_jobs)} jobs")
        else:
            log.error(f"  [Email] Failed: {res.status_code} - {res.text}")
    except Exception as e:
        log.error(f"  [Email] Error: {e}")

def run():
    log.info("── Scan started ──")
    log.info("  [Init] Loading state")
    scraper = cloudscraper.create_scraper(
        browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
    )
    scraper.timeout = 10
    seen = load_state()
    log.info(f"  [Init] {len(seen)} jobs already seen")
    new_jobs = []
    seen_fps = set()

    log.info("  [Phase 1] Starting Indeed + LinkedIn scrape")
    for target in TARGETS:
        for func in (scrape_indeed, scrape_linkedin):
            try:
                for job in func(scraper, target['bank'], target['role'], target['category']):
                    fp = fingerprint(job)
                    if job['id'] not in seen and fp not in seen_fps:
                        new_jobs.append(job)
                        seen_fps.add(fp)
            except Exception as e:
                log.warning(f"  {func.__name__} failed [{target['bank']}]: {e}")
            time.sleep(1)
    log.info(f"  [Phase 1] Complete — {len(new_jobs)} new jobs so far")

    log.info("  [Phase 2] Starting ATS scrape")
    def timeout_handler(signum, frame):
        raise TimeoutError("ATS scrape timed out")
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(120)
    try:
        for job in scrape_bank_ats(TARGETS):
            fp = fingerprint(job)
            if job['id'] not in seen and fp not in seen_fps:
                new_jobs.append(job)
                seen_fps.add(fp)
        signal.alarm(0)
    except (Exception, TimeoutError) as e:
        signal.alarm(0)
        log.warning(f"  [Phase 2] ATS timed out or failed: {e}")
    log.info(f"  [Phase 2] Complete — {len(new_jobs)} total new jobs")

    if new_jobs:
        send_email(new_jobs)
        seen.update(job['id'] for job in new_jobs)
        save_state(seen)
    else:
        log.info("  [Result] No new jobs found")

    log.info("── Scan complete ──")

if __name__ == '__main__':
    run()
    schedule.every(1).hours.do(run)
    while True:
        schedule.run_pending()
        time.sleep(30)
