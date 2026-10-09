<script lang="ts">
	import { invoke, isTauri } from '@tauri-apps/api/core';
	import { chooseDestinationDirectory, chooseStagingDirectory } from './agent';
	import type { Locale } from './i18n';
	export let locale: Locale;

	interface ArchiveRecord {
		name: string;
		created?: string;
		size?: number;
		parent_name?: string | null;
		stream_completeness?: string;
		checksum?: { value?: string | null; algorithm?: string };
	}

	let backupFolder = '';
	let staging = '';
	let points: ArchiveRecord[] = [];
	let selected = '';
	let plan = '';
	let result = '';
	let error = '';
	let busy = false;
	$: current = points.find((point) => point.name === selected);
	const label = (en: string, fr: string, zh: string) =>
		locale === 'fr' ? fr : locale === 'zh-CN' ? zh : en;

	async function browseBackups() {
		if (busy) return;
		error = '';
		plan = '';
		result = '';
		try {
			const chosen = await chooseDestinationDirectory(backupFolder || undefined);
			if (!chosen) return;
			busy = true;
			const response = await invoke<string>('archive_restore_catalog', { source: chosen });
			const items: unknown = JSON.parse(response);
			if (!Array.isArray(items)) throw new Error('Invalid backup archive catalog');
			points = items.filter(
				(item): item is ArchiveRecord =>
					item && typeof item === 'object' && typeof item.name === 'string'
			);
			points.sort((a, b) => (b.created ?? '').localeCompare(a.created ?? ''));
			backupFolder = chosen;
			selected = points[0]?.name ?? '';
		} catch (reason) {
			error = String(reason);
		} finally {
			busy = false;
		}
	}

	async function browseStaging() {
		if (busy) return;
		error = '';
		plan = '';
		try {
			const chosen = await chooseStagingDirectory(staging || undefined);
			if (chosen) staging = chosen;
		} catch (reason) {
			error = String(reason);
		}
	}

	async function restore(dryRun: boolean) {
		if (!isTauri() || busy || !backupFolder || !staging || !selected || (!dryRun && !plan)) return;
		if (
			!dryRun &&
			!window.confirm(
				label(
					'Receive this backup into the chosen Btrfs staging directory? Existing systems will not be overwritten.',
					'Recevoir cette sauvegarde dans le dossier Btrfs de préparation ? Aucun système existant ne sera écrasé.',
					'确认将备份恢复到指定的 Btrfs 暂存目录？不会覆盖现有系统。'
				)
			)
		)
			return;
		busy = true;
		error = '';
		result = '';
		try {
			const output = await invoke<string>('archive_restore_staged', {
				source: backupFolder,
				staging,
				snapshotName: selected,
				dryRun
			});
			if (dryRun)
				plan =
					output || label('Recovery plan ready', 'Plan de récupération prêt', '恢复计划已准备');
			else {
				result =
					output ||
					label(
						'Staged restore completed',
						'Restauration dans le dossier de préparation terminée',
						'暂存恢复已完成'
					);
				plan = '';
			}
		} catch (reason) {
			plan = '';
			error = String(reason);
		} finally {
			busy = false;
		}
	}
</script>

<article class="panel portable-recovery">
	<h3>
		{label('Recover from destination backup', 'Restaurer depuis une sauvegarde', '从备份目标恢复')}
	</h3>
	<div class="recovery-section">
		<button class="primary" disabled={busy} onclick={() => void browseBackups()}>
			{label('Choose backup folder…', 'Choisir le dossier de sauvegardes…', '选择备份目录…')}
		</button>
		{#if backupFolder}<code>{backupFolder}</code>{/if}
	</div>
	{#if backupFolder}
		{#if points.length > 0}
			<label class="field">
				{label('Recovery point', 'Point de restauration', '恢复点')}
				<select
					bind:value={selected}
					disabled={busy}
					onchange={() => {
						plan = '';
						result = '';
					}}
				>
					{#each points as point (point.name)}
						<option value={point.name}
							>{point.created ? new Date(point.created).toLocaleString(locale) : point.name} · {point.name}</option
						>
					{/each}
				</select>
			</label>
			{#if current && !current.checksum?.value}
				<p class="warning" role="status">
					{label(
						'Legacy archive: SHA-256 verification cannot be confirmed.',
						'Archive ancienne : vérification SHA-256 non garantie.',
						'旧备份：无法保证 SHA-256 校验。'
					)}
				</p>
			{/if}
			<div class="recovery-section">
				<button class="secondary" disabled={busy} onclick={() => void browseStaging()}>
					{label(
						'Choose Btrfs recovery folder…',
						'Choisir le dossier Btrfs de restauration…',
						'选择 Btrfs 恢复目录…'
					)}
				</button>
				{#if staging}<code>{staging}</code>{/if}
			</div>
			{#if staging}
				<div class="recovery-section">
					<button class="secondary" disabled={busy} onclick={() => void restore(true)}>
						{label('Check recovery', 'Vérifier la restauration', '检查恢复')}
					</button>
					<button class="primary" disabled={busy || !plan} onclick={() => void restore(false)}>
						{label('Restore to staging', 'Restaurer dans le dossier', '恢复到暂存目录')}
					</button>
				</div>
			{/if}
			{#if plan}
				<pre class="plan">{plan}</pre>
			{/if}
			{#if result}<p role="status">{result}</p>{/if}
			<p class="warning">
				{label(
					'System backups are staged safely. Bootable disk replacement (partitions, EFI and bootloader) still requires separate recovery validation.',
					'La sauvegarde système est restaurée dans un dossier de préparation. Le remplacement amorçable du disque (partitions, EFI et chargeur de démarrage) demande encore une validation distincte.',
					'系统备份仅恢复到暂存目录。可启动磁盘替换（分区、EFI、引导程序）仍需单独验证。'
				)}
			</p>
		{:else}
			<p>
				{label(
					'No complete archive found in this directory. Select the folder containing .btrfs.zst and its .meta file.',
					'Aucune archive trouvée ici. Choisir le dossier contenant les fichiers .btrfs.zst et .meta.',
					'此目录中未发现备份。请选择包含 .btrfs.zst 和 .meta 的目录。'
				)}
			</p>
		{/if}
	{/if}
	{#if error}<p class="warning" role="alert">{error}</p>{/if}
</article>

<style>
	.portable-recovery {
		display: grid;
		gap: 18px;
		padding: 22px;
	}
	.portable-recovery h3 {
		margin: 0;
		font-size: 22px;
	}
	.recovery-section {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 12px;
	}
	.field {
		display: grid;
		gap: 8px;
		font-weight: 650;
	}
	.field select {
		width: 100%;
		padding: 12px;
		color: var(--text);
		background: var(--surface);
		border: 1px solid var(--border);
		border-radius: 8px;
	}
	code {
		font-size: 13px;
		overflow-wrap: anywhere;
	}
	.warning {
		font-size: 13px;
		line-height: 1.5;
		color: var(--warning, #c28a31);
	}
	.plan {
		max-height: 230px;
		overflow: auto;
		overflow-wrap: anywhere;
		font-size: 12px;
		padding: 12px;
		border-radius: 8px;
		background: var(--surface);
	}
</style>
