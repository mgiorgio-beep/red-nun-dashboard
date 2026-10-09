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

Tab addresses (UI plan Phase 3): every /manage and /invoices item in the role's menu is
opened by its address (/manage?tab=...) as a bookmark and refreshed; Back/Forward is
walked across views; old #view links and a bare /manage land on the right view.

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
# What the server must refuse each role (web/server.py _role_gate), checked directly:
# a menu that hides a page is not protection.
REFUSED = {'manager': ['/payments', '/print-checks', '/registers', '/profit-loss', '/opening-balances', '/reconcile',
                       '/import-statement', '/bank-reconcile', '/bank-transactions', '/sales-journal', '/sales-mapping',
                       '/reports', '/transfer/settle', '/api/billpay/invoices', '/api/payments', '/api/print-queue',
                       '/api/register/accounts', '/api/gl-accounts', '/api/bank-reconcile/uploads',
                       '/api/sales-journal/entries', '/api/reports/profit-loss', '/api/payroll/runs',
                       '/api/intercompany/state']}
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
            id: e.id, label: e.innerText.trim(), page: e.dataset.page, tab: e.dataset.tab,
            mobile: e.classList.contains('mobile-nav-item'), visible: !!e.offsetParent || getComputedStyle(e).display !== 'none' && e.closest('.rn-sb-group') && getComputedStyle(e.closest('.rn-sb-group')).display !== 'none'
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
        out['tabs'] = tab_checks(ctx, [it for it in items if it['visible']])
        out['refused'] = []
        for path in REFUSED.get(role, []):
            r = ctx.request.get(BASE + path, max_redirects=0)
            out['refused'].append({'id': 'refuse ' + path, 'label': 'refuse ' + path, 'status': r.status,
                                   'problems': [] if r.status in (401, 403) else [f'HTTP {r.status}: {role} can open it']})
        out['home_bill_card_hidden'] = None
        if role == 'manager':
            pg = ctx.new_page(); pg.goto(BASE + '/manage', wait_until='domcontentloaded'); pg.wait_for_timeout(1500)
            out['home_bill_card_hidden'] = pg.evaluate("[...document.querySelectorAll('.rn-role-ac')].every(e => !e.offsetParent)")
            pg.close()
            out['refused'].append({'id': 'home bill card', 'label': 'home bill card hidden', 'problems': []
                                   if out['home_bill_card_hidden'] else ['bill pay card shows on Home for a manager']})
        b.close()
    return out


STATE_JS = """() => {
    const act = document.querySelector('.rn-sb-child.active');
    const v = document.querySelector('.view.active');
    return {url: location.pathname + location.search + location.hash, active: act ? act.id : null,
            view: v ? v.id.replace('view-', '') : null};
}"""


def tab_checks(ctx, items):
    """Bookmark + Refresh for each /manage and /invoices tab, Back/Forward, old links."""
    out = []
    tabs = [it for it in items if it.get('page') in ('/manage', '/invoices') and it.get('tab')
            and not it.get('mobile')]

    def check(label, url, expect_url, expect_active, steps=None, expect_view=None, store=None):
        info = {'id': 'tab ' + label, 'label': 'tab ' + label, 'console': [], 'failed': [], 'writes': []}
        pg = ctx.new_page()
        watch(pg, info)
        try:
            if store:   # what a bare address falls back to
                pg.goto(BASE + '/login', wait_until='domcontentloaded')
                pg.evaluate('([k, v]) => localStorage.setItem(k, v)', list(store))
            pg.goto(BASE + url, wait_until='domcontentloaded')
            pg.wait_for_timeout(1800)
            for st in steps or []:
                st(pg)
                pg.wait_for_timeout(1500)
            info.update(pg.evaluate(STATE_JS))
        except Exception as e:
            info['error'] = str(e)[:160]
        pg.close()
        p = problems(info, sidebar_page=False)
        if info.get('url') != expect_url:
            p.append(f"address {info.get('url')} (want {expect_url})")
        if expect_active and info.get('active') != expect_active:
            p.append(f"highlights {info.get('active')} (want {expect_active})")
        if expect_view and info.get('view') != expect_view:
            p.append(f"shows view {info.get('view')} (want {expect_view})")
        info['problems'] = p
        out.append(info)

    click = lambda nid: (lambda pg: pg.evaluate(f"document.getElementById({json.dumps(nid)}).click()"))
    for it in tabs:
        addr = f"{it['page']}?tab={it['tab']}"
        view = it['tab'] if it['page'] == '/manage' else None
        check(f"{it['label']} (bookmark)", addr, addr, it['id'], expect_view=view)
        check(f"{it['label']} (refresh)", addr, addr, it['id'], [lambda pg: pg.reload(wait_until='domcontentloaded')],
              expect_view=view)
    for page in ('/manage', '/invoices'):
        t = [it for it in tabs if it['page'] == page]
        if len(t) < 2:
            continue
        a, b, c = t[0], t[1], t[2 if len(t) > 2 else 0]   # /invoices on a PC has two tabs
        start = f"{page}?tab={a['tab']}"
        check(f"{page} back", start, f"{page}?tab={b['tab']}", b['id'],
              [click(b['id']), click(c['id']), lambda pg: pg.go_back(wait_until='commit')])
        check(f"{page} back x2 + fwd", start, f"{page}?tab={b['tab']}", b['id'],
              [click(b['id']), click(c['id']), lambda pg: pg.go_back(wait_until='commit'),
               lambda pg: pg.go_back(wait_until='commit'), lambda pg: pg.go_forward(wait_until='commit')])
    ids = {it['id'] for it in tabs}
    legacy = [('/manage#inventory', '/manage?tab=inv', 'nav-inventory'),
              ('/manage#bp-payroll', '/manage?tab=bp-payroll', 'nav-bp-payroll'),
              ('/manage?tab=no-such-view', '/manage?tab=dashboard', 'nav-dashboard'),
              ('/invoices#list', '/invoices?tab=history', 'nav-invhistory'),
              ('/invoices#reports', '/invoices?tab=reports', None)]
    for old, new, nid in legacy:
        if nid and nid not in ids | {'nav-dashboard'}:
            continue
        check(f"old link {old}", old, new, nid)
    check('bare /manage (stored view)', '/manage', '/manage?tab=vendors', 'nav-vendors', store=('manageView', 'vendors'))
    check('bare /invoices (stored view)', '/invoices', '/invoices?tab=pending', 'nav-pending',
          store=('invoiceView', 'pending'))
    if 'nav-recipes' in ids:
        from integrations.toast.data_store import get_connection
        cn = get_connection()
        r = cn.execute('SELECT id FROM recipes ORDER BY id LIMIT 1').fetchone()
        cn.close()
        if r:
            addr = f"/manage?tab=recipe-edit&id={r['id']}"
            check('recipe editor (bookmark)', addr, addr, 'nav-recipes', expect_view='recipe-edit')
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
    if sidebar_page and not i.get('visible'):
        # Not in this role's menu: being refused (403) is the point; only a crash counts.
        return [x for x in p if x.startswith('error') or '500' in x]
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
    rows = res['sidebar'] + res['standalone'] + res.get('tabs', []) + res.get('refused', [])
    fails = [r for r in rows if r['problems'] and r['id'] not in KNOWN]
    for r in rows:
        mark = ('known' if r['id'] in KNOWN else 'FAIL') if r['problems'] else ' ok '
        vis = '' if r.get('visible', True) else ' (hidden)'
        print(f"{mark} {r['label'][:24]:24}{vis:9} {r.get('url', '')[:34]:34} {'; '.join(r['problems'])[:120]}")
    print(f"\n{len(rows) - len(fails)}/{len(rows)} ok for role {role}")
    path = BASELINE.replace('.json', f'_{role}.json') if role != 'admin' else BASELINE
    if os.path.exists(path) and '--save' not in sys.argv:
        old = json.load(open(path))
        base = {r['id']: r for r in old['sidebar'] + old['standalone'] + old.get('tabs', []) + old.get('refused', [])}
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
