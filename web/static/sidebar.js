(function(){
'use strict';
var icons={
overview:'<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="1.8" d="M4 5a1 1 0 011-1h4a1 1 0 011 1v7a1 1 0 01-1 1H5a1 1 0 01-1-1V5zM14 5a1 1 0 011-1h4a1 1 0 011 1v2a1 1 0 01-1 1h-4a1 1 0 01-1-1V5zM4 16a1 1 0 011-1h4a1 1 0 011 1v3a1 1 0 01-1 1H5a1 1 0 01-1-1v-3zM14 12a1 1 0 011-1h4a1 1 0 011 1v7a1 1 0 01-1 1h-4a1 1 0 01-1-1v-7z"/></svg>',
revenue:'<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="1.8" d="M12 8c-1.657 0-3 .895-3 2s1.343 2 3 2 3 .895 3 2-1.343 2-3 2m0-8c1.11 0 2.08.402 2.599 1M12 8V7m0 1v8m0 0v1m0-1c-1.11 0-2.08-.402-2.599-1M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>',
labor:'<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="1.8" d="M12 4.354a4 4 0 110 5.292M15 21H3v-1a6 6 0 0112 0v1zm0 0h6v-1a6 6 0 00-9-5.197M13 7a4 4 0 11-8 0 4 4 0 018 0z"/></svg>',
bevcost:'<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="1.8" d="M19.428 15.428a2 2 0 00-1.022-.547l-2.387-.477a6 6 0 00-3.86.517l-.318.158a6 6 0 01-3.86.517L6.05 15.21a2 2 0 00-1.806.547M8 4h8l-1 1v5.172a2 2 0 00.586 1.414l5 5c1.26 1.26.367 3.414-1.415 3.414H4.828c-1.782 0-2.674-2.154-1.414-3.414l5-5A2 2 0 009 10.172V5L8 4z"/></svg>',
foodcost:'<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="1.8" d="M3 3h2l.4 2M7 13h10l4-8H5.4M7 13L5.4 5M7 13l-2.293 2.293c-.63.63-.184 1.707.707 1.707H17m0 0a2 2 0 100 4 2 2 0 000-4zm-8 2a2 2 0 11-4 0 2 2 0 014 0z"/></svg>',
servers:'<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="1.8" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z"/></svg>',
products:'<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="1.8" d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"/></svg>',
vendors:'<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="1.8" d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4"/></svg>',
inventory:'<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="1.8" d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4"/></svg>',
recipes:'<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="1.8" d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253"/></svg>',
scan:'<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="1.8" d="M6.827 6.175A2.31 2.31 0 015.186 7.23c-.38.054-.757.112-1.134.175C2.999 7.58 2.25 8.507 2.25 9.574V18a2.25 2.25 0 002.25 2.25h15A2.25 2.25 0 0021.75 18V9.574c0-1.067-.75-1.994-1.802-2.169a47.865 47.865 0 00-1.134-.175 2.31 2.31 0 01-1.64-1.055l-.822-1.316a2.192 2.192 0 00-1.736-1.039 48.774 48.774 0 00-5.232 0 2.192 2.192 0 00-1.736 1.039l-.821 1.316z"/><path stroke-width="1.8" d="M16.5 12.75a4.5 4.5 0 11-9 0 4.5 4.5 0 019 0z"/></svg>',
history:'<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="1.8" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z"/></svg>',
catalog:'<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="1.8" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"/></svg>',
analytics:'<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="1.8" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z"/></svg>',
mgmt:'<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="1.8" d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"/><path stroke-width="1.8" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"/></svg>',
invoices:'<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="1.8" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"/></svg>',
accounting:'<svg fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg"><path stroke-linecap="round" stroke-linejoin="round" d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 0 1 3 19.875v-6.75ZM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 0 1-1.125-1.125V8.625ZM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 0 1-1.125-1.125V4.125Z"/></svg>',
billpay:'<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="1.8" d="M17 9V7a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2m2 4h10a2 2 0 002-2v-6a2 2 0 00-2-2H9a2 2 0 00-2 2v6a2 2 0 002 2zm7-5a2 2 0 11-4 0 2 2 0 014 0z"/></svg>'
};
var chevron='<svg class="rn-chevron" viewBox="0 0 20 20" fill="currentColor"><path d="M7.21 14.77a.75.75 0 01.02-1.06L11.168 10 7.23 6.29a.75.75 0 111.04-1.08l4.5 4.25a.75.75 0 010 1.08l-4.5 4.25a.75.75 0 01-1.06-.02z"/></svg>';
var sections=[

{id:'sec-analytics',label:'Analytics',icon:'analytics',children:[
  {id:'nav-labor',label:'Labor',page:'/',tab:'labor'},
  {id:'nav-bevcost',label:'Bev Cost',page:'/',tab:'pourcost'},
  {id:'nav-foodcost',label:'Food Cost',page:'/',tab:'cogs'}
]},
{id:'sec-mgmt',label:'Management',icon:'mgmt',children:[
  {id:'nav-vendors',label:'Vendors',page:'/manage',tab:'vendors'},
  {id:'nav-inventory',label:'Inventory',page:'/manage',tab:'inv'},
  {id:'nav-orderguide',label:'Order Guide',page:'/order-guide'},
  {id:'nav-aicount',label:'Smart Count',page:'/ai-inventory',mobileOnly:true},
  {id:'nav-specials',label:'Specials Board',page:'/specials-admin'}
]},
{id:'sec-products',label:'Products',icon:'products',children:[
  {id:'nav-products',label:'Products',page:'/manage',tab:'products'},
  {id:'nav-prodsetup',label:'Product Setup',page:'/manage',tab:'prodsetup'}
]},
{id:'sec-recipes',label:'Recipes',icon:'recipes',children:[
  {id:'nav-recipes',label:'Menu Items',page:'/manage',tab:'recipes'},
  {id:'nav-prepared',label:'Prepared Items',page:'/manage',tab:'prepared-items'},
  {id:'nav-menuanalysis',label:'Menu Analysis',page:'/manage',tab:'recipe-analysis'},
  {id:'nav-recipeviewer',label:'Recipe Viewer',page:'/manage',tab:'recipe-viewer'},
  {id:'nav-voicerecipe',label:'Voice Recipe',page:'/voice-recipe'},
  {id:'nav-pmixmapping',label:'PMIX Mapping',page:'/manage',tab:'pmix-mapping'}
]},
{id:'sec-invoices',label:'Invoices',icon:'invoices',children:[
  {id:'nav-invhistory',label:'Invoice History',page:'/invoices',tab:'history'},
  {id:'nav-scan',label:'Scan Invoice',page:'/invoices',tab:'scan'},
  {id:'nav-pending',label:'Pending Review',page:'/invoices',tab:'pending'},
  {id:'nav-vendorstatus',label:'Vendor Scrapers',page:'/vendor-status'}
]},
{id:'sec-billpay',label:'Bill Pay',icon:'billpay',children:[
  {id:'nav-bp-outstanding',label:'Outstanding',page:'/manage',tab:'billpay'},
  {id:'nav-payments',label:'Payments',page:'/payments'},
  {id:'nav-bp-vendors',label:'Vendor Setup',page:'/manage',tab:'bp-vendors'},
  {id:'nav-bp-checksetup',label:'Check Setup',page:'/manage',tab:'bp-checksetup'},
  {id:'nav-bp-payroll',label:'Payroll',page:'/manage',tab:'bp-payroll'}
]}
,
{id:'sec-accounting',label:'Accounting',icon:'accounting',children:[
  {id:'nav-acct-reports',label:'Reports',page:'/reports'},
  {id:'nav-acct-entries',label:'Sales Entries',page:'/sales-journal'},
  {id:'nav-acct-export',label:'Export',page:'/sales-journal?tab=export'},
  {id:'nav-acct-mapping',label:'Sales Mapping',page:'/sales-mapping'},
  {id:'nav-acct-payaccts',label:'Payment Accounts',page:'/payment-accounts'}
]}];
function buildSidebar(){
var h='<div class="rn-sb-logo"><div class="rn-sb-logo-icon">RN</div><div><div class="rn-sb-logo-text">Red Nun</div><div class="rn-sb-logo-sub">Dashboard</div></div></div>';
h+='<nav class="rn-sb-nav">';
h+='<a class="rn-sb-child rn-sb-toplevel" id="nav-dashboard" data-page="/manage" data-tab="dashboard" href="javascript:void(0)" style="padding:12px 16px;font-weight:600;display:flex;align-items:center;gap:10px;font-size:14px"><span style="width:20px;height:20px;flex-shrink:0">'+icons.overview+'</span><span>Dashboard</span></a>';
for(var i=0;i<sections.length;i++){
  var sec=sections[i];
  h+='<div class="rn-sb-group" id="'+sec.id+'">';
  h+='<div class="rn-sb-parent" data-section="'+sec.id+'">'+icons[sec.icon]+'<span>'+sec.label+'</span>'+chevron+'</div>';
  h+='<div class="rn-sb-children">';
  for(var j=0;j<sec.children.length;j++){
    var c=sec.children[j];
    h+='<a class="rn-sb-child'+(c.mobileOnly?' mobile-nav-item':'')+'" id="'+c.id+'" data-page="'+c.page+'"';
    if(c.tab)h+=' data-tab="'+c.tab+'"';
    h+=' href="javascript:void(0)"><span>'+c.label+'</span></a>';
  }
  h+='</div></div>';
}
h+='</nav>';

return h;
}
function getActiveId(){
var path=window.location.pathname;
var hash=window.location.hash.replace('#','').split('.')[0];
if(path==='/'||path==='/index.html'){
  var tm={overview:'nav-dashboard',labor:'nav-labor',pourcost:'nav-bevcost',cogs:'nav-foodcost'};
  return tm[hash]||'nav-dashboard';
}
if(path==='/manage'){
  var view=localStorage.getItem('manageView')||'dashboard';
  var vm={dashboard:'nav-dashboard',products:'nav-products',vendors:'nav-vendors',inv:'nav-inventory',recipes:'nav-recipes','prepared-items':'nav-prepared','recipe-analysis':'nav-menuanalysis','recipe-viewer':'nav-recipeviewer',prodsetup:'nav-prodsetup',settings:'nav-settings','recipe-edit':'nav-recipes','prepared-edit':'nav-prepared','data-export':'nav-dataexport','user-accounts':'nav-users',orderguide:'nav-orderguide','pmix-mapping':'nav-pmixmapping',billpay:'nav-bp-outstanding','bp-payments':'nav-bp-payments','bp-vendors':'nav-bp-vendors','bp-checksetup':'nav-bp-checksetup','bp-payroll':'nav-bp-payroll'};
  return vm[view]||'nav-dashboard';
}
if(path==='/invoices'){var iv=localStorage.getItem('invoiceView')||'history';var ivm={history:'nav-invhistory',scan:'nav-scan',pending:'nav-pending'};return ivm[iv]||'nav-invhistory';}
if(path==='/ai-inventory')return 'nav-aicount';
if(path==='/order-guide')return 'nav-orderguide';
if(path==='/specials-admin')return 'nav-specials';
if(path==='/payments')return 'nav-payments';
if(path==='/sales-journal'){var sp=new URLSearchParams(window.location.search);return sp.get('tab')==='export'?'nav-acct-export':'nav-acct-entries';}
if(path==='/reports')return 'nav-acct-reports';
if(path==='/sales-mapping')return 'nav-acct-mapping';
return 'nav-dashboard';
}
function setActiveItem(id){
document.querySelectorAll('.rn-sb-child').forEach(function(el){el.classList.toggle('active',el.id===id)});
// Auto-expand the parent section that contains the active item
document.querySelectorAll('.rn-sb-group').forEach(function(g){
  var hasActive=g.querySelector('.rn-sb-child.active');
  if(hasActive)g.classList.add('open');
});
}
// Expose setActiveItem globally for cross-page sync
window.setActiveItem=setActiveItem;
function openMobile(){
var sb=document.querySelector('.rn-sidebar');
var ov=document.querySelector('.rn-sb-overlay');
if(sb)sb.classList.add('open');
if(ov){ov.classList.add('open');setTimeout(function(){ov.classList.add('visible')},10)}
}
function closeMobile(){
var sb=document.querySelector('.rn-sidebar');
var ov=document.querySelector('.rn-sb-overlay');
if(sb)sb.classList.remove('open');
if(ov){ov.classList.remove('visible');setTimeout(function(){ov.classList.remove('open')},250)}
}
function findChild(id){
for(var i=0;i<sections.length;i++){
  for(var j=0;j<sections[i].children.length;j++){
    if(sections[i].children[j].id===id)return sections[i].children[j];
  }
}return null;
}
function handleNavClick(item){
var page=item.page;var tab=item.tab;var cur=window.location.pathname;
var curFull=cur+window.location.search;
if(curFull===page||(cur===''&&page==='/')){
  if(page==='/'){
    if(typeof window.switchTab==='function')window.switchTab(tab);
    else{window.location.hash=tab;window.location.reload()}
  }else if(page==='/manage'){
    if(typeof window.showView==='function'){window.showView(tab);localStorage.setItem('manageView',tab)}
  }else if(page==='/invoices'){
    if(typeof window.showView==='function'){window.showView(tab);localStorage.setItem('invoiceView',tab)}
  }else if(page==='/sales-journal'){
    if(typeof window.backToList==='function')window.backToList();
  }
  setActiveItem(item.id);closeMobile();return;
}
var url=page;
if(page==='/'&&tab)url='/#'+tab;
else if(page==='/manage'&&tab)localStorage.setItem('manageView',tab);
else if(page==='/invoices'&&tab)localStorage.setItem('invoiceView',tab);
window.location.href=url;
}
function init(){
var body=document.body;
var children=[];while(body.firstChild)children.push(body.removeChild(body.firstChild));
var layout=document.createElement('div');layout.className='rn-layout';
var sidebar=document.createElement('aside');sidebar.className='rn-sidebar';sidebar.innerHTML=buildSidebar();
var main=document.createElement('div');main.className='rn-main';
var topbar=document.createElement('div');topbar.className='rn-topbar';
topbar.innerHTML='<div class="rn-topbar-left"></div><select class="rn-sb-location" id="rn-location"><option value="dennis">Dennis Port</option><option value="chatham">Chatham</option></select><div class="rn-user-menu" id="rn-user-menu"></div>';
main.appendChild(topbar);
for(var c=0;c<children.length;c++)main.appendChild(children[c]);
var hamburger=document.createElement('button');hamburger.className='rn-hamburger';
hamburger.innerHTML='<svg fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-width="2" stroke-linecap="round" d="M4 6h16M4 12h16M4 18h16"/></svg>';
hamburger.addEventListener('click',openMobile);
var overlay=document.createElement('div');overlay.className='rn-sb-overlay';
overlay.addEventListener('click',closeMobile);
layout.appendChild(sidebar);layout.appendChild(main);
body.appendChild(hamburger);body.appendChild(overlay);body.appendChild(layout);
// Remove old navs inside main
var old=main.querySelectorAll('.page-tabs');for(var t=0;t<old.length;t++)old[t].remove();
var oldSb=main.querySelectorAll('.sidebar');for(var s=0;s<oldSb.length;s++)oldSb[s].remove();
var oldLay=main.querySelectorAll('.layout');
for(var l=0;l<oldLay.length;l++){
  var mc=oldLay[l].querySelector('.main-content');
  if(mc){while(mc.firstChild)oldLay[l].parentNode.insertBefore(mc.firstChild,oldLay[l])}
  oldLay[l].remove();
}
// Bind parent section toggles
document.querySelectorAll('.rn-sb-parent').forEach(function(p){
  p.addEventListener('click',function(){
    var group=document.getElementById(p.dataset.section);
    if(group)group.classList.toggle('open');
  });
});
// Bind child clicks
document.querySelectorAll('.rn-sb-child').forEach(function(el){
  el.addEventListener('click',function(e){
    e.preventDefault();
    var item=findChild(el.id);
    if(!item && el.dataset.page && el.dataset.tab){
      item={page:el.dataset.page,tab:el.dataset.tab,id:el.id};
    }
    if(item)handleNavClick(item);
  });
});
setActiveItem(getActiveId());
// Re-trigger page init after sidebar restructured the DOM
var path=window.location.pathname;
if(path==='/manage'){
  var view=localStorage.getItem('manageView')||'dashboard';
  if(typeof window.showView==='function')setTimeout(function(){window.showView(view)},50);
}
if(path==='/invoices'){
  var iv=localStorage.getItem('invoiceView')||'history';
  if(typeof window.showView==='function')setTimeout(function(){window.showView(iv)},50);
}
if(path==='/'||path==='/index.html'){
  if(typeof window.switchTab==='function'){
    var hash=window.location.hash.replace('#','').split('.')[0]||'overview';
    setTimeout(function(){window.switchTab(hash)},50);
  }
}
// Location sync
var sel=document.getElementById('rn-location');
if(sel){
  var saved=localStorage.getItem('globalLocation')||'dennis';
  sel.value=saved;
  window.currentLocation=saved;
  sel.addEventListener('change',function(){
    var v=sel.value;
    localStorage.setItem('globalLocation',v);
    window.currentLocation=v;
    if(typeof window.setGlobalLocation==='function')window.setGlobalLocation(v);
    if(typeof window.setLoc==='function'){
      var fakeBtn={dataset:{loc:v}};
      window.setLoc(fakeBtn);
    }
    var oldSel=document.getElementById('global-location');
    if(oldSel&&oldSel!==sel){oldSel.value=v;oldSel.dispatchEvent(new Event('change'))}
    document.querySelectorAll('.loc-pill,.loc-btn,[data-loc]').forEach(function(b){
      if(b.dataset&&b.dataset.loc!==undefined){
        b.classList.toggle('active',b.dataset.loc===v);
      }
    });
  });
}
window.addEventListener('hashchange',function(){setActiveItem(getActiveId())});
setTimeout(function(){
  var orig=window.showView;
  var curPath=window.location.pathname;
  if(typeof orig==='function'){window.showView=function(v){orig(v);
    if(curPath==='/manage'){
      localStorage.setItem('manageView',v);
      var vm={dashboard:'nav-dashboard',products:'nav-products',vendors:'nav-vendors',inv:'nav-inventory',recipes:'nav-recipes','prepared-items':'nav-prepared','recipe-analysis':'nav-menuanalysis','recipe-viewer':'nav-recipeviewer',prodsetup:'nav-prodsetup',settings:'nav-settings','recipe-edit':'nav-recipes','prepared-edit':'nav-prepared','data-export':'nav-dataexport','user-accounts':'nav-users',orderguide:'nav-orderguide','pmix-mapping':'nav-pmixmapping',billpay:'nav-bp-outstanding','bp-payments':'nav-bp-payments','bp-vendors':'nav-bp-vendors','bp-checksetup':'nav-bp-checksetup','bp-payroll':'nav-bp-payroll'};
      setActiveItem(vm[v]||'nav-dashboard');
    } else if(curPath==='/invoices'){
      localStorage.setItem('invoiceView',v);
      var ivm={history:'nav-invhistory',scan:'nav-scan',pending:'nav-pending'};
      setActiveItem(ivm[v]||'nav-invhistory');
    }
  }}
},100);

// User avatar in topbar + admin sidebar section
fetch('/api/auth/check').then(function(r){return r.json()}).then(function(u){
  if(!u.authenticated)return;
  window._rnUser=u;
  // Build initials
  var parts=(u.full_name||u.username||'?').trim().split(/\s+/);
  var ini=parts.length>=2?(parts[0][0]+parts[parts.length-1][0]).toUpperCase():((u.full_name||u.username||'?').slice(0,2).toUpperCase());
  var locLabel={dennis:'Dennis Port',chatham:'Chatham',both:'Both Locations'}[u.location]||'Both';

  // Avatar + dropdown
  var menu=document.getElementById('rn-user-menu');
  if(menu){
    var adminLink=u.role==='admin'?'<a class="rn-ud-item" href="/admin/users"><svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><path d="M8 2a3 3 0 110 6 3 3 0 010-6zM2 13s-1 0-1-1 1-4 7-4 7 3 7 4-1 1-1 1H2z"/></svg>Manage users</a>':'';
    menu.innerHTML='<button class="rn-avatar-btn" id="rn-avatar-btn">'+ini+'</button>'
      +'<div class="rn-ud" id="rn-ud">'
      +'<div class="rn-ud-hdr"><div class="rn-ud-name">'+((u.full_name||u.username)||'')+'</div>'
      +'<div class="rn-ud-email">'+(u.email||'')+'</div>'
      +'<div style="margin-top:6px;"><span class="rn-ud-badge">'+locLabel+'</span> <span class="rn-ud-badge" style="text-transform:capitalize;">'+u.role+'</span></div></div>'
      +adminLink
      +'<a class="rn-ud-item" href="/change-password"><svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="7" width="10" height="8" rx="2"/><path d="M5 7V5a3 3 0 016 0v2" stroke-linecap="round"/></svg>Change password</a>'
      +'<div class="rn-ud-sep"></div>'
      +'<a class="rn-ud-item rn-ud-danger" href="/logout"><svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M6 2H3a1 1 0 00-1 1v10a1 1 0 001 1h3M10 11l3-3-3-3M13 8H6"/></svg>Sign out</a>'
      +'</div>';
    document.getElementById('rn-avatar-btn').addEventListener('click',function(e){
      e.stopPropagation();
      var dd=document.getElementById('rn-ud');
      dd.classList.toggle('open');
    });
    document.addEventListener('click',function(e){
      if(!menu.contains(e.target)){var dd=document.getElementById('rn-ud');if(dd)dd.classList.remove('open');}
    });
  }

  // Hide restricted sections for non-admin roles
  if(u.role!=='admin'){
    // Hide Vendor Scrapers nav item (admin only)
    var vs=document.getElementById('nav-vendorstatus');
    if(vs)vs.style.display='none';
    // Hide Sales Mapping, Payment Accounts, Export (admin only)
    var sm=document.getElementById('nav-acct-mapping');
    if(sm)sm.style.display='none';
    var pa=document.getElementById('nav-acct-payaccts');
    if(pa)pa.style.display='none';
    var ex=document.getElementById('nav-acct-export');
    if(ex)ex.style.display='none';
    // Manager: hide Bill Pay entirely
    if(u.role==='manager'){
      var bp=document.getElementById('sec-billpay');
      if(bp)bp.style.display='none';
    }
    // Re-run active item highlighting after hiding so section stays open
    setActiveItem(getActiveId());
  }

  // Admin sidebar section
  if(u.role==='admin'){
    var nav=document.querySelector('.rn-sb-nav');
    if(nav){
      var adminHtml='<div class="rn-sb-group" id="sec-admin">'
        +'<div class="rn-sb-parent" data-section="sec-admin">'+icons.mgmt+'<span>Admin</span>'+chevron+'</div>'
        +'<div class="rn-sb-children">'
        +'<a class="rn-sb-child" id="nav-users" data-page="/admin/users" href="javascript:void(0)"><span>User Accounts</span></a>'
        +'<a class="rn-sb-child" id="nav-logins" data-page="/admin/logins" href="javascript:void(0)"><span>Login History</span></a>'
        +'</div></div>';
      nav.insertAdjacentHTML('beforeend',adminHtml);
      // Bind toggle
      var adminParent=document.querySelector('#sec-admin .rn-sb-parent');
      if(adminParent)adminParent.addEventListener('click',function(){document.getElementById('sec-admin').classList.toggle('open')});
      // Bind nav clicks
      ['nav-users','nav-logins'].forEach(function(id){
        var el=document.getElementById(id);
        if(el)el.addEventListener('click',function(e){
          e.preventDefault();
          window.location.href=el.dataset.page;
        });
      });
      // Highlight active if on admin page
      if(window.location.pathname==='/admin/users'){setActiveItem('nav-users');document.getElementById('sec-admin').classList.add('open');}
      if(window.location.pathname==='/admin/logins'){setActiveItem('nav-logins');document.getElementById('sec-admin').classList.add('open');}
    }
  }
}).catch(function(e){console.error('sidebar avatar error:',e)});
}
if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',init);else init();

function pollBadges(){
fetch('/api/invoices/pending-count').then(function(r){return r.json()}).then(function(d){
  var count=d.count||0;
  var invParent=document.querySelector('#sec-invoices .rn-sb-parent');
  var pendingItem=document.getElementById('nav-pending');
  if(invParent){if(count>0){invParent.classList.add('has-alert')}else{invParent.classList.remove('has-alert')}}
  if(pendingItem){if(count>0){pendingItem.classList.add('has-alert')}else{pendingItem.classList.remove('has-alert')}}
}).catch(function(){});
  fetch('/api/inventory/product-settings/unreviewed-count').then(function(r){return r.json()}).then(function(d){
    var ct=d.count||0;
    var psItem=document.getElementById('nav-prodsetup');
    if(psItem){if(ct>0){psItem.classList.add('has-alert')}else{psItem.classList.remove('has-alert')}}
  }).catch(function(){});
}
setInterval(pollBadges,30000);
setTimeout(pollBadges,500);
})();

/* Red Nun — load universal sortable-tables script */
(function(){
  var s = document.createElement('script');
  s.src = '/static/rn-sortable.js';
  s.defer = true;
  document.head.appendChild(s);
})();
