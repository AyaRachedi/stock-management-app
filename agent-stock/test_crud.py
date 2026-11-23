
from database import SessionLocal
from schemas import ProduitCreate, ProduitUpdate

from stock_service import (
    ajouter_produit,
    lister_produits,
    obtenir_produit,
    modifier_produit,
    supprimer_produit
)

TENANT_ID = "entreprise_test"

db = SessionLocal()

try:
    # 1. CREATE : ajouter un produit
    print("\n--- 1. AJOUTER UN PRODUIT ---")

    nouveau = ProduitCreate(
        sku="TEST-001",
        designation="Clavier",
        quantite_disponible=20,
        seuil_alerte=5,
        entrepot="Principal"
    )

    produit = ajouter_produit(db, TENANT_ID, nouveau)
    print("Produit ajouté :", produit.designation)
    print("Quantité :", produit.quantite_disponible)

    # 2. READ : afficher tous les produits
    print("\n--- 2. LISTER LES PRODUITS ---")

    produits = lister_produits(db, TENANT_ID)

    for p in produits:
        print(p.sku, "-", p.designation, "-", p.quantite_disponible)

    # 3. READ : consulter un produit
    print("\n--- 3. CONSULTER UN PRODUIT ---")

    produit = obtenir_produit(db, TENANT_ID, "TEST-001")
    print("Produit trouvé :", produit.designation)

    # 4. UPDATE : modifier le produit
    print("\n--- 4. MODIFIER LE PRODUIT ---")

    modifications = ProduitUpdate(
        quantite_disponible=3
    )

    produit = modifier_produit(
        db, TENANT_ID, "TEST-001", modifications
    )

    print("Nouvelle quantité :", produit.quantite_disponible)
    print("Niveau d'alerte :", produit.alerte_niveau)

    # 5. DELETE : supprimer le produit
    print("\n--- 5. SUPPRIMER LE PRODUIT ---")

    supprimer_produit(db, TENANT_ID, "TEST-001")
    print("Produit supprimé !")

    # Vérifier la suppression
    resultat = obtenir_produit(db, TENANT_ID, "TEST-001")
    print("Produit encore présent :", resultat is not None)

except Exception as erreur:
    db.rollback()
    print("Erreur :", erreur)

finally:
    db.close()
