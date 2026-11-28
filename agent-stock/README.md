# Agent de gestion de stock

Agent d'automatisation métier (filière **automatisation_metier**) destiné à être branché au noyau
de la plateforme. Il suit les niveaux de stock d'une entreprise, enregistre les mouvements,
détecte les stocks faibles et les ruptures, et collabore avec les autres agents via le bus d'échange du noyau.

- **Version** : 1.0.0 (SemVer)
- **Stack** : Python 3.12, FastAPI, SQLAlchemy, PostgreSQL
- **Port d'écoute** : 9001 (identique à `base_url` dans `agent.manifest.json`)

## Ce que fait l'agent

| Rôle | Détail |
|---|---|
| Stock | Produits (SKU, désignation, quantité, seuil d'alerte, entrepôt), mouvements (entrée, sortie, ajustement), import CSV |
| Alertes | `stock_faible` (severity `medium`), `stock_rupture` (`critical`) et `commande_non_traitee` (`high`) envoyées au webhook du noyau |
| Produit (bus) | `stock_level`, `mouvement_stock` |
| Consomme (bus) | `commande` (via `POST /exchange/notify`) |
| Multi-entreprises | Isolation stricte par entreprise (tenant) ; les données sont supprimées à la désactivation |

### Règles métier sur les commandes
- Une commande au statut **`confirmee`** fait sortir le stock (un mouvement `sortie` par ligne).
  Tout ou rien : si un produit est inconnu ou si le stock est insuffisant, rien ne bouge et une alerte
  `commande_non_traitee` est envoyée au noyau.
- Une commande **`annulee` après confirmation** remet le stock (un mouvement `entree` par ligne), une seule fois.
- Les autres statuts (`en_attente`, `expediee`, `facturee`) et une annulation sans confirmation préalable sont ignorés.
- Une même commande n'est jamais traitée deux fois (anti-doublon par numéro de commande).

### Seuil d'alerte
Le seuil se définit **produit par produit** (création, modification ou colonne `seuil_alerte` du CSV).
S'il n'est pas précisé, la valeur par défaut est **5**. Niveaux : `ok`, `bas` (stock inférieur ou égal au seuil), `rupture` (stock = 0).

## Organisation du code

```
main.py              API FastAPI : endpoints du contrat, produits, mouvements, alertes, import CSV, /exchange/notify
models.py            tables : tenants, produit, mouvement_stock, commandes_traitees
schemas.py           schémas de validation (Pydantic)
stock_service.py     logique métier du stock (CRUD, niveaux d'alerte, import CSV)
database.py          connexion PostgreSQL (variables d'environnement)
agent.manifest.json  manifeste exigé par le noyau
Dockerfile, docker-compose.yml
test_agent_contrat.py, test_import_csv.py   tests automatiques
exemple_import.csv   exemple de fichier d'import
```

## Lancer l'agent en local

### Prérequis
- Python 3.12
- PostgreSQL, avec une base vide (par défaut `agent_stock_db`) : `CREATE DATABASE agent_stock_db;`

### Installation
```bash
python -m venv env
# Windows : env\Scripts\activate        Linux/Mac : source env/bin/activate
pip install -r requirements.txt
cp .env.example .env        # puis remplir les valeurs (voir ci-dessous)
```

### Variables d'environnement (fichier `.env`, jamais commité)

| Variable | Rôle | Défaut |
|---|---|---|
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER` | Connexion PostgreSQL | localhost, 5432, agent_stock_db, postgres |
| `DB_PASSWORD` | Mot de passe PostgreSQL (**obligatoire**) | - |
| `FERNET_KEY` | Clé qui chiffre les clés d'accès des entreprises (**obligatoire**) | - |
| `NOYAU_URL` | Adresse du noyau | http://localhost:8000 |
| `CORS_ORIGINS` | Adresses du front autorisées (séparées par des virgules) | http://localhost:5173,http://localhost:3000 |

Générer une `FERNET_KEY` :
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### Démarrage
```bash
uvicorn main:app --port 9001
```
Les tables sont créées automatiquement au premier démarrage.
- Santé : http://localhost:9001/health
- Documentation interactive : http://localhost:9001/docs

## Tests

PostgreSQL et le fichier `.env` sont nécessaires ; le noyau n'a pas besoin de tourner (ses réponses sont simulées).
```bash
python -m pytest -v
```
Les tests couvrent : les 5 endpoints du contrat et l'enveloppe standard, la suppression des données,
les mouvements et alertes, l'isolation entre entreprises, `/exchange/notify` (commande, doublon, annulation,
stock insuffisant), l'import CSV, le CORS et le cas du noyau injoignable.

## API de l'agent

Tous les appels (sauf `/health` et `/config-schema`) portent `X-Tenant-Id` et `Authorization: Bearer <tenant_api_key>`.
Toutes les réponses ont la forme `{ "status": "ok" | "error", "data": ..., "error": ... }`.

| Endpoint | Rôle |
|---|---|
| `GET /health` | Santé de l'agent |
| `POST /tenant/activate` | Activation pour une entreprise (appelé par le noyau) |
| `POST /tenant/deactivate` | Désactivation et suppression des données |
| `GET /tenant/{tenant_id}/status` | État de l'entreprise (`running`) |
| `GET /config-schema` | Paramètres d'activation (aucun) |
| `POST /exchange/notify` | Réception des objets du bus (`commande`) |
| `GET/POST /produits`, `GET/PUT/DELETE /produits/{sku}`, `GET /produits/{sku}/stock` | Produits |
| `POST /produits/import-csv` | Import CSV (champ `fichier`) |
| `GET/POST /mouvements` | Mouvements de stock |
| `GET /alertes` | Alertes actuelles |

Format du CSV : colonnes `sku`, `designation`, `quantite_disponible` (obligatoires), `seuil_alerte`, `entrepot`
(facultatives), séparateur `,` ou `;`, encodage UTF-8. Import tout ou rien (voir `exemple_import.csv`).

## Intégration avec le noyau

1. Lancer le noyau (port 8000) et l'agent (port 9001).
2. Soumettre l'agent : `POST /agents/submit` (page `/docs` du noyau). Le noyau attend les champs à plat
   plus le manifeste complet dans `manifest`. Exemple PowerShell, depuis ce dossier :
```powershell
$m = Get-Content .\agent.manifest.json -Raw -Encoding UTF8 | ConvertFrom-Json
$body = @{
  slug=$m.slug; name=$m.name; version=$m.version; description=$m.description
  filiere=$m.filiere; category=$m.category; developer_email=$m.developer.email
  team_name=$m.team_name; base_url=$m.endpoints.base_url; pricing_model=$m.pricing_model
  price=$m.price; manifest=$m; produces=@($m.produces); consumes=@($m.consumes)
} | ConvertTo-Json -Depth 10
Invoke-RestMethod -Uri http://localhost:8000/agents/submit -Method Post -ContentType "application/json; charset=utf-8" -Body $body
```
3. L'administrateur certifie puis publie l'agent (page `/admin`).
4. Une entreprise souscrit depuis `/catalogue` : le noyau appelle `/tenant/activate`.
5. La clé d'accès de l'entreprise apparaît sur `/mon-compte` du noyau.

## Docker

```bash
cp .env.example .env     # remplir DB_PASSWORD et FERNET_KEY
docker compose up --build
```
- L'agent écoute sur http://localhost:9001 et sa base PostgreSQL tourne dans un conteneur séparé (volume persistant).
- Le noyau, lancé sur la machine hôte, est joint via `host.docker.internal:8000`
  (modifiable avec `NOYAU_URL_DOCKER` dans `.env`).
- Arrêter : `docker compose down` (ajouter `-v` pour supprimer aussi la base).

## Sécurité
- Aucun secret dans le code : tout passe par des variables d'environnement.
- Les clés d'accès des entreprises sont chiffrées (Fernet) en base et comparées en temps constant.
- Chaque requête est filtrée par entreprise : une entreprise ne peut ni lire ni modifier les données d'une autre.
- L'agent ne communique qu'avec le noyau (`NOYAU_URL`).

## Limites connues
- Supprimer un produit dans l'agent ne retire pas son dernier `stock_level` du bus du noyau.
- Aucun réglage n'est proposé à l'activation (`/config-schema` est vide).