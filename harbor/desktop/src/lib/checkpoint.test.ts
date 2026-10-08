import { describe, expect, it } from 'vitest';
import {
	checkpointStateLabel,
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
