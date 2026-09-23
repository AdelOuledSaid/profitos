'use strict';
document.addEventListener('DOMContentLoaded', function () {
  var btn = document.getElementById('offline-retry');
  if (btn) btn.addEventListener('click', function () { location.reload(); });
});
