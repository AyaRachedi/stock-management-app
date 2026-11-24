
import os
import time
import hmac
import httpx

from datetime import datetime, timezone
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Depends,
    Request
)
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from starlette.exceptions import HTTPException as StarletteHTTPException

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
# 1. INITIALISATION
# ============================================================

app = FastAPI(title="Agent de gestion de stock")

demarre_le = time.time()

NOYAU_URL = os.getenv(
    "NOYAU_URL",
    "http://localhost:8000"
).rstrip("/")

# Mécanisme d'authentification Bearer pour FastAPI / Swagger.
# Le bouton Authorize sera disponible dans Swagger.
bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="BearerAuth"
)


# ============================================================
# 2. FORMAT STANDARD DES REPONSES HTTP
# ============================================================

def reponse_ok(data=None):
    return {
        "status": "ok",
        "data": jsonable_encoder(data),
        "error": None
    }


@app.exception_handler(StarletteHTTPException)
async def gestion_erreur_http(
    request: Request,
    exc: StarletteHTTPException
):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "status": "error",
            "data": None,
            "error": str(exc.detail)
        }
    )


@app.exception_handler(RequestValidationError)
async def gestion_erreur_validation(
    request: Request,
    exc: RequestValidationError
):
    return JSONResponse(
        status_code=422,
        content={
            "status": "error",
            "data": None,
            "error": "Données invalides"
        }
    )


@app.exception_handler(Exception)
async def gestion_erreur_interne(
    request: Request,
    exc: Exception
):
    return JSONResponse(
        status_code=500,
        content={
            "status": "error",
            "data": None,
            "error": "Erreur interne du serveur"
        }
    )


# ============================================================
# 3. SECURITE ET GESTION DES TENANTS
# ============================================================

def extraire_cle_bearer(
    credentials: Optional[HTTPAuthorizationCredentials]
) -> str:
    """
    Récupère et vérifie la présence de la clé Bearer.
    """

    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Clé API Bearer manquante"
        )

    if credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=401,
            detail="Authentification Bearer obligatoire"
        )

    cle_api = credentials.credentials.strip()

    if not cle_api:
        raise HTTPException(
            status_code=401,
            detail="Clé API Bearer vide"
        )

    return cle_api


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


def verifier_cle_api(
    tenant: Tenant,
    credentials: Optional[HTTPAuthorizationCredentials]
):
    """
    Compare la clé Bearer reçue avec la clé enregistrée
    pour le tenant.
    """

    cle_api_recue = extraire_cle_bearer(credentials)

    if not tenant.tenant_api_key_chiffree:
        raise HTTPException(
            status_code=401,
            detail="Aucune clé API enregistrée pour ce tenant"
        )

    cle_api_stockee = dechiffrer_cle_api(
        tenant.tenant_api_key_chiffree
    )

    if not hmac.compare_digest(
        cle_api_recue,
        cle_api_stockee
    ):
        raise HTTPException(
            status_code=401,
            detail="Clé API invalide"
        )


def obtenir_tenant_authentifie(
    db: Session,
    tenant_id: str,
    credentials: Optional[HTTPAuthorizationCredentials]
):
    """
    Vérifie que le tenant existe, est actif et possède
    une clé API valide.
    """

    tenant = obtenir_tenant(
        db,
        tenant_id
    )

    verifier_cle_api(
        tenant,
        credentials
    )

    return tenant


# ============================================================
# 4. COMMUNICATION AVEC LE NOYAU
# ============================================================

async def envoyer_alerte(
    tenant: Tenant,
    alerte: dict
):
    """
    Envoie une alerte de stock au webhook du noyau.
    Une indisponibilité du webhook ne bloque pas le mouvement.
    """

    if not tenant.tenant_api_key_chiffree:
        return

    cle_api = dechiffrer_cle_api(
        tenant.tenant_api_key_chiffree
    )

    try:
        async with httpx.AsyncClient(timeout=5) as client:

            response = await client.post(
                f"{NOYAU_URL}/webhooks/agent-events",
                headers={
                    "Authorization": f"Bearer {cle_api}",
                    "X-Tenant-Id": tenant.tenant_id
                },
                json=alerte
            )

            response.raise_for_status()

    except httpx.HTTPError:
        # L'alerte reste consultable dans l'agent.
        # Une erreur du webhook ne bloque pas le mouvement.
        pass


async def publier_sur_bus(
    tenant: Tenant,
    type_objet: str,
    external_id: str,
    payload: dict
):
    """
    Publie un objet métier sur le bus d'échange du noyau.
    """

    if not tenant.tenant_api_key_chiffree:
        raise HTTPException(
            status_code=401,
            detail="Aucune clé API enregistrée pour ce tenant"
        )

    cle_api = dechiffrer_cle_api(
        tenant.tenant_api_key_chiffree
    )

    try:
        async with httpx.AsyncClient(timeout=5) as client:

            response = await client.post(
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

            response.raise_for_status()

    except httpx.TimeoutException:
        raise HTTPException(
            status_code=504,
            detail="Le noyau n'a pas répondu dans le délai prévu"
        )

    except httpx.HTTPStatusError as erreur:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Le noyau a refusé la publication de "
                f"{type_objet} : HTTP {erreur.response.status_code}"
            )
        )

    except httpx.RequestError:
        raise HTTPException(
            status_code=502,
            detail="Impossible de contacter le noyau"
        )


# ============================================================
# 5. ROUTES OBLIGATOIRES DU NOYAU
# ============================================================

@app.get("/health")
def health():
    return reponse_ok({
        "uptime_seconds": int(time.time() - demarre_le)
    })


@app.post("/tenant/activate")
def activate(
    body: dict,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(
        bearer_scheme
    ),
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

    # La clé fournie par le noyau est enregistrée
    # pour les futurs échanges avec ce tenant.
    cle_api = extraire_cle_bearer(credentials)

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

    return reponse_ok({
        "activated": True
    })


@app.post("/tenant/deactivate")
def deactivate(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(
        bearer_scheme
    ),
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

    # Exiger une clé même si le tenant n'existe déjà plus.
    if tenant is None:
        extraire_cle_bearer(credentials)

    else:
        verifier_cle_api(
            tenant,
            credentials
        )

        try:
            # Les données associées doivent être supprimées
            # grâce aux contraintes de suppression configurées
            # dans les modèles et dans PostgreSQL.
            db.delete(tenant)
            db.commit()

        except Exception:
            db.rollback()
            raise

    return reponse_ok({
        "deactivated": True,
        "data_purged": True
    })


@app.get("/tenant/{tenant_id}/status")
def tenant_status(
    tenant_id: str,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(
        bearer_scheme
    ),
    x_tenant_id: str = Header(
        ...,
        alias="X-Tenant-Id"
    ),
    db: Session = Depends(get_db)
):
    if tenant_id != x_tenant_id:
        raise HTTPException(
            status_code=403,
            detail="Le tenant indiqué ne correspond pas au tenant authentifié"
        )

    tenant = obtenir_tenant_authentifie(
        db,
        tenant_id,
        credentials
    )

    return reponse_ok({
        "state": tenant.state
    })


@app.get("/config-schema")
def config_schema():
    return reponse_ok({
        "type": "object",
        "properties": {
            "seuil_alerte": {
                "type": "integer",
                "description": "Seuil de stock déclenchant une alerte",
                "default": 5
            }
        }
    })


# ============================================================
# 6. CRUD DES PRODUITS
# ============================================================

@app.post("/produits")
def creer_produit(
    produit_data: ProduitCreate,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(
        bearer_scheme
    ),
    x_tenant_id: str = Header(
        ...,
        alias="X-Tenant-Id"
    ),
    db: Session = Depends(get_db)
):
    obtenir_tenant_authentifie(
        db,
        x_tenant_id,
        credentials
    )

    try:
        produit = ajouter_produit(
            db,
            x_tenant_id,
            produit_data
        )

        return reponse_ok(
            ProduitResponse.model_validate(produit)
        )

    except ValueError as erreur:
        raise HTTPException(
            status_code=409,
            detail=str(erreur)
        )


@app.get("/produits")
def lire_produits(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(
        bearer_scheme
    ),
    x_tenant_id: str = Header(
        ...,
        alias="X-Tenant-Id"
    ),
    db: Session = Depends(get_db)
):
    obtenir_tenant_authentifie(
        db,
        x_tenant_id,
        credentials
    )

    produits = lister_produits(
        db,
        x_tenant_id
    )

    produits_reponse = [
        ProduitResponse.model_validate(produit)
        for produit in produits
    ]

    return reponse_ok(produits_reponse)


@app.get("/produits/{sku}/stock")
def consulter_stock(
    sku: str,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(
        bearer_scheme
    ),
    x_tenant_id: str = Header(
        ...,
        alias="X-Tenant-Id"
    ),
    db: Session = Depends(get_db)
):
    obtenir_tenant_authentifie(
        db,
        x_tenant_id,
        credentials
    )

    produit = obtenir_produit(
        db,
        x_tenant_id,
        sku
    )

    if produit is None:
        raise HTTPException(
            status_code=404,
            detail="Produit introuvable"
        )

    return reponse_ok(
        StockResponse.model_validate(produit)
    )


@app.get("/produits/{sku}")
def lire_produit(
    sku: str,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(
        bearer_scheme
    ),
    x_tenant_id: str = Header(
        ...,
        alias="X-Tenant-Id"
    ),
    db: Session = Depends(get_db)
):
    obtenir_tenant_authentifie(
        db,
        x_tenant_id,
        credentials
    )

    produit = obtenir_produit(
        db,
        x_tenant_id,
        sku
    )

    if produit is None:
        raise HTTPException(
            status_code=404,
            detail="Produit introuvable"
        )

    return reponse_ok(
        ProduitResponse.model_validate(produit)
    )


@app.put("/produits/{sku}")
def mettre_a_jour_produit(
    sku: str,
    modifications: ProduitUpdate,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(
        bearer_scheme
    ),
    x_tenant_id: str = Header(
        ...,
        alias="X-Tenant-Id"
    ),
    db: Session = Depends(get_db)
):
    obtenir_tenant_authentifie(
        db,
        x_tenant_id,
        credentials
    )

    try:
        produit = modifier_produit(
            db,
            x_tenant_id,
            sku,
            modifications
        )

        return reponse_ok(
            ProduitResponse.model_validate(produit)
        )

    except ValueError as erreur:
        raise HTTPException(
            status_code=404,
            detail=str(erreur)
        )


@app.delete("/produits/{sku}")
def effacer_produit(
    sku: str,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(
        bearer_scheme
    ),
    x_tenant_id: str = Header(
        ...,
        alias="X-Tenant-Id"
    ),
    db: Session = Depends(get_db)
):
    obtenir_tenant_authentifie(
        db,
        x_tenant_id,
        credentials
    )

    try:
        supprimer_produit(
            db,
            x_tenant_id,
            sku
        )

        return reponse_ok({
            "message": "Produit supprimé"
        })

    except ValueError as erreur:
        raise HTTPException(
            status_code=404,
            detail=str(erreur)
        )


# ============================================================
# 7. GESTION DES MOUVEMENTS DE STOCK
# ============================================================

@app.post("/mouvements")
async def creer_mouvement(
    mouvement_data: MouvementCreate,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(
        bearer_scheme
    ),
    x_tenant_id: str = Header(
        ...,
        alias="X-Tenant-Id"
    ),
    db: Session = Depends(get_db)
):
    tenant = obtenir_tenant_authentifie(
        db,
        x_tenant_id,
        credentials
    )

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

    if (
        mouvement_data.type_mouvement in ("entree", "sortie")
        and quantite <= 0
    ):
        raise HTTPException(
            status_code=400,
            detail="La quantité doit être positive pour une entrée ou une sortie"
        )

    if (
        mouvement_data.type_mouvement == "ajustement"
        and quantite == 0
    ):
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
        await envoyer_alerte(
            tenant,
            alerte
        )

    return reponse_ok(
        MouvementResponse.model_validate(mouvement)
    )


@app.get("/mouvements")
def lire_mouvements(
    sku: Optional[str] = None,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(
        bearer_scheme
    ),
    x_tenant_id: str = Header(
        ...,
        alias="X-Tenant-Id"
    ),
    db: Session = Depends(get_db)
):
    obtenir_tenant_authentifie(
        db,
        x_tenant_id,
        credentials
    )

    requete = db.query(MouvementStock).filter(
        MouvementStock.tenant_id == x_tenant_id
    )

    if sku:
        requete = requete.filter(
            MouvementStock.sku == sku
        )

    mouvements = requete.order_by(
        MouvementStock.date_mouvement.desc()
    ).all()

    mouvements_reponse = [
        MouvementResponse.model_validate(mouvement)
        for mouvement in mouvements
    ]

    return reponse_ok(mouvements_reponse)


# ============================================================
# 8. CONSULTATION DES ALERTES
# ============================================================

@app.get("/alertes")
def lire_alertes(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(
        bearer_scheme
    ),
    x_tenant_id: str = Header(
        ...,
        alias="X-Tenant-Id"
    ),
    db: Session = Depends(get_db)
):
    obtenir_tenant_authentifie(
        db,
        x_tenant_id,
        credentials
    )

    produits = db.query(Produit).filter(
        Produit.tenant_id == x_tenant_id,
        Produit.alerte_niveau.in_(["bas", "rupture"])
    ).all()

    alertes = []

    for produit in produits:
        alerte = preparer_alerte(produit)

        if alerte:
            alertes.append(
                AlerteResponse.model_validate(alerte)
            )

    return reponse_ok(alertes)


# ============================================================
# 9. RECEPTION DES COMMANDES DE L'AGENT COMMERCIAL
# ============================================================

@app.post("/exchange/notify")
async def exchange_notify(
    body: dict,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(
        bearer_scheme
    ),
    x_tenant_id: str = Header(
        ...,
        alias="X-Tenant-Id"
    ),
    db: Session = Depends(get_db)
):
    # ========================================================
    # 1. Vérification de l'authentification
    # ========================================================

    tenant = obtenir_tenant_authentifie(
        db,
        x_tenant_id,
        credentials
    )

    # ========================================================
    # 2. Vérification du type d'objet reçu
    # ========================================================

    if body.get("object_type") != "commande":
        return reponse_ok({
            "processed": False,
            "reason": "Type d'objet ignoré"
        })

    # ========================================================
    # 3. Validation de la commande
    # ========================================================

    try:
        commande = CommandeCreate.model_validate(
            body.get("payload", {})
        )

    except Exception:
        raise HTTPException(
            status_code=422,
            detail="Commande invalide"
        )

    # Vérifier la cohérence de l'identifiant externe,
    # lorsqu'il est fourni par le noyau.
    external_id = body.get("external_id")

    if (
        external_id is not None
        and external_id != commande.numero_commande
    ):
        raise HTTPException(
            status_code=422,
            detail="L'identifiant externe ne correspond pas au numéro de commande"
        )

    # ========================================================
    # 4. Vérification des commandes déjà traitées
    # ========================================================

    deja_traitee = db.query(CommandeTraitee).filter(
        CommandeTraitee.tenant_id == x_tenant_id,
        CommandeTraitee.numero_commande == commande.numero_commande
    ).first()

    if deja_traitee:
        return reponse_ok({
            "processed": True,
            "duplicate": True
        })

    # ========================================================
    # 5. Vérification des produits et des quantités
    # ========================================================

    try:
        produits_commande = []
        mouvements_crees = []

        # Vérifier toutes les lignes avant de modifier les stocks.
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

            produits_commande.append(
                (produit, ligne)
            )

        # ====================================================
        # 6. Mise à jour du stock et création des mouvements
        # ====================================================

        for produit, ligne in produits_commande:

            # Diminuer la quantité disponible.
            produit.quantite_disponible -= ligne.quantite

            # Recalculer le niveau du stock.
            produit.alerte_niveau = determiner_niveau_stock(
                produit.quantite_disponible,
                produit.seuil_alerte
            )

            # Mettre à jour la date de modification.
            produit.updated_at = datetime.now(timezone.utc)

            # Créer un mouvement de sortie.
            mouvement = MouvementStock(
                tenant_id=x_tenant_id,
                sku=produit.sku,
                type_mouvement="sortie",
                quantite=ligne.quantite,
                reference_commande=commande.numero_commande
            )

            db.add(mouvement)

            mouvements_crees.append(mouvement)

        # ====================================================
        # 7. Enregistrer la commande comme traitée
        # ====================================================

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

    # ========================================================
    # 8. Publier les mouvements de stock au noyau
    # ========================================================

    for mouvement in mouvements_crees:

        await publier_sur_bus(
            tenant,
            "mouvement_stock",
            str(mouvement.id_mouvement),
            {
                "id_mouvement": str(mouvement.id_mouvement),
                "sku": mouvement.sku,
                "type_mouvement": mouvement.type_mouvement,
                "quantite": mouvement.quantite,
                "reference_commande": mouvement.reference_commande
            }
        )

    # ========================================================
    # 9. Envoyer les alertes et publier les niveaux de stock
    # ========================================================

    for produit, ligne in produits_commande:

        alerte = preparer_alerte(produit)

        if alerte:
            await envoyer_alerte(
                tenant,
                alerte
            )

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

    # ========================================================
    # 10. Réponse finale au noyau
    # ========================================================

    return reponse_ok({
        "processed": True,
        "numero_commande": commande.numero_commande
    })
