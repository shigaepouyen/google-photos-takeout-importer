# Google Photos Takeout Importer

Outil Python pour importer une archive Google Photos Takeout dans un autre compte Google Photos, avec préparation des métadonnées et reconstruction des albums.

Interface en français. Destiné à un usage personnel sur **macOS ou Linux**, avec Python **3.10+** et ExifTool. Windows n'est pas pris en charge en l'état (verrou `fcntl`).

## Ce que fait l'outil

- Parcourt Takeout et ses sous-dossiers et associe les médias aux JSON correspondants.
- Prépare des **copies** en complétant uniquement les métadonnées manquantes : date de prise de vue, description et GPS lorsque le format le permet.
- Vérifie l'adresse exacte du compte Google de destination avant tout appel d'import.
- Importe les médias, crée les albums et reconstruit leurs associations avec Google Photos Library API.
- Dédoublonne au sein de la migration selon le contenu et les métadonnées utiles.
- Affiche une progression, les éléments restants et les albums créés ou réutilisés.
- Conserve un journal pour reprendre après interruption ; attend et réessaie lors des réponses HTTP 429.
- Consigne les médias explicitement refusés et poursuit les autres, sans annoncer un succès complet s'il manque des fichiers.

**Aucune archive, photo, adresse de destination, clé OAuth ou donnée de migration n'est fournie dans ce dépôt.** Les tests utilisent des fichiers temporaires synthétiques et des réponses Google simulées.

## Installation

```sh
git clone https://github.com/shigaepouyen/google-photos-takeout-importer.git
cd google-photos-takeout-importer
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Installer ExifTool séparément :

```sh
# macOS
brew install exiftool

# Debian / Ubuntu
sudo apt install libimage-exiftool-perl
```

## Connexion Google : une fois par utilisateur

**Guide complet : [Configurer Google Cloud et OAuth](docs/google-cloud-setup.md)** — projet, activation API, audience, utilisateurs test, scopes, client de bureau, Workspace, jetons et dépannage.

L'outil ne demande jamais de mot de passe Google. Il utilise OAuth dans le navigateur ; chaque utilisateur crée son propre projet et son propre client.

1. Créer ou sélectionner un projet dans [Google Cloud Console](https://console.cloud.google.com/).
2. Activer **Photos Library API** (`photoslibrary.googleapis.com`).
3. Configurer Google Auth Platform / l'écran de consentement. Pour une application externe en mode test, ajouter l'adresse Google de destination aux utilisateurs test. Pour Workspace, l'administrateur peut devoir autoriser l'application.
4. Créer un client OAuth de type **Application de bureau** et télécharger son JSON sous le nom `client_secret.json` dans ce dossier.
5. Au premier import, choisir le compte indiqué par `--account` et accepter les autorisations.

Le script demande `openid`, `userinfo.email` et `photoslibrary.appendonly`. Il vérifie l'email et sa validation auprès de Google ; un autre compte est refusé. Le journal est lié à l'identifiant Google du compte et à l'ID du client OAuth. Les comptes de service ne sont pas utilisés.

[OAuth pour applications de bureau](https://developers.google.com/identity/protocols/oauth2/native-app) · [Autorisations Google Photos](https://developers.google.com/photos/overview/authorization).

## Utilisation

Les chemins et l'adresse ci-dessous sont des exemples. Remplacer `destination@example.com` par le véritable compte Google cible.

### 1. Analyser, sans connexion Google

```sh
python migrate.py scan --source '/chemin/vers/Takeout'
```

Le rapport détaillé est enregistré dans `migration-work/scan.json`. Les associations absentes ou ambiguës y sont indiquées. Elles bloquent par défaut la préparation et l'import. `--allow-unmatched` permet explicitement de laisser ces fichiers de côté.

### 2. Préparer les copies, sans import

```sh
python migrate.py prepare --source '/chemin/vers/Takeout'
```

Les copies et reçus de vérification sont placés dans `migration-work/prepared/`. L'espace disque nécessaire dépend du volume de médias distincts. Les originaux ne sont pas modifiés. Les métadonnées déjà présentes sont conservées même si elles diffèrent du JSON : l'outil complète les champs manquants, il ne réconcilie pas les corrections historiques.

### 3. Faire un essai de trois médias

```sh
python migrate.py import --source '/chemin/vers/Takeout' \
  --account 'destination@example.com' --client client_secret.json --limit 3
```

Cette commande **importe réellement** jusqu'à trois médias. Vérifier leur compte, leurs dates et leurs albums dans Google Photos avant l'import complet.

### 4. Importer le reste

```sh
python migrate.py import --source '/chemin/vers/Takeout' \
  --account 'destination@example.com' --client client_secret.json
```

Les médias de l'essai sont réutilisés si le dossier de travail et le client OAuth restent les mêmes.

### Options utiles

| Option | Effet |
| --- | --- |
| `--source PATH` | Source Takeout, obligatoire pour chaque commande. |
| `--account EMAIL` | Compte exact de destination, obligatoire pour `import`. |
| `--client FILE` | JSON OAuth Application de bureau, obligatoire pour `import`. |
| `--work PATH` | Dossier de travail ; par défaut `migration-work` dans le répertoire courant. |
| `--limit N` | Limite la sélection à N médias distincts. |
| `--album TITLE` | Sélectionne un album par titre exact ou chemin relatif. |
| `--retry-failed` | Réessaie également les médias précédemment refusés. |
| `--allow-unmatched` | Ignore explicitement les fichiers dont l'association JSON est incertaine. |
| `--no-color` | Désactive les couleurs ; `NO_COLOR=1` fonctionne aussi. |

Pour plusieurs migrations, utiliser un `--work` distinct par source/compte. Le dossier de travail doit rester **hors de la source Takeout**. Conserver les chemins stables pendant une migration.

## Reprise et limites de débit

`migration-work` contient les copies, le jeton OAuth, les rapports et `state.json`, le journal des médias, albums et associations confirmés. **Ne pas supprimer ce dossier pour reprendre.** Relancer la même commande avec le même `--work` et le même client OAuth.

- Un fichier interrompu pendant l'envoi binaire recommence depuis le début ; pas de reprise par blocs.
- Les médias et associations déjà confirmés ne sont pas renvoyés.
- Un ajout interrompu à un album peut être repris automatiquement avec les mêmes identifiants connus, sans créer de média. Le journal est sauvegardé avant cette récupération.
- Une réponse HTTP 429 explicite déclenche jusqu'à cinq reprises, avec un délai d'au moins 30 secondes, croissant et respectant `Retry-After`. Si la limite persiste, relancer plus tard et vérifier les quotas du projet.
- Si une **création de média ou d'album** a pu réussir mais que sa réponse a été perdue, le script bloque sur `pending`. Il ne peut pas garantir une reprise automatique sans doublon dans ce cas. Les timeouts et les erreurs serveur ne sont pas traités comme des refus certains.

En cas de création incertaine : sauvegarder `state.json`, vérifier le résultat dans Google Photos et via l'API avec ce même client, puis réconcilier l'ID dans `albums` ou `media` avant de remettre `pending` à `null`. Ne pas effacer tout le journal. Ce cas peut demander une intervention technique ; aucune commande de récupération aveugle n'est fournie.

[Quotas Google Photos](https://developers.google.com/photos/overview/api-limits-quotas) · [Bonnes pratiques de reprise](https://developers.google.com/photos/overview/best-practices).

## Médias refusés

Les refus individuels de type `INVALID_ARGUMENT` (3) ou `FAILED_PRECONDITION` (9) sont enregistrés dans `failed-media.json` et le journal. Les autres médias continuent d'être importés et classés. Une erreur globale de compte, quota ou serveur arrête toujours le script.

Pour réessayer les refus, ajouter `--retry-failed` à la commande d'import. Certains fichiers peuvent nécessiter une réparation ou conversion indépendante ; le script ne transcode pas les vidéos. Un import effectué manuellement dans Google Photos n'est pas détecté par ce journal.

Codes de sortie : `0` terminé, `1` erreur, `2` import incomplet ou arguments invalides, `130` interruption par l'utilisateur. Le bilan distingue les médias importés des associations aux albums.

## Métadonnées et limites connues

- Utilise `photoTakenTime` pour la capture ; `creationTime` du JSON n'est pas une date de capture de remplacement.
- Écrit les dates JSON en UTC : EXIF/XMP pour les images, QuickTime pour MOV/MP4/M4V. D'autres formats vidéo peuvent demander un traitement spécifique.
- Complète les coordonnées EXIF pour les images et `Keys:GPSCoordinates` pour MOV/MP4/M4V, si aucune position n'existe. `(0,0)` est traité comme une valeur absente de Takeout.
- Envoie aussi les descriptions via le champ API dédié ; sa limite de moins de 1 000 caractères est conservée. Une description trop longue provoque un arrêt.
- Reconnaît les JSON classiques et `.supplemental-metadata.json`, ainsi que certaines variantes `-modifié`/`-edited` associées explicitement à l'unique JSON de l'original. Pas d'association approximative par nom tronqué : les cas ambigus sont signalés.
- Prend le titre de l'album dans ses métadonnées, à défaut dans le nom du sous-dossier. Les dossiers annuels « Photos de YYYY » / « Photos from YYYY » ne sont pas créés comme albums ; les albums vides ne sont pas créés. Deux dossiers distincts de même titre restent distincts.
- Dédoublonne uniquement cette migration ; ne recherche pas les doublons dans toute la photothèque préexistante. Des métadonnées utiles différentes donnent deux éléments distincts.
- Corrige l'extension d'une copie si ExifTool détecte un type réel différent.
- Google peut interpréter différemment certains champs, notamment les dates et GPS vidéo : vérifier l'essai. L'outil ne restaure pas les visages, commentaires, favoris, partages ou historiques d'édition.
- Les imports API sont en qualité originale et peuvent consommer le stockage du compte. Les quotas Google et restrictions Workspace restent applicables.

[Import API](https://developers.google.com/photos/library/guides/upload-media) · [Gestion des albums](https://developers.google.com/photos/library/guides/manage-albums).

## Confidentialité

Le dossier de travail est privé (permissions 700), et les fichiers d'état/jeton sont écrits en mode 600. Un verrou empêche deux exécutions simultanées sur le même dossier. Les noms des fichiers et albums sont affichés dans le terminal : un journal de terminal peut donc contenir des informations privées.

`.gitignore` exclut les archives, médias, JSON, clés, jetons, dossiers de travail et environnements Python. Ne jamais forcer leur ajout avec `git add -f`. Ne joindre à un ticket ni Takeout, ni jeton, ni rapport brut : fournir seulement une erreur et un exemple anonymisés. Le script appelle uniquement les endpoints Google nécessaires à l'authentification et à l'import ; aucune télémétrie n'est ajoutée.

## Développement

```sh
python -m unittest discover -s . -v
```

Les tests couvrent notamment les associations de JSON, la conservation des métadonnées, le contrôle du compte, la reprise, les refus individuels et le traitement des 429. Ils ne contactent pas Google et ne constituent pas une validation d'import de tous les formats média. La CI exécute ces tests sur macOS et Linux.

Licence MIT. Contributions bienvenues avec des tests et des exemples synthétiques, sans données personnelles.
