import { describe, expect, it } from 'vitest';
import { chooseBackupNow } from './backup-now';

describe('Back up now - durable selection', () => {
	it('starts when there is no unfinished transaction', () => {
		expect(chooseBackupNow([{ set_id: 'old', status: 'completed_btrfs_only' }], [])).toEqual({
			kind: 'start'
		});
	});
	it('resumes a machine set rather than starting a duplicate', () => {
		expect(chooseBackupNow([{ set_id: 'a', status: 'pending' }], [])).toEqual({
			kind: 'resume-set',
			id: 'a'
		});
	});
	it('does not guess between two unfinished machine sets', () => {
		expect(
			chooseBackupNow(
				[
					{ set_id: 'a', status: 'sending' },
					{ set_id: 'b', status: 'pending' }
				],
				[]
			)
		).toEqual({ kind: 'choose-set' });
	});
	it('resumes a single source checkpoint if no machine set is incomplete', () => {
		expect(chooseBackupNow([], [{ transfer_id: 'stream', resumable: true }])).toEqual({
			kind: 'resume-stream',
			id: 'stream'
		});
	});
	it('does not guess between multiple source checkpoints', () => {
		expect(
			chooseBackupNow(
				[],
				[
					{ transfer_id: 'a', resumable: true },
					{ transfer_id: 'b', resumable: true }
				]
			)
		).toEqual({ kind: 'choose-stream' });
	});
	it('prefers a machine-set resume to an interrupted individual snapshot', () => {
		expect(
			chooseBackupNow(
				[{ set_id: 'machine', status: 'sending' }],
				[{ transfer_id: 'stream', resumable: true }]
			)
		).toEqual({ kind: 'resume-set', id: 'machine' });
	});
});
