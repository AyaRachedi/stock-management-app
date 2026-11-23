
import os
import time
import httpx

from datetime import datetime, timezone
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from fastapi import FastAPI, Header, HTTPException, Depends
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from database import get_db
from models import Tenant, Produit, MouvementStock, CommandeTraitee

from schemas import (
    ProduitCreate,
    ProduitUpdate,
    ProduitResponse,
    MouvementCreate,
    MouvementResponse,
    AlerteResponse,
    StockResponse,
    CommandeCreate
)

from stock_service import (
    determiner_niveau_stock,
    preparer_alerte,
    ajouter_produit,
    lister_produits,
    obtenir_produit,
    modifier_produit,
    supprimer_produit
)


# ============================================================
# INITIALISATION
# ============================================================

app = FastAPI(title="Agent de gestion de stock")

demarre_le = time.time()

NOYAU_URL = os.getenv("NOYAU_URL", "http://localhost:8000")


# ============================================================
# OUTILS DE SECURITE ET DE COMMUNICATION AVEC LE NOYAU
# ============================================================

def obtenir_fernet():
    """
    Récupère la clé de chiffrement depuis le fichier .env.
    """
    cle = os.getenv("FERNET_KEY")

    if not cle:
        raise HTTPException(
            status_code=500,
            detail="FERNET_KEY absente du fichier .env"
        )

    try:
        return Fernet(cle.encode())
    except Exception:
        raise HTTPException(
            status_code=500,
            detail="FERNET_KEY invalide"
        )


def chiffrer_cle_api(cle_api: str) -> str:
    return obtenir_fernet().encrypt(
        cle_api.encode()
    ).decode()


def dechiffrer_cle_api(cle_chiffree: str) -> str:
    try:
        return obtenir_fernet().decrypt(
            cle_chiffree.encode()
        ).decode()
    except InvalidToken:
        raise HTTPException(
            status_code=500,
            detail="Impossible de déchiffrer la clé API du tenant"
        )


def obtenir_tenant(
    db: Session,
    tenant_id: str
):
    tenant = db.query(Tenant).filter(
        Tenant.tenant_id == tenant_id
    ).first()

    if tenant is None or tenant.state != "running":
        raise HTTPException(
            status_code=404,
            detail="Tenant non actif"
        )

    return tenant


async def envoyer_alerte(
    db: Session,
    tenant: Tenant,
    alerte: dict
):
    """
    Envoie une alerte de stock au webhook du noyau.
    """
    if not tenant.tenant_api_key_chiffree:
        return

    cle_api = dechiffrer_cle_api(
        tenant.tenant_api_key_chiffree
    )

    async with httpx.AsyncClient(timeout=5) as client:
        try:
            reponse = await client.post(
                f"{NOYAU_URL}/webhooks/agent-events",
                headers={
                    "Authorization": f"Bearer {cle_api}",
                    "X-Tenant-Id": tenant.tenant_id
                },
                json=alerte
            )
            reponse.raise_for_status()

        except httpx.HTTPError:
            # L'alerte reste consultable dans l'agent.
            # Une erreur du noyau ne doit pas annuler le mouvement.
            pass


async def publier_sur_bus(
    tenant: Tenant,
    type_objet: str,
    external_id: str,
    payload: dict
):
    """
    Publie un objet sur le bus d'échange du noyau.
    """
    if not tenant.tenant_api_key_chiffree:
        return

    cle_api = dechiffrer_cle_api(
        tenant.tenant_api_key_chiffree
    )

    async with httpx.AsyncClient(timeout=5) as client:
        try:
            await client.post(
                f"{NOYAU_URL}/exchange/agent/{type_objet}",
                headers={
                    "Authorization": f"Bearer {cle_api}",
                    "X-Tenant-Id": tenant.tenant_id
                },
                json={
                    "external_id": external_id,
                    "payload": payload
                }
            )
        except httpx.HTTPError:
            # Le noyau pourra être indisponible temporairement.
            pass


# ============================================================
# 1. ROUTES OBLIGATOIRES DU NOYAU
# ============================================================

@app.get("/health")
def health():
    return {
        "status": "ok",
        "data": {
            "uptime_seconds": int(time.time() - demarre_le)
        },
        "error": None
    }


@app.post("/tenant/activate")
def activate(
    body: dict,
    authorization: Optional[str] = Header(None),
    x_tenant_id: Optional[str] = Header(
        None,
        alias="X-Tenant-Id"
    ),
    db: Session = Depends(get_db)
):
    if not x_tenant_id:
        raise HTTPException(
            status_code=400,
            detail="X-Tenant-Id manquant"
        )

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Clé API Bearer manquante"
        )

    cle_api = authorization.replace("Bearer ", "", 1)

    config = body.get("config", {})

    tenant = db.query(Tenant).filter(
        Tenant.tenant_id == x_tenant_id
    ).first()

    try:
        if tenant is None:
            tenant = Tenant(
                tenant_id=x_tenant_id,
                state="running",
                config=config,
                tenant_api_key_chiffree=chiffrer_cle_api(cle_api)
            )
            db.add(tenant)
        else:
            tenant.state = "running"
            tenant.config = config
            tenant.tenant_api_key_chiffree = chiffrer_cle_api(
                cle_api
            )

        db.commit()

    except Exception:
        db.rollback()
        raise

    return {
        "status": "ok",
        "data": {"activated": True},
        "error": None
    }


@app.post("/tenant/deactivate")
def deactivate(
    x_tenant_id: Optional[str] = Header(
        None,
        alias="X-Tenant-Id"
    ),
    db: Session = Depends(get_db)
):
    if not x_tenant_id:
        raise HTTPException(
            status_code=400,
            detail="X-Tenant-Id manquant"
        )

    tenant = db.query(Tenant).filter(
        Tenant.tenant_id == x_tenant_id
    ).first()

    if tenant is not None:
        try:
            # Les produits, mouvements et commandes traitées
            # sont supprimés grâce aux relations ON DELETE CASCADE.
            db.delete(tenant)
            db.commit()
        except Exception:
            db.rollback()
            raise

    return {
        "status": "ok",
        "data": {
            "deactivated": True,
            "data_purged": True
        },
        "error": None
    }


@app.get("/tenant/{tenant_id}/status")
def tenant_status(
    tenant_id: str,
    db: Session = Depends(get_db)
):
    tenant = db.query(Tenant).filter(
        Tenant.tenant_id == tenant_id
    ).first()

    if tenant is None or tenant.state != "running":
        raise HTTPException(
            status_code=404,
            detail="Tenant non actif"
        )

    return {
        "status": "ok",
        "data": {
            "state": tenant.state
        },
        "error": None
    }


@app.get("/config-schema")
def config_schema():
    return {
        "status": "ok",
        "data": {
            "type": "object",
            "properties": {
                "seuil_alerte": {
                    "type": "integer",
                    "description": "Seuil de stock déclenchant une alerte",
                    "default": 5
                }
            }
        },
        "error": None
    }


# ============================================================
# 2. CRUD DES PRODUITS
# ============================================================

@app.post("/produits", response_model=ProduitResponse)
def creer_produit(
    produit_data: ProduitCreate,
    x_tenant_id: str = Header(..., alias="X-Tenant-Id"),
    db: Session = Depends(get_db)
):
    obtenir_tenant(db, x_tenant_id)

    try:
        return ajouter_produit(
            db,
            x_tenant_id,
            produit_data
        )
    except ValueError as erreur:
        raise HTTPException(
            status_code=409,
            detail=str(erreur)
        )


@app.get("/produits", response_model=list[ProduitResponse])
def lire_produits(
    x_tenant_id: str = Header(..., alias="X-Tenant-Id"),
    db: Session = Depends(get_db)
):
    obtenir_tenant(db, x_tenant_id)

    return lister_produits(db, x_tenant_id)


@app.get("/produits/{sku}/stock", response_model=StockResponse)
def consulter_stock(
    sku: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-Id"),
    db: Session = Depends(get_db)
):
    obtenir_tenant(db, x_tenant_id)

    produit = obtenir_produit(db, x_tenant_id, sku)

    if produit is None:
        raise HTTPException(
            status_code=404,
            detail="Produit introuvable"
        )

    return produit


@app.get("/produits/{sku}", response_model=ProduitResponse)
def lire_produit(
    sku: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-Id"),
    db: Session = Depends(get_db)
):
    obtenir_tenant(db, x_tenant_id)

    produit = obtenir_produit(db, x_tenant_id, sku)

    if produit is None:
        raise HTTPException(
            status_code=404,
            detail="Produit introuvable"
        )

    return produit


@app.put("/produits/{sku}", response_model=ProduitResponse)
def mettre_a_jour_produit(
    sku: str,
    modifications: ProduitUpdate,
    x_tenant_id: str = Header(..., alias="X-Tenant-Id"),
    db: Session = Depends(get_db)
):
    obtenir_tenant(db, x_tenant_id)

    try:
        return modifier_produit(
            db,
            x_tenant_id,
            sku,
            modifications
        )
    except ValueError as erreur:
        raise HTTPException(
            status_code=404,
            detail=str(erreur)
        )


@app.delete("/produits/{sku}")
def effacer_produit(
    sku: str,
    x_tenant_id: str = Header(..., alias="X-Tenant-Id"),
    db: Session = Depends(get_db)
):
    obtenir_tenant(db, x_tenant_id)

    try:
        supprimer_produit(db, x_tenant_id, sku)
        return {
            "status": "ok",
            "message": "Produit supprimé"
        }
    except ValueError as erreur:
        raise HTTPException(
            status_code=404,
            detail=str(erreur)
        )


# ============================================================
# 3. GESTION DES MOUVEMENTS DE STOCK
# ============================================================

@app.post("/mouvements", response_model=MouvementResponse)
async def creer_mouvement(
    mouvement_data: MouvementCreate,
    x_tenant_id: str = Header(..., alias="X-Tenant-Id"),
    db: Session = Depends(get_db)
):
    tenant = obtenir_tenant(db, x_tenant_id)

    produit = obtenir_produit(
        db,
        x_tenant_id,
        mouvement_data.sku
    )

    if produit is None:
        raise HTTPException(
            status_code=404,
            detail="Produit introuvable"
        )

    quantite = mouvement_data.quantite

    if mouvement_data.type_mouvement in ("entree", "sortie") and quantite <= 0:
        raise HTTPException(
            status_code=400,
            detail="La quantité doit être positive pour une entrée ou une sortie"
        )

    if mouvement_data.type_mouvement == "ajustement" and quantite == 0:
        raise HTTPException(
            status_code=400,
            detail="La quantité d'ajustement ne peut pas être nulle"
        )

    if mouvement_data.type_mouvement == "entree":
        nouvelle_quantite = produit.quantite_disponible + quantite

    elif mouvement_data.type_mouvement == "sortie":
        nouvelle_quantite = produit.quantite_disponible - quantite

    else:
        nouvelle_quantite = produit.quantite_disponible + quantite

    if nouvelle_quantite < 0:
        raise HTTPException(
            status_code=400,
            detail="Stock insuffisant pour effectuer cette opération"
        )

    mouvement = MouvementStock(
        tenant_id=x_tenant_id,
        sku=mouvement_data.sku,
        type_mouvement=mouvement_data.type_mouvement,
        quantite=quantite,
        reference_commande=mouvement_data.reference_commande
    )

    try:
        produit.quantite_disponible = nouvelle_quantite
        produit.alerte_niveau = determiner_niveau_stock(
            nouvelle_quantite,
            produit.seuil_alerte
        )
        produit.updated_at = datetime.now(timezone.utc)

        db.add(mouvement)
        db.commit()
        db.refresh(mouvement)
        db.refresh(produit)

    except Exception:
        db.rollback()
        raise

    # Publier le mouvement sur le bus du noyau.
    await publier_sur_bus(
        tenant,
        "mouvement_stock",
        str(mouvement.id_mouvement),
        {
            "id_mouvement": str(mouvement.id_mouvement),
            "sku": produit.sku,
            "type_mouvement": mouvement.type_mouvement,
            "quantite": mouvement.quantite,
            "reference_commande": mouvement.reference_commande
        }
    )

    # Publier le niveau de stock actualisé.
    await publier_sur_bus(
        tenant,
        "stock_level",
        produit.sku,
        {
            "sku": produit.sku,
            "designation": produit.designation,
            "quantite_disponible": produit.quantite_disponible,
            "seuil_alerte": produit.seuil_alerte,
            "entrepot": produit.entrepot
        }
    )

    # Envoyer une alerte si nécessaire.
    alerte = preparer_alerte(produit)

    if alerte:
        await envoyer_alerte(db, tenant, alerte)

    return mouvement


@app.get("/mouvements", response_model=list[MouvementResponse])
def lire_mouvements(
    sku: Optional[str] = None,
    x_tenant_id: str = Header(..., alias="X-Tenant-Id"),
    db: Session = Depends(get_db)
):
    obtenir_tenant(db, x_tenant_id)

    requete = db.query(MouvementStock).filter(
        MouvementStock.tenant_id == x_tenant_id
    )

    if sku:
        requete = requete.filter(
            MouvementStock.sku == sku
        )

    return requete.order_by(
        MouvementStock.date_mouvement.desc()
    ).all()


# ============================================================
# 4. CONSULTATION DES ALERTES
# ============================================================

@app.get("/alertes", response_model=list[AlerteResponse])
def lire_alertes(
    x_tenant_id: str = Header(..., alias="X-Tenant-Id"),
    db: Session = Depends(get_db)
):
    obtenir_tenant(db, x_tenant_id)

    produits = db.query(Produit).filter(
        Produit.tenant_id == x_tenant_id,
        Produit.alerte_niveau.in_(["bas", "rupture"])
    ).all()

    alertes = []

    for produit in produits:
        alerte = preparer_alerte(produit)

        if alerte:
            alertes.append(alerte)

    return alertes


# ============================================================
# 5. RECEPTION DES COMMANDES DE L'AGENT COMMERCIAL
# ============================================================

@app.post("/exchange/notify")
async def exchange_notify(
    body: dict,
    x_tenant_id: str = Header(..., alias="X-Tenant-Id"),
    db: Session = Depends(get_db)
):
    tenant = obtenir_tenant(db, x_tenant_id)

    if body.get("object_type") != "commande":
        return {
            "status": "ok",
            "data": {
                "processed": False,
                "reason": "Type d'objet ignoré"
            },
            "error": None
        }

    try:
        commande = CommandeCreate.model_validate(
            body.get("payload", {})
        )
    except Exception as erreur:
        raise HTTPException(
            status_code=422,
            detail=f"Commande invalide : {erreur}"
        )

    deja_traitee = db.query(CommandeTraitee).filter(
        CommandeTraitee.tenant_id == x_tenant_id,
        CommandeTraitee.numero_commande == commande.numero_commande
    ).first()

    if deja_traitee:
        return {
            "status": "ok",
            "data": {
                "processed": True,
                "duplicate": True
            },
            "error": None
        }

    try:
        # Vérifier toutes les lignes avant de modifier les stocks.
        produits_commande = []

        for ligne in commande.lignes:
            produit = obtenir_produit(
                db,
                x_tenant_id,
                ligne.sku
            )

            if produit is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"Produit {ligne.sku} introuvable"
                )

            if produit.quantite_disponible < ligne.quantite:
                raise HTTPException(
                    status_code=400,
                    detail=f"Stock insuffisant pour {ligne.sku}"
                )

            produits_commande.append((produit, ligne))

        # Enregistrer les sorties de stock.
        for produit, ligne in produits_commande:
            produit.quantite_disponible -= ligne.quantite

            produit.alerte_niveau = determiner_niveau_stock(
                produit.quantite_disponible,
                produit.seuil_alerte
            )

            produit.updated_at = datetime.now(timezone.utc)

            mouvement = MouvementStock(
                tenant_id=x_tenant_id,
                sku=produit.sku,
                type_mouvement="sortie",
                quantite=ligne.quantite,
                reference_commande=commande.numero_commande
            )

            db.add(mouvement)

        # Marquer la commande comme traitée.
        db.add(
            CommandeTraitee(
                tenant_id=x_tenant_id,
                numero_commande=commande.numero_commande
            )
        )

        db.commit()

    except HTTPException:
        db.rollback()
        raise

    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Cette commande a déjà été traitée"
        )

    except Exception:
        db.rollback()
        raise

    # Envoyer les alertes et publier les niveaux actualisés.
    for produit, ligne in produits_commande:
        alerte = preparer_alerte(produit)

        if alerte:
            await envoyer_alerte(db, tenant, alerte)

        await publier_sur_bus(
            tenant,
            "stock_level",
            produit.sku,
            {
                "sku": produit.sku,
                "designation": produit.designation,
                "quantite_disponible": produit.quantite_disponible,
                "seuil_alerte": produit.seuil_alerte,
                "entrepot": produit.entrepot
            }
        )

    return {
        "status": "ok",
        "data": {
            "processed": True,
            "numero_commande": commande.numero_commande
        },
        "error": None
    }
