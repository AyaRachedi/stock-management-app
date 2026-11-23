
import os
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker, declarative_base

# Charger les variables du fichier .env
load_dotenv()

# Récupérer les paramètres PostgreSQL
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_NAME = os.getenv("DB_NAME", "agent_stock_db")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD")

if not DB_PASSWORD:
    raise ValueError("Mot de passe PostgreSQL absent du fichier .env")

# Construire l'URL de connexion
DATABASE_URL = URL.create(
    drivername="postgresql+psycopg2",
    username=DB_USER,
    password=DB_PASSWORD,
    host=DB_HOST,
    port=DB_PORT,
    database=DB_NAME
)

# Créer le moteur de connexion
engine = create_engine(DATABASE_URL, pool_pre_ping=True)

# Créer les sessions
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

# Base des futurs modèles SQLAlchemy
Base = declarative_base()

# Fournir une session à FastAPI
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
