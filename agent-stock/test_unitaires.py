"""
test_unitaires.py : tests unitaires de l'agent.

Chaque test vérifie une petite fonction isolée. Aucune base de données, aucun
serveur et aucun appel au noyau ne sont nécessaires : seul le fichier .env doit
exister (il est lu à l'import de l'application).

Lancement, depuis le dossier agent-stock :
    python -m pytest test_unitaires.py -v
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError

import main
import stock_service
from schemas import (CommandeCreate, LigneCommande, MouvementCreate,
                     ProduitCreate, ProduitUpdate)
from stock_service import determiner_niveau_stock, lire_csv_stock, preparer_alerte


# ============================================================
# 1. Niveau de stock et alertes
# ============================================================

@pytest.mark.parametrize("quantite, seuil, attendu", [
    (0, 5, "rupture"),     # plus rien en stock
    (0, 0, "rupture"),     # la rupture prime, même avec un seuil à 0
    (1, 5, "bas"),
    (5, 5, "bas"),         # pile au seuil : stock bas
    (6, 5, "ok"),          # juste au-dessus du seuil
    (1, 0, "ok"),          # seuil à 0 : alerte seulement à la rupture
    (100, 5, "ok"),
])
def test_niveau_de_stock(quantite, seuil, attendu):
    assert determiner_niveau_stock(quantite, seuil) == attendu


def _produit(quantite, seuil=5):
    return SimpleNamespace(sku="SKU-001", designation="Clavier",
                           quantite_disponible=quantite, seuil_alerte=seuil)


def test_alerte_rupture():
    alerte = preparer_alerte(_produit(0))
    assert alerte["event_type"] == "stock_rupture"
    assert alerte["severity"] == "critical"
    assert alerte["data"] == {"sku": "SKU-001", "produit": "Clavier",
                              "quantite_disponible": 0, "seuil_alerte": 5}


def test_alerte_stock_faible():
    alerte = preparer_alerte(_produit(3))
    assert alerte["event_type"] == "stock_faible"
    assert alerte["severity"] == "medium"
    assert alerte["data"]["quantite_disponible"] == 3


def test_pas_d_alerte_si_stock_suffisant():
    assert preparer_alerte(_produit(50)) is None


# ============================================================
# 2. Lecture et vérification du fichier CSV
# ============================================================

def csv(texte, encodage="utf-8"):
    return texte.encode(encodage)


def test_csv_valide():
    lignes, erreurs = lire_csv_stock(csv(
        "sku,designation,quantite_disponible,seuil_alerte,entrepot\n"
        "A1,Clavier,10,3,Nord\n"
        "A2,Souris,2,5,Sud\n"))
    assert erreurs == []
    assert lignes == [
        {"sku": "A1", "designation": "Clavier", "quantite_disponible": 10,
         "seuil_alerte": 3, "entrepot": "Nord"},
        {"sku": "A2", "designation": "Souris", "quantite_disponible": 2,
         "seuil_alerte": 5, "entrepot": "Sud"},
    ]


def test_csv_separateur_point_virgule():
    lignes, erreurs = lire_csv_stock(csv(
        "sku;designation;quantite_disponible\nA1;Clavier;10\n"))
    assert erreurs == [] and lignes[0]["sku"] == "A1"


def test_csv_avec_bom_utf8():
    lignes, erreurs = lire_csv_stock(csv(
        "\ufeffsku,designation,quantite_disponible\nA1,Clavier,10\n"))
    assert erreurs == [] and len(lignes) == 1


def test_csv_encodage_windows_1252():
    lignes, erreurs = lire_csv_stock(csv(
        "sku,designation,quantite_disponible\nA1,Écran,4\n", "cp1252"))
    assert erreurs == [] and lignes[0]["designation"] == "Écran"


def test_csv_seuil_et_entrepot_facultatifs():
    lignes, _ = lire_csv_stock(csv(
        "sku,designation,quantite_disponible,seuil_alerte,entrepot\nA1,Clavier,10,,\n"))
    assert lignes[0]["seuil_alerte"] is None
    assert lignes[0]["entrepot"] is None


def test_csv_lignes_vides_ignorees():
    lignes, erreurs = lire_csv_stock(csv(
        "sku,designation,quantite_disponible\nA1,Clavier,10\n,,\n\nA2,Souris,2\n"))
    assert erreurs == [] and [l["sku"] for l in lignes] == ["A1", "A2"]


def test_csv_quantite_non_numerique_signale_la_ligne():
    lignes, erreurs = lire_csv_stock(csv(
        "sku,designation,quantite_disponible\nA1,Clavier,10\nA2,Souris,abc\n"))
    assert len(lignes) == 1                      # la ligne correcte est conservée
    assert erreurs[0]["ligne"] == 3              # l'en-tête est la ligne 1
    assert "quantite_disponible" in erreurs[0]["erreur"]


def test_csv_quantite_negative_refusee():
    _, erreurs = lire_csv_stock(csv(
        "sku,designation,quantite_disponible\nA1,Clavier,-4\n"))
    assert "négatif" in erreurs[0]["erreur"]


def test_csv_sku_en_double():
    _, erreurs = lire_csv_stock(csv(
        "sku,designation,quantite_disponible\nA1,Clavier,10\nA1,Autre,3\n"))
    assert erreurs[0]["ligne"] == 3 and "déjà présent" in erreurs[0]["erreur"]


def test_csv_sku_ou_designation_manquants():
    _, erreurs = lire_csv_stock(csv(
        "sku,designation,quantite_disponible\n,Clavier,10\nA2,,5\n"))
    messages = [e["erreur"] for e in erreurs]
    assert "sku manquant" in messages and "designation manquante" in messages


def test_csv_sku_trop_long():
    _, erreurs = lire_csv_stock(csv(
        f"sku,designation,quantite_disponible\n{'X' * 51},Clavier,1\n"))
    assert "trop long" in erreurs[0]["erreur"]


@pytest.mark.parametrize("contenu, extrait", [
    (b"", "vide"),
    (csv("sku,designation\nA1,Clavier\n"), "Colonnes obligatoires manquantes"),
    (csv("sku,designation,quantite_disponible,couleur\nA1,Clavier,1,rouge\n"), "Colonnes inconnues"),
    (csv("sku,designation,quantite_disponible\n"), "aucune ligne"),
    (b"x" * (stock_service.TAILLE_MAX_OCTETS + 1), "volumineux"),
])
def test_csv_fichier_invalide(contenu, extrait):
    with pytest.raises(ValueError) as erreur:
        lire_csv_stock(contenu)
    assert extrait in str(erreur.value)


# ============================================================
# 3. Règles de validation des données (schémas)
# ============================================================

def test_produit_valeurs_par_defaut():
    p = ProduitCreate(sku="A1", designation="Clavier")
    assert (p.quantite_disponible, p.seuil_alerte, p.entrepot) == (0, 5, "Principal")


@pytest.mark.parametrize("donnees", [
    {"sku": "", "designation": "Clavier"},                           # SKU vide
    {"sku": "A1", "designation": ""},                                # désignation vide
    {"sku": "A" * 51, "designation": "Clavier"},                     # SKU trop long
    {"sku": "A1", "designation": "Clavier", "seuil_alerte": -1},     # seuil négatif
    {"sku": "A1", "designation": "Clavier", "quantite_disponible": -5},
    {"sku": "A1", "designation": "Clavier", "seuil_alerte": None},   # null refusé
    {"sku": "A1", "designation": "Clavier", "seuil_alerte": "abc"},
])
def test_produit_donnees_invalides(donnees):
    with pytest.raises(ValidationError):
        ProduitCreate(**donnees)


def test_modification_tous_champs_facultatifs():
    assert ProduitUpdate().model_dump(exclude_unset=True) == {}
    assert ProduitUpdate(seuil_alerte=8).model_dump(exclude_unset=True) == {"seuil_alerte": 8}
    with pytest.raises(ValidationError):
        ProduitUpdate(seuil_alerte=-2)


def test_mouvement_type_valide():
    for type_mouvement in ("entree", "sortie", "ajustement"):
        assert MouvementCreate(sku="A1", type_mouvement=type_mouvement, quantite=1)
    with pytest.raises(ValidationError):
        MouvementCreate(sku="A1", type_mouvement="vol", quantite=1)


def _commande(**changements):
    donnees = {
        "numero_commande": "CMD-1", "date_commande": "2027-01-10T10:00:00Z",
        "client": {"nom": "Dupont", "email": "d@x.fr"},
        "lignes": [{"sku": "A1", "quantite": 4, "prix_unitaire_ht": 4.5}],
        "montant_total_ht": 18.0, "statut": "confirmee",
    }
    donnees.update(changements)
    return donnees


def test_commande_valide():
    commande = CommandeCreate(**_commande())
    assert commande.numero_commande == "CMD-1" and commande.lignes[0].quantite == 4


def test_commande_champ_inconnu_ignore():
    # Règle du contrat inter-agents : un champ inconnu ne doit jamais provoquer d'erreur.
    commande = CommandeCreate(**_commande(champ_futur="x"))
    assert not hasattr(commande, "champ_futur")


@pytest.mark.parametrize("statut", ["en_attente", "confirmee", "expediee", "facturee", "annulee"])
def test_commande_statuts_du_contrat(statut):
    assert CommandeCreate(**_commande(statut=statut)).statut == statut


def test_commande_statut_inconnu_refuse():
    with pytest.raises(ValidationError):
        CommandeCreate(**_commande(statut="perdue"))


def test_ligne_de_commande_quantite_strictement_positive():
    with pytest.raises(ValidationError):
        LigneCommande(sku="A1", quantite=0, prix_unitaire_ht=1)


# ============================================================
# 4. Enveloppe de réponse, clé d'accès et chiffrement
# ============================================================

def test_enveloppe_de_reponse_standard():
    assert main.reponse_ok({"a": 1}) == {"status": "ok", "data": {"a": 1}, "error": None}
    assert main.reponse_ok() == {"status": "ok", "data": None, "error": None}


def test_chiffrement_de_la_cle_aller_retour():
    cle = "tak_exemple_de_cle_secrete"
    chiffree = main.chiffrer_cle_api(cle)
    assert chiffree != cle and cle not in chiffree
    assert main.dechiffrer_cle_api(chiffree) == cle


def test_dechiffrement_d_une_valeur_invalide():
    with pytest.raises(HTTPException) as erreur:
        main.dechiffrer_cle_api("valeur-qui-n-est-pas-chiffree")
    assert erreur.value.status_code == 500


def test_extraction_de_la_cle_bearer():
    ok = HTTPAuthorizationCredentials(scheme="Bearer", credentials="  tak_abc  ")
    assert main.extraire_cle_bearer(ok) == "tak_abc"


@pytest.mark.parametrize("credentials", [
    None,
    HTTPAuthorizationCredentials(scheme="Basic", credentials="tak_abc"),
    HTTPAuthorizationCredentials(scheme="Bearer", credentials="   "),
])
def test_extraction_de_la_cle_refusee(credentials):
    with pytest.raises(HTTPException) as erreur:
        main.extraire_cle_bearer(credentials)
    assert erreur.value.status_code == 401