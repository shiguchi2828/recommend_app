(function () {
  'use strict';

  const COLORS = [
    '#3b82f6', '#f97316', '#06b6d4', '#8b5cf6',
    '#ec4899', '#10b981', '#f59e0b', '#ef4444',
    '#6366f1', '#84cc16',
  ];

  const canvas      = document.getElementById('rouletteCanvas');
  const spinBtn     = document.getElementById('spinBtn');
  const rlResult    = document.getElementById('rlResult');
  const rlPlanCard  = document.getElementById('rlPlanCard');
  const candidates  = document.querySelectorAll('.rl-candidate');

  if (!canvas || !window.ROULETTE_PLANS || !window.ROULETTE_PLANS.length) return;

  const plans  = window.ROULETTE_PLANS;
  const n      = plans.length;
  const ctx    = canvas.getContext('2d');
  const arc    = (Math.PI * 2) / n;

  let currentAngle = 0;
  let spinning     = false;
  let canvasSize   = 0;

  /* ── Sizing ── */
  function resize() {
    const max = Math.min(window.innerWidth - 48, 400);
    canvasSize = max;
    canvas.width  = max;
    canvas.height = max;
    draw(currentAngle);
  }

  /* ── Draw wheel ── */
  function draw(angle) {
    const s  = canvasSize;
    const cx = s / 2;
    const cy = s / 2;
    const r  = s / 2 - 3;

    ctx.clearRect(0, 0, s, s);

    for (let i = 0; i < n; i++) {
      const startA = angle + i * arc - Math.PI / 2;
      const endA   = startA + arc;

      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.arc(cx, cy, r, startA, endA);
      ctx.closePath();
      ctx.fillStyle = COLORS[i % COLORS.length];
      ctx.fill();

      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.arc(cx, cy, r, startA, endA);
      ctx.closePath();
      ctx.strokeStyle = 'rgba(255,255,255,0.75)';
      ctx.lineWidth = 2;
      ctx.stroke();

      const midA  = startA + arc / 2;
      const textR = r * 0.62;

      ctx.save();
      ctx.translate(cx + Math.cos(midA) * textR, cy + Math.sin(midA) * textR);
      ctx.rotate(midA + Math.PI / 2);
      ctx.fillStyle = '#fff';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';

      const fs = Math.max(9, Math.min(13, s / (n * 2.2)));
      ctx.font = `700 ${fs}px Outfit, sans-serif`;
      ctx.shadowColor = 'rgba(0,0,0,0.3)';
      ctx.shadowBlur  = 2;

      const maxLen = n <= 4 ? 9 : n <= 6 ? 7 : 5;
      const label  = plans[i].name.length > maxLen
        ? plans[i].name.slice(0, maxLen - 1) + '…'
        : plans[i].name;

      ctx.fillText(label, 0, 0);
      ctx.restore();
    }

    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.strokeStyle = '#e2e8f0';
    ctx.lineWidth   = 5;
    ctx.stroke();

    const btnR = s * 0.12;
    ctx.beginPath();
    ctx.arc(cx, cy, btnR, 0, Math.PI * 2);
    ctx.fillStyle = '#fff';
    ctx.shadowColor = 'rgba(0,0,0,0.12)';
    ctx.shadowBlur  = 8;
    ctx.fill();
    ctx.shadowBlur  = 0;
    ctx.strokeStyle = '#e2e8f0';
    ctx.lineWidth   = 3;
    ctx.stroke();
  }

  function easeOutQuart(t) {
    return 1 - Math.pow(1 - t, 4);
  }

  /* ── Spin ── */
  function spin() {
    if (spinning) return;
    spinning = true;
    spinBtn.disabled = true;
    spinBtn.classList.add('is-spinning');
    candidates.forEach(c => c.classList.remove('is-winner'));

    // reset plan card
    if (rlPlanCard) {
      rlPlanCard.hidden = true;
      rlPlanCard.innerHTML = '';
    }

    rlResult.innerHTML = '<p class="rl-result-spinning">🎲 選択中...</p>';

    const targetIndex = Math.floor(Math.random() * n);

    const desiredAngle = -((targetIndex + 0.5) * arc);
    let delta = ((desiredAngle - currentAngle) % (Math.PI * 2) + Math.PI * 2) % (Math.PI * 2);
    if (delta < 0.1) delta += Math.PI * 2;
    const extraTurns = (5 + Math.floor(Math.random() * 3)) * Math.PI * 2;
    const totalDelta = extraTurns + delta;

    const startAngle = currentAngle;
    const startTime  = performance.now();
    const duration   = 4200;

    function animate(now) {
      const elapsed = now - startTime;
      const t       = Math.min(elapsed / duration, 1);
      const eased   = easeOutQuart(t);

      currentAngle = startAngle + totalDelta * eased;
      draw(currentAngle);

      if (t < 1) {
        requestAnimationFrame(animate);
        return;
      }

      currentAngle = currentAngle % (Math.PI * 2);
      spinning     = false;
      spinBtn.disabled = false;
      spinBtn.classList.remove('is-spinning');

      candidates.forEach(c => {
        if (parseInt(c.dataset.index) === targetIndex) c.classList.add('is-winner');
      });

      showWinner(targetIndex);
    }

    requestAnimationFrame(animate);
  }

  /* ── Build winner display ── */
  function showWinner(index) {
    const plan = plans[index];

    // result badge
    rlResult.innerHTML =
      '<div class="rl-result-badge">🎉 決定！</div>' +
      '<div class="rl-result-name">' + esc(plan.name) + '</div>';

    // build plan card
    if (!rlPlanCard) return;

    const scheduleHtml = buildSchedule(plan.schedule_items);
    const linksHtml    = buildLinks(plan);

    rlPlanCard.innerHTML =
      '<div class="rl-pc-header">' +
        '<div class="rl-pc-type">' + esc(plan.type || '') + '</div>' +
        '<h2 class="rl-pc-name">' + esc(plan.name) + '</h2>' +
        (plan.highlight ? '<p class="rl-pc-highlight">' + esc(plan.highlight) + '</p>' : '') +
      '</div>' +

      (plan.sns_reason ?
        '<div class="rl-pc-sns">' +
          '<span class="rl-pc-sns-icon">📸</span>' +
          '<p class="rl-pc-sns-text">' + esc(plan.sns_reason) + '</p>' +
        '</div>' : '') +

      '<div class="rl-pc-meta">' +
        '<span>📍 ' + esc(plan.area || '') + '</span>' +
        (plan.stay_time ? '<span>⏱ ' + esc(plan.stay_time) + '</span>' : '') +
        (plan.hours && plan.hours !== '要確認' ? '<span>🕐 ' + esc(plan.hours) + '</span>' : '') +
      '</div>' +

      scheduleHtml +
      linksHtml +

      '<div class="rl-pc-btns">' +
        '<button class="rl-btn-again" id="rlSpinAgain" type="button">🎲 もう一度回す</button>' +
        '<a class="rl-btn-decide" href="/plan/' + esc(plan.id) + '">✓ このプランに決定</a>' +
      '</div>';

    rlPlanCard.hidden = false;

    // scroll to card
    setTimeout(() => rlPlanCard.scrollIntoView({ behavior: 'smooth', block: 'start' }), 100);

    // wire spin-again
    document.getElementById('rlSpinAgain')?.addEventListener('click', () => {
      rlPlanCard.hidden = true;
      window.scrollTo({ top: 0, behavior: 'smooth' });
      spin();
    });
  }

  function buildSchedule(items) {
    if (!items || !items.length) return '';
    let rows = '';
    items.forEach(item => {
      rows +=
        '<div class="rl-sched-row">' +
          '<div class="rl-sched-time">' +
            '<span class="rl-sched-start">' + esc(item.time || '') + '</span>' +
            (item.end_time ? '<span class="rl-sched-end">' + esc(item.end_time) + '</span>' : '') +
          '</div>' +
          '<div class="rl-sched-body">' +
            '<span class="rl-sched-place">' + esc(item.place || '') + '</span>' +
            '<span class="rl-sched-act">' + esc(item.activity || '') + '</span>' +
          '</div>' +
        '</div>' +
        (item.travel_min > 0 ?
          '<div class="rl-sched-travel">🚶 移動 ' + item.travel_min + '分</div>'
          : '');
    });
    return '<div class="rl-pc-schedule"><p class="rl-pc-sched-title">📅 行動プラン</p>' + rows + '</div>';
  }

  function buildLinks(plan) {
    const links = plan.links || {};
    const btns = [
      { key: 'map',       label: '🗺 Map',       cls: 'lk-map' },
      { key: 'directions',label: '🧭 経路', cls: 'lk-dir' },
      { key: 'instagram', label: 'Instagram',              cls: 'lk-ig'  },
      { key: 'tiktok',    label: 'TikTok',                 cls: 'lk-tt'  },
      { key: 'official',  label: '公式',            cls: 'lk-of'  },
    ].filter(b => links[b.key]);

    if (!btns.length) return '';
    return '<div class="rl-pc-links">' +
      btns.map(b =>
        '<a class="rl-pc-link ' + b.cls + '" href="' + links[b.key] + '" target="_blank" rel="noreferrer">' + b.label + '</a>'
      ).join('') +
      '</div>';
  }

  function esc(str) {
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  spinBtn.addEventListener('click', spin);
  window.addEventListener('resize', resize);
  resize();
})();
