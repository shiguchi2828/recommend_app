(function () {
  'use strict';

  const planId = (window.PLAN_DETAIL || {}).planId || '';
  const VISITED_KEY = 'pd_visited_' + planId;

  // ── Visited marks ──────────────────────────────────────────────────────────

  function loadVisited() {
    try { return JSON.parse(localStorage.getItem(VISITED_KEY) || '[]'); }
    catch { return []; }
  }

  function saveVisited(list) {
    localStorage.setItem(VISITED_KEY, JSON.stringify(list));
  }

  function markVisitedUI(btn) {
    btn.textContent = '✓ 行った！';
    btn.classList.add('visited');
    btn.disabled = true;
  }

  document.querySelectorAll('.pd-visited-btn').forEach(btn => {
    const name = btn.dataset.spotName;
    const visited = loadVisited();
    if (visited.includes(name)) markVisitedUI(btn);

    btn.addEventListener('click', () => {
      if (btn.disabled) return;
      markVisitedUI(btn);
      const list = loadVisited();
      if (!list.includes(name)) list.push(name);
      saveVisited(list);

      fetch('/api/visited', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ spot_name: name, plan_id: planId }),
      }).catch(() => {});
    });
  });

  // ── Spot details (営業時間・電話・サイト) ────────────────────────────────────

  const detailsCache = {};

  document.querySelectorAll('.pd-details-wrap').forEach(wrap => {
    const placeId = wrap.dataset.placeId;
    const btn     = wrap.querySelector('.pd-details-btn');
    const content = wrap.querySelector('.pd-details-content');
    if (!btn || !content || !placeId) return;

    btn.addEventListener('click', async () => {
      if (!content.hidden) {
        content.hidden = true;
        btn.textContent = '⏰ 営業時間・電話を確認';
        return;
      }

      btn.textContent = '読み込み中…';
      btn.disabled = true;

      try {
        if (!detailsCache[placeId]) {
          const res  = await fetch('/api/spot_details/' + encodeURIComponent(placeId));
          detailsCache[placeId] = await res.json();
        }
        renderDetails(content, detailsCache[placeId]);
        content.hidden = false;
        btn.textContent = '▲ 閉じる';
      } catch {
        content.innerHTML = '<span style="color:#ef4444">取得できませんでした</span>';
        content.hidden = false;
        btn.textContent = '▲ 閉じる';
      } finally {
        btn.disabled = false;
      }
    });
  });

  function renderDetails(el, data) {
    if (!data || !data.ok) {
      el.innerHTML = '情報を取得できませんでした。';
      return;
    }
    let html = '';

    if (data.open_now !== undefined && data.open_now !== null) {
      html += '<div style="margin-bottom:.4rem;font-weight:700;color:' +
        (data.open_now ? '#059669' : '#ef4444') + '">' +
        (data.open_now ? '🟢 営業中' : '🔴 営業時間外') +
        '</div>';
    }

    if (data.weekday_text && data.weekday_text.length) {
      html += '<div style="margin-bottom:.35rem"><strong>営業時間</strong><br>' +
        data.weekday_text.map(t => escHtml(t)).join('<br>') +
        '</div>';
    }

    if (data.phone) {
      html += '<div>📞 <a href="tel:' + escHtml(data.phone) + '" style="color:inherit">' +
        escHtml(data.phone) + '</a></div>';
    }

    if (data.website) {
      html += '<div style="margin-top:.3rem">🌐 <a href="' + escHtml(data.website) +
        '" target="_blank" rel="noreferrer" style="word-break:break-all;color:#059669">' +
        escHtml(data.website.replace(/^https?:\/\//, '')) + '</a></div>';
    }

    el.innerHTML = html || '詳細情報がありません。';
  }

  function escHtml(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  // ── Save / Favorite buttons ────────────────────────────────────────────────

  document.querySelectorAll('.save-button').forEach(btn => {
    btn.addEventListener('click', async () => {
      const pid    = btn.dataset.planId;
      const saved  = btn.dataset.saved === 'true';
      const method = saved ? 'DELETE' : 'POST';

      try {
        const res  = await fetch('/api/favorites/' + encodeURIComponent(pid), { method });
        const json = await res.json();
        if (json.ok) {
          const nowSaved = !saved;
          btn.dataset.saved = nowSaved ? 'true' : 'false';
          // update all save-button instances with same plan-id
          document.querySelectorAll('.save-button[data-plan-id="' + pid + '"]').forEach(b => {
            b.dataset.saved = nowSaved ? 'true' : 'false';
            if (b.classList.contains('pd-save')) {
              b.textContent = nowSaved ? '★' : '☆';
            } else {
              b.textContent = nowSaved ? '★ 保存済み' : '💾 このプランを保存';
              b.classList.toggle('is-saved', nowSaved);
            }
          });
        }
      } catch { /* network error — ignore */ }
    });
  });

})();
