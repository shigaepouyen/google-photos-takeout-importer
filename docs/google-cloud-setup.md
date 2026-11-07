# Configurer Google Cloud et OAuth

Ce guide prépare l'accès de **votre propre copie du script** à votre compte Google Photos. Aucun identifiant du mainteneur n'est nécessaire. Les noms de menus peuvent varier légèrement selon la langue et les évolutions de la console.

## Avant de commencer

Préparer :

- Un compte ayant le droit de créer/configurer un projet Google Cloud.
- L'adresse exacte du **compte Google de destination**, celui qui recevra les photos.
- Un Takeout extrait sur l'ordinateur et le script installé selon le [README](../README.md).

Le compte qui administre le projet Cloud et le compte qui reçoit les photos peuvent être différents. C'est le compte choisi pendant OAuth, vérifié contre `--account`, qui recevra les fichiers. La source Takeout est locale : le script n'a pas besoin de se connecter à l'ancien compte Google Photos.

## 1. Créer et sélectionner le projet

1. Ouvrir [Google Cloud Console](https://console.cloud.google.com/).
2. Cliquer sur le sélecteur de projet en haut, puis **Nouveau projet**.
3. Donner un nom, par exemple `Takeout Photos Importer`, et choisir l'organisation si nécessaire.
4. Créer le projet, puis le sélectionner dans la barre supérieure.

Pour toutes les étapes suivantes, vérifier que c'est **ce même projet** qui est sélectionné. L'activation de l'API et le client OAuth doivent appartenir au même projet.

## 2. Activer Photos Library API

1. Aller dans **API et services → Bibliothèque**.
2. Chercher **Photos Library API**.
3. Vérifier le service `photoslibrary.googleapis.com`, puis cliquer **Activer**.

Lien : [fiche Photos Library API](https://console.cloud.google.com/apis/library/photoslibrary.googleapis.com).

Le script utilise Library API pour importer et organiser les médias qu'il crée. Il ne nécessite ni Google Drive API, ni Photos Picker API, ni compte de service. [Autorisations Google Photos](https://developers.google.com/photos/overview/authorization).

## 3. Configurer l'écran de consentement

Ouvrir **Google Auth Platform** ; on peut aussi y accéder par **API et services → Écran de consentement OAuth**. Si un assistant de démarrage apparaît, le compléter.

Dans **Branding / Informations sur l'application** :

- Nom de l'application : un nom reconnaissable, par exemple `Takeout Photos Importer`.
- Email d'assistance et contact développeur : une adresse que vous contrôlez.
- Compléter les champs obligatoires affichés et enregistrer.

Dans **Audience** :

| Situation | Choix |
| --- | --- |
| Compte personnel, ou utilisateurs de plusieurs organisations | **Externe** |
| Projet d'une organisation Workspace et utilisateurs exclusivement de celle-ci | **Interne**, si disponible et adapté |

Pour un usage personnel avec audience **Externe**, conserver le mode **Test**, puis dans **Utilisateurs test**, ajouter **l'adresse du compte de destination**. Ajouter seulement le propriétaire du projet ne suffit pas si le compte de destination est différent.

Partager le code sur GitHub ne nécessite pas de publier votre application OAuth pour tous les utilisateurs : chaque utilisateur peut configurer son propre projet. Une distribution avec un client OAuth commun au public suit les règles de publication/vérification Google ; ce dépôt ne fournit pas ce client commun.

Référence : [Configurer le consentement et les autorisations](https://developers.google.com/workspace/guides/configure-oauth-consent).

## 4. Déclarer les autorisations

Dans **Accès aux données / Data Access**, ajouter ou sélectionner ces scopes, puis enregistrer :

| Scope | Utilisation dans le script |
| --- | --- |
| `openid` | Identification du compte connecté. |
| `https://www.googleapis.com/auth/userinfo.email` | Lecture et vérification de l'adresse de destination. |
| `https://www.googleapis.com/auth/photoslibrary.appendonly` | Ajout de médias et création/association d'albums. |

Le script demande aussi ces scopes lors de la connexion. Il ne demande pas la lecture de toute la photothèque : il ne peut donc pas rechercher les doublons déjà présents ou reconnaître un import manuel effectué à côté.

## 5. Créer le client OAuth de bureau

1. Dans **Google Auth Platform → Clients**, choisir **Créer un client**.
2. Type d'application : **Application de bureau / Desktop app**.
3. Donner un nom local, par exemple `Takeout Desktop`, puis créer.
4. Télécharger le JSON du client.
5. Le renommer `client_secret.json` et le placer à côté de `migrate.py`, ou garder son emplacement et passer son chemin avec `--client`.

Ne pas choisir **Application Web** et ne pas télécharger de clé de compte de service. Le script vérifie que le JSON contient la configuration `installed` d'une application de bureau.

La redirection locale `http://127.0.0.1:<port>/` est gérée par le script sur un port disponible. Il n'y a pas de serveur public à déployer, ni de domaine ou d'URI Web à configurer manuellement pour ce flux de bureau.

Référence : [OAuth pour applications de bureau](https://developers.google.com/identity/protocols/oauth2/native-app).

## 6. Connecter le bon compte et faire un essai

Depuis le dossier du script, avec le venv actif :

```sh
python migrate.py import --source '/chemin/vers/Takeout' \
  --account 'destination@example.com' --client client_secret.json --limit 3
```

Remplacer les exemples par vos valeurs. Le navigateur s'ouvre :

1. Choisir le compte indiqué par `--account`, même si un autre compte Google est déjà ouvert.
2. Vérifier le nom de votre application, puis accepter les autorisations proposées.
3. Attendre le message du terminal **Compte vérifié** et la fin de l'essai.
4. Vérifier les médias et albums dans Google Photos sous cette même adresse.

Si Google affiche un avertissement d'application non vérifiée, vérifier d'abord le projet, le nom de l'application et l'utilisateur test. Ne poursuivre un éventuel parcours de test proposé par Google que pour **votre propre application reconnue**. Si l'accès est bloqué sans parcours de test, corriger l'audience ou demander l'intervention de l'administrateur ; le script ne contourne pas ces restrictions.

Pour importer l'ensemble, reprendre la même commande **sans `--limit 3`**, avec le même `--work` si vous en aviez indiqué un. Les trois médias de l'essai seront réutilisés.

## 7. Conserver les fichiers nécessaires à la reprise

| Fichier | Rôle |
| --- | --- |
| `client_secret.json` | Identité du client OAuth de bureau. |
| `migration-work/token.json` | Autorisation et jetons du compte Google connecté. |
| `migration-work/state.json` | Journal des médias/albums/associations confirmés. |

Garder le **même client OAuth** pour reprendre une migration. Changer de client peut rendre les médias créés par l'ancien client inaccessibles au nouveau et le journal refuse ce changement.

Ces fichiers sont locaux et exclus par `.gitignore`. Ne les partager ni en ticket GitHub, ni en capture d'écran. En cas de révocation de l'application dans le compte Google, une nouvelle autorisation peut être nécessaire.

En mode externe **Test**, Google limite normalement la durée de vie des refresh tokens à sept jours pour ce type d'autorisation. Si une reprise tardive échoue avec `invalid_grant`, arrêter le script, conserver `state.json` et le même client OAuth, puis déplacer seulement le jeton pour déclencher une nouvelle connexion :

```sh
mv migration-work/token.json migration-work/token.expired.json
```

Relancer ensuite la même commande. Adapter le chemin si `--work` est différent ; ne pas déplacer le journal de migration. Une politique Workspace ou une révocation peut aussi faire expirer le jeton.

Référence : [Expiration des jetons OAuth](https://developers.google.com/identity/protocols/oauth2#expiration).

## 8. Workspace et administration

Si le compte cible est un compte Workspace :

- Vérifier que Google Photos est disponible pour ce compte.
- Vérifier l'audience : une application **Interne** ne permet pas de connecter un compte extérieur à l'organisation concernée.
- Si une politique bloque l'application ou ses scopes, communiquer à l'administrateur **l'ID public du client OAuth** et les trois scopes ci-dessus, pas le jeton ou le JSON complet.
- L'administrateur détermine si cette application peut être autorisée. Le statut d'utilisateur test ne remplace pas les politiques de l'organisation.

## 9. Quotas et stockage

Dans le projet Cloud : **API et services → Photos Library API → Quotas et limites système**, ou la page générale de quotas, filtrée sur ce service. Examiner aussi les métriques et réponses d'erreur du projet.

Les erreurs **429** peuvent correspondre à une limite de débit ou à un quota épuisé. Le script attend et réessaie ; si elles persistent, consulter la limite atteinte avant de relancer. Ne pas multiplier les clients ou projets pour contourner ces limites. Les possibilités d'ajustement dépendent du quota et des règles Google.

Le quota API est distinct de l'espace disponible dans le compte Google Photos cible. Les imports sont en qualité originale et peuvent consommer son stockage.

Référence : [Quotas Google Photos](https://developers.google.com/photos/overview/api-limits-quotas).

## Dépannage rapide

| Message / symptôme | Vérifications |
| --- | --- |
| `No module named google...` | Activer `.venv`, puis `python -m pip install -r requirements.txt`. |
| Client de type bureau requis / `redirect_uri_mismatch` | JSON **Desktop app**, pas Web ; bon fichier passé à `--client`. |
| `access_denied`, application en test | Adresse cible ajoutée aux utilisateurs test ; bon compte choisi ; audience adaptée. |
| Accès bloqué par l'organisation | Politiques Workspace, activation de Photos, autorisation du client par l'administrateur. |
| `Compte refusé` | L'adresse connectée doit être exactement celle de `--account` ; un alias peut différer de l'email renvoyé par Google. |
| API désactivée / accès non configuré | API activée dans le projet qui a créé ce client OAuth. |
| `invalid_grant` | Jeton expiré ou révoqué ; procédure de reconnexion ci-dessus, sans effacer le journal. |
| `429` | Attendre ; inspecter les quotas/métriques du bon projet si la limite persiste. |
| `pending` | Voir les règles de reprise dans le [README](../README.md#reprise-et-limites-de-débit) ; ne pas effacer le journal. |
| Refus d'un média avec code 3 ou 9 | Rapport `failed-media.json` ; ce n'est pas nécessairement un problème GCP. |
| Navigateur sur une autre machine | Lancer le script sur la même machine que le navigateur pour ce flux de bureau local. |

En partageant une erreur, masquer noms de fichiers privés, emails, identifiants de projet/client et URLs d'authentification. Ne jamais publier de jeton ou mot de passe.
