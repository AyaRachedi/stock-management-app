
from pydantic import BaseModel, Field, ConfigDict
from typing import Optional, Literal
from datetime import datetime
from uuid import UUID


# -------------------------
# 1. Schémas des produits
# -------------------------

class ProduitBase(BaseModel):
    sku: str = Field(min_length=1, max_length=50)
    designation: str = Field(min_length=1, max_length=255)
    quantite_disponible: int = Field(default=0, ge=0)
    seuil_alerte: int = Field(default=5, ge=0)
    entrepot: str = Field(default="Principal", max_length=100)


class ProduitCreate(ProduitBase):
    pass


class ProduitUpdate(BaseModel):
    designation: Optional[str] = Field(default=None, max_length=255)
    quantite_disponible: Optional[int] = Field(default=None, ge=0)
    seuil_alerte: Optional[int] = Field(default=None, ge=0)
    entrepot: Optional[str] = Field(default=None, max_length=100)


class ProduitResponse(ProduitBase):
    tenant_id: str
    alerte_niveau: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


# -------------------------
# 2. Schémas des mouvements
# -------------------------

class MouvementCreate(BaseModel):
    sku: str = Field(min_length=1, max_length=50)
    type_mouvement: Literal["entree", "sortie", "ajustement"]
    quantite: int
    reference_commande: Optional[str] = None


class MouvementResponse(BaseModel):
    id_mouvement: UUID
    tenant_id: str
    sku: str
    type_mouvement: str
    quantite: int
    reference_commande: Optional[str] = None
    date_mouvement: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


# -------------------------
# 3. Schémas des commandes
# -------------------------

# -------------------------
# 3. Schémas des commandes
# -------------------------

class ClientCommande(BaseModel):
    nom: str
    email: str

    model_config = ConfigDict(extra="ignore")


class LigneCommande(BaseModel):
    sku: str
    quantite: int = Field(gt=0)
    prix_unitaire_ht: float = Field(ge=0)

    model_config = ConfigDict(extra="ignore")


class CommandeCreate(BaseModel):
    numero_commande: str
    date_commande: datetime
    client: ClientCommande
    lignes: list[LigneCommande]
    montant_total_ht: float = Field(ge=0)
    statut: Literal[
        "en_attente",
        "confirmee",
        "expediee",
        "facturee",
        "annulee"
    ]

    model_config = ConfigDict(extra="ignore")

# -------------------------
# 4. Schéma des alertes
# -------------------------

class AlerteResponse(BaseModel):
    event_type: Literal["stock_faible", "stock_rupture"]
    severity: Literal["medium", "critical"]
    data: dict


# -------------------------
# 5. Schéma du stock
# -------------------------

class StockResponse(BaseModel):
    sku: str
    designation: str
    quantite_disponible: int
    seuil_alerte: int
    entrepot: str

    model_config = ConfigDict(from_attributes=True)
