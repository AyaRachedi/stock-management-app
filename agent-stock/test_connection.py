
from sqlalchemy import text
from database import engine

try:
    with engine.connect() as connection:
        result = connection.execute(
            text("SELECT current_database(), current_user")
        )
        database, user = result.fetchone()

        print("Connexion PostgreSQL réussie !")
        print("Base de données :", database)
        print("Utilisateur :", user)

except Exception as error:
    print("Erreur de connexion :")
    print(error)
