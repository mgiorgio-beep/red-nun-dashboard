"""
Dashboard navigation check (UI plan 2026-10-08, Phase 0 safety net).

Logs in as a role (default admin) in a headless browser and CLICKS every sidebar
item the way a person would, then checks:
  - the page loads (HTTP 200, no console errors from our own code)
  - the item clicked is the one highlighted
  - pages that use the dashboard sidebar actually render it (styled, ~230px wide)
  - opening the page doesn't write anything (every non-GET is blocked and reported;
    ALLOWED_WRITES lists the known, harmless ones)
Also opens the standalone pages directly and checks they load.

Read-only: every POST/PUT/PATCH/DELETE is blocked. Run after every UI change:

  venv/bin/python3 tests/ui_check.py                 # admin; prints a table, exit 1 on failure
  venv/bin/python3 tests/ui_check.py --role manager  # what a manager sees
  venv/bin/python3 tests/ui_check.py --save          # record the current state as the baseline
Compares with tests/ui_baseline.json when present and lists what changed.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
from dotenv import load_dotenv  # noqa: E402
load_dotenv(os.path.join(ROOT, '.env'))

BASE = os.getenv('UI_CHECK_BASE', 'http://127.0.0.1:8080')
BASELINE = os.path.join(ROOT, 'tests', 'ui_baseline.json')
# Known writes on page load that are harmless (documented in the UI plan).
ALLOWED_WRITES = {'POST /staff/api/tvs/sync'}
# Known broken, tracked in the UI plan (not a regression): Analytics Bev/Food Cost lost
# their /api/cogs/* endpoints in the MarginEdge removal (bac5bf6, 2026-04-16).
KNOWN = {'nav-bevcost', 'nav-foodcost'}
# Standalone pages (no dashboard sidebar by design) that must still load.
STANDALONE = ['/count', '/count/list', '/week', '/storage', '/unit-setup', '/voice-recipe', '/transfer', '/waste',
              '/staff', '/staff/specials', '/staff/specials/edit', '/staff/specials/tv', '/login', '/hiring', '/availability']
USERS = {'admin': (1, 'admin'), 'manager': (None, 'manager'), 'accountant': (None, 'accountant')}


def session_cookie(role):
    from web.server import app
    from integrations.toast.data_store import get_connection
    uid = USERS[role][0]
    if uid is None:
        c = get_connection()
        r = c.execute("SELECT id FROM users WHERE role = ? ORDER BY id LIMIT 1", (role,)).fetchone()
        c.close()
        uid = r['id'] if r else 999999
    data = {'user_id': uid, 'username': f'ui-check-{role}', 'full_name': f'UI check ({role})', 'role': role,
            'location': 'both', 'email': 'ui-check@rednun.com'}
    return app.session_interface.get_signing_serializer(app).dumps(data)


def own(url):
    return url.startswith(BASE) or url.startswith('/')


def watch(page, info):
    page.on('console', lambda m: info['console'].append(m.text[:140])
            if m.type == 'error' and 'espn' not in m.text and 'Access to fetch' not in m.text else None)
    page.on('response', lambda r: info['failed'].append(f"{r.status} {r.url.replace(BASE, '')[:80]}")
            if r.status >= 400 and own(r.url) else None)

    def guard(route, req):
        if req.method != 'GET':
            w = f"{req.method} {req.url.replace(BASE, '').split('?')[0]}"
            info['writes'].append(w)
            return route.abort()
        return route.continue_()
    page.route('**/*', guard)


def run(role='admin'):
    from playwright.sync_api import sync_playwright
    out = {'role': role, 'sidebar': [], 'standalone': []}
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        ctx = b.new_context(viewport={'width': 1440, 'height': 900})
        ctx.add_cookies([{'name': 'session', 'value': session_cookie(role), 'domain': '127.0.0.1', 'path': '/'}])
        p = ctx.new_page()
        p.goto(BASE + '/manage', wait_until='domcontentloaded')
        p.wait_for_timeout(1500)
        items = p.evaluate("""() => [...document.querySelectorAll('.rn-sb-child')].map(e => ({
            id: e.id, label: e.innerText.trim(), visible: !!e.offsetParent || getComputedStyle(e).display !== 'none' && e.closest('.rn-sb-group') && getComputedStyle(e.closest('.rn-sb-group')).display !== 'none'
          }))""")
        p.close()
        for it in items:
            info = {'id': it['id'], 'label': it['label'], 'visible': it['visible'], 'console': [], 'failed': [], 'writes': []}
            pg = ctx.new_page()
            watch(pg, info)
            try:
                pg.goto(BASE + '/manage', wait_until='domcontentloaded')
                pg.wait_for_timeout(900)
                info['console'].clear(); info['failed'].clear(); info['writes'].clear()   # only what the click causes
                pg.evaluate(f"""() => {{ const el = document.getElementById({json.dumps(it['id'])}); if (el) el.click(); }}""")
                pg.wait_for_timeout(2500)
                info.update(pg.evaluate("""() => {
                    const sb = document.querySelector('.rn-sidebar');
                    const act = document.querySelector('.rn-sb-child.active');
                    return {url: location.pathname + location.search + location.hash,
                            sidebar: !!sb, sidebar_ok: !!sb && Math.abs(sb.getBoundingClientRect().width - 230) < 40,
                            active: act ? act.id : null,
                            title: ((document.querySelector('#pageTitle, h1, .page-title, .hdr-title') || {}).innerText || '').trim().slice(0, 60),
                            text: document.body.innerText.length};
                }"""))
            except Exception as e:
                info['error'] = str(e)[:160]
            pg.close()
            info['problems'] = problems(info, sidebar_page=True)
            out['sidebar'].append(info)
        for path in STANDALONE:
            info = {'id': path, 'label': path, 'console': [], 'failed': [], 'writes': []}
            pg = ctx.new_page()
            watch(pg, info)
            try:
                r = pg.goto(BASE + path, wait_until='domcontentloaded')
                pg.wait_for_timeout(1800)
                info['status'] = r.status if r else None
                info['text'] = pg.evaluate('document.body.innerText.length')
            except Exception as e:
                info['error'] = str(e)[:160]
            pg.close()
            info['problems'] = problems(info, sidebar_page=False)
            out['standalone'].append(info)
        b.close()
    return out


def problems(i, sidebar_page):
    p = []
    if i.get('error'):
        p.append('error: ' + i['error'])
    if [f for f in i['failed'] if f.startswith('5') or f.startswith('404')]:
        p.append('failed: ' + '; '.join(i['failed'][:2]))
    # our own blocked writes, and the browser's generic 'Failed to load resource' (our own
    # failed requests are already in i['failed'] with their URL; outside sites aren't ours)
    cons = [c for c in i['console'] if not c.startswith('Failed to load resource')]
    if cons:
        p.append('console: ' + cons[0])
    bad_writes = [w for w in i['writes'] if w not in ALLOWED_WRITES]
    if bad_writes:
        p.append('writes on load: ' + ', '.join(sorted(set(bad_writes))))
    if sidebar_page and i.get('visible'):
        if i.get('sidebar') and not i.get('sidebar_ok'):
            p.append('sidebar not styled (missing sidebar.css?)')
        if i.get('sidebar') and i.get('active') != i['id']:
            p.append(f"highlights {i.get('active')} instead")
        if (i.get('text') or 0) < 40:
            p.append('page looks empty')
    if not sidebar_page and i.get('status') not in (200, None):
        p.append(f"HTTP {i.get('status')}")
    return p


def main():
    role = sys.argv[sys.argv.index('--role') + 1] if '--role' in sys.argv else 'admin'
    res = run(role)
    rows = res['sidebar'] + res['standalone']
    fails = [r for r in rows if r['problems'] and r['id'] not in KNOWN]
    for r in rows:
        mark = ('known' if r['id'] in KNOWN else 'FAIL') if r['problems'] else ' ok '
        vis = '' if r.get('visible', True) else ' (hidden)'
        print(f"{mark} {r['label'][:24]:24}{vis:9} {r.get('url', '')[:34]:34} {'; '.join(r['problems'])[:120]}")
    print(f"\n{len(rows) - len(fails)}/{len(rows)} ok for role {role}")
    path = BASELINE.replace('.json', f'_{role}.json') if role != 'admin' else BASELINE
    if os.path.exists(path) and '--save' not in sys.argv:
        base = {r['id']: r for r in json.load(open(path))['sidebar'] + json.load(open(path))['standalone']}
        now = {r['id']: r for r in rows}
        changed = []
        for k in sorted(set(base) | set(now)):
            a, b = base.get(k), now.get(k)
            if not a:
                changed.append(f"+ new: {b['label']}")
            elif not b:
                changed.append(f"- gone: {a['label']}")
            elif bool(a['problems']) != bool(b['problems']):
                changed.append(f"{'fixed' if a['problems'] else 'BROKE'}: {b['label']} {'; '.join(b['problems'] or a['problems'])[:80]}")
        print('\nvs baseline:\n  ' + ('\n  '.join(changed) if changed else 'no change'))
    if '--save' in sys.argv:
        json.dump(res, open(path, 'w'), indent=1)
        print(f'saved baseline {path}')
    sys.exit(1 if fails else 0)


if __name__ == '__main__':
    main()
