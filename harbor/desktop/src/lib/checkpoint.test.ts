import { describe, expect, it } from 'vitest';
import {
	checkpointStateLabel,
	parseHostSnapperChoices,
	checkpointStatusIsVerified,
	type CheckpointTransfer
} from './checkpoint';

const paused: CheckpointTransfer = {
	transfer_id: '3b88e8c1-5cb8-4f67-aa10-f9544b6228f0',
	state: 'paused',
	source: '/home',
	checkpoint_count: 2,
	committed_raw_bytes: 268435456,
	committed_compressed_bytes: 84000000,
	last_checkpoint_at: '2026-10-09T00:00:00Z',
	resumable: true,
	verified: false
};

describe('Checkpoint timeline', () => {
	it('labels a paused transfer without claiming restoration has passed', () => {
		expect(checkpointStateLabel(paused.state, 'fr')).toBe('En pause');
		expect(checkpointStatusIsVerified(paused)).toBe(false);
	});
	it('does not mistake publication for a tested restore', () => {
		const published = { ...paused, state: 'completed', resumable: false } as CheckpointTransfer;
		expect(checkpointStatusIsVerified(published)).toBe(false);
		expect(checkpointStateLabel('completed', 'en')).toContain('not restore-tested');
	});
	it('has translations for paused, replay and interruption', () => {
		expect(checkpointStateLabel('replaying', 'zh-CN')).toContain('重新');
		expect(checkpointStateLabel('failed_resumable', 'fr')).toContain('reprenable');
	});
});

describe('Native host Snapper snapshot picker', () => {
	const json = JSON.stringify({
		configs: [
			{
				name: 'root',
				snapshots: [
					{ number: 41, date: '2026-10-09T09:00:00Z', type: 'post', description: 'After update' },
					{ number: 40, date: '2026-10-09T08:00:00Z', type: 'pre', description: 'Before update' },
					{ number: 0, date: '2026-10-09T07:00:00Z', type: 'single', description: 'Current' },
					{ number: -1, date: '2026-10-09T07:00:00Z', type: 'single', description: 'Invalid' }
				]
			}
		]
	});
	it('uses the real host list and sorts latest-first without inventing snapshots', () => {
		expect(parseHostSnapperChoices(json, 'root').map((s) => s.number)).toEqual([41, 40]);
		expect(parseHostSnapperChoices(json, 'home')).toEqual([]);
	});
	it('rejects malformed responses instead of displaying fake numbers', () => {
		expect(() => parseHostSnapperChoices('{}', 'root')).toThrow();
		expect(() => parseHostSnapperChoices('{"configs":{}}', 'root')).toThrow();
	});
});
