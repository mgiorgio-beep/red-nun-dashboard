f = open('static/index.html', 'r').read()

bev_html = """
  <div id="section-bev" style="display: none;">
    <div class="kpi-row">
      <div class="kpi-card animate-in delay-1">
        <div class="kpi-label">Total Bev Cost</div>
        <div class="kpi-value" id="bevTotalKpi">--</div>
        <div class="kpi-sub" id="bevTotalSub"></div>
      </div>
      <div class="kpi-card animate-in delay-2">
        <div class="kpi-label">Pour Cost %</div>
        <div class="kpi-value" id="bevPourPct">--</div>
        <div class="kpi-sub" id="bevPourSub"></div>
      </div>
      <div class="kpi-card animate-in delay-3">
        <div class="kpi-label">Liquor</div>
        <div class="kpi-value" id="bevLiquorKpi">--</div>
        <div class="kpi-sub" id="bevLiquorSub"></div>
      </div>
      <div class="kpi-card animate-in delay-4">
        <div class="kpi-label">Beer</div>
        <div class="kpi-value" id="bevBeerKpi">--</div>
        <div class="kpi-sub" id="bevBeerSub"></div>
      </div>
    </div>
    <div class="chart-grid animate-in delay-5">
      <div class="card">
        <div class="card-header">
          <div class="card-title">Beverage Spending Breakdown</div>
          <div class="card-badge" style="background: var(--green-bg); color: var(--green-light);">MarginEdge</div>
        </div>
        <div class="chart-container" style="height: 280px;">
          <canvas id="bevChart"></canvas>
        </div>
      </div>
      <div class="card">
        <div class="card-header">
          <div class="card-title">Top Bev Vendors</div>
        </div>
        <div id="bevVendorList" style="max-height: 320px; overflow-y: auto;">
          <div class="loading-text">Loading...</div>
        </div>
      </div>
    </div>
    <div class="chart-grid-equal animate-in delay-6">
      <div class="card">
        <div class="card-header">
          <div class="card-title">Beverage Product Costs</div>
        </div>
        <div id="bevProductList2" style="max-height: 360px; overflow-y: auto;">
          <div class="loading-text">Loading...</div>
        </div>
      </div>
      <div class="card">
        <div class="card-header">
          <div class="card-title">Recent Bev Invoices</div>
        </div>
        <div id="bevInvoiceList" style="max-height: 360px; overflow-y: auto;">
          <div class="loading-text">Loading...</div>
        </div>
      </div>
    </div>
  </div>
"""

f = f.replace('</main>', bev_html + '</main>')
open('static/index.html', 'w').write(f)
print("DONE - Bev section added")
