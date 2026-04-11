content = open('/opt/rednun/static/invoices.html').read()
original_len = len(content)

# 1. Add additionalImages array
old = 'let currentImage = null;'
new = 'let currentImage = null;\nlet additionalImages = [];'
assert content.count(old) == 1, f"Pattern 1 count: {content.count(old)}"
content = content.replace(old, new, 1)

# 2. Clear additionalImages in resetScan
old = '  currentImage = null;\n  currentInvoice = null;'
new = '  currentImage = null;\n  currentInvoice = null;\n  additionalImages = [];'
assert content.count(old) == 1, f"Pattern 2 count: {content.count(old)}"
content = content.replace(old, new, 1)

# 3. Show Add Page button after image loads
old = "    $('scanBtn').disabled = false;\n  };\n  reader.readAsDataURL(file);"
new = "    $('scanBtn').disabled = false;\n    showAddPageBtn();\n  };\n  reader.readAsDataURL(file);"
assert content.count(old) == 1, f"Pattern 3 count: {content.count(old)}"
content = content.replace(old, new, 1)

# 4. Remove addPageBtn in resetScan (insert before zone.innerHTML line)
old = "  zone.innerHTML = `<div class=\"scan-prompt\"><div class=\"scan-prompt-icon\"><svg width=\"32\""
new = "  const apb = document.getElementById('addPageBtn'); if (apb) apb.remove();\n  zone.innerHTML = `<div class=\"scan-prompt\"><div class=\"scan-prompt-icon\"><svg width=\"32\""
assert content.count(old) == 1, f"Pattern 4 count: {content.count(old)}"
content = content.replace(old, new, 1)

# 5. Inject showAddPageBtn + addPage functions before resetScan
add_page_js = """
function showAddPageBtn() {
  if (document.getElementById('addPageBtn')) {
    var btn = document.getElementById('addPageBtn');
    btn.textContent = additionalImages.length ? '+ Add Another Page (' + additionalImages.length + ' added)' : '+ Add Page 2';
    return;
  }
  var btn = document.createElement('button');
  btn.id = 'addPageBtn';
  btn.textContent = additionalImages.length ? '+ Add Another Page (' + additionalImages.length + ' added)' : '+ Add Page 2';
  btn.style.cssText = 'margin-top:10px;width:100%;padding:10px;background:var(--bg-card);border:1px dashed var(--border);border-radius:8px;color:var(--text-muted);font-size:14px;cursor:pointer;';
  btn.onclick = function() { document.getElementById('addPageInput').click(); };
  var zone = document.getElementById('scanZone');
  zone.parentNode.insertBefore(btn, zone.nextSibling);
  if (!document.getElementById('addPageInput')) {
    var inp = document.createElement('input');
    inp.type = 'file';
    inp.id = 'addPageInput';
    inp.accept = 'image/*,application/pdf';
    inp.style.display = 'none';
    inp.onchange = function() { addPage(this); };
    document.body.appendChild(inp);
  }
}

function addPage(input) {
  var file = input.files[0];
  if (!file) return;
  var reader = new FileReader();
  reader.onload = function(e) {
    var b64 = e.target.result.split(',')[1];
    var mime = 'image/jpeg';
    if (file.name.toLowerCase().endsWith('.pdf')) mime = 'application/pdf';
    else if (file.name.toLowerCase().endsWith('.png')) mime = 'image/png';
    additionalImages.push({data: b64, mime: mime});
    var btn = document.getElementById('addPageBtn');
    if (btn) btn.textContent = '+ Add Another Page (' + additionalImages.length + ' added)';
  };
  reader.readAsDataURL(file);
  input.value = '';
}

"""

old = 'function resetScan() {'
new = add_page_js + 'function resetScan() {'
assert content.count(old) == 1, f"Pattern 5 count: {content.count(old)}"
content = content.replace(old, new, 1)

open('/opt/rednun/static/invoices.html', 'w').write(content)
print(f"Done. Length {original_len} -> {len(content)}")
