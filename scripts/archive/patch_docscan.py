html = open('/opt/rednun/static/invoices.html').read()

# 1. Replace Camera button to open doc scanner overlay instead of fileInput
old_btn = '''      <button class="action-btn btn-camera" onclick="document.getElementById('fileInput').click()">
        <svg width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24"><path d="M6.827 6.175A2.31 2.31 0 015.186 7.23c-.38.054-.757.112-1.134.175C2.999 7.58 2.25 8.507 2.25 9.574V18a2.25 2.25 0 002.25 2.25h15A2.25 2.25 0 0021.75 18V9.574c0-1.067-.75-1.994-1.802-2.169a47.865 47.865 0 00-1.134-.175 2.31 2.31 0 01-1.64-1.055l-.822-1.316a2.192 2.192 0 00-1.736-1.039 48.774 48.774 0 00-5.232 0 2.192 2.192 0 00-1.736 1.039l-.821 1.316z"/><path d="M16.5 12.75a4.5 4.5 0 11-9 0 4.5 4.5 0 019 0z"/></svg> Camera
      </button>'''

new_btn = '''      <button class="action-btn btn-camera" onclick="openDocScanner()">
        <svg width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" viewBox="0 0 24 24"><path d="M6.827 6.175A2.31 2.31 0 015.186 7.23c-.38.054-.757.112-1.134.175C2.999 7.58 2.25 8.507 2.25 9.574V18a2.25 2.25 0 002.25 2.25h15A2.25 2.25 0 0021.75 18V9.574c0-1.067-.75-1.994-1.802-2.169a47.865 47.865 0 00-1.134-.175 2.31 2.31 0 01-1.64-1.055l-.822-1.316a2.192 2.192 0 00-1.736-1.039 48.774 48.774 0 00-5.232 0 2.192 2.192 0 00-1.736 1.039l-.821 1.316z"/><path d="M16.5 12.75a4.5 4.5 0 11-9 0 4.5 4.5 0 019 0z"/></svg> Camera
      </button>'''

assert html.count(old_btn) == 1, f"btn count: {html.count(old_btn)}"
html = html.replace(old_btn, new_btn, 1)
print("Button patched")

# 2. Insert doc scanner overlay HTML before </body>
overlay_html = '''
<!-- ── Doc Scanner Overlay ── -->
<div id="docScannerOverlay" style="display:none;position:fixed;inset:0;z-index:9999;background:#000;flex-direction:column;">
  <div style="position:relative;flex:1;overflow:hidden;">
    <video id="docVideo" autoplay playsinline muted style="width:100%;height:100%;object-fit:cover;"></video>
    <canvas id="docCanvas" style="position:absolute;inset:0;width:100%;height:100%;pointer-events:none;"></canvas>
    <!-- Corner brackets -->
    <div id="docBracket" style="position:absolute;inset:0;pointer-events:none;">
      <div id="bTL" style="position:absolute;width:40px;height:40px;border-top:3px solid #fff;border-left:3px solid #fff;border-radius:3px 0 0 0;transition:all 0.2s;"></div>
      <div id="bTR" style="position:absolute;width:40px;height:40px;border-top:3px solid #fff;border-right:3px solid #fff;border-radius:0 3px 0 0;transition:all 0.2s;"></div>
      <div id="bBL" style="position:absolute;width:40px;height:40px;border-bottom:3px solid #fff;border-left:3px solid #fff;border-radius:0 0 0 3px;transition:all 0.2s;"></div>
      <div id="bBR" style="position:absolute;width:40px;height:40px;border-bottom:3px solid #fff;border-right:3px solid #fff;border-radius:0 0 3px 0;transition:all 0.2s;"></div>
    </div>
    <!-- Status label -->
    <div id="docStatus" style="position:absolute;bottom:20px;left:0;right:0;text-align:center;color:#fff;font-size:14px;font-weight:500;text-shadow:0 1px 4px rgba(0,0,0,0.8);">Point at invoice</div>
    <!-- Stable progress ring -->
    <svg id="docProgress" style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);display:none;" width="80" height="80" viewBox="0 0 80 80">
      <circle cx="40" cy="40" r="34" fill="none" stroke="rgba(255,255,255,0.2)" stroke-width="6"/>
      <circle id="docProgressRing" cx="40" cy="40" r="34" fill="none" stroke="#ff3b30" stroke-width="6" stroke-linecap="round"
        stroke-dasharray="213" stroke-dashoffset="213" transform="rotate(-90 40 40)" style="transition:stroke-dashoffset 0.1s;"/>
    </svg>
  </div>
  <div style="background:#111;padding:16px;display:flex;gap:12px;justify-content:center;align-items:center;">
    <button onclick="captureManually()" style="flex:1;max-width:200px;padding:14px;background:#ff3b30;border:none;border-radius:12px;color:#fff;font-size:16px;font-weight:600;cursor:pointer;">📸 Capture</button>
    <button onclick="closeDocScanner()" style="padding:14px 20px;background:#333;border:none;border-radius:12px;color:#fff;font-size:14px;cursor:pointer;">Cancel</button>
  </div>
</div>
'''

old_body = '</body>'
assert html.count(old_body) == 1
html = html.replace(old_body, overlay_html + old_body, 1)
print("Overlay HTML inserted")

# 3. Insert doc scanner JS before </script> of last script tag
scanner_js = '''
// ── Doc Scanner ──
var docStream = null;
var docAnimFrame = null;
var docStableStart = null;
var docLastRect = null;
var docCaptured = false;
var DOC_STABLE_MS = 1500;

function openDocScanner() {
  docCaptured = false;
  docStableStart = null;
  docLastRect = null;
  var overlay = document.getElementById('docScannerOverlay');
  overlay.style.display = 'flex';
  var video = document.getElementById('docVideo');
  navigator.mediaDevices.getUserMedia({
    video: { facingMode: 'environment', width: {ideal:1920}, height: {ideal:1080} }
  }).then(function(stream) {
    docStream = stream;
    video.srcObject = stream;
    video.onloadedmetadata = function() {
      video.play();
      requestAnimationFrame(docScanFrame);
    };
  }).catch(function(err) {
    closeDocScanner();
    // Fall back to native camera
    document.getElementById('fileInput').click();
  });
}

function closeDocScanner() {
  if (docStream) { docStream.getTracks().forEach(function(t){t.stop();}); docStream = null; }
  if (docAnimFrame) { cancelAnimationFrame(docAnimFrame); docAnimFrame = null; }
  document.getElementById('docScannerOverlay').style.display = 'none';
  document.getElementById('docProgress').style.display = 'none';
  setBracketColor('#fff');
}

function docScanFrame() {
  if (!docStream) return;
  var video = document.getElementById('docVideo');
  var canvas = document.getElementById('docCanvas');
  var vw = video.videoWidth, vh = video.videoHeight;
  if (!vw || !vh) { docAnimFrame = requestAnimationFrame(docScanFrame); return; }
  canvas.width = vw; canvas.height = vh;
  var ctx = canvas.getContext('2d');
  ctx.drawImage(video, 0, 0, vw, vh);
  var rect = detectDocument(ctx, vw, vh);
  updateBrackets(rect, vw, vh);
  checkStability(rect, vw, vh);
  docAnimFrame = requestAnimationFrame(docScanFrame);
}

function detectDocument(ctx, w, h) {
  // Sample grid to find bright rectangular region (white paper)
  var data = ctx.getImageData(0, 0, w, h).data;
  var gridX = 12, gridY = 16;
  var bright = [];
  for (var gy = 0; gy < gridY; gy++) {
    for (var gx = 0; gx < gridX; gx++) {
      var px = Math.floor((gx + 0.5) * w / gridX);
      var py = Math.floor((gy + 0.5) * h / gridY);
      var idx = (py * w + px) * 4;
      var lum = 0.299*data[idx] + 0.587*data[idx+1] + 0.114*data[idx+2];
      bright.push({gx:gx, gy:gy, lum:lum});
    }
  }
  var threshold = 180;
  var brightCells = bright.filter(function(c){return c.lum > threshold;});
  if (brightCells.length < 20) return null;
  var minGx = Math.min.apply(null, brightCells.map(function(c){return c.gx;}));
  var maxGx = Math.max.apply(null, brightCells.map(function(c){return c.gx;}));
  var minGy = Math.min.apply(null, brightCells.map(function(c){return c.gy;}));
  var maxGy = Math.max.apply(null, brightCells.map(function(c){return c.gy;}));
  var spanX = (maxGx - minGx) / gridX;
  var spanY = (maxGy - minGy) / gridY;
  if (spanX < 0.3 || spanY < 0.3) return null;
  var pad = 0.02;
  return {
    x: Math.max(0, (minGx / gridX - pad) * w),
    y: Math.max(0, (minGy / gridY - pad) * h),
    x2: Math.min(w, ((maxGx+1) / gridX + pad) * w),
    y2: Math.min(h, ((maxGy+1) / gridY + pad) * h)
  };
}

function updateBrackets(rect, vw, vh) {
  var ow = document.getElementById('docScannerOverlay').offsetWidth;
  var oh = document.getElementById('docScannerOverlay').querySelector('div').offsetHeight;
  var scaleX = ow / vw, scaleY = oh / vh;
  var status = document.getElementById('docStatus');
  if (!rect) {
    // Reset to corners
    positionBrackets(20, 20, ow-60, oh-60);
    status.textContent = 'Point at invoice';
    document.getElementById('docProgress').style.display = 'none';
    setBracketColor('#fff');
    return;
  }
  var x = rect.x * scaleX, y = rect.y * scaleY;
  var x2 = rect.x2 * scaleX, y2 = rect.y2 * scaleY;
  positionBrackets(x, y, x2, y2);
  status.textContent = 'Hold steady...';
}

function positionBrackets(x, y, x2, y2) {
  var tl = document.getElementById('bTL');
  var tr = document.getElementById('bTR');
  var bl = document.getElementById('bBL');
  var br = document.getElementById('bBR');
  tl.style.left = x+'px'; tl.style.top = y+'px';
  tr.style.left = (x2-40)+'px'; tr.style.top = y+'px';
  bl.style.left = x+'px'; bl.style.top = (y2-40)+'px';
  br.style.left = (x2-40)+'px'; br.style.top = (y2-40)+'px';
}

function setBracketColor(color) {
  ['bTL','bTR','bBL','bBR'].forEach(function(id) {
    var el = document.getElementById(id);
    el.style.borderColor = color;
  });
}

function checkStability(rect, vw, vh) {
  if (!rect) { docStableStart = null; docLastRect = null; return; }
  var progress = document.getElementById('docProgress');
  var ring = document.getElementById('docProgressRing');
  if (docLastRect) {
    var dx = Math.abs(rect.x - docLastRect.x) / vw;
    var dy = Math.abs(rect.y - docLastRect.y) / vh;
    var dx2 = Math.abs(rect.x2 - docLastRect.x2) / vw;
    var dy2 = Math.abs(rect.y2 - docLastRect.y2) / vh;
    if (dx + dy + dx2 + dy2 > 0.04) {
      docStableStart = null;
      progress.style.display = 'none';
      setBracketColor('#fff');
      docLastRect = rect;
      return;
    }
  }
  docLastRect = rect;
  if (!docStableStart) docStableStart = Date.now();
  var elapsed = Date.now() - docStableStart;
  var pct = Math.min(elapsed / DOC_STABLE_MS, 1);
  var circ = 213;
  ring.setAttribute('stroke-dashoffset', circ * (1 - pct));
  setBracketColor(pct > 0.5 ? '#ff3b30' : '#fff');
  progress.style.display = 'block';
  if (pct >= 1 && !docCaptured) {
    docCaptured = true;
    captureFrame(rect);
  }
}

function captureManually() {
  var video = document.getElementById('docVideo');
  var vw = video.videoWidth, vh = video.videoHeight;
  if (!vw) return;
  captureFrame(null);
}

function captureFrame(rect) {
  var video = document.getElementById('docVideo');
  var vw = video.videoWidth, vh = video.videoHeight;
  var offscreen = document.createElement('canvas');
  if (rect) {
    offscreen.width = rect.x2 - rect.x;
    offscreen.height = rect.y2 - rect.y;
    offscreen.getContext('2d').drawImage(video, rect.x, rect.y, offscreen.width, offscreen.height, 0, 0, offscreen.width, offscreen.height);
  } else {
    offscreen.width = vw; offscreen.height = vh;
    offscreen.getContext('2d').drawImage(video, 0, 0);
  }
  // Flash effect
  var flash = document.createElement('div');
  flash.style.cssText = 'position:fixed;inset:0;background:#fff;opacity:0.8;z-index:10000;pointer-events:none;';
  document.body.appendChild(flash);
  setTimeout(function(){flash.remove();}, 200);
  closeDocScanner();
  offscreen.toBlob(function(blob) {
    var file = new File([blob], 'invoice_scan.jpg', {type:'image/jpeg'});
    var dt = new DataTransfer();
    dt.items.add(file);
    var inp = document.getElementById('fileInput');
    inp.files = dt.files;
    handleFile(inp);
  }, 'image/jpeg', 0.92);
}
'''

# Insert before the last </script>
last_script = html.rfind('</script>')
assert last_script != -1
html = html[:last_script] + scanner_js + '\n' + html[last_script:]
print("Scanner JS inserted")

open('/opt/rednun/static/invoices.html', 'w').write(html)
print(f"Done. File size: {len(html)} chars")
