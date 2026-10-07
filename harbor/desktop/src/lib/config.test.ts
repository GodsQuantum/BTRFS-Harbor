import { describe, expect, it } from 'vitest';
import {
	backupSourceFromDiscovery,
	createDefaultConfiguration,
	describeDraftIssue,
	draftIssues,
	formatSshDestination,
	parseSshDestination,
	sourceDisplayName,
	resolveDestination,
	resolveProfile,
	setSourceEnabled
} from './config';

const ids = ['11111111-1111-4111-8111-111111111111', '22222222-2222-4222-8222-222222222222'];
const uuidFactory = () => ids.shift() ?? '33333333-3333-4333-8333-333333333333';

describe('Harbor profile editor model', () => {
	it('gives common Btrfs sources human names before technical paths', () => {
		expect(sourceDisplayName('/')).toBe('System');
		expect(sourceDisplayName('/home')).toBe('Personal files');
		expect(sourceDisplayName('/srv')).toBe('Data');
		expect(sourceDisplayName('/var/lib/libvirt')).toBe('libvirt');
	});

	it('round-trips structured SSH destination fields', () => {
		const endpoint = formatSshDestination({
			user: 'backup',
			host: '10.0.0.5',
			port: 2222,
			path: '/backups/workstation'
		});
		expect(endpoint).toBe('ssh://backup@10.0.0.5:2222/backups/workstation');
		expect(parseSshDestination(endpoint)).toEqual({
			user: 'backup',
			host: '10.0.0.5',
			port: 2222,
			path: '/backups/workstation'
		});
	});

	it('creates a conservative recovery draft without inventing a network target', () => {
		const config = createDefaultConfiguration(uuidFactory);
		const profile = resolveProfile(config);
		const destination = resolveDestination(config, profile);

		expect(profile.sources).toEqual([]);
		expect(profile.retention).toEqual({ hourly: 0, daily: 7, weekly: 4, monthly: 3, yearly: 0 });
		expect(destination.kind).toBe('raw');
		expect(destination.path).toBe('');
		expect(destination.mount_point).toBeNull();
		expect(destination.expected_mount_source).toBeNull();
	});

	it('turns internal draft issue tokens into actionable user guidance', () => {
		expect(describeDraftIssue('sources')).toEqual({
			section: 'sources',
			messageKey: 'validationSourcesRequired'
		});
		expect(describeDraftIssue('destination_path')).toEqual({
			section: 'destination',
			messageKey: 'validationDestinationRequired'
		});
		expect(describeDraftIssue('mount_point')).toEqual({
			section: 'destination',
			messageKey: 'validationDestinationUnavailable'
		});
	});

	it('starts incomplete until live discovery and destination selection are provided', () => {
		const config = createDefaultConfiguration(uuidFactory);
		const profile = resolveProfile(config);

		expect(draftIssues(config, profile)).toEqual(['sources', 'destination_path']);
	});

	it('reports required NFS identity fields after a real source is selected', () => {
		const config = createDefaultConfiguration(uuidFactory);
		const profile = resolveProfile(config);
		const destination = resolveDestination(config, profile);
		profile.sources.push({
			path: '/',
			snapshot_prefix: 'root-',
			snapper_config: 'root',
			target_subdir: 'rootfs'
		});
		destination.kind = 'nfs';

		expect(draftIssues(config, profile)).toEqual([
			'destination_path',
			'mount_point',
			'expected_mount_source'
		]);
	});

	it('can disable and re-enable a recommended source without duplicates', () => {
		const config = createDefaultConfiguration(uuidFactory);
		let profile = resolveProfile(config);

		profile = setSourceEnabled(profile, '/srv', false);
		expect(profile.sources.some((source) => source.path === '/srv')).toBe(false);

		profile = setSourceEnabled(profile, '/srv', true);
		expect(profile.sources.filter((source) => source.path === '/srv')).toHaveLength(1);
		expect(profile.sources.find((source) => source.path === '/srv')?.target_subdir).toBe('srv');
	});

	it('builds a source from live discovery without losing unicode or Snapper metadata', () => {
		const source = backupSourceFromDiscovery({
			mount_point: '/srv/Données',
			source: '/dev/mapper/root[/@Données]',
			subvolume: '@Données',
			snapper_config: 'donnees',
			hint: 'optional'
		});

		expect(source).toEqual({
			path: '/srv/Données',
			snapshot_prefix: 'srv-Données-',
			snapper_config: 'donnees',
			target_subdir: 'srv-Données'
		});
	});

	it('can enable a discovered source outside the built-in recommended paths', () => {
		const config = createDefaultConfiguration(uuidFactory);
		let profile = resolveProfile(config);
		const template = backupSourceFromDiscovery({
			mount_point: '/var/lib/libvirt',
			source: '/dev/mapper/root[/@libvirt]',
			subvolume: '@libvirt',
			snapper_config: null,
			hint: 'optional'
		});

		profile = setSourceEnabled(profile, template.path, true, template);

		expect(profile.sources.find((source) => source.path === '/var/lib/libvirt')).toEqual(template);
	});
});
