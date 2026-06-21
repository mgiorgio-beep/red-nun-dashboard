/* Red Nun — Shared Product Editor
   ONE product card used on every screen. Include this script, then call:
     openProductEditor(productId, { onSaved: (product, meta) => {...} })
   meta = { archived:true } when the product was archived.
   Writes: catalog fields via PUT /api/inventory/products/<id>;
           count unit / recipe unit via POST /api/storage/product/<id>/units;
           conversions via /api/inventory/products/<id>/conversions (live);
           archive via units endpoint {active:false}.
*/
(function(){
  if (window.openProductEditor) return;

  var COUNT_UNITS = ['each','package','case','bag','bottle','can','keg','lb','oz','gal','liter','slice','serving','portion'];
  var RECIPE_UNITS = ['oz','lb','ea','slice','fl_oz','gal','qt','pt','cup','tbsp','tsp','ml','l'];
  var PURCHASE_UNITS = ['case','each','lb','oz','gal','liter','bottle','can','keg','bag','box'];
  var CATS = [['FOOD','Food'],['BEER','Beer'],['LIQUOR','Liquor'],['WINE','Wine'],['NA_BEVERAGES','NA Beverages'],
              ['SUPPLIES','Supplies'],['NON_COGS','Non-COGS'],['TOGO_SUPPLIES','Togo'],['DR_SUPPLIES','DR'],['KITCHEN_SUPPLIES','Kitchen']];

  var cur = { id:null, prod:null, onSaved:null };

  function esc(s){ var d=document.createElement('div'); d.textContent=(s==null?'':s); return d.innerHTML; }

  function injectOnce(){
    if (document.getElementById('rnpe-style')) return;
    var css = ''+
    '.rnpe-ov{position:fixed;inset:0;background:rgba(0,0,0,0.62);display:none;align-items:center;justify-content:center;z-index:9000;padding:18px}'+
    '.rnpe-ov.open{display:flex}'+
    '.rnpe{background:#1a1a1e;border:1px solid rgba(255,255,255,0.16);border-radius:16px;width:100%;max-width:460px;max-height:92vh;overflow-y:auto;color:#f5f5f7;font-family:Inter,-apple-system,sans-serif;padding:22px}'+
    '.rnpe h3{font-size:18px;font-weight:700;margin:0 0 2px}'+
    '.rnpe .sub{font-size:12px;color:rgba(245,245,247,0.4);margin-bottom:16px}'+
    '.rnpe .f{margin-bottom:12px}'+
    '.rnpe .f label{display:block;font-size:11px;font-weight:700;color:rgba(245,245,247,0.4);text-transform:uppercase;letter-spacing:.5px;margin-bottom:5px}'+
    '.rnpe .f input,.rnpe .f select{width:100%;background:#222226;border:1px solid rgba(255,255,255,0.1);color:#f5f5f7;border-radius:8px;padding:9px 10px;font-size:14px;outline:none}'+
    '.rnpe .f input:focus,.rnpe .f select:focus{border-color:#0A84FF}'+
    '.rnpe .row{display:flex;gap:10px}.rnpe .row .f{flex:1}'+
    '.rnpe .vline{font-size:12px;color:rgba(245,245,247,0.6);background:#222226;border:1px solid rgba(255,255,255,0.08);border-radius:8px;padding:8px 10px;margin-bottom:12px}'+
    '.rnpe .convs{margin-bottom:8px}'+
    '.rnpe .conv{display:flex;justify-content:space-between;align-items:center;padding:4px 0;font-size:13px}'+
    '.rnpe .conv button{background:none;border:none;color:#FF453A;cursor:pointer;font-size:16px}'+
    '.rnpe .convadd{display:flex;gap:6px;align-items:center;flex-wrap:wrap}'+
    '.rnpe .convadd input{background:#222226;border:1px solid rgba(255,255,255,0.1);color:#f5f5f7;border-radius:8px;padding:8px;font-size:13px}'+
    '.rnpe .convadd .mini{padding:8px 12px;border-radius:8px;border:1px solid rgba(255,255,255,0.1);background:#222226;color:#f5f5f7;font-weight:700;font-size:13px;cursor:pointer}'+
    '.rnpe .acts{display:flex;gap:10px;margin-top:18px}'+
    '.rnpe .acts button{flex:1;padding:12px;border-radius:10px;font-size:14px;font-weight:700;cursor:pointer;border:none}'+
    '.rnpe .save{background:#30D158;color:#000}'+
    '.rnpe .cancel{background:#222226;color:rgba(245,245,247,0.7);border:1px solid rgba(255,255,255,0.1)}'+
    '.rnpe .arch{margin-top:10px;width:100%;background:none;border:1px solid rgba(255,69,58,0.4);color:#FF453A;border-radius:10px;padding:9px;font-size:12px;font-weight:600;cursor:pointer}'+
    '.rnpe .hr{border-top:1px solid rgba(255,255,255,0.08);margin:14px 0 12px}'+
    '.rnpe .toast{position:fixed;top:24px;left:50%;transform:translateX(-50%);background:#30D158;color:#000;padding:8px 16px;border-radius:8px;font-size:13px;font-weight:700;z-index:9100;opacity:0;transition:.25s;pointer-events:none}'+
    '.rnpe .toast.show{opacity:1}';
    var st=document.createElement('style'); st.id='rnpe-style'; st.textContent=css; document.head.appendChild(st);

    function sel(id, list, cur){
      var opts='<option value="">—</option>';
      var has=false;
      list.forEach(function(u){ if(u===cur)has=true; opts+='<option value="'+u+'">'+u+'</option>'; });
      if(cur && !has) opts='<option value="'+esc(cur)+'">'+esc(cur)+'</option>'+opts;
      return '<select id="'+id+'">'+opts+'</select>';
    }
    var catOpts = CATS.map(function(c){return '<option value="'+c[0]+'">'+c[1]+'</option>';}).join('');
    var ov=document.createElement('div'); ov.className='rnpe-ov'; ov.id='rnpe-ov';
    ov.innerHTML =
      '<div class="rnpe" onclick="event.stopPropagation()">'+
        '<h3 id="rnpe-title">Edit product</h3>'+
        '<div class="sub">One product card — used on every screen.</div>'+
        '<div class="vline" id="rnpe-vline" style="display:none"></div>'+
        '<div class="f"><label>Display name (clean name for recipes)</label><input id="rnpe-display" placeholder="e.g. Ranch Dressing"></div>'+
        '<div class="f"><label>Full name</label><input id="rnpe-name"></div>'+
        '<div class="row">'+
          '<div class="f"><label>Category</label><select id="rnpe-cat">'+catOpts+'</select></div>'+
          '<div class="f"><label>Price ($)</label><input id="rnpe-price" type="number" step="0.01"></div>'+
        '</div>'+
        '<div class="row">'+
          '<div class="f"><label>Buy (purchase unit)</label>'+sel('rnpe-unit',PURCHASE_UNITS,'')+'</div>'+
          '<div class="f"><label>Count (inventory unit)</label>'+sel('rnpe-invunit',COUNT_UNITS,'')+'</div>'+
          '<div class="f"><label>Recipe unit</label>'+sel('rnpe-recunit',RECIPE_UNITS,'')+'</div>'+
        '</div>'+
        '<div class="row">'+
          '<div class="f"><label>Par</label><input id="rnpe-par" type="number" step="0.5"></div>'+
          '<div class="f"><label>Reorder</label><input id="rnpe-reorder" type="number" step="0.5"></div>'+
        '</div>'+
        '<div class="hr"></div>'+
        '<div class="f"><label>Unit conversions <span style="text-transform:none;color:rgba(245,245,247,0.4);font-weight:500">— 1 case = 2 package, 1 package = 144 slice</span></label>'+
          '<div class="convs" id="rnpe-convs"></div>'+
          '<div class="convadd">'+
            '<span style="font-size:13px;color:rgba(245,245,247,0.4)">1</span>'+
            '<input list="rnpe-units" id="rnpe-cf" placeholder="from unit" style="flex:1;min-width:64px">'+
            '<span style="font-size:13px;color:rgba(245,245,247,0.4)">=</span>'+
            '<input type="number" step="0.01" id="rnpe-cq" placeholder="qty" style="width:60px">'+
            '<input list="rnpe-units" id="rnpe-ct" placeholder="to unit" style="flex:1;min-width:64px">'+
            '<button class="mini" id="rnpe-cadd">+ Add</button>'+
          '</div>'+
          '<datalist id="rnpe-units">'+COUNT_UNITS.map(function(u){return '<option value="'+u+'">';}).join('')+'</datalist>'+
        '</div>'+
        '<div class="acts"><button class="cancel" id="rnpe-cancel">Cancel</button><button class="save" id="rnpe-save">Save</button></div>'+
        '<button class="arch" id="rnpe-arch">Archive — don\'t inventory this item</button>'+
      '</div>';
    ov.addEventListener('click', close);
    document.body.appendChild(ov);

    document.getElementById('rnpe-cancel').addEventListener('click', close);
    document.getElementById('rnpe-save').addEventListener('click', save);
    document.getElementById('rnpe-arch').addEventListener('click', archive);
    document.getElementById('rnpe-cadd').addEventListener('click', addConv);
  }

  function toast(msg, err){
    var t=document.createElement('div'); t.className='toast'+(err?' err':''); t.style.cssText='position:fixed;top:24px;left:50%;transform:translateX(-50%);background:'+(err?'#FF453A':'#30D158')+';color:'+(err?'#fff':'#000')+';padding:8px 16px;border-radius:8px;font-size:13px;font-weight:700;z-index:9100';
    t.textContent=msg; document.body.appendChild(t); setTimeout(function(){t.remove();},1800);
  }
  function setSel(id,val){ var e=document.getElementById(id); if(!e)return; var v=(val||'');
    var found=false; for(var i=0;i<e.options.length;i++){ if(e.options[i].value===v){found=true;break;} }
    if(v && !found){ var o=document.createElement('option'); o.value=v; o.textContent=v; e.insertBefore(o,e.firstChild); }
    e.value=v;
  }

  window.openProductEditor = function(id, opts){
    injectOnce();
    cur.id=id; cur.onSaved=(opts&&opts.onSaved)||null; cur.prod=null;
    document.getElementById('rnpe-ov').classList.add('open');
    document.getElementById('rnpe-title').textContent='Loading…';
    document.getElementById('rnpe-convs').innerHTML='';
    fetch('/api/inventory/products/'+id,{credentials:'include'}).then(function(r){return r.json();}).then(function(p){
      cur.prod=p;
      document.getElementById('rnpe-title').textContent='Edit: '+(p.display_name||p.name||'Product');
      document.getElementById('rnpe-display').value=p.display_name||'';
      document.getElementById('rnpe-name').value=p.name||'';
      document.getElementById('rnpe-cat').value=p.category||'FOOD';
      document.getElementById('rnpe-price').value=p.current_price||'';
      setSel('rnpe-unit', p.unit);
      setSel('rnpe-invunit', p.inventory_unit);
      setSel('rnpe-recunit', p.recipe_unit);
      document.getElementById('rnpe-par').value=p.par_level||'';
      document.getElementById('rnpe-reorder').value=p.reorder_point||'';
    });
    // vendor line (read-only context: how it's bought + cost)
    fetch('/api/inventory/products/'+id+'/vendor-items',{credentials:'include'}).then(function(r){return r.ok?r.json():[];}).then(function(vi){
      var v=(vi||[]).find(function(x){return x.pack_contains;})||(vi||[])[0];
      var el=document.getElementById('rnpe-vline');
      if(v){ el.style.display='block'; el.innerHTML='<b>'+esc(v.vendor_name||'Vendor')+'</b> — '+esc(v.pack_size||'')+' &middot; $'+(v.purchase_price||0).toFixed(2)+(v.price_per_unit?(' &rarr; $'+(+v.price_per_unit).toFixed(3)+'/'+esc(v.contains_unit||'unit')):''); }
      else { el.style.display='none'; }
    });
    loadConvs(id);
  };

  function loadConvs(id){
    fetch('/api/inventory/products/'+id+'/conversions',{credentials:'include'}).then(function(r){return r.json();}).then(function(rows){
      var el=document.getElementById('rnpe-convs');
      if(!rows.length){ el.innerHTML='<div style="color:rgba(245,245,247,0.35);font-size:12px">No conversions yet.</div>'; return; }
      el.innerHTML=rows.map(function(c){
        return '<div class="conv"><span>1 '+esc(c.from_unit)+' = <b>'+(+c.to_qty)+'</b> '+esc(c.to_unit)+'</span><button data-cid="'+c.id+'">&times;</button></div>';
      }).join('');
      el.querySelectorAll('button[data-cid]').forEach(function(b){ b.addEventListener('click',function(){ delConv(b.getAttribute('data-cid')); }); });
    });
  }
  function addConv(){
    if(!cur.id)return;
    var from=(document.getElementById('rnpe-cf').value||'').trim();
    var q=parseFloat(document.getElementById('rnpe-cq').value);
    var to=(document.getElementById('rnpe-ct').value||'').trim();
    if(!from||!to||!q||q<=0){ toast('Enter: 1 from = qty to', true); return; }
    fetch('/api/inventory/products/'+cur.id+'/conversions',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'include',body:JSON.stringify({from_qty:1,from_unit:from,to_qty:q,to_unit:to})})
      .then(function(r){ if(r.ok){ document.getElementById('rnpe-cf').value='';document.getElementById('rnpe-cq').value='';document.getElementById('rnpe-ct').value=''; loadConvs(cur.id); } else toast('Could not add',true); });
  }
  function delConv(cid){
    fetch('/api/inventory/products/conversions/'+cid,{method:'DELETE',credentials:'include'}).then(function(r){ if(r.ok) loadConvs(cur.id); });
  }

  function save(){
    if(!cur.id||!cur.prod)return;
    var p=cur.prod;
    var body={
      display_name:document.getElementById('rnpe-display').value, name:document.getElementById('rnpe-name').value,
      category:document.getElementById('rnpe-cat').value, subcategory:p.subcategory||'',
      unit:document.getElementById('rnpe-unit').value, pack_size:p.pack_size||'', pack_unit:p.pack_unit||'',
      preferred_vendor_id:p.preferred_vendor_id||null,
      current_price:document.getElementById('rnpe-price').value, par_level:document.getElementById('rnpe-par').value,
      reorder_point:document.getElementById('rnpe-reorder').value, storage_location:p.storage_location||'', notes:p.notes||''
    };
    var invUnit=document.getElementById('rnpe-invunit').value||null;
    var recUnit=document.getElementById('rnpe-recunit').value||null;
    var btn=document.getElementById('rnpe-save'); btn.textContent='Saving…'; btn.disabled=true;
    fetch('/api/inventory/products/'+cur.id,{method:'PUT',headers:{'Content-Type':'application/json'},credentials:'include',body:JSON.stringify(body)})
      .then(function(r){ if(!r.ok) throw new Error(); return fetch('/api/storage/product/'+cur.id+'/units',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'include',body:JSON.stringify({inventory_unit:invUnit,recipe_unit:recUnit})}); })
      .then(function(){
        var updated=Object.assign({},p,{display_name:body.display_name,name:body.name,category:body.category,unit:body.unit,current_price:parseFloat(body.current_price)||null,par_level:body.par_level,reorder_point:body.reorder_point,inventory_unit:invUnit,recipe_unit:recUnit});
        btn.textContent='Save'; btn.disabled=false;
        toast('Saved');
        if(cur.onSaved) cur.onSaved(updated, {});
        close();
      })
      .catch(function(){ btn.textContent='Save'; btn.disabled=false; toast('Save failed',true); });
  }

  function archive(){
    if(!cur.id||!cur.prod)return;
    if(!confirm('Archive "'+(cur.prod.display_name||cur.prod.name)+'"?\n\nHidden from Inventory, Count and Storage. Invoice history is kept; can be restored later.')) return;
    fetch('/api/storage/product/'+cur.id+'/units',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'include',body:JSON.stringify({active:false})})
      .then(function(r){ if(!r.ok) throw new Error(); toast('Archived'); var oid=cur.id; if(cur.onSaved) cur.onSaved(null,{archived:true,id:oid}); close(); })
      .catch(function(){ toast('Could not archive',true); });
  }

  function close(){
    var ov=document.getElementById('rnpe-ov'); if(ov) ov.classList.remove('open');
    cur.id=null; cur.prod=null; cur.onSaved=null;
  }
})();
