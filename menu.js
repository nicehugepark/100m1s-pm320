(function(){
  var btn=document.getElementById('menu-toggle');
  var drw=document.getElementById('mobile-drawer');
  var bd=document.getElementById('drawer-backdrop');
  if(!btn||!drw||!bd)return;
  function toggle(open){
    var willOpen=(typeof open==='boolean')?open:!drw.classList.contains('open');
    drw.classList.toggle('open',willOpen);
    bd.classList.toggle('open',willOpen);
    btn.classList.toggle('open',willOpen);
    btn.setAttribute('aria-expanded',willOpen);
  }
  btn.addEventListener('click',function(){toggle();});
  bd.addEventListener('click',function(){toggle(false);});
  /* 실 링크 탭 시 드로어 닫기 — 단, aria-disabled(예정) 행은 네비 없음 → 닫지 않음(오작동 방지) */
  drw.querySelectorAll('a').forEach(function(a){
    a.addEventListener('click',function(){ if(a.getAttribute('aria-disabled')==='true')return; toggle(false); });
  });
  /* R28 P2 (조니 2심, 2026-06-11) — 드로어 본체 탭(링크 외 영역) + ESC 로도 닫기.
     종전: backdrop 탭·링크 탭만 닫힘 — 드로어 안 빈 영역 탭/ESC 무반응.
     ESC 닫힘 시 포커스는 토글 버튼으로 복귀(키보드 동선 유지). */
  drw.addEventListener('click',function(e){
    /* 간극 해소: Products 접이식 토글(.drawer-group-toggle)·하위 영역 탭은 드로어 유지(닫지 않음) */
    if(e.target.closest('a')||e.target.closest('.drawer-group'))return;
    toggle(false);
  });
  document.addEventListener('keydown',function(e){
    if(e.key==='Escape'&&drw.classList.contains('open')){toggle(false);btn.focus();}
  });

  /* 간극 해소 (대표 2026-06-14) — Products 접이식(accordion). 탭하면 주식 라인·ByVias 하위 펼침(드로어 유지) */
  var grpBtn=document.getElementById('drawer-products-toggle');
  if(grpBtn){
    grpBtn.addEventListener('click',function(){
      var open=grpBtn.getAttribute('aria-expanded')==='true';
      grpBtn.setAttribute('aria-expanded',String(!open));
    });
  }

  /* 데스크탑 nav 드롭다운 — CSS hover/focus-within 으로 열리되, 트리거 aria-expanded 동기(스크린리더 정합) */
  document.querySelectorAll('.nav-item.has-submenu').forEach(function(item){
    var trig=item.querySelector('.nav-trigger');
    if(!trig)return;
    function sync(open){ trig.setAttribute('aria-expanded',String(open)); }
    item.addEventListener('mouseenter',function(){sync(true);});
    item.addEventListener('mouseleave',function(){sync(false);});
    item.addEventListener('focusin',function(){sync(true);});
    item.addEventListener('focusout',function(){ if(!item.contains(document.activeElement)) sync(false); });
    trig.addEventListener('click',function(e){
      e.preventDefault();
      var isOpen=item.contains(document.activeElement);
      if(isOpen){ trig.blur(); sync(false); }
      else { trig.focus(); sync(true); }
    });
  });
})();
