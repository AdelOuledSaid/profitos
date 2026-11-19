/* v524 — Recherche dans le guide.
 *
 * Le guide fait une quarantaine d'écrans d'affilée : on ne pouvait que le parcourir,
 * et quelqu'un qui cherche « comment faire ma première facture » n'avait aucune entrée.
 * Ce script découpe le contenu en sections (un titre et ce qui le suit), filtre à la
 * frappe, et masque dans le sommaire les sections devenues vides. Sans recherche saisie,
 * la page reste exactement telle qu'elle était : rien ne dépend du script pour lire.
 *
 * Aucun style en ligne, aucune dépendance : la politique de sécurité n'autorise que les
 * scripts et feuilles servis par le site.
 */
(function () {
  'use strict';

  var champ = document.getElementById('help-search');
  var contenu = document.querySelector('.help-content');
  if (!champ || !contenu) return;

  var compteur = document.getElementById('help-search-count');
  var sommaire = document.querySelector('.help-toc');

  // Découpe : chaque titre (h2 ou h3) emporte les éléments qui le suivent jusqu'au
  // prochain titre de même niveau ou supérieur.
  var sections = [];
  var courante = null;
  Array.prototype.forEach.call(contenu.children, function (el) {
    var niveau = el.tagName === 'H2' ? 2 : (el.tagName === 'H3' ? 3 : 0);
    if (niveau) {
      courante = { titre: el, elements: [el], niveau: niveau,
                   id: el.id || '', texte: (el.textContent || '').toLowerCase() };
      sections.push(courante);
    } else if (courante) {
      courante.elements.push(el);
      courante.texte += ' ' + (el.textContent || '').toLowerCase();
    }
  });
  if (!sections.length) return;

  function sansAccents(s) {
    return s.normalize ? s.normalize('NFD').replace(/[̀-ͯ]/g, '') : s;
  }

  function afficher(section, visible) {
    section.elements.forEach(function (el) { el.hidden = !visible; });
  }

  function filtrer() {
    var requete = sansAccents((champ.value || '').toLowerCase()).trim();
    var mots = requete.split(/\s+/).filter(Boolean);
    var trouvees = 0;
    var idsVisibles = {};

    sections.forEach(function (section) {
      var texte = sansAccents(section.texte);
      var ok = !mots.length || mots.every(function (mot) { return texte.indexOf(mot) !== -1; });
      afficher(section, ok);
      if (ok) {
        trouvees += 1;
        if (section.id) idsVisibles[section.id] = true;
        // une sous-section trouvée doit laisser visible le titre qui la porte
        if (section.niveau === 3) {
          for (var i = sections.indexOf(section); i >= 0; i--) {
            if (sections[i].niveau === 2) { afficher(sections[i], true); if (sections[i].id) idsVisibles[sections[i].id] = true; break; }
          }
        }
      }
    });

    if (sommaire) {
      Array.prototype.forEach.call(sommaire.querySelectorAll('a[href^="#"]'), function (lien) {
        var cible = lien.getAttribute('href').slice(1);
        var li = lien.closest('li') || lien;
        li.hidden = mots.length ? !idsVisibles[cible] : false;
      });
    }

    if (compteur) {
      if (!mots.length) compteur.textContent = '';
      else if (!trouvees) compteur.textContent = 'Aucun passage ne contient ces mots.';
      else compteur.textContent = trouvees + (trouvees > 1 ? ' passages trouvés' : ' passage trouvé');
    }
  }

  champ.addEventListener('input', filtrer);
  champ.addEventListener('search', filtrer);
  // Les raccourcis d'entrée du guide remplissent le champ plutôt que d'y faire défiler.
  Array.prototype.forEach.call(document.querySelectorAll('[data-help-query]'), function (bouton) {
    bouton.addEventListener('click', function (ev) {
      ev.preventDefault();
      champ.value = bouton.getAttribute('data-help-query');
      filtrer();
      champ.focus();
    });
  });
})();
