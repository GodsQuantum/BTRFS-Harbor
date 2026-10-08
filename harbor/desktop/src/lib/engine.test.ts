import { describe, expect, it } from 'vitest';
import {
	engineHeadline,
	engineUpdatePresentation,
	type EngineSelectionStatus,
	type EngineUpdateOptions
} from './engine';

function bundledStatus(): EngineSelectionStatus {
	return {
		policy: 'auto',
		active: {
			executable: '/app/portable/btrfs-backup-ng',
			origin: 'bundled',
			version: '0.9.12'
		},
		system: null,
		bundled: {
			executable: '/app/portable/btrfs-backup-ng',
			origin: 'bundled',
			version: '0.9.12',
			compatible: true
		},
		minimum_version: '0.9.12',
		fallback_reason: 'system engine not found; using Harbor bundled engine'
	};
}

describe('engine presentation', () => {
	it('shows the bundled engine before any backup starts', () => {
		expect(engineHeadline(bundledStatus())).toBe('Harbor bundled 0.9.12 · active');
	});

	it('shows the exact system path when the system engine is active', () => {
		const status = bundledStatus();
		status.active = {
			executable: '/usr/bin/btrfs-backup-ng',
			origin: 'system',
			version: '0.9.12'
		};
		status.system = {
			executable: '/usr/bin/btrfs-backup-ng',
			origin: 'system',
			version: '0.9.12',
			compatible: true
		};
		expect(engineHeadline(status)).toBe('System 0.9.12 · /usr/bin/btrfs-backup-ng · active');
	});

	it('explains stale-system fallback without hiding the discovered system engine', () => {
		const status = bundledStatus();
		status.system = {
			executable: '/usr/bin/btrfs-backup-ng',
			origin: 'system',
			version: '0.9.8',
			compatible: false
		};
		expect(engineHeadline(status)).toBe(
			'Harbor bundled 0.9.12 · active · system 0.9.8 incompatible'
		);
		expect(status.system.compatible).toBe(false);
	});
});

describe('engine update presentation', () => {
	it('updates bundled engine by updating Harbor itself', () => {
		const options: EngineUpdateOptions = {
			strategy: 'harbor_application',
			can_update: true,
			package: null,
			provenance: 'bundled_harbor',
			reason: 'Bundled engine is updated with Btrfs Harbor.'
		};
		expect(engineUpdatePresentation(options)).toEqual({
			label: 'Update Btrfs Harbor',
			actionable: true
		});
	});

	it('does not offer destructive update for unknown manual system engine', () => {
		const options: EngineUpdateOptions = {
			strategy: 'guidance_only',
			can_update: false,
			package: null,
			provenance: 'manual_unknown',
			reason: 'Manual engine ownership is unknown.'
		};
		expect(engineUpdatePresentation(options)).toEqual({
			label: 'Review update instructions',
			actionable: false
		});
	});
});
