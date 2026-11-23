
from types import SimpleNamespace
from stock_service import determiner_niveau_stock, preparer_alerte

# Trois produits de test
produits = [
    SimpleNamespace(
        sku="P001",
        designation="Clavier",
        quantite_disponible=20,
        seuil_alerte=5
    ),
    SimpleNamespace(
        sku="P002",
        designation="Souris",
        quantite_disponible=3,
        seuil_alerte=5
    ),
    SimpleNamespace(
        sku="P003",
        designation="Ecran",
        quantite_disponible=0,
        seuil_alerte=5
    )
]

for produit in produits:
    niveau = determiner_niveau_stock(
        produit.quantite_disponible,
        produit.seuil_alerte
    )

    print(f"\nProduit : {produit.designation}")
    print(f"Niveau : {niveau}")
    print("Alerte :", preparer_alerte(produit))
