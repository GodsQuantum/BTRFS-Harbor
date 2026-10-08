import { describe, expect, it } from 'vitest';
import {
	backupProgressPercent,
	backupProgressPresentation,
	runtimeRepresentsScheduledJob,
	type BackupProgressEvent,
	type ProfileRuntime
} from './status';

function event(phase: string, kind: BackupProgressEvent['event'] = 'phase'): BackupProgressEvent {
	return { event: kind, phase, message: phase };
}

function runtime(overrides: Partial<ProfileRuntime> = {}): ProfileRuntime {
	return {
		profile_id: '11111111-1111-4111-8111-111111111111',
		timer_unit: 'btrfs-harbor.timer',
		service_unit: 'btrfs-harbor.service',
		timer_installed: false,
		timer_enabled: false,
		timer_active: false,
		timer_sub_state: null,
		next_elapse_realtime: null,
		last_trigger: null,
		service_active: false,
		service_sub_state: null,
		service_result: null,
		service_exit_status: null,
		...overrides
	};
}

describe('backup progress presentation', () => {
	it('moves monotonically through Harbor backup phases', () => {
		const phases = [
			'start',
			'prepare',
			'engine',
			'mount_guard',
			'backup',
			'verify',
			'recovery_kit',
			'complete'
		];
		const values = phases.map((phase) => backupProgressPercent(event(phase)));
		expect(values).toEqual([2, 8, 12, 20, 65, 86, 96, 100]);
	});

	it('marks a finished backup complete even when the final phase name changes', () => {
		expect(backupProgressPercent(event('anything', 'finished'))).toBe(100);
	});

	it('keeps per-volume engine completion output technical while the global backup is still running', () => {
		const progress: BackupProgressEvent = {
			event: 'phase',
			phase: 'backup',
			message: 'Running btrfs-backup-ng'
		};
		expect(backupProgressPresentation(progress, 'Transfer completed successfully')).toEqual({
			primary: 'Running btrfs-backup-ng',
			technical: 'Transfer completed successfully',
			complete: false,
			indeterminate: true
		});
	});

	it('only marks the global backup complete on a finished event', () => {
		const progress: BackupProgressEvent = {
			event: 'finished',
			phase: 'complete',
			message: 'Profile Workstation-A completed successfully'
		};
		expect(backupProgressPresentation(progress, 'Transfer completed successfully')).toEqual({
			primary: 'Profile Workstation-A completed successfully',
			technical: '',
			complete: true,
			indeterminate: false
		});
	});

	it('treats the transfer stage as indeterminate until real byte telemetry exists', () => {
		expect(backupProgressPresentation(event('backup'), '').indeterminate).toBe(true);
		expect(backupProgressPresentation(event('complete', 'finished'), '').indeterminate).toBe(false);
	});
});

describe('scheduled job visibility', () => {
	it('does not treat a manual profile as scheduled when no timer is installed', () => {
		expect(runtimeRepresentsScheduledJob(null)).toBe(false);
		expect(runtimeRepresentsScheduledJob(runtime())).toBe(false);
	});

	it('treats an installed timer as a scheduled job even when currently disabled', () => {
		expect(runtimeRepresentsScheduledJob(runtime({ timer_installed: true }))).toBe(true);
	});
});
