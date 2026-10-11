/** Prefer a durable unfinished transaction to a new machine backup. */
export type BackupNowChoice =
	| { kind: 'start' }
	| { kind: 'resume-set'; id: string }
	| { kind: 'resume-stream'; id: string }
	| { kind: 'choose-set' }
	| { kind: 'choose-stream' };

export function isMachineSetFinished(status: string): boolean {
	return status === 'completed_btrfs_only' || status === 'completed_btrfs_and_boot_files';
}

export function chooseBackupNow(
	sets: ReadonlyArray<{ set_id: string; status: string }>,
	transfers: ReadonlyArray<{ transfer_id: string; resumable: boolean }>
): BackupNowChoice {
	const pendingSets = sets.filter((item) => !isMachineSetFinished(item.status));
	if (pendingSets.length > 1) return { kind: 'choose-set' };
	if (pendingSets.length === 1) return { kind: 'resume-set', id: pendingSets[0].set_id };
	const pendingStreams = transfers.filter((item) => item.resumable);
	if (pendingStreams.length > 1) return { kind: 'choose-stream' };
	if (pendingStreams.length === 1)
		return { kind: 'resume-stream', id: pendingStreams[0].transfer_id };
	return { kind: 'start' };
}

/** Display historical source and trigger separately; never invent an automated backup. */
export function machineBackupOrigin(
	record: {
		snapshot_provider?: 'harbor' | 'snapper' | 'mixed';
		backup_trigger?: 'manual' | 'automatic';
	},
	locale: 'fr' | 'en' | 'zh-CN'
): string {
	if (record.snapshot_provider === 'snapper') return 'Snapper';
	if (record.snapshot_provider === 'mixed')
		return locale === 'fr'
			? 'Snapper + Harbor'
			: locale === 'zh-CN'
				? 'Snapper + Harbor'
				: 'Snapper + Harbor';
	if (record.backup_trigger === 'automatic')
		return locale === 'fr'
			? 'Harbor automatique'
			: locale === 'zh-CN'
				? 'Harbor 自动'
				: 'Harbor automatic';
	return locale === 'fr' ? 'Harbor manuel' : locale === 'zh-CN' ? 'Harbor 手动' : 'Harbor manual';
}
