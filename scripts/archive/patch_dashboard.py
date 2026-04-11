"""
Patch Red Nun Dashboard — adds date picker, today live view, quick range buttons.
Run from toast-analytics directory: python patch_dashboard.py
"""

with open("static/index.html", "r", encoding="utf-8") as f:
    html = f.read()

original_len = len(html)

# ======== PATCH 1: Add CSS for new controls ========
css_insert = """
  .quick-btn {
    padding: 6px 14px; border-radius: 6px;
    border: 1px solid var(--border); background: var(--bg-accent);
    color: var(--text-muted); font-size: 12px; font-weight: 500;
    cursor: pointer; transition: all 0.2s;
    font-family: 'DM Sans', sans-serif; white-space: nowrap;
  }
  .quick-btn:hover { border-color: var(--text-muted); color: var(--text-secondary); }
  .quick-btn.active {
    background: var(--red-nun); border-color: var(--red-nun);
    color: white;
  }
  .date-input {
    padding: 6px 10px; border-radius: 6px;
    border: 1px solid var(--border); background: var(--bg-accent);
    color: var(--text-secondary); font-size: 12px;
    font-family: 'DM Sans', sans-serif; cursor: pointer;
  }
  .date-input::-webkit-calendar-picker-indicator { filter: invert(0.7); }

  .today-banner {
    background: linear-gradient(135deg, var(--red-nun-glow), rgba(74,127,212,0.1));
    border: 1px solid rgba(196,59,59,0.3);
    border-radius: var(--radius); padding: 16px 24px;
    margin-bottom: 20px; display: none; align-items: center;
    justify-content: space-between;
  }
  .today-pulse { width: 10px; height: 10px; background: var(--green);
    border-radius: 50%; animation: pulse 1.5s infinite; }
  .today-label { font-size: 14px; font-weight: 600; }
  .today-time { font-size: 12px; color: var(--text-muted); }
  .today-revenue { font-family: 'Playfair Display', serif;
    font-size: 28px; font-weight: 700; color: var(--green-light); }

  .date-controls {
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  }

  @media (max-width: 1024px) {
    .date-controls { gap: 4px; }
    .quick-btn { padding: 5px 10px; font-size: 11px; }
  }
"""

# Insert before </style>
html = html.replace("</style>", css_insert + "\n</style>")

# ======== PATCH 2: Replace header-right with date controls ========
old_hr = """<div class="header-right">
    <div class="status-badge">
      <div class="status-dot"></div>
      <span id="statusText">Live Data</span>
    </div>
    <div class="date-range" id="dateRange">Loading...</div>
  </div>"""

new_hr = """<div class="header-right">
    <div class="status-badge">
      <div class="status-dot"></div>
      <span id="statusText">Live</span>
    </div>
    <div class="date-controls">
      <button class="quick-btn active" onclick="setRange('today',this)">Today</button>
      <button class="quick-btn" onclick="setRange('week',this)">This Week</button>
      <button class="quick-btn" onclick="setRange('lastweek',this)">Last Week</button>
      <button class="quick-btn" onclick="setRange('30d',this)">30 Days</button>
      <input type="date" id="startDate" class="date-input" onchange="setCustomRange()">
      <span style="color:var(--text-muted);font-size:12px;">to</span>
      <input type="date" id="endDate" class="date-input" onchange="setCustomRange()">
    </div>
  </div>"""

html = html.replace(old_hr, new_hr)

# ======== PATCH 3: Add today banner before KPI row ========
banner_html = """<!-- Today Live Banner -->
  <div class="today-banner" id="todayBanner">
    <div style="display:flex;align-items:center;gap:12px;">
      <div class="today-pulse"></div>
      <div>
        <div class="today-label" id="todayLabel">Live Today</div>
        <div class="today-time" id="todayTime">--</div>
      </div>
    </div>
    <div style="text-align:right;">
      <div class="today-revenue" id="todayRevenue">--</div>
      <div style="font-size:11px; color:var(--text-muted);" id="todayOrders">-- orders</div>
    </div>
  </div>

  """

html = html.replace("<!-- KPI Row -->", banner_html + "<!-- KPI Row -->")

# ======== PATCH 4: Add date range JS before loadAll ========
date_js = """
  // ============================================================
  // DATE RANGE MANAGEMENT
  // ============================================================
  let currentRange = 'today';
  let todayTimer = null;

  function todayYMD() {
    const d = new Date();
    return d.getFullYear() + String(d.getMonth()+1).padStart(2,'0') + String(d.getDate()).padStart(2,'0');
  }
  function dateToYMD(d) {
    return d.getFullYear() + String(d.getMonth()+1).padStart(2,'0') + String(d.getDate()).padStart(2,'0');
  }

  function getDateRange() {
    const si = document.getElementById('startDate').value;
    const ei = document.getElementById('endDate').value;
    if (si && ei && currentRange === 'custom') {
      return { start: si.replace(/-/g,''), end: ei.replace(/-/g,'') };
    }
    const today = new Date();
    switch(currentRange) {
      case 'today': return { start: todayYMD(), end: todayYMD() };
      case 'week': {
        const m = new Date(today); m.setDate(today.getDate() - ((today.getDay()+6)%7));
        return { start: dateToYMD(m), end: todayYMD() };
      }
      case 'lastweek': {
        const m = new Date(today); m.setDate(today.getDate() - ((today.getDay()+6)%7) - 7);
        const s = new Date(m); s.setDate(m.getDate() + 6);
        return { start: dateToYMD(m), end: dateToYMD(s) };
      }
      case '30d': {
        const d = new Date(today); d.setDate(today.getDate() - 30);
        return { start: dateToYMD(d), end: todayYMD() };
      }
      default: return { start: todayYMD(), end: todayYMD() };
    }
  }

  function setRange(range, el) {
    currentRange = range;
    document.querySelectorAll('.quick-btn').forEach(b => b.classList.remove('active'));
    if (el) el.classList.add('active');
    document.getElementById('startDate').value = '';
    document.getElementById('endDate').value = '';

    const banner = document.getElementById('todayBanner');
    if (range === 'today') {
      banner.style.display = 'flex';
      startTodayRefresh();
    } else {
      banner.style.display = 'none';
      stopTodayRefresh();
    }
    loadAll();
  }

  function setCustomRange() {
    const s = document.getElementById('startDate').value;
    const e = document.getElementById('endDate').value;
    if (s && e) {
      currentRange = 'custom';
      document.querySelectorAll('.quick-btn').forEach(b => b.classList.remove('active'));
      document.getElementById('todayBanner').style.display = 'none';
      stopTodayRefresh();
      loadAll();
    }
  }

  function startTodayRefresh() {
    stopTodayRefresh();
    updateTodayBanner();
    todayTimer = setInterval(() => { updateTodayBanner(); loadAll(); }, 2 * 60 * 1000);
  }
  function stopTodayRefresh() {
    if (todayTimer) { clearInterval(todayTimer); todayTimer = null; }
  }

  async function updateTodayBanner() {
    const now = new Date();
    const timeStr = now.toLocaleTimeString('en-US', { hour:'numeric', minute:'2-digit' });
    const dateStr = now.toLocaleDateString('en-US', { weekday:'long', month:'long', day:'numeric' });
    document.getElementById('todayTime').textContent = dateStr + ' \\u00b7 ' + timeStr;
    try {
      const t = todayYMD();
      const lp = currentLocation ? '&location=' + currentLocation : '';
      const r = await fetch('/api/revenue/daily?start=' + t + '&end=' + t + lp);
      const d = await r.json();
      if (d && d.length) {
        const rev = d.reduce((s,x) => s + (x.net_revenue||0), 0);
        const ord = d.reduce((s,x) => s + (x.order_count||0), 0);
        document.getElementById('todayRevenue').textContent = '$' + rev.toLocaleString(undefined,{maximumFractionDigits:0});
        document.getElementById('todayOrders').textContent = ord + ' orders so far';
      } else {
        document.getElementById('todayRevenue').textContent = '$0';
        document.getElementById('todayOrders').textContent = 'No orders yet';
      }
    } catch(e) { console.error(e); }
  }

"""

# Insert before the loadAll function
html = html.replace("  async function loadAll() {", date_js + "\n  async function loadAll() {")

# ======== PATCH 5: Update loadAll to use dynamic dates ========
# Replace the date logic inside loadAll
old_date_logic = """    const today = new Date();
    const monday = new Date(today);
    monday.setDate(today.getDate() - today.getDay() + (today.getDay() === 0 ? -6 : 1));
    const startDate = monday.toISOString().slice(0,10).replace(/-/g,'');
    const endDate = today.toISOString().slice(0,10).replace(/-/g,'');"""

new_date_logic = """    const { start: startDate, end: endDate } = getDateRange();"""

if old_date_logic in html:
    html = html.replace(old_date_logic, new_date_logic)
else:
    # Try alternate patterns
    import re
    # Look for the date setup in loadAll
    pattern = r"const today = new Date\(\);\s*const monday.*?const endDate.*?;"
    match = re.search(pattern, html, re.DOTALL)
    if match:
        html = html[:match.start()] + "    const { start: startDate, end: endDate } = getDateRange();" + html[match.end():]
        print("  Replaced date logic (regex)")
    else:
        print("  WARNING: Could not find date logic in loadAll - may need manual fix")

# ======== PATCH 6: Update dateRange display ========
# Replace dateRange text update with formatted date range
html = html.replace(
    "$('dateRange').textContent =",
    "// dateRange replaced by date picker\n    // $('dateRange').textContent ="
)

# ======== PATCH 7: Replace auto-refresh with today-aware refresh ========
old_init = """  loadAll();

  // Auto-refresh every 5 minutes
  setInterval(loadAll, 5 * 60 * 1000);"""

new_init = """  // Start with today view
  document.getElementById('todayBanner').style.display = 'flex';
  startTodayRefresh();
  loadAll();"""

html = html.replace(old_init, new_init)

# ======== VERIFY ========
if len(html) > original_len:
    with open("static/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Dashboard patched! ({original_len} -> {len(html)} chars)")
    print("  + Date picker controls (Today / This Week / Last Week / 30 Days)")
    print("  + Custom date range inputs")
    print("  + Live Today banner with 2-minute auto-refresh")
    print("  + Dynamic date range for all API calls")
    print("")
    print("Restart the server to see changes:")
    print("  taskkill /f /im python.exe")
    print("  python server.py")
else:
    print("ERROR: Patch may have failed - file size didn't increase")
    print(f"  Original: {original_len}, After: {len(html)}")
