
import uuid

from sqlalchemy import (
    Column,
    String,
    Integer,
    Text,
    DateTime,
    ForeignKey,
    CheckConstraint,
    ForeignKeyConstraint,
    UniqueConstraint,
    Index,
    BigInteger
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func

from database import Base


# 1. Table des entreprises activées
class Tenant(Base):
    __tablename__ = "tenants"

    tenant_id = Column(String(100), primary_key=True)
    tenant_api_key_chiffree = Column(Text, nullable=True)
    state = Column(String(20), nullable=False, default="running")
    config = Column(JSONB, nullable=False, default=dict)
    date_activation = Column(
        DateTime(timezone=True),
        server_default=func.current_timestamp()
    )


# 2. Table des produits
class Produit(Base):
    __tablename__ = "produit"

    tenant_id = Column(
        String(100),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        primary_key=True
    )
    sku = Column(String(50), primary_key=True)
    designation = Column(String(255), nullable=False)
    quantite_disponible = Column(Integer, nullable=False, default=0)
    seuil_alerte = Column(Integer, nullable=False, default=5)
    entrepot = Column(String(100), nullable=False, default="Principal")
    alerte_niveau = Column(String(10), nullable=False, default="ok")
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.current_timestamp()
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.current_timestamp()
    )

    __table_args__ = (
        CheckConstraint(
            "quantite_disponible >= 0",
            name="check_quantite_positive"
        ),
        CheckConstraint(
            "seuil_alerte >= 0",
            name="check_seuil_positive"
        ),
        CheckConstraint(
            "alerte_niveau IN ('ok', 'bas', 'rupture')",
            name="check_alerte_niveau"
        ),
        Index("idx_produit_alerte", "tenant_id", "alerte_niveau"),
    )


# 3. Table des mouvements de stock
class MouvementStock(Base):
    __tablename__ = "mouvement_stock"

    id_mouvement = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4
    )
    tenant_id = Column(String(100), nullable=False)
    sku = Column(String(50), nullable=False)
    type_mouvement = Column(String(20), nullable=False)
    quantite = Column(Integer, nullable=False)
    reference_commande = Column(String(100), nullable=True)
    date_mouvement = Column(
        DateTime(timezone=True),
        server_default=func.current_timestamp()
    )

    __table_args__ = (
       ForeignKeyConstraint(
    ["tenant_id", "sku"],
    ["produit.tenant_id", "produit.sku"],
    ondelete="CASCADE"
),
        CheckConstraint(
            "type_mouvement IN ('entree', 'sortie', 'ajustement')",
            name="check_type_mouvement"
        ),
        CheckConstraint(
            "(type_mouvement = 'ajustement' AND quantite <> 0) "
            "OR (type_mouvement IN ('entree', 'sortie') AND quantite > 0)",
            name="check_quantite_mouvement"
        ),
        Index("idx_mouvement_tenant_sku", "tenant_id", "sku"),
        Index("idx_mouvement_commande", "tenant_id", "reference_commande"),
        Index("idx_mouvement_date", "tenant_id", "date_mouvement"),
    )


# 4. Table des commandes déjà traitées
class CommandeTraitee(Base):
    __tablename__ = "commandes_traitees"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id = Column(
        String(100),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False
    )
    numero_commande = Column(String(100), nullable=False)
    date_traitement = Column(
        DateTime(timezone=True),
        server_default=func.current_timestamp()
    )

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "numero_commande",
            name="uq_commande_tenant"
        ),
        Index("idx_commandes_tenant", "tenant_id"),
    )
