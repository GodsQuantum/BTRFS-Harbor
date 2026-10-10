/** Prefer a durable unfinished transaction to a new machine backup. */
export type BackupNowChoice =
	| { kind: 'start' }
	| { kind: 'resume-set'; id: string }
	| { kind: 'resume-stream'; id: string }
	| { kind: 'choose-set' }
	| { kind: 'choose-stream' };

export function chooseBackupNow(
	sets: ReadonlyArray<{ set_id: string; status: string }>,
	transfers: ReadonlyArray<{ transfer_id: string; resumable: boolean }>
): BackupNowChoice {
	const pendingSets = sets.filter((item) => item.status !== 'completed_btrfs_only');
	if (pendingSets.length > 1) return { kind: 'choose-set' };
	if (pendingSets.length === 1) return { kind: 'resume-set', id: pendingSets[0].set_id };
	const pendingStreams = transfers.filter((item) => item.resumable);
	if (pendingStreams.length > 1) return { kind: 'choose-stream' };
	if (pendingStreams.length === 1)
		return { kind: 'resume-stream', id: pendingStreams[0].transfer_id };
	return { kind: 'start' };
}
