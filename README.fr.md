<p align="center">
  <img src="branding/logo.svg" alt="Btrfs Harbor" width="560">
</p>

<h1 align="center">Btrfs Harbor</h1>

<p align="center">
  <strong>⚓ Snapshots in. Systems back.</strong><br>
  Sauvegarde, vérification, récupération et réplication Btrfs pour Linux.
</p>

<p align="center">
  <a href="https://github.com/GodsQuantum/btrfs-harbor/actions/workflows/harbor.yml"><img alt="Harbor CI" src="https://github.com/GodsQuantum/btrfs-harbor/actions/workflows/harbor.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="Licence MIT" src="https://img.shields.io/badge/license-MIT-3dd7cf"></a>
  <img alt="Linux" src="https://img.shields.io/badge/platform-Linux-0B1622">
  <img alt="Btrfs" src="https://img.shields.io/badge/filesystem-Btrfs-58D6C5">
  <img alt="Tauri" src="https://img.shields.io/badge/desktop-Tauri%202-24C8DB">
  <img alt="CachyOS" src="https://img.shields.io/badge/CachyOS-ready-00A3FF">
</p>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="README.fr.md">Français</a> ·
  <a href="README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <img src="docs/assets/screenshots/overview.png" width="100%" alt="Vue générale Btrfs Harbor avec données de démonstration génériques">
</p>

Btrfs Harbor transforme les **snapshots locaux Btrfs/Snapper en vraies sauvegardes hors machine**. Il regroupe destinations contrôlées, flux Btrfs complets + incrémentaux, vérification, planification systemd, récupération en staging et réplication LXC Proxmox dans une application desktop native.

Un snapshot local sur le même disque est utile, mais ce n’est **pas** une sauvegarde hors machine. Harbor conserve cette distinction partout dans l’interface.

## ✨ Pourquoi Harbor ?

- **Sauvegardes Btrfs full + incrémentales** — la filiation des snapshots est conservée.
- **Destinations NFS / SMB / local / SSH** — y compris des streams raw sur un filesystem classique.
- **Pas de fallback NAS silencieux** — la destination réseau est identifiée avant toute écriture.
- **Vérification intégrée** — checksum optionnel après la sauvegarde.
- **Planification persistante** — services et timers systemd natifs par UUID.
- **Recovery Kits** — topologie, paquets, Snapper et notes de boot à côté des sauvegardes.
- **Récupération en staging** — restauration et vérification séparées du root live.
- **Réplication LXC Proxmox** — déploiement vers un conteneur arrêté avec snapshot/rollback natif.
- **Desktop à privilèges minimaux** — D-Bus en lecture + helper privilégié fixe, aucun shell root générique.
- **English / Français / 简体中文** — UI et documentation complètes.
- **Aucun runtime Docker** — Harbor s’installe comme une application Linux native.

## 📦 Télécharger et installer

Le moyen le plus simple de **tester Harbor ou faire des sauvegardes manuelles** est l’**AppImage portable**. Elle reste portable : son lancement n’installe rien. Elle détecte Btrfs/Snapper, permet de choisir les sources et la destination, de la valider, de lancer **Sauvegarder maintenant**, de parcourir les snapshots sauvegardés et de préparer une restauration. Les opérations Btrfs privilégiées demandent polkit seulement quand c’est nécessaire.

Installe Harbor sur le système uniquement si tu veux des **sauvegardes automatiques en arrière-plan** qui continuent quand l’AppImage est fermée : planification systemd, démarrage et déclenchement après chaque snapshot Snapper.

La release fournit :

- **AppImage portable** — sauvegarde manuelle + récupération sans installation système.
- **Installateur universel `.run`** — Linux x86_64 glibc + systemd ; installe l’automatisation persistante.
- **DEB** — Debian / Ubuntu et dérivées.
- **RPM** — Fedora / famille RHEL / openSUSE.
- **SHA256SUMS** — checksums de tous les artefacts.

Installateur universel :

```bash
curl -LO https://github.com/GodsQuantum/BTRFS-Harbor/releases/download/v0.2.1/BTRFS-Harbor-0.2.1-linux-x86_64.run
chmod +x BTRFS-Harbor-0.2.1-linux-x86_64.run
./BTRFS-Harbor-0.2.1-linux-x86_64.run
```

Harbor **préfère un `btrfs-backup-ng` système compatible (>= 0.9.12)** s’il existe déjà. Sinon il utilise le moteur compatible embarqué. Harbor ne remplace plus et n’entre plus en conflit avec un moteur upstream installé par l’utilisateur.

## 🚀 Compilation depuis les sources sur CachyOS / Arch Linux

L’installation recommandée depuis les sources utilise le paquet Arch fourni par le repo :

```bash
git clone https://github.com/GodsQuantum/btrfs-harbor.git
cd btrfs-harbor
./install-cachyos.sh
```

Le script :

1. vérifie qu’il tourne sur un système utilisant pacman ;
2. installe uniquement les prérequis Arch standards ;
3. compile le `PKGBUILD` avec `makepkg` sous ton utilisateur normal ;
4. installe le paquet avec pacman ;
5. active le service natif `btrfs-harbor-agent.service` ;
6. supprime le dossier temporaire de build.

Ensuite, lance **Btrfs Harbor** depuis le menu des applications ou :

```bash
btrfs-harbor
```

> Les actions privilégiées utilisent polkit. Ne lance pas l’interface desktop en root.

## 🛟 Première sauvegarde

1. Ouvre Harbor. Il identifie la machine courante et détecte ses **subvolumes Btrfs**, ses configurations Snapper et les snapshots read-only utilisables par `btrfs send`.
2. Dans **Ce qui sera sauvegardé**, vérifie les subvolumes persistants détectés. Les libellés humains passent avant `@`, chemins et détails Snapper.
3. Choisis la destination :
   - **Dossier** — choisis un seul répertoire puis clique **Valider**. Harbor détecte local/NFS/SMB en interne sans afficher la plomberie de montage.
   - **Serveur SSH** — renseigne hôte, utilisateur, port et dossier distant, puis teste la connexion.
4. Clique **Sauvegarder maintenant**. Cela fonctionne depuis l’AppImage portable sans installer Harbor.
5. Si tu veux des sauvegardes récurrentes/en arrière-plan, choisis la fréquence puis clique **Installer Harbor pour les sauvegardes automatiques**.
6. Vérifie que le premier envoi passe à **SAUVEGARDÉ** puis **VÉRIFIÉ**.
7. Ouvre **Récupérer**, choisis un vrai snapshot sauvegardé et fais un test de restauration en staging.

Le premier lancement natif n’affiche aucune IP de démonstration, aucun dossier récent, aucun nom de machine ni chemin privé.

## 🧭 Comprendre les statuts

| État | Signification |
| --- | --- |
| **LOCAL** | Snapshot uniquement sur la machine source |
| **SAUVEGARDÉ** | La destination contient le stream |
| **VÉRIFIÉ** | Le stream raw stocké a passé la vérification |
| **COMPLET** | Envoi full indépendant |
| **INCRÉMENTAL** | Envoi basé sur une chaîne de parent valide |
| **RESTAURATION TESTÉE** | Une restauration en staging a réussi |

## ♻️ Récupérer un snapshot

L’onglet **Récupérer** lit réellement le dépôt de sauvegarde et liste les snapshots disponibles du plus récent au plus ancien. Choisis le subvolume, la destination et le snapshot exact, puis restaure-le dans un **staging Btrfs séparé**. Harbor vérifie les données reçues avant de considérer le test terminé.

- Un subvolume non-root peut être restauré en staging depuis le système en cours d’exécution.
- Harbor n’écrase jamais le `/` actif. La récupération du système/root passe volontairement par un **mode rescue/live ISO** avec le Recovery Kit.
- Le Recovery Kit conserve notamment topologie système, `fstab`/`crypttab`, configuration Snapper, inventaires de paquets et informations boot/initramfs.
- Btrfs Assistant peut ensuite servir à inspecter/rollback des snapshots Snapper locaux, mais n’est pas nécessaire pour recevoir la sauvegarde hors machine.

Un snapshot Btrfs ne contient pas automatiquement l’ESP/EFI, la table de partitions ni les secrets situés hors des subvolumes sélectionnés. Harbor affiche donc une **couverture de récupération** au lieu de promettre abusivement une restauration intégrale du PC.

## 🧬 System Replica

Harbor peut réutiliser le userspace Linux sur une autre cible sans considérer l’état matériel comme portable.

- **Machine physique** — userspace + réconciliation explicite boot/hardware.
- **VM** — userspace + adaptation au matériel virtuel.
- **LXC** — userspace uniquement ; EFI et noyau physiques sont exclus.

Pour un LXC Proxmox arrêté, Harbor peut créer un snapshot PVE de rollback, monter le rootfs cible, préserver réseau/hostname, respecter le namespace UID/GID réel du conteneur, réinitialiser machine-id et clés SSH hôte, puis rollback automatiquement en cas d’échec.

## 🛡️ Modèle de sécurité

### Pas de fallback local silencieux

Pour NFS/SMB, Harbor mémorise le point de montage **et** le serveur/share attendu. Le helper contrôle la table de montages live avant la sauvegarde. La configuration générée utilise aussi `require_mount`.

### Pas de shell root générique

Le desktop reste non privilégié. Les lectures passent par un service D-Bus système étroit. Les mutations passent uniquement par des commandes Harbor fixes et validées via polkit.

### Récupération en staging

```text
plan → vérification de chaîne → restauration en staging → vérification → réconciliation → activation
```

Harbor refuse d’écraser le root en cours d’exécution.

### Pas de secrets dans les Recovery Kits

Les Recovery Kits contiennent l’intention système et la topologie, jamais les fichiers de credentials.

## 🏗️ Architecture

```mermaid
flowchart LR
    UI["Tauri 2 + SvelteKit"] -->|D-Bus lecture seule| Agent["Rust harbor-agent"]
    UI -->|polkit + helper fixe| Ctl["Rust btrfs-harborctl"]
    Agent --> Engine["moteur btrfs-backup-ng"]
    Ctl --> Engine
    Ctl --> Systemd["timers systemd"]
    Engine --> Btrfs["Btrfs / Snapper"]
    Engine --> Raw["streams raw://"]
    Raw --> Target["NFS / SMB / filesystem local"]
    Recovery["Rust harbor-recovery"] --> Raw
```

Harbor est basé sur [berrym/btrfs-backup-ng](https://github.com/berrym/btrfs-backup-ng). Le moteur Python upstream conserve la logique sensible send/receive, rétention, metadata raw, locks et restore ; Harbor ajoute un control-plane Rust et un desktop natif.

## ⚙️ Configuration d’exemple

Une configuration totalement générique est disponible dans [examples/harbor.toml](examples/harbor.toml).

La configuration active est stockée sous :

```text
/etc/btrfs-harbor/harbor.toml
```

Les profils moteur générés sont stockés sous :

```text
/var/lib/btrfs-harbor/generated/
```

## 🧪 Développement

```bash
uv sync --frozen --extra test --extra dev
uv run --frozen pytest -q

cd harbor
cargo test --workspace --exclude btrfs-harbor-desktop --locked

cd desktop
pnpm install --frozen-lockfile
pnpm test
pnpm check
pnpm build
```

## 📦 État du projet

Btrfs Harbor est une **v0.2** publique précoce. Les chemins principaux de sauvegarde, vérification, récupération en staging et réplication LXC arrêté ont été exercés sur des environnements de test jetables.

**Ne fais jamais d’un outil de sauvegarde non stabilisé l’unique copie de données importantes.** Garde une autre sauvegarde et valide une restauration réelle.

## 🤝 Héritage upstream

Btrfs Harbor est basé sur **btrfs-backup-ng** de Michael Berry et ses contributeurs. Le moteur upstream reste sous licence MIT et son historique est préservé. Son README original est conservé dans [docs/upstream/UPSTREAM_ENGINE_README.md](docs/upstream/UPSTREAM_ENGINE_README.md).

## 📄 Licence

MIT — voir [LICENSE](LICENSE).
