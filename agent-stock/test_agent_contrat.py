"""
test_agent_contrat.py — Tests du contrat d'intégration, des mouvements,
de l'isolation entre entreprises et de la réception des commandes.

Le noyau n'a pas besoin de tourner : ses réponses sont remplacées par
un faux. Il faut seulement PostgreSQL et le fichier .env.

Lancement, depuis le dossier agent-stock :
    python -m pytest test_agent_contrat.py -v
"""
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


@pytest.fixture(autouse=True)
def faux_noyau(monkeypatch):
    envois = {"bus": [], "alertes": []}

    async def faux_publier(tenant, type_objet, external_id, payload):
        envois["bus"].append((type_objet, external_id, payload))
        return True

    async def fausse_alerte(tenant, alerte):
        envois["alertes"].append(alerte)

    monkeypatch.setattr(main, "publier_sur_bus", faux_publier)
    monkeypatch.setattr(main, "envoyer_alerte", fausse_alerte)
    return envois


def _activer():
    tid = f"test-ctr-{uuid.uuid4().hex[:8]}"
    cle = f"tak_{uuid.uuid4().hex}"
    h = {"X-Tenant-Id": tid, "Authorization": f"Bearer {cle}"}
    r = client.post("/tenant/activate", headers=h, json={"config": {}})
    assert r.status_code == 200, r.text
    return h


@pytest.fixture
def ent():
    h = _activer()
    yield h
    client.post("/tenant/deactivate", headers=h)


@pytest.fixture
def autre():
    h = _activer()
    yield h
    client.post("/tenant/deactivate", headers=h)


def enveloppe_ok(r):
    j = r.json()
    return set(j) == {"status", "data", "error"}


def creer(h, sku="SKU-001", qte=10, seuil=3):
    r = client.post("/produits", headers=h, json={
        "sku": sku, "designation": "Câble HDMI",
        "quantite_disponible": qte, "seuil_alerte": seuil})
    assert r.status_code in (200, 201), r.text
    return r


def stock(h, sku="SKU-001"):
    return client.get(f"/produits/{sku}", headers=h).json()["data"]["quantite_disponible"]


def commande(numero="CMD-1", lignes=None, statut="confirmee"):
    return {
        "object_type": "commande",
        "external_id": numero,
        "version": 1,
        "payload": {
            "numero_commande": numero,
            "date_commande": "2027-01-10T10:00:00Z",
            "client": {"nom": "Dupont", "email": "a@b.fr"},
            "lignes": lignes or [{"sku": "SKU-001", "quantite": 4, "prix_unitaire_ht": 4.5}],
            "montant_total_ht": 18.0,
            "statut": statut,
        },
    }


# ---------------- Contrat : endpoints obligatoires ----------------

def test_health_enveloppe():
    r = client.get("/health")
    assert r.status_code == 200 and enveloppe_ok(r)
    assert "uptime_seconds" in r.json()["data"]


def test_config_schema_enveloppe():
    r = client.get("/config-schema")
    assert r.status_code == 200 and enveloppe_ok(r)


def test_activate_status_deactivate(ent):
    tid = ent["X-Tenant-Id"]
    r = client.get(f"/tenant/{tid}/status", headers=ent)
    assert r.status_code == 200 and r.json()["data"]["state"] == "running"
    r = client.post("/tenant/deactivate", headers=ent)
    assert r.json()["data"] == {"deactivated": True, "data_purged": True}
    assert enveloppe_ok(r)
    r = client.get(f"/tenant/{tid}/status", headers=ent)
    assert r.status_code == 404 and enveloppe_ok(r)


def test_deactivate_supprime_les_donnees():
    h = _activer()
    creer(h)
    client.post("/mouvements", headers=h,
                json={"sku": "SKU-001", "type_mouvement": "entree", "quantite": 5})
    client.post("/tenant/deactivate", headers=h)
    h2 = {"X-Tenant-Id": h["X-Tenant-Id"], "Authorization": h["Authorization"]}
    client.post("/tenant/activate", headers=h2, json={"config": {}})
    assert client.get("/produits", headers=h2).json()["data"] == []
    assert client.get("/mouvements", headers=h2).json()["data"] == []
    client.post("/tenant/deactivate", headers=h2)


def test_activate_sans_cle_refuse():
    r = client.post("/tenant/activate", headers={"X-Tenant-Id": "x"}, json={"config": {}})
    assert r.status_code == 401 and enveloppe_ok(r)


def test_status_mauvaise_cle(ent):
    tid = ent["X-Tenant-Id"]
    r = client.get(f"/tenant/{tid}/status",
                   headers={"X-Tenant-Id": tid, "Authorization": "Bearer mauvaise"})
    assert r.status_code == 401 and enveloppe_ok(r)


def test_erreur_validation_enveloppe(ent):
    r = client.post("/produits", headers=ent, json={"sku": ""})
    assert r.status_code == 422 and enveloppe_ok(r)
    assert r.json()["status"] == "error"


# ---------------- Mouvements ----------------

def test_entree_sortie_ajustement(ent, faux_noyau):
    creer(ent, qte=10)
    for typ, q, attendu in [("entree", 5, 15), ("sortie", 3, 12), ("ajustement", -2, 10)]:
        r = client.post("/mouvements", headers=ent,
                        json={"sku": "SKU-001", "type_mouvement": typ, "quantite": q})
        assert r.status_code == 200, r.text
        assert stock(ent) == attendu
    types = [t for t, _, _ in faux_noyau["bus"]]
    assert types.count("mouvement_stock") == 3 and types.count("stock_level") == 3


def test_sortie_stock_insuffisant(ent):
    creer(ent, qte=2)
    r = client.post("/mouvements", headers=ent,
                    json={"sku": "SKU-001", "type_mouvement": "sortie", "quantite": 5})
    assert r.status_code == 400 and enveloppe_ok(r)
    assert stock(ent) == 2


def test_mouvement_produit_inconnu(ent):
    r = client.post("/mouvements", headers=ent,
                    json={"sku": "NOPE", "type_mouvement": "entree", "quantite": 1})
    assert r.status_code == 404


def test_alerte_stock_faible_puis_rupture(ent, faux_noyau):
    creer(ent, qte=10, seuil=3)
    client.post("/mouvements", headers=ent,
                json={"sku": "SKU-001", "type_mouvement": "sortie", "quantite": 8})
    assert faux_noyau["alertes"][-1]["event_type"] == "stock_faible"
    client.post("/mouvements", headers=ent,
                json={"sku": "SKU-001", "type_mouvement": "sortie", "quantite": 2})
    assert faux_noyau["alertes"][-1]["event_type"] == "stock_rupture"
    assert faux_noyau["alertes"][-1]["severity"] == "critical"
    alertes = client.get("/alertes", headers=ent).json()["data"]
    assert [a["event_type"] for a in alertes] == ["stock_rupture"]


# ---------------- Isolation entre entreprises ----------------

def test_isolation_produits_mouvements_alertes(ent, autre):
    creer(ent, qte=0)
    client.post("/mouvements", headers=ent,
                json={"sku": "SKU-001", "type_mouvement": "entree", "quantite": 1})
    assert client.get("/produits", headers=autre).json()["data"] == []
    assert client.get("/mouvements", headers=autre).json()["data"] == []
    assert client.get("/alertes", headers=autre).json()["data"] == []
    assert client.get("/produits/SKU-001", headers=autre).status_code == 404
    r = client.post("/mouvements", headers=autre,
                    json={"sku": "SKU-001", "type_mouvement": "entree", "quantite": 1})
    assert r.status_code == 404


def test_cle_dune_autre_entreprise_refusee(ent, autre):
    h = {"X-Tenant-Id": ent["X-Tenant-Id"], "Authorization": autre["Authorization"]}
    assert client.get("/produits", headers=h).status_code == 401


# ---------------- /exchange/notify (commandes) ----------------

def test_notify_commande_valide(ent, faux_noyau):
    creer(ent, qte=10)
    r = client.post("/exchange/notify", headers=ent, json=commande())
    assert r.status_code == 200 and r.json()["data"]["processed"] is True
    assert stock(ent) == 6
    mvts = client.get("/mouvements?sku=SKU-001", headers=ent).json()["data"]
    assert mvts[0]["reference_commande"] == "CMD-1"
    types = [t for t, _, _ in faux_noyau["bus"]]
    assert "mouvement_stock" in types and "stock_level" in types


def test_notify_commande_en_double(ent):
    creer(ent, qte=10)
    client.post("/exchange/notify", headers=ent, json=commande())
    r = client.post("/exchange/notify", headers=ent, json=commande())
    assert r.json()["data"]["duplicate"] is True
    assert stock(ent) == 6


def test_notify_stock_insuffisant_tout_ou_rien(ent, faux_noyau):
    creer(ent, "A", qte=10)
    creer(ent, "B", qte=1)
    lignes = [{"sku": "A", "quantite": 2, "prix_unitaire_ht": 1},
              {"sku": "B", "quantite": 5, "prix_unitaire_ht": 1}]
    r = client.post("/exchange/notify", headers=ent, json=commande("CMD-2", lignes))
    assert r.status_code == 400 and enveloppe_ok(r)
    assert stock(ent, "A") == 10 and stock(ent, "B") == 1
    assert faux_noyau["alertes"][-1]["event_type"] == "commande_non_traitee"
    assert faux_noyau["alertes"][-1]["data"]["numero_commande"] == "CMD-2"


def test_notify_sku_inconnu(ent, faux_noyau):
    lignes = [{"sku": "NOPE", "quantite": 1, "prix_unitaire_ht": 1}]
    r = client.post("/exchange/notify", headers=ent, json=commande("CMD-3", lignes))
    assert r.status_code == 404
    assert faux_noyau["alertes"][-1]["event_type"] == "commande_non_traitee"


@pytest.mark.parametrize("statut", ["en_attente", "annulee", "expediee", "facturee"])
def test_notify_commande_non_confirmee_ignoree(ent, statut):
    creer(ent, qte=10)
    r = client.post("/exchange/notify", headers=ent, json=commande(statut=statut))
    assert r.status_code == 200 and r.json()["data"]["processed"] is False
    assert stock(ent) == 10


def test_notify_commande_traitee_quand_elle_devient_confirmee(ent):
    creer(ent, qte=10)
    client.post("/exchange/notify", headers=ent, json=commande(statut="en_attente"))
    assert stock(ent) == 10
    r = client.post("/exchange/notify", headers=ent, json=commande(statut="confirmee"))
    assert r.json()["data"]["processed"] is True
    assert stock(ent) == 6


def test_notify_type_ignore(ent):
    r = client.post("/exchange/notify", headers=ent,
                    json={"object_type": "expedition", "payload": {}})
    assert r.status_code == 200 and r.json()["data"]["processed"] is False


def test_notify_champ_inconnu_ignore(ent):
    creer(ent, qte=10)
    c = commande()
    c["payload"]["champ_futur"] = "x"
    assert client.post("/exchange/notify", headers=ent, json=c).status_code == 200


def test_notify_commande_invalide(ent):
    r = client.post("/exchange/notify", headers=ent,
                    json={"object_type": "commande", "payload": {"numero_commande": "X"}})
    assert r.status_code == 422 and enveloppe_ok(r)


def test_notify_sans_cle(ent):
    r = client.post("/exchange/notify", headers={"X-Tenant-Id": ent["X-Tenant-Id"]},
                    json=commande())
    assert r.status_code == 401


def test_notify_isolation(ent, autre):
    creer(ent, qte=10)
    r = client.post("/exchange/notify", headers=autre, json=commande())
    assert r.status_code == 404  # produit absent chez l'autre entreprise
    assert stock(ent) == 10


# ---------------- Noyau injoignable ----------------

def test_noyau_injoignable_ne_bloque_pas(ent, monkeypatch):
    """Sans faux noyau : la vraie publication échoue, le mouvement reste valide."""
    class ClientCasse:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): raise httpx.ConnectError("noyau down")

    creer(ent, qte=10)
    monkeypatch.undo()  # retire le faux noyau
    monkeypatch.setattr(main.httpx, "AsyncClient", ClientCasse)
    r = client.post("/mouvements", headers=ent,
                    json={"sku": "SKU-001", "type_mouvement": "sortie", "quantite": 4})
    assert r.status_code == 200, r.text
    assert stock(ent) == 6


# ---------------- Seuil d'alerte : défaut côté back ----------------

def test_config_schema_sans_reglage():
    r = client.get("/config-schema")
    assert r.json()["data"]["properties"] == {}


def test_creation_sans_seuil_donne_5(ent):
    r = client.post("/produits", headers=ent, json={
        "sku": "S1", "designation": "Souris", "quantite_disponible": 10})
    assert r.status_code in (200, 201), r.text
    assert client.get("/produits/S1", headers=ent).json()["data"]["seuil_alerte"] == 5


def test_seuil_negatif_refuse(ent):
    r = client.post("/produits", headers=ent, json={
        "sku": "S1", "designation": "Souris", "quantite_disponible": 10,
        "seuil_alerte": -1})
    assert r.status_code == 422 and enveloppe_ok(r)


def test_modifier_le_seuil(ent):
    creer(ent, "S1", qte=10, seuil=3)
    r = client.put("/produits/S1", headers=ent, json={"seuil_alerte": 8})
    assert r.status_code == 200, r.text
    assert client.get("/produits/S1", headers=ent).json()["data"]["seuil_alerte"] == 8


def _csv(h, texte):
    return client.post("/produits/import-csv", headers=h,
                       files={"fichier": ("s.csv", texte.encode("utf-8"), "text/csv")})


def test_csv_sans_colonne_seuil_donne_5(ent):
    r = _csv(ent, "sku,designation,quantite_disponible\nC1,Clavier,10\n")
    assert r.status_code == 200, r.text
    assert client.get("/produits/C1", headers=ent).json()["data"]["seuil_alerte"] == 5


def test_csv_seuil_vide_donne_5(ent):
    r = _csv(ent, "sku,designation,quantite_disponible,seuil_alerte\nC1,Clavier,10,\n")
    assert r.status_code == 200, r.text
    assert client.get("/produits/C1", headers=ent).json()["data"]["seuil_alerte"] == 5


def test_csv_seuil_vide_garde_le_seuil_existant(ent):
    creer(ent, "C1", qte=10, seuil=9)
    _csv(ent, "sku,designation,quantite_disponible,seuil_alerte\nC1,Clavier,12,\n")
    assert client.get("/produits/C1", headers=ent).json()["data"]["seuil_alerte"] == 9


# ---------------- Annulation d'une commande ----------------

def test_annulation_apres_confirmation_remet_le_stock(ent, faux_noyau):
    creer(ent, qte=10)
    client.post("/exchange/notify", headers=ent, json=commande(statut="confirmee"))
    assert stock(ent) == 6
    r = client.post("/exchange/notify", headers=ent, json=commande(statut="annulee"))
    assert r.status_code == 200 and r.json()["data"]["restocked"] is True
    assert stock(ent) == 10
    mvts = client.get("/mouvements?sku=SKU-001", headers=ent).json()["data"]
    assert sorted(m["type_mouvement"] for m in mvts) == ["entree", "sortie"]
    assert all(m["reference_commande"] == "CMD-1" for m in mvts)
    types = [t for t, _, _ in faux_noyau["bus"]]
    assert types.count("mouvement_stock") == 2


def test_annulation_recue_deux_fois_remet_une_seule_fois(ent):
    creer(ent, qte=10)
    client.post("/exchange/notify", headers=ent, json=commande(statut="confirmee"))
    client.post("/exchange/notify", headers=ent, json=commande(statut="annulee"))
    r = client.post("/exchange/notify", headers=ent, json=commande(statut="annulee"))
    assert r.json()["data"]["duplicate"] is True
    assert stock(ent) == 10


def test_annulation_sans_confirmation_ne_change_rien(ent):
    creer(ent, qte=10)
    r = client.post("/exchange/notify", headers=ent, json=commande(statut="annulee"))
    assert r.status_code == 200 and r.json()["data"]["processed"] is False
    assert stock(ent) == 10
    assert client.get("/mouvements", headers=ent).json()["data"] == []


def test_annulation_commande_a_plusieurs_lignes(ent):
    creer(ent, "A", qte=10)
    creer(ent, "B", qte=5)
    lignes = [{"sku": "A", "quantite": 3, "prix_unitaire_ht": 1},
              {"sku": "B", "quantite": 2, "prix_unitaire_ht": 1}]
    client.post("/exchange/notify", headers=ent, json=commande("CMD-9", lignes))
    assert (stock(ent, "A"), stock(ent, "B")) == (7, 3)
    client.post("/exchange/notify", headers=ent,
                json=commande("CMD-9", lignes, statut="annulee"))
    assert (stock(ent, "A"), stock(ent, "B")) == (10, 5)


def test_annulation_isolation_entre_entreprises(ent, autre):
    creer(ent, qte=10)
    creer(autre, qte=10)
    client.post("/exchange/notify", headers=ent, json=commande(statut="confirmee"))
    client.post("/exchange/notify", headers=autre, json=commande(statut="annulee"))
    assert stock(ent) == 6 and stock(autre) == 10


# ---------------- CORS (front) ----------------

def test_cors_preflight_front_autorise():
    r = client.options("/produits", headers={
        "Origin": "http://localhost:5173",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,x-tenant-id,content-type",
    })
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "x-tenant-id" in r.headers["access-control-allow-headers"].lower()


def test_cors_origine_inconnue_refusee():
    r = client.options("/produits", headers={
        "Origin": "http://site-inconnu.com",
        "Access-Control-Request-Method": "GET",
    })
    assert "access-control-allow-origin" not in r.headers