#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
À exécuter UNE SEULE FOIS, sur ton ordinateur (pas sur GitHub Actions).
Objectif : obtenir un "refresh token" qui permettra ensuite au script GitHub
d'agir sur ton Google Drive EN TON NOM PROPRE (et non via le service account,
qui n'a pas de quota de stockage).

Prérequis :
  1. pip install google-auth-oauthlib google-auth
  2. Avoir téléchargé le fichier JSON de l'identifiant OAuth "Desktop app"
     (Google Cloud Console > Credentials > ton client OAuth > Download JSON)
  3. Renommer ce fichier "client_secret.json" et le mettre dans le même dossier
     que ce script.

Lancement :
  python obtenir_refresh_token.py

Une page de ton navigateur va s'ouvrir : connecte-toi avec ton compte Google
(epicureomassimo@gmail.com), accepte l'avertissement "Google n'a pas vérifié
cette application" (normal, c'est TON appli, à usage personnel) en cliquant
sur "Continuer" / "Avancé > Accéder à ... (non sécurisé)", puis autorise
l'accès à Drive.

Le script affichera ensuite 3 valeurs à copier dans les secrets GitHub :
  GOOGLE_OAUTH_CLIENT_ID
  GOOGLE_OAUTH_CLIENT_SECRET
  GOOGLE_OAUTH_REFRESH_TOKEN
"""

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]

def main():
    flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
    creds = flow.run_local_server(port=0)

    print("\n" + "=" * 70)
    print("✅ Autorisation réussie ! Copie ces 3 valeurs dans les secrets GitHub :")
    print("=" * 70)
    print(f"\nGOOGLE_OAUTH_CLIENT_ID\n{creds.client_id}")
    print(f"\nGOOGLE_OAUTH_CLIENT_SECRET\n{creds.client_secret}")
    print(f"\nGOOGLE_OAUTH_REFRESH_TOKEN\n{creds.refresh_token}")
    print("\n" + "=" * 70)

    if not creds.refresh_token:
        print("⚠️  Aucun refresh_token reçu. Cause fréquente : tu avais déjà")
        print("    autorisé cette appli avant. Va sur https://myaccount.google.com/permissions")
        print("    retire l'accès à l'appli, puis relance ce script.")

if __name__ == "__main__":
    main()
