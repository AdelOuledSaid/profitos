"""v501 — L'adresse du destinataire vient de l'annuaire, pas d'une supposition.

Veqalio tenait l'adresse électronique d'un destinataire pour son SIREN. C'est la convention
française la plus répandue, pas une règle : l'adresse peut porter un suffixe de service, et un
document adressé à un identifiant absent de l'annuaire est refusé avant tout acheminement —
« receiver address <0225:000000002> does not accept this document ».
"""
from profitos import einvoice_superpdp as ES


def test_reconnait_une_adresse_quelle_que_soit_la_cle():
    """La forme des entrées n'est pas documentée : l'adresse est reconnue à son motif."""
    assert ES._address_in({'electronic_address': '0225:315143296_121168'}) == '0225:315143296_121168'
    assert ES._address_in({'peppol': {'value': '0225:315143296_121167'}}) == '0225:315143296_121167'
    assert ES._address_in([{'address': '0088:5790000435975'}]) == '0088:5790000435975'


def test_prefere_les_champs_les_plus_probables():
    entry = {'zzz': '0225:999999999', 'electronic_address': '0225:315143296_121168'}
    assert ES._address_in(entry) == '0225:315143296_121168'


def test_ignore_ce_qui_n_est_pas_une_adresse():
    assert ES._address_in({'name': 'Burger Queen', 'city': 'Millau', 'number': '000000002'}) is None
    assert ES._address_in({'url': 'https://exemple.fr/annuaire'}) is None     # a bien un « : »
    assert ES._address_in('') is None
    assert ES._address_in(None) is None


def test_l_annuaire_indisponible_n_empeche_pas_d_emettre(monkeypatch):
    """Une recherche d'adresse ne doit jamais bloquer une émission : mieux vaut un refus de la
    plateforme qu'une facture qu'on ne peut pas transmettre du tout."""
    def boom(*a, **k):
        raise RuntimeError('annuaire injoignable')
    monkeypatch.setattr(ES, 'client_for', boom)
    assert ES.directory_address(None, None, 1, '000000002') is None


def test_un_numero_vide_ne_declenche_aucun_appel(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("l'annuaire ne doit pas être interrogé sans numéro")
    monkeypatch.setattr(ES, 'client_for', boom)
    assert ES.directory_address(None, None, 1, '') is None
    assert ES.directory_address(None, None, 1, None) is None


def test_renvoie_l_adresse_trouvee(monkeypatch):
    class FakeClient:
        def directory(self, number):
            assert number == '000000002'
            return [{'number': '000000002', 'electronic_address': '0225:315143296_121168'}]
    monkeypatch.setattr(ES, 'client_for', lambda *a, **k: FakeClient())
    assert ES.directory_address(None, None, 1, '000000002') == '0225:315143296_121168'
