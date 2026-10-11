import { describe, expect, it } from 'vitest';
import { render } from 'svelte/server';
import CheckpointControls from './CheckpointControls.svelte';
import type { BackupProfile, DestinationSpec } from './config';

const profile: BackupProfile = {
	id: '7e783c0c-2f69-4f3d-97dd-e3c18bf8c65b',
	name: 'This computer',
	sources: [
		{ path: '/', target_subdir: 'rootfs', snapshot_prefix: 'root-', snapper_config: 'root' },
		{ path: '/home', target_subdir: 'home', snapshot_prefix: 'home-', snapper_config: null },
		{
			path: '/root',
			target_subdir: 'root-home',
			snapshot_prefix: 'root-home-',
			snapper_config: null
		},
		{ path: '/srv', target_subdir: 'srv', snapshot_prefix: 'srv-', snapper_config: null }
	],
	destination_ids: [],
	on_calendar: '*-*-* 02:00:00',
	retention: { hourly: 0, daily: 7, weekly: 4, monthly: 6, yearly: 0 },
	verify_after_backup: true
};
const destination: DestinationSpec = {
	id: '1bc47b13-57e9-4285-b1d7-0e7e9f385f6a',
	name: 'Cloud9',
	kind: 'nfs',
	path: '/mnt/Cloud9/backup-local/Workstations/Pegasus',
	mount_point: '/mnt/Cloud9',
	expected_mount_source: 'test-server:/data',
	compression: 'zstd',
	optional: false
};
const renderCard = (withSources = true) =>
	render(CheckpointControls, {
		props: {
			profile: withSources ? profile : { ...profile, sources: [] },
			destination,
			locale: 'fr',
			onChooseDestination: async () => {}
		}
	}).body;

describe('One-button whole-computer backup UX', () => {
	it('defaults to complete machine backup instead of exposing per-source choice', () => {
		const body = renderCard();
		expect(body).toContain('Sauvegarder cet ordinateur');
		expect(body).toContain('Sauvegarder maintenant');
		expect(body).toContain('Créer une nouvelle capture Harbor');
		expect(body).toContain('Réutiliser les derniers snapshots Snapper');
		expect(body).toContain('value="harbor"');
		expect(body).toContain('value="snapper"');
		expect(body).toContain('data-testid="backup-now-primary"');
		expect(body).toContain('Où enregistrer la sauvegarde');
		expect(body).toContain('/mnt/Cloud9/backup-local/Workstations/Pegasus');
		expect(body).not.toContain('/mnt/Cloud9/backup-local/Workstations/Pegasus/rootfs');
		expect(body).not.toContain('Non inclus dans cet envoi');
		expect(body).not.toContain('Sauvegarder tous les volumes Btrfs');
		expect(body).not.toContain('Envoyer un snapshot</h3>');
		expect(body).toContain('Expert : envoyer un snapshot individuel');
	});
	it('still offers machine backup when no individual source is configured', () => {
		const body = renderCard(false);
		expect(body).toContain('Sauvegarder cet ordinateur');
		expect(body).toContain('data-testid="backup-now-primary"');
		expect(body).toContain('Aucune source individuelle configurée');
	});
	it('rejects presenting direct SSH as a working whole-computer backup', () => {
		const body = render(CheckpointControls, {
			props: {
				profile,
				destination: { ...destination, kind: 'ssh' as const, path: 'ssh://host/backups' },
				locale: 'fr' as const,
				onChooseDestination: async () => {}
			}
		}).body;
		expect(body).toContain('Destination SSH directe non disponible');
		expect(body).not.toContain('data-testid="backup-now-primary"');
	});
});
