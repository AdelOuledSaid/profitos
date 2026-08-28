'use strict';

if ('serviceWorker' in navigator) {
  window.addEventListener('load', function () {
    navigator.serviceWorker.register('/service-worker.js').catch(function () {});
  });
}

document.addEventListener('change', function (event) {
  var target = event.target;
  if (target && target.matches('[data-auto-submit]') && target.form) {
    target.form.requestSubmit ? target.form.requestSubmit() : target.form.submit();
  }
});

document.addEventListener('click', function (event) {
  var toggle = event.target.closest('[data-notif-toggle]');
  if (toggle) {
    var d = document.getElementById('notif-dropdown');
    if (d) d.style.display = d.style.display === 'block' ? 'none' : 'block';
    return;
  }
  var changelogToggle = event.target.closest('[data-changelog-toggle]');
  if (changelogToggle) {
    var cd = document.getElementById('changelog-dropdown');
    if (cd) cd.style.display = cd.style.display === 'block' ? 'none' : 'block';
    return;
  }
  var replay = event.target.closest('[data-tour-replay]');
  if (replay && window.profitosStartTour) {
    window.profitosStartTour();
  }
});

// ---------------------------------------------------------------------------
// Tour guidé — visite interactive de 5 étapes sur les liens de navigation
// (repérés via [data-tour="..."] dans base.html). Se lance automatiquement
// à la première visite du dashboard, rejouable depuis Settings.
// ---------------------------------------------------------------------------
(function () {
  var STEPS = [
    { selector: '[data-tour="recover"]', title: 'RECOVER', text: "Toutes tes créances échues et retenues de garantie, classées par urgence." },
    { selector: '[data-tour="save"]', title: 'SAVE', text: "Économies détectées automatiquement : doublons, hausses fournisseurs, contrats dormants." },
    { selector: '[data-tour="grow"]', title: 'GROW', text: "Appels d'offres publics qui correspondent à ton profil, mis à jour depuis BOAMP." },
    { selector: '[data-tour="actions"]', title: 'Action Center', text: "Prépare, approuve puis envoie tes relances — rien ne part sans ta validation." },
    { selector: '[data-tour="uploads"]', title: 'Importer', text: "Uploade tes factures et dépenses Excel/CSV pour lancer ta première analyse." }
  ];
  var STORAGE_KEY = 'profitos_tour_seen';

  function buildOverlay() {
    var overlay = document.createElement('div');
    overlay.id = 'profitos-tour-overlay';
    overlay.style.cssText = 'position:fixed;inset:0;z-index:9999;pointer-events:none;';
    document.body.appendChild(overlay);
    return overlay;
  }

  function showStep(index, overlay) {
    overlay.innerHTML = '';
    if (index >= STEPS.length) { overlay.remove(); localStorage.setItem(STORAGE_KEY, '1'); return; }
    var step = STEPS[index];
    var el = document.querySelector(step.selector);
    if (!el) { showStep(index + 1, overlay); return; }
    var rect = el.getBoundingClientRect();

    var highlight = document.createElement('div');
    highlight.style.cssText = 'position:fixed;pointer-events:none;border:2px solid #5fe0ac;border-radius:8px;' +
      'top:' + (rect.top - 4) + 'px;left:' + (rect.left - 4) + 'px;width:' + (rect.width + 8) + 'px;height:' + (rect.height + 8) + 'px;' +
      'box-shadow:0 0 0 4000px rgba(3,7,18,0.72);transition:all .2s;';
    overlay.appendChild(highlight);

    var card = document.createElement('div');
    card.style.cssText = 'position:fixed;pointer-events:auto;background:#0f1c33;border:1px solid #294064;border-radius:12px;' +
      'padding:16px;max-width:280px;color:#f6f8fc;font-family:inherit;box-shadow:0 8px 30px rgba(0,0,0,.4);' +
      'top:' + Math.min(rect.bottom + 12, window.innerHeight - 160) + 'px;left:' + Math.min(rect.left, window.innerWidth - 300) + 'px;';
    card.innerHTML = '<div style="font-size:11px;color:#8fa9d3;text-transform:uppercase;letter-spacing:.05em;">Étape ' + (index + 1) + '/' + STEPS.length + '</div>' +
      '<div style="font-weight:700;margin:4px 0 6px;">' + step.title + '</div>' +
      '<div style="font-size:13px;color:#c4d3ef;margin-bottom:12px;">' + step.text + '</div>' +
      '<div style="display:flex;gap:8px;justify-content:flex-end;">' +
      '<button data-tour-skip style="background:none;border:none;color:#8fa9d3;font-size:12px;cursor:pointer;">Passer</button>' +
      '<button data-tour-next style="background:#5fe0ac;color:#03210f;border:none;border-radius:6px;padding:6px 12px;font-size:12px;font-weight:700;cursor:pointer;">' +
      (index + 1 === STEPS.length ? 'Terminer' : 'Suivant') + '</button></div>';
    overlay.appendChild(card);

    card.querySelector('[data-tour-next]').addEventListener('click', function () { showStep(index + 1, overlay); });
    card.querySelector('[data-tour-skip]').addEventListener('click', function () { overlay.remove(); localStorage.setItem(STORAGE_KEY, '1'); });
  }

  function startTour() {
    var overlay = buildOverlay();
    showStep(0, overlay);
  }

  window.profitosStartTour = startTour; // exposé pour le lien "Revoir la visite" dans Settings

  document.addEventListener('DOMContentLoaded', function () {
    if (document.body.dataset.tourAuto === '1' && !localStorage.getItem(STORAGE_KEY)) {
      setTimeout(startTour, 600);
    }
  });
})();

// ---------------------------------------------------------------------------
// Révélation au scroll pour la landing page publique — éléments marqués
// [data-reveal] passent en classe .is-visible dès qu'ils entrent dans l'écran.
// Dégradation silencieuse : sans IntersectionObserver, tout reste visible.
// ---------------------------------------------------------------------------
(function () {
  document.addEventListener('DOMContentLoaded', function () {
    var items = document.querySelectorAll('[data-reveal]');
    if (!items.length) return;
    if (!('IntersectionObserver' in window)) {
      items.forEach(function (el) { el.classList.add('is-visible'); });
      return;
    }
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-visible');
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0.15 });
    items.forEach(function (el) { observer.observe(el); });
  });
})();

// ---------------------------------------------------------------------------
// Compteurs animés — éléments [data-count-to] comptent de 0 jusqu'à leur
// valeur cible dès qu'ils entrent dans l'écran. Dégradation silencieuse :
// sans IntersectionObserver, la valeur finale s'affiche directement.
// ---------------------------------------------------------------------------
(function () {
  function formatNumber(n) {
    return Math.round(n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, '\u00a0');
  }

  function animateCount(el) {
    var target = parseFloat(el.getAttribute('data-count-to'));
    var suffix = el.getAttribute('data-count-suffix') || '';
    if (isNaN(target)) return;
    var duration = 1200;
    var start = null;
    function step(timestamp) {
      if (!start) start = timestamp;
      var progress = Math.min((timestamp - start) / duration, 1);
      var eased = 1 - Math.pow(1 - progress, 3);
      el.textContent = formatNumber(target * eased) + suffix;
      if (progress < 1) {
        window.requestAnimationFrame(step);
      } else {
        el.textContent = formatNumber(target) + suffix;
      }
    }
    window.requestAnimationFrame(step);
  }

  document.addEventListener('DOMContentLoaded', function () {
    var items = document.querySelectorAll('[data-count-to]');
    if (!items.length) return;
    if (!('IntersectionObserver' in window) || window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      items.forEach(function (el) {
        var target = parseFloat(el.getAttribute('data-count-to'));
        var suffix = el.getAttribute('data-count-suffix') || '';
        if (!isNaN(target)) el.textContent = formatNumber(target) + suffix;
      });
      return;
    }
    var observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          animateCount(entry.target);
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0.3 });
    items.forEach(function (el) { observer.observe(el); });
  });
})();



// V1.7.7 — Decision Simulator real-business-constraint controls.
document.addEventListener('DOMContentLoaded', function () {
  const form = document.querySelector('.decision-form');
  if (!form) return;
  const delay = form.querySelector('input[type="checkbox"][name="allow_delay"]');
  const maxDelay = form.querySelector('[name="max_delay"]');
  const split = form.querySelector('input[type="checkbox"][name="allow_installments"]');
  const maxInst = form.querySelector('[name="max_installments"]');
  const financing = form.querySelector('input[type="checkbox"][name="allow_financing"]');
  const maxFin = form.querySelector('[name="max_financing"]');
  const sync = function () {
    if (maxDelay && delay) maxDelay.disabled = !delay.checked;
    if (maxInst && split) maxInst.disabled = !split.checked;
    if (maxFin && financing) maxFin.disabled = !financing.checked;
  };
  if (delay) delay.addEventListener('change', sync);
  if (split) split.addEventListener('change', sync);
  if (financing) financing.addEventListener('change', sync);
  sync();
});
