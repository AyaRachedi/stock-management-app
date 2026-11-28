"""
test_import_csv.py — Tests automatiques de l'import CSV (POST /produits/import-csv).

Le noyau n'a pas besoin de tourner : les envois vers le noyau sont
remplacés par un faux qui les enregistre, pour vérifier ce qui aurait
été publié. Il faut seulement PostgreSQL et le fichier .env.

Lancement, depuis le dossier agent-stock :
    pip install pytest
    python -m pytest test_import_csv.py -v
"""
import uuid

import pytest
from fastapi.testclient import TestClient

import main

client = TestClient(main.app)


# ------------------------------------------------------------------
# Faux noyau : enregistre les publications et les alertes
# ------------------------------------------------------------------
@pytest.fixture(autouse=True)
def faux_noyau(monkeypatch):
    envois = {"bus": [], "alertes": []}

    async def faux_publier(tenant, type_objet, external_id, payload):
        envois["bus"].append((type_objet, external_id, payload))

    async def fausse_alerte(tenant, alerte):
        envois["alertes"].append(alerte)

    monkeypatch.setattr(main, "publier_sur_bus", faux_publier)
    monkeypatch.setattr(main, "envoyer_alerte", fausse_alerte)
    return envois


def _activer():
    """Active une entreprise fictive comme le ferait le noyau."""
    tid = f"test-csv-{uuid.uuid4().hex[:8]}"
    cle = f"tak_{uuid.uuid4().hex}"
    h = {"X-Tenant-Id": tid, "Authorization": f"Bearer {cle}"}
    r = client.post("/tenant/activate", headers=h, json={"config": {}})
    assert r.status_code == 200, r.text
    return h


@pytest.fixture
def entreprise():
    h = _activer()
    yield h
    client.post("/tenant/deactivate", headers=h)


@pytest.fixture
def autre_entreprise():
    h = _activer()
    yield h
    client.post("/tenant/deactivate", headers=h)


# ------------------------------------------------------------------
# Outils
# ------------------------------------------------------------------
def importer(h, texte, encodage="utf-8"):
    return client.post(
        "/produits/import-csv",
        headers=h,
        files={"fichier": ("stock.csv", texte.encode(encodage), "text/csv")},
    )


def produit(h, sku):
    r = client.get(f"/produits/{sku}", headers=h)
    return r.json()["data"] if r.status_code == 200 else None


def quantite(h, sku):
    p = produit(h, sku)
    return p["quantite_disponible"] if p else None


def mouvements(h, sku):
    return client.get(f"/mouvements?sku={sku}", headers=h).json()["data"]


def enveloppe(r):
    return set(r.json()) == {"status", "data", "error"}


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------
def test_creation_des_produits(entreprise, faux_noyau):
    r = importer(entreprise,
                 "sku,designation,quantite_disponible,seuil_alerte,entrepot\n"
                 "PROD-001,Clavier sans fil,20,5,Principal\n"
                 "PROD-002,Souris optique,3,,\n")
    assert r.status_code == 200 and enveloppe(r), r.text
    assert r.json()["data"] == {"lignes": 2, "crees": 2, "mis_a_jour": 0, "inchanges": 0}
    assert quantite(entreprise, "PROD-001") == 20
    souris = produit(entreprise, "PROD-002")
    assert souris["seuil_alerte"] == 5 and souris["entrepot"] == "Principal"
    assert [m["type_mouvement"] for m in mouvements(entreprise, "PROD-001")] == ["entree"]


def test_publication_au_noyau_et_alerte(entreprise, faux_noyau):
    importer(entreprise, "sku,designation,quantite_disponible,seuil_alerte\nK1,Clavier,3,5\n")
    types = [t for t, _, _ in faux_noyau["bus"]]
    assert types == ["mouvement_stock", "stock_level"]
    niveau = faux_noyau["bus"][1][2]
    assert niveau == {"sku": "K1", "designation": "Clavier", "quantite_disponible": 3,
                      "seuil_alerte": 5, "entrepot": "Principal"}
    assert [a["event_type"] for a in faux_noyau["alertes"]] == ["stock_faible"]


def test_remplacement_de_la_quantite_et_ecart(entreprise):
    importer(entreprise, "sku,designation,quantite_disponible\nPROD-001,Clavier,20\n")
    r = importer(entreprise, "sku,designation,quantite_disponible\nPROD-001,Clavier,17\n")
    assert r.json()["data"]["mis_a_jour"] == 1
    assert quantite(entreprise, "PROD-001") == 17
    ajustements = [m["quantite"] for m in mouvements(entreprise, "PROD-001")
                   if m["type_mouvement"] == "ajustement"]
    assert ajustements == [-3]


def test_produit_inchange_rien_publie(entreprise, faux_noyau):
    importer(entreprise, "sku,designation,quantite_disponible\nPROD-001,Clavier,20\n")
    faux_noyau["bus"].clear()
    r = importer(entreprise, "sku,designation,quantite_disponible\nPROD-001,Clavier,20\n")
    assert r.json()["data"]["inchanges"] == 1
    assert faux_noyau["bus"] == []


def test_format_excel_francais(entreprise):
    """Excel en francais : separateur ';', BOM UTF-8 et accents."""
    r = importer(entreprise,
                 "�sku;designation;quantite_disponible;seuil_alerte;entrepot\n"
                 "PROD-010;Écran 24 pouces;8;2;Entrepôt Nord\n")
    assert r.status_code == 200, r.text
    ecran = produit(entreprise, "PROD-010")
    assert ecran["designation"] == "Écran 24 pouces" and ecran["entrepot"] == "Entrepôt Nord"


def test_ancien_encodage_windows(entreprise):
    r = importer(entreprise, "sku;designation;quantite_disponible\nP-1;Clé USB;4\n", encodage="cp1252")
    assert r.status_code == 200, r.text
    assert produit(entreprise, "P-1")["designation"] == "Clé USB"


def test_tout_ou_rien(entreprise, faux_noyau):
    """Une seule ligne fausse : rien n'est importe, et l'erreur indique la ligne."""
    importer(entreprise, "sku,designation,quantite_disponible\nPROD-001,Clavier,20\n")
    faux_noyau["bus"].clear()
    r = importer(entreprise,
                 "sku,designation,quantite_disponible\n"
                 "PROD-001,Clavier,5\n"
                 "PROD-002,Souris,-4\n"
                 "PROD-003,,abc\n")
    assert r.status_code == 400 and enveloppe(r)
    assert {e["ligne"] for e in r.json()["data"]["erreurs"]} == {3, 4}
    assert quantite(entreprise, "PROD-001") == 20
    assert produit(entreprise, "PROD-002") is None
    assert faux_noyau["bus"] == []


def test_sku_en_double(entreprise):
    r = importer(entreprise, "sku,designation,quantite_disponible\nA,x,1\nA,y,2\n")
    assert r.status_code == 400
    assert "ligne 2" in r.json()["data"]["erreurs"][0]["erreur"]


def test_colonne_obligatoire_manquante(entreprise):
    r = importer(entreprise, "sku,designation\nA,x\n")
    assert r.status_code == 400 and enveloppe(r)
    assert "quantite_disponible" in r.json()["error"]


def test_colonne_inconnue(entreprise):
    r = importer(entreprise, "sku,designation,quantite_disponible,prix\nA,x,1,3\n")
    assert r.status_code == 400 and "prix" in r.json()["error"]


def test_fichier_vide(entreprise):
    r = importer(entreprise, "")
    assert r.status_code == 400 and enveloppe(r)


def test_isolation_entre_entreprises(entreprise, autre_entreprise):
    importer(entreprise, "sku,designation,quantite_disponible\nPROD-001,Clavier,20\n")
    importer(autre_entreprise, "sku,designation,quantite_disponible\nPROD-001,Clavier,99\n")
    assert quantite(entreprise, "PROD-001") == 20
    assert quantite(autre_entreprise, "PROD-001") == 99


def test_refuse_sans_cle(entreprise):
    sans_cle = {"X-Tenant-Id": entreprise["X-Tenant-Id"]}
    r = importer(sans_cle, "sku,designation,quantite_disponible\nA,x,1\n")
    assert r.status_code == 401 and enveloppe(r)


def test_refuse_avec_mauvaise_cle(entreprise):
    pirate = {"X-Tenant-Id": entreprise["X-Tenant-Id"], "Authorization": "Bearer fausse"}
    r = importer(pirate, "sku,designation,quantite_disponible\nA,x,1\n")
    assert r.status_code == 401
    assert produit(entreprise, "A") is None