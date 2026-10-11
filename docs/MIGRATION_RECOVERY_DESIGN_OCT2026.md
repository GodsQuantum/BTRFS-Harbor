# Btrfs Harbor : deux sources de snapshots et migration entre PC

## Livré dans le code preview après validation

Option A : nouvelle capture Harbor : photographie en lecture seule des Btrfs système montés.
Option B : derniers snapshots Snapper : sélectionne le dernier snapshot stable,
en lecture seule, de CHAQUE sous-volume Btrfs système monté. Aucun snapshot
Harbor n'est créé dans ce mode ; si un sous-volume est sans Snapper
admissible, échec explicite de la sauvegarde groupée avant le transfert.
La date Snapper peut différer entre / et /home : ce n'est pas un instantané
atomique à une même heure.

Les deux modes utilisent machine-set v2 et checkpoint-v2 ; les sauvegardes
sont restaurables depuis leur destination indépendamment de Snapper sur
le système de récupération. Origine de source et date Snapper conservées
dans le catalogue ; sauvegardes déclenchées manuellement marquées manual.
Snapper est protégé temporairement des nettoyages durant le transfert par
le mécanisme existant de pin durable. Ce n'est PAS la copie du journal
historique complet de Snapper, seulement le dernier point admissible
par sous-volume sélectionné.

## Futur : SSD plus grand ou plus petit et ISO ReaR

ReaR est démarré depuis une clé USB dédiée, pas depuis le disque cible.
Pour un nouveau NVMe 2 To à la place du SSD original plus petit :
- vérifier by-id et serial du SSD cible, sa capacité, sa table de partitions
  et interdire absolument d'effacer le disque qui conserve les sauvegardes ;
- montrer un plan des partitions et obtenir une confirmation explicite ;
- ReaR en MIGRATION_MODE peut agrandir la dernière partition admissible,
  mais il ne garantit pas l'extension automatique du système de fichiers ;
- quand la partition Btrfs a réellement été agrandie, étendre Btrfs
  séparément et vérifier ensuite la capacité effective de 2 To ;
- refuser une conversion automatique non qualifiée si LUKS, plusieurs OS,
  ordre de partitions incompatible ou disque cible ambigu.
Ce workflow complet n'est PAS encore implémenté ni certifié.
Sources : https://github.com/rear/rear/blob/master/usr/share/rear/conf/default.conf
https://btrfs.readthedocs.io/en/stable/btrfs-filesystem.html

## Futur : clone de Pegasus vers un autre ordinateur CachyOS

Deux parcours doivent exister, avec intégrité du système cible :
1. Restaurer le système d'origine sur disque nouveau, avec UUID, EFI,
   initramfs et bootloader adaptés.
2. Migrer les données, paquets et paramètres depuis un backup Pegasus vers
   un CachyOS fraîchement installé sur un autre PC. Importer les comptes,
   UID/GID compatibles, HOME/KDE et préférences applicatives ; NE PAS
   écraser aveuglément les fichiers de démarrage, fichiers du gestionnaire
   de paquets ni tout /etc de la nouvelle installation.

Si les deux ordinateurs fonctionnent en parallèle, le clone doit disposer
d'un nouveau hostname ET d'un nouvel identifiant systemd machine-id,
de nouvelles clés SSH hôte, de paramètres réseau propres et d'une identité
distincte pour les services à identifiants machine (Tailscale/WireGuard,
certificats, caches DHCP, etc.). Certains logiciels doivent être reconfigurés.
Ne pas modifier les identités de Pegasus pendant la préparation du clone.
Référence : https://systemd.io/BUILDING_IMAGES/

ACTUELLEMENT : récupération du machine-set en répertoire Btrfs temporaire,
pas fusion automatique CachyOS ni clone universel démarrable. Les fonctions
d'agrandissement et de clonage requièrent d'abord une matrice de tests sur
disques virtuels jetables et des confirmations explicites.
