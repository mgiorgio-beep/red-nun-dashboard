"""FANZO Scraper - Auto-Auth via Gmail IMAP. No more expiring cookies."""
import os, re, json, time, imaplib, email, logging, requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import pytz

logger = logging.getLogger(__name__)

EASTERN = pytz.timezone('America/New_York')

def _game_start_ts(date_short, time_str):
    """Convert guide date + time string to Unix timestamp (Eastern)."""
    try:
        time_clean = time_str.strip().lower()
        time_clean = re.sub(r'(\d)(am|pm)', r'\1 \2', time_clean)
        if ':' not in time_clean:
            time_clean = time_clean.replace(' am', ':00 am').replace(' pm', ':00 pm')
        dt_naive = datetime.strptime(f"{date_short} {time_clean}", "%m/%d/%y %I:%M %p")
        dt_eastern = EASTERN.localize(dt_naive)
        return int(dt_eastern.timestamp())
    except Exception:
        return None

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
DATA_FILE = os.path.join(DATA_DIR, 'sports_guide.json')
FANZO_GUIDE_URL = 'https://guide.thedailyrail.com/guide/display/239743'
FANZO_LOGIN_PAGE = 'https://guide.thedailyrail.com/'

FAVORITE_TEAMS = ['bruins','celtics','patriots','red sox','harvard','dartmouth','boston college','boston col.','umass','bc']

STREAMING_SERVICES = {
    'espnplus': {'display':'ESPN+','color':'#3B82F6','app':'ESPN App'},
    'espn+': {'display':'ESPN+','color':'#3B82F6','app':'ESPN App'},
    'peacock sp': {'display':'Peacock','color':'#8B6CEF','app':'Peacock App'},
    'peacock': {'display':'Peacock','color':'#8B6CEF','app':'Peacock App'},
    'apple tv+': {'display':'Apple TV+','color':'#333333','app':'Apple TV App'},
    'amazon prime': {'display':'Prime Video','color':'#00A8E1','app':'Prime App'},
    'prime video': {'display':'Prime Video','color':'#00A8E1','app':'Prime App'},
    'max': {'display':'Max','color':'#002BE7','app':'Max App'},
    'paramount+': {'display':'Paramount+','color':'#0064FF','app':'Paramount+ App'},
}

SPORT_ICONS = {
    'nba basketball':'\U0001F3C0','basketball':'\U0001F3C0','golf':'\u26F3',
    'nascar':'\U0001F3C1','auto racing':'\U0001F3C1','hockey':'\U0001F3D2',
    'nhl':'\U0001F3D2','lacrosse':'\U0001F94D','soccer':'\u26BD',
    'football':'\U0001F3C8','olympics':'\U0001F3C5','baseball':'\u26BE',
    'softball':'\U0001F94E','tennis':'\U0001F3BE','ncaa basketball':'\U0001F393',
    "ncaa basketball - men's":'\U0001F393',"ncaa basketball - women's":'\U0001F393',
    'favorites':'\u2B50','streaming':'\U0001F4E1',
}

HEADERS = {
    'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36',
    'Accept':'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
}

def get_guide_link_from_gmail():
    from dotenv import load_dotenv; load_dotenv()
    addr = os.getenv('GMAIL_ADDRESS') or os.getenv('INVOICE_EMAIL')
    pw = os.getenv('GMAIL_APP_PASSWORD') or os.getenv('INVOICE_EMAIL_APP_PASSWORD')
    if not addr or not pw: return None
    try:
        mail = imaplib.IMAP4_SSL('imap.gmail.com', 993)
        mail.login(addr, pw)
        mail.select('INBOX')
        since = (datetime.now() - timedelta(days=3)).strftime('%d-%b-%Y')
        link = None
        for src in ['thedailyrail.com','fanzo','sportstv']:
            st, msgs = mail.search(None, '(FROM "%s" SINCE %s)' % (src, since))
            if st != 'OK' or not msgs[0]: continue
            for mid in reversed(msgs[0].split()):
                st2, md = mail.fetch(mid, '(RFC822)')
                if st2 != 'OK': continue
                msg = email.message_from_bytes(md[0][1])
                body = _get_body(msg)
                urls = re.findall(r'https?://guide\.thedailyrail\.com/[^\s"\'<>]+', body)
                if urls:
                    disp = [u for u in urls if '/guide/display/' in u]
                    link = (disp[0] if disp else urls[0]).rstrip('.'); link = re.sub(r'(View|Launch|Click|Here|Guide)+$', '', link)
                    logger.info("Found guide link: %s", link)
                    break
            if link: break
        mail.logout()
        return link
    except Exception as e:
        logger.error("Gmail error: %s", e); return None

def _get_body(msg):
    body = ''
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ('text/html','text/plain'):
                try: body += part.get_payload(decode=True).decode(part.get_content_charset() or 'utf-8', errors='replace')
                except: pass
    else:
        try: body = msg.get_payload(decode=True).decode(msg.get_content_charset() or 'utf-8', errors='replace')
        except: pass
    return body

def request_new_fanzo_link():
    from dotenv import load_dotenv; load_dotenv()
    addr = os.getenv('GMAIL_ADDRESS') or os.getenv('INVOICE_EMAIL')
    if not addr: return False
    try:
        s = requests.Session(); s.headers.update(HEADERS)
        r = s.get(FANZO_LOGIN_PAGE, timeout=30)
        soup = BeautifulSoup(r.text, 'html.parser')
        form = soup.find('form')
        if not form: return False
        action = form.get('action', FANZO_LOGIN_PAGE)
        if not action.startswith('http'): action = FANZO_LOGIN_PAGE.rstrip('/') + '/' + action.lstrip('/')
        data = {}
        for inp in form.find_all('input'):
            n = inp.get('name')
            if n:
                data[n] = addr if (inp.get('type')=='email' or 'email' in n.lower()) else inp.get('value','')
        logger.info("Requesting new FANZO link to %s", addr)
        r = s.post(action, data=data, timeout=30)
        return r.status_code == 200
    except Exception as e:
        logger.error("Request link failed: %s", e); return False

def fetch_guide_html():
    from dotenv import load_dotenv; load_dotenv()
    s = requests.Session(); s.headers.update(HEADERS)
    cookie = os.getenv('FANZO_SESSION_COOKIE','')
    if cookie:
        logger.info("Trying existing cookie...")
        s.cookies.set('PHPSESSID', cookie, domain='guide.thedailyrail.com')
        r = s.get(FANZO_GUIDE_URL, timeout=30)
        if r.status_code == 200 and 'section-to-print' in r.text:
            logger.info("Cookie valid!"); return r.text
        logger.info("Cookie expired, trying Gmail...")
    link = get_guide_link_from_gmail()
    if not link:
        logger.info("No email found, requesting new link...")
        if request_new_fanzo_link():
            for i in range(6):
                time.sleep(30)
                logger.info("Check Gmail attempt %d/6...", i+1)
                link = get_guide_link_from_gmail()
                if link: break
    if not link: raise Exception("FANZO auth failed: no guide link")
    logger.info("Following: %s", link)
    f = requests.Session(); f.headers.update(HEADERS)
    r = f.get(link, timeout=30)
    if r.status_code == 200 and 'section-to-print' in r.text:
        nc = f.cookies.get('PHPSESSID', domain='guide.thedailyrail.com')
        if nc: _save_cookie(nc)
        return r.text
    if r.status_code == 200:
        r2 = f.get(FANZO_GUIDE_URL, timeout=30)
        if r2.status_code == 200 and 'section-to-print' in r2.text:
            nc = f.cookies.get('PHPSESSID', domain='guide.thedailyrail.com')
            if nc: _save_cookie(nc)
            return r2.text
    raise Exception("FANZO fetch failed (%d)" % r.status_code)

def _save_cookie(nc):
    ep = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env')
    try:
        with open(ep,'r') as f: lines = f.readlines()
        found = False
        for i,l in enumerate(lines):
            if l.startswith('FANZO_SESSION_COOKIE='):
                lines[i] = 'FANZO_SESSION_COOKIE=%s\n' % nc; found = True; break
        if not found: lines.append('FANZO_SESSION_COOKIE=%s\n' % nc)
        with open(ep,'w') as f: f.writelines(lines)
        logger.info("Saved new cookie to .env")
    except Exception as e: logger.warning("Could not update .env: %s", e)

def _icon(name):
    sn = name.lower().strip()
    for k,v in SPORT_ICONS.items():
        if k in sn: return v
    return '\U0001F4FA'

def _is_fav(text):
    return any(t in text.lower() for t in FAVORITE_TEAMS)

def _stream_badge(svc):
    key = svc.lower().strip()
    key = key.replace('peaccok', 'peacock')  # FANZO typo
    return STREAMING_SERVICES.get(key)

def _parse_ch(game, cc, dc):
    if not cc: return
    cns = [s.strip() for s in cc.get_text(separator='|').split('|') if s.strip()]
    dns = [s.strip() for s in dc.get_text(separator='|').split('|') if s.strip()] if dc else []
    for i,n in enumerate(cns):
        game['channels'].append({'name':n,'drtv':dns[i] if i<len(dns) else ''})

def parse_guide_html(html):
    soup = BeautifulSoup(html, 'html.parser')
    dd = soup.find('div', class_='blue-divider')
    dt = ''
    if dd:
        h4 = dd.find('h4')
        if h4:
            dt = h4.get_text(strip=True)
            dt = re.sub(r'\s+by\s+(Sport|Time|QScore).*$','',dt,flags=re.IGNORECASE)
    content = soup.find('div', id='section-to-print')
    if not content: return None
    secs = []; cur = None
    for el in content.children:
        if not hasattr(el,'name'): continue
        if el.name == 'h3':
            sn = el.get_text(strip=True)
            cur = {'name':sn,'icon':_icon(sn),'games':[]}; secs.append(cur)
        elif el.name == 'div' and 'table-responsive' in el.get('class',[]):
            if not cur: continue
            tbl = el.find('table')
            if not tbl: continue
            tb = tbl.find('tbody') or tbl
            for row in tb.find_all('tr'):
                cells = row.find_all('td')
                if not cells: continue
                si = 1 if cells[0].find('a',class_='listing-note-btn') else 0; si += 1
                dc = cells[si:]
                if len(dc) < 3: continue
                g = _parse_row(dc, cur['name'])
                if g: cur['games'].append(g)
    # Extract date_short (e.g. "2/15/26") from dt (e.g. "Sunday 2/15/26")
    date_short = ''
    dm = re.search(r'(\d{1,2}/\d{1,2}/\d{2,4})', dt)
    if dm:
        date_short = dm.group(1)
    # Dedup: merge games with same event+time within each section
    for sec in secs:
        merged = {}
        for g in sec.get("games", []):
            key = (g.get("event",""), g.get("time",""))
            if key in merged:
                for ch in g.get("channels", []):
                    if ch and ch not in merged[key].get("channels", []):
                        merged[key].setdefault("channels", []).append(ch)
            else:
                merged[key] = g
        sec["games"] = list(merged.values())
    # Add start_ts to every game
    if date_short:
        for sec in secs:
            for g in sec.get("games", []):
                g['start_ts'] = _game_start_ts(date_short, g.get('time', ''))
    return {'date':dt,'updated_at':datetime.now().isoformat(),'sections':secs}

def _parse_row(cells, sn):
    sl = sn.lower()
    g = {'favorite':False,'channels':[],'streaming':None}
    try:
        if 'favorites' in sl:
            g['time']=cells[0].get_text(strip=True); g['sport']=cells[1].get_text(strip=True)
            g['event']=cells[2].get_text(strip=True); g['detail']=cells[3].get_text(strip=True) if len(cells)>3 else ''
            _parse_ch(g, cells[4] if len(cells)>4 else None, cells[5] if len(cells)>5 else None)
            g['favorite']=True
        elif 'streaming' in sl:
            g['time']=cells[0].get_text(strip=True); g['sport']=cells[1].get_text(strip=True)
            g['event']=cells[2].get_text(strip=True); g['detail']=cells[3].get_text(strip=True) if len(cells)>3 else ''
            svc = cells[4].get_text(strip=True) if len(cells)>4 else ''
            b = _stream_badge(svc)
            g['streaming'] = b if b else {'display':svc,'color':'#666','app':svc}
        elif 'golf' in sl:
            g['event']=cells[0].get_text(strip=True); g['detail']=cells[1].get_text(strip=True)
            g['time']=cells[2].get_text(strip=True)
            _parse_ch(g, cells[3] if len(cells)>3 else None, cells[4] if len(cells)>4 else None)
        elif 'nascar' in sl or 'auto racing' in sl:
            g['event']=cells[0].get_text(strip=True); g['detail']=cells[1].get_text(strip=True)
            g['time']=cells[2].get_text(strip=True)
            _parse_ch(g, cells[3] if len(cells)>3 else None, cells[4] if len(cells)>4 else None)
        elif 'other' in sl:
            g['sport']=cells[0].get_text(strip=True); g['event']=cells[1].get_text(strip=True)
            g['time']=cells[2].get_text(strip=True)
            _parse_ch(g, cells[3] if len(cells)>3 else None, cells[4] if len(cells)>4 else None)
        else:
            g['event']=cells[0].get_text(strip=True); g['detail']=cells[1].get_text(strip=True)
            g['time']=cells[2].get_text(strip=True)
            _parse_ch(g, cells[3] if len(cells)>3 else None, cells[4] if len(cells)>4 else None)
        if not g['favorite']:
            g['favorite'] = _is_fav('%s %s %s' % (g.get('event',''),g.get('detail',''),g.get('sport','')))
        for ch in g['channels']:
            b = _stream_badge(ch.get('name',''))
            if b and not g['streaming']: g['streaming'] = b
        return g
    except Exception as e:
        logger.warning("Parse error: %s", e); return None

def save_sports_data(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(DATA_FILE,'w') as f: json.dump(data,f,indent=2)

def load_sports_data():
    try:
        with open(DATA_FILE,'r') as f: return json.load(f)
    except: return None

def _streaming_target(sport, event):
    """Route streaming games to correct section using event prefix."""
    ev = event.strip()
    is_womens = ev.startswith('(W)NCAA:') or ev.startswith('(W)NCCA:')
    is_ncaa = ev.startswith('NCAA:') or is_womens
    sl = sport.lower()
    if 'basketball' in sl:
        if is_womens: return "women"
        if is_ncaa: return "men"
        return "nba"
    if 'baseball' in sl:
        if is_ncaa: return "ncaa baseball"
        return "mlb"
    if 'hockey' in sl:
        if is_ncaa: return "ncaa hockey"
        return "nhl"
    if 'football' in sl:
        if is_ncaa: return "ncaa football"
        return "nfl"
    return None

def _merge_streaming(data):
    if not data or not data.get('sections'): return data
    streaming = None
    sport_secs = {}
    for sec in data['sections']:
        if 'streaming' in sec['name'].lower():
            streaming = sec
        else:
            sport_secs[sec['name'].lower()] = sec
    if not streaming: return data
    for game in streaming['games']:
        sport = game.get('sport','').lower()
        event = game.get('event','')
        placed = False
        # Smart match using event prefix (NCAA vs NBA etc)
        target = _streaming_target(sport, event)
        if target:
            for sname, sec in sport_secs.items():
                if target in sname:
                    game['streaming_only'] = True
                    sec['games'].append(game)
                    placed = True; break
        # Fallback: exact sport match
        if not placed:
            for sname, sec in sport_secs.items():
                if sport and (sport in sname or sname in sport):
                    game['streaming_only'] = True
                    sec['games'].append(game)
                    placed = True; break
        # Last resort: create new section
        if not placed and sport:
            new_sec = {'name': sport.title(), 'icon': _icon(sport), 'games': [game]}
            game['streaming_only'] = True
            data['sections'].append(new_sec)
            sport_secs[sport] = new_sec
    data['sections'] = [s for s in data['sections'] if 'streaming' not in s['name'].lower()]
    return data

def scrape_fanzo_guide():
    try:
        html = fetch_guide_html()
        data = parse_guide_html(html)
        data = _merge_streaming(data)
        if data and data.get('sections'):
            save_sports_data(data)
            logger.info("Scraped: %s, %d games", data.get('date','?'), sum(len(s['games']) for s in data['sections']))
            return True
        logger.error("No sections"); return False
    except Exception as e:
        logger.error("Scrape failed: %s", e); return False

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    from dotenv import load_dotenv; load_dotenv()
    if scrape_fanzo_guide():
        d = load_sports_data()
        print("Date:", d['date'])
        for s in d['sections']: print("  %s %s: %d" % (s['icon'],s['name'],len(s['games'])))
    else: print("Failed")
