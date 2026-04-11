f = open('static/index.html', 'r').read()

bev_js = """
  async function loadBevCost() {
    const {start, end} = getDateRange();
    const locParam = currentLocation ? '&location=' + currentLocation : '';
    const resp = await fetch('/api/cogs/summary?start=' + start + '&end=' + end + locParam);
    const data = await resp.json();
    if (!data) return;
    const cats = data.categories || [];
    const liquor = cats.find(c => c.category_type === 'LIQUOR');
    const beer = cats.find(c => c.category_type === 'BEER');
    const wine = cats.find(c => c.category_type === 'WINE');
    const bevCost = (liquor ? liquor.total_cost : 0) + (beer ? beer.total_cost : 0) + (wine ? wine.total_cost : 0);
    $('bevTotalKpi').textContent = fmt(bevCost);
    $('bevTotalSub').textContent = data.period_start + ' to ' + data.period_end;
    $('bevLiquorKpi').textContent = fmt(liquor ? liquor.total_cost : 0);
    $('bevLiquorSub').textContent = (liquor ? liquor.invoice_count : 0) + ' invoices';
    $('bevBeerKpi').textContent = fmt(beer ? beer.total_cost : 0);
    $('bevBeerSub').textContent = (beer ? beer.invoice_count : 0) + ' invoices';
    const revResp = await fetch('/api/revenue/daily?start=' + start + '&end=' + end + locParam);
    const revData = await revResp.json();
    let totalRev = 0;
    if (revData) revData.forEach(d => totalRev += d.net_revenue || 0);
    const bevRev = totalRev * 0.30;
    const pourPct = bevRev > 0 ? (bevCost / bevRev * 100) : 0;
    $('bevPourPct').textContent = pourPct.toFixed(1) + '%';
    $('bevPourSub').textContent = 'Bev COGS ' + fmt(bevCost) + ' / Est Rev ' + fmt(bevRev);
    const vResp = await fetch('/api/cogs/vendors?start=' + start + '&end=' + end + '&category=bev' + locParam);
    const vendors = await vResp.json();
    const vList = $('bevVendorList');
    if (vendors && vendors.length) {
      const mx = vendors[0].total_spent || 1;
      vList.innerHTML = vendors.map(v => '<div style="padding:10px 12px;border-bottom:1px solid var(--border);"><div style="display:flex;justify-content:space-between;"><strong>' + v.vendor_name + '</strong><strong>' + fmt(v.total_spent) + '</strong></div><div style="margin-top:4px;height:3px;background:var(--border);border-radius:2px;"><div style="height:100%;width:' + (v.total_spent/mx*100) + '%;background:var(--red-light);border-radius:2px;"></div></div><div style="font-size:11px;color:var(--text-muted);margin-top:4px;">' + v.invoice_count + ' invoices</div></div>').join('');
    } else { vList.innerHTML = '<div style="padding:20px;color:var(--text-muted);">No bev invoices</div>'; }
    const iResp = await fetch('/api/cogs/invoices?start=' + start + '&end=' + end + '&category=bev' + locParam);
    const invoices = await iResp.json();
    const iList = $('bevInvoiceList');
    if (invoices && invoices.length) {
      iList.innerHTML = invoices.map(i => '<div style="padding:8px 12px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;"><div><div style="font-weight:600;">' + i.vendor_name + '</div><div style="font-size:11px;color:var(--text-muted);">' + i.invoice_date + '</div></div><div style="font-weight:600;">' + fmt(i.order_total) + '</div></div>').join('');
    } else { iList.innerHTML = '<div style="padding:20px;color:var(--text-muted);">No bev invoices</div>'; }
  }
"""

# Insert before switchTab function
f = f.replace('  function switchTab(', bev_js + '  function switchTab(')

# Add loadBevCost call in switchTab
f = f.replace("loadCogs();", "if (tab === 'food') loadCogs(); else loadBevCost();")

open('static/index.html', 'w').write(f)
print("DONE - loadBevCost added")
