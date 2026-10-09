/**
 * v2 checkpoint preview. The manifest is not an integrity/restore proof;
 * never turn a transfer state into a "verified backup" UI badge.
 */
export interface CheckpointTransfer {
	transfer_id: string;
	name?: string | null;
	state: string;
	source: string;
	checkpoint_count: number;
	committed_raw_bytes: number;
	committed_compressed_bytes: number;
	last_checkpoint_at: string | null;
	resumable: boolean;
	verified: false;
}

export function checkpointStateLabel(state: string, locale: 'en' | 'fr' | 'zh-CN'): string {
	const labels: Record<string, [string, string, string]> = {
		preparing: ['Preparing', 'Préparation', '准备中'],
		uploading: ['Sending', 'Envoi', '发送中'],
		pause_requested: ['Pause requested', 'Pause demandée', '已请求暂停'],
		paused: ['Paused', 'En pause', '已暂停'],
		replaying: ['Rechecking source', 'Relecture de la source', '重新核对来源'],
		failed_resumable: ['Interrupted — resumable', 'Interrompu — reprenable', '已中断，可续传'],
		finalizing: ['Finalizing', 'Finalisation', '完成中'],
		completed: [
			'Published — not restore-tested',
			'Publié — restauration non testée',
			'已发布，尚未验证恢复'
		]
	};
	const index = locale === 'fr' ? 1 : locale === 'zh-CN' ? 2 : 0;
	return labels[state]?.[index] ?? state;
}

export function checkpointStatusIsVerified(record: CheckpointTransfer): boolean {
	// Not even "completed" proves btrfs receive succeeded. This type is an
	// intentionally unverified status view of the current manifest.
	return record.verified;
}

export interface SnapperSnapshotChoice {
	number: number;
	type: string;
	date: string;
	description: string;
}

/** Parse existing host Snapper JSON; never synthesize a snapshot number. */
export function parseHostSnapperChoices(json: string, configName: string): SnapperSnapshotChoice[] {
	const payload: unknown = JSON.parse(json);
	if (!payload || typeof payload !== 'object' || !('configs' in payload)) {
		throw new Error('Host Snapper did not return a configuration list');
	}
	const configs = (payload as { configs: unknown }).configs;
	if (!Array.isArray(configs)) throw new Error('Invalid Snapper configuration list');
	const selected = configs.find((entry) => entry?.name === configName);
	if (!selected) return [];
	if (!Array.isArray(selected.snapshots)) throw new Error('Invalid host snapshot list');
	return selected.snapshots
		.filter((entry: unknown): entry is SnapperSnapshotChoice => {
			if (!entry || typeof entry !== 'object') return false;
			const item = entry as Record<string, unknown>;
			return (
				Number.isSafeInteger(item.number) &&
				(item.number as number) > 0 &&
				typeof item.date === 'string' &&
				typeof item.description === 'string' &&
				typeof item.type === 'string'
			);
		})
		.sort((a: SnapperSnapshotChoice, b: SnapperSnapshotChoice) => b.number - a.number);
}
