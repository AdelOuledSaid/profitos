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
