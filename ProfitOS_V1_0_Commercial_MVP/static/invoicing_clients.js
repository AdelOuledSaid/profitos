document.addEventListener('DOMContentLoaded', function () {
  const select = document.getElementById('saved-client');
  if (!select) return;

  function fillClient() {
    const option = select.options[select.selectedIndex];
    if (!option || !option.value) return;

    const name = document.getElementById('invoice-client-name');
    const email = document.getElementById('invoice-client-email');
    const address = document.getElementById('invoice-client-address');

    if (name) name.value = option.dataset.name || '';
    if (email) email.value = option.dataset.email || '';
    if (address) address.value = option.dataset.address || '';
  }

  select.addEventListener('change', fillClient);
  fillClient();
});
