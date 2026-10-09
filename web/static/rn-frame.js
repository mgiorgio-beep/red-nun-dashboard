/* Dashboard frame for the standalone phone-first pages (UI plan Phase 4).
   On a PC (mouse + wide screen) the page gets the light dashboard colours and the
   sidebar; phones, tablets and the home-screen app keep the page exactly as it was.
   Include first thing in <head>: <script src="/static/rn-frame.js"></script>
   Pages style framed-only tweaks under html.rn-framed. */
(function(){
  var standalone = window.navigator.standalone || (window.matchMedia && matchMedia('(display-mode: standalone)').matches);
  var pc = window.matchMedia && matchMedia('(hover: hover) and (pointer: fine)').matches && window.innerWidth >= 900;
  if (!pc || standalone) return;
  document.documentElement.classList.add('rn-framed');
  document.write('<link rel="stylesheet" href="/static/sidebar.css"><link rel="stylesheet" href="/static/rn-frame.css">');
  document.addEventListener('DOMContentLoaded', function(){
    var s = document.createElement('script');
    s.src = '/static/sidebar.js';
    document.body.appendChild(s);
  });
})();
