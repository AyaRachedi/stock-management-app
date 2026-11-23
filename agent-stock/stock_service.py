
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from models import Produit


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
