import csv
import io
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from models import Produit, MouvementStock


# Déterminer le niveau du stock
def determiner_niveau_stock(quantite_disponible, seuil_alerte):
    if quantite_disponible == 0:
        return "rupture"
    elif quantite_disponible <= seuil_alerte:
        return "bas"
    else:
        return "ok"


# Préparer une alerte
def preparer_alerte(produit):
    niveau = determiner_niveau_stock(
        produit.quantite_disponible,
        produit.seuil_alerte
    )

    if niveau == "rupture":
        return {
            "event_type": "stock_rupture",
            "severity": "critical",
            "data": {
                "sku": produit.sku,
                "produit": produit.designation,
                "quantite_disponible": produit.quantite_disponible,
                "seuil_alerte": produit.seuil_alerte
            }
        }

    elif niveau == "bas":
        return {
            "event_type": "stock_faible",
            "severity": "medium",
            "data": {
                "sku": produit.sku,
                "produit": produit.designation,
                "quantite_disponible": produit.quantite_disponible,
                "seuil_alerte": produit.seuil_alerte
            }
        }

    return None


# CREATE : ajouter un produit
def ajouter_produit(db: Session, tenant_id: str, produit_data):
    produit_existant = obtenir_produit(
        db, tenant_id, produit_data.sku
    )

    if produit_existant:
        raise ValueError("Ce produit existe déjà.")

    nouveau_produit = Produit(
        tenant_id=tenant_id,
        sku=produit_data.sku,
        designation=produit_data.designation,
        quantite_disponible=produit_data.quantite_disponible,
        seuil_alerte=produit_data.seuil_alerte,
        entrepot=produit_data.entrepot,
        alerte_niveau=determiner_niveau_stock(
            produit_data.quantite_disponible,
            produit_data.seuil_alerte
        )
    )

    try:
        db.add(nouveau_produit)
        db.commit()
        db.refresh(nouveau_produit)
        return nouveau_produit
    except Exception:
        db.rollback()
        raise


# READ : lister les produits d'une entreprise
def lister_produits(db: Session, tenant_id: str):
    return db.query(Produit).filter(
        Produit.tenant_id == tenant_id
    ).order_by(Produit.sku).all()


# READ : consulter un produit par son SKU
def obtenir_produit(db: Session, tenant_id: str, sku: str):
    return db.query(Produit).filter(
        Produit.tenant_id == tenant_id,
        Produit.sku == sku
    ).first()


# UPDATE : modifier un produit
def modifier_produit(
    db: Session,
    tenant_id: str,
    sku: str,
    modifications
):
    produit = obtenir_produit(db, tenant_id, sku)

    if produit is None:
        raise ValueError("Produit introuvable.")

    donnees = modifications.model_dump(exclude_unset=True)

    for champ, valeur in donnees.items():
        if valeur is not None:
            setattr(produit, champ, valeur)

    produit.alerte_niveau = determiner_niveau_stock(
        produit.quantite_disponible,
        produit.seuil_alerte
    )

    produit.updated_at = datetime.now(timezone.utc)

    try:
        db.commit()
        db.refresh(produit)
        return produit
    except Exception:
        db.rollback()
        raise


# DELETE : supprimer un produit
def supprimer_produit(db: Session, tenant_id: str, sku: str):
    produit = obtenir_produit(db, tenant_id, sku)

    if produit is None:
        raise ValueError("Produit introuvable.")

    try:
        db.delete(produit)
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise


# IMPORT CSV (inventaire)
COLONNES_OBLIGATOIRES = ["sku", "designation", "quantite_disponible"]
COLONNES_FACULTATIVES = ["seuil_alerte", "entrepot"]
TAILLE_MAX_OCTETS = 1_000_000
LIGNES_MAX = 5000 


# Lire le texte du fichier (UTF-8, ou encodage Windows d'Excel)
def _decoder_fichier(contenu):
    try:
        return contenu.decode("utf-8-sig")
    except UnicodeDecodeError:
        return contenu.decode("cp1252")

# Lire un nombre entier positif dans une case du fichier
def _lire_entier(valeur: str, nom: str, erreurs: list, num_ligne: int):
    try:
        nombre = int(valeur)
    except ValueError:
        erreurs.append({"ligne": num_ligne, "erreur": f"{nom} doit être un nombre entier (reçu : '{valeur}')"})
        return None
    if nombre < 0:
        erreurs.append({"ligne": num_ligne, "erreur": f"{nom} ne peut pas être négatif"})
        return None
    return nombre


# Lire et vérifier le fichier CSV, sans rien écrire en base
def lire_csv_stock(contenu: bytes):
    if not contenu:
        raise ValueError("Le fichier est vide.")
    if len(contenu) > TAILLE_MAX_OCTETS:
        raise ValueError("Fichier trop volumineux (1 Mo maximum).")

    texte = _decoder_fichier(contenu)

    # Séparateur "," ou ";" (Excel en français utilise ";")
    premiere_ligne = texte.splitlines()[0] if texte.strip() else ""
    separateur = ";" if premiere_ligne.count(";") > premiere_ligne.count(",") else ","

    lecteur = csv.DictReader(io.StringIO(texte), delimiter=separateur)
    lecteur.fieldnames = [
    col.strip().lstrip("\ufeff").lstrip("\ufffd").strip()
    for col in (lecteur.fieldnames or [])
]
    entetes = [
    col.strip().lstrip("\ufeff").strip()
    for col in (lecteur.fieldnames or [])
]

    lecteur.fieldnames = entetes
    
    colonnes = [c.strip().lower() for c in (lecteur.fieldnames or [])]

    manquantes = [c for c in COLONNES_OBLIGATOIRES if c not in colonnes]
    if manquantes:
        raise ValueError(
            "Colonnes obligatoires manquantes : " + ", ".join(manquantes)
            + ". En-tête attendu : sku,designation,quantite_disponible,seuil_alerte,entrepot"
        )
    inconnues = [c for c in colonnes if c not in COLONNES_OBLIGATOIRES + COLONNES_FACULTATIVES]
    if inconnues:
        raise ValueError("Colonnes inconnues : " + ", ".join(inconnues))

    lignes, erreurs, skus_vus = [], [], {}

    # La ligne 1 est l'en-tête : les données commencent à la ligne 2
    for num_ligne, brute in enumerate(lecteur, start=2):
        ligne = {
            (cle or "").strip().lower(): (valeur or "").strip()
            for cle, valeur in brute.items()
            if cle is not None
        }
        if not any(ligne.values()):
            continue

        if len(lignes) + len(erreurs) >= LIGNES_MAX:
            raise ValueError(f"Trop de lignes ({LIGNES_MAX} maximum).")

        nb_erreurs_avant = len(erreurs)
        sku = ligne.get("sku", "")
        designation = ligne.get("designation", "")

        if not sku:
            erreurs.append({"ligne": num_ligne, "erreur": "sku manquant"})
        elif len(sku) > 50:
            erreurs.append({"ligne": num_ligne, "erreur": "sku trop long (50 caractères maximum)"})
        elif sku in skus_vus:
            erreurs.append({"ligne": num_ligne, "erreur": f"sku {sku} déjà présent ligne {skus_vus[sku]}"})
        else:
            skus_vus[sku] = num_ligne

        if not designation:
            erreurs.append({"ligne": num_ligne, "erreur": "designation manquante"})
        elif len(designation) > 255:
            erreurs.append({"ligne": num_ligne, "erreur": "designation trop longue (255 caractères maximum)"})

        quantite = _lire_entier(ligne.get("quantite_disponible", ""), "quantite_disponible", erreurs, num_ligne)

        seuil = None
        if ligne.get("seuil_alerte"):
            seuil = _lire_entier(ligne["seuil_alerte"], "seuil_alerte", erreurs, num_ligne)

        entrepot = ligne.get("entrepot") or None
        if entrepot and len(entrepot) > 100:
            erreurs.append({"ligne": num_ligne, "erreur": "entrepot trop long (100 caractères maximum)"})

        if len(erreurs) == nb_erreurs_avant:
            lignes.append({
                "sku": sku,
                "designation": designation,
                "quantite_disponible": quantite,
                "seuil_alerte": seuil,
                "entrepot": entrepot,
            })

    if not lignes and not erreurs:
        raise ValueError("Le fichier ne contient aucune ligne de produit.")

    return lignes, erreurs


# Enregistrer les lignes vérifiées, en une seule transaction
def importer_stock_csv(db: Session, tenant_id: str, lignes: list):
    resume = {"crees": 0, "mis_a_jour": 0, "inchanges": 0}
    changements = []
    maintenant = datetime.now(timezone.utc)

    try:
        for ligne in lignes:
            produit = obtenir_produit(db, tenant_id, ligne["sku"])
            mouvement = None

            if produit is None:
                # Produit nouveau : on le crée
                produit = Produit(
                    tenant_id=tenant_id,
                    sku=ligne["sku"],
                    designation=ligne["designation"],
                    quantite_disponible=ligne["quantite_disponible"],
                    seuil_alerte=ligne["seuil_alerte"] if ligne["seuil_alerte"] is not None else 5,
                    entrepot=ligne["entrepot"] or "Principal",
                )
                db.add(produit)
                # Écrire le produit avant son mouvement (clé étrangère)
                db.flush()
                if ligne["quantite_disponible"] > 0:
                    mouvement = MouvementStock(
                        tenant_id=tenant_id,
                        sku=ligne["sku"],
                        type_mouvement="entree",
                        quantite=ligne["quantite_disponible"],
                        reference_commande="IMPORT-CSV",
                    )
                resume["crees"] += 1

            else:
                # Produit existant : on remplace la quantité
                ecart = ligne["quantite_disponible"] - produit.quantite_disponible
                modifie = ecart != 0

                if produit.designation != ligne["designation"]:
                    produit.designation = ligne["designation"]
                    modifie = True
                if ligne["seuil_alerte"] is not None and produit.seuil_alerte != ligne["seuil_alerte"]:
                    produit.seuil_alerte = ligne["seuil_alerte"]
                    modifie = True
                if ligne["entrepot"] and produit.entrepot != ligne["entrepot"]:
                    produit.entrepot = ligne["entrepot"]
                    modifie = True

                if not modifie:
                    resume["inchanges"] += 1
                    continue

                if ecart != 0:
                    produit.quantite_disponible = ligne["quantite_disponible"]
                    mouvement = MouvementStock(
                        tenant_id=tenant_id,
                        sku=ligne["sku"],
                        type_mouvement="ajustement",
                        quantite=ecart,
                        reference_commande="IMPORT-CSV",
                    )
                produit.updated_at = maintenant
                resume["mis_a_jour"] += 1

            produit.alerte_niveau = determiner_niveau_stock(
                produit.quantite_disponible,
                produit.seuil_alerte
            )
            if mouvement is not None:
                db.add(mouvement)
            changements.append((produit, mouvement))

        db.commit()

    except Exception:
        db.rollback()
        raise

    for produit, mouvement in changements:
        db.refresh(produit)
        if mouvement is not None:
            db.refresh(mouvement)

    return resume, changements