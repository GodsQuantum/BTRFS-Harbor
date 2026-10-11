<script lang="ts">
	import { invoke, isTauri } from '@tauri-apps/api/core';
	import {
		chooseDestinationDirectory,
		chooseStagingDirectory,
		loadMachineSetCatalog,
		runCheckpointAction
	} from './agent';
	import type { Locale } from './i18n';
	import { isMachineSetFinished, machineBackupOrigin } from './backup-now';
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
	interface MachineSetRecord {
		set_id: string;
		status: string;
		members: number;
		created_utc?: string;
		snapshot_provider?: 'harbor' | 'snapper';
		backup_trigger?: 'manual' | 'automatic';
		source_snapshot_dates?: string[];
	}
	let machineSets: MachineSetRecord[] = [];
	let selectedSet = '';
	let machinePlan = '';
	let machineResult = '';
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
			// New whole-computer backups are machine sets and need not contain
			// any old-style per-snapshot archives. Their folder must still open.
			let legacyError = '';
			try {
				const response = await invoke<string>('archive_restore_catalog', { source: chosen });
				const items: unknown = JSON.parse(response);
				if (!Array.isArray(items)) throw new Error('Invalid backup archive catalog');
				points = items.filter(
					(item): item is ArchiveRecord =>
						item && typeof item === 'object' && typeof item.name === 'string'
				);
			} catch (reason) {
				points = [];
				legacyError = String(reason);
			}
			points.sort((a, b) => (b.created ?? '').localeCompare(a.created ?? ''));
			backupFolder = chosen;
			selected = points[0]?.name ?? '';
			let groupJSON: string;
			try {
				groupJSON = await loadMachineSetCatalog(chosen);
			} catch (reason) {
				// A broken machine manifest is a real failure; never mislabel it
				// as 'no backups'. Only legacy archives may be listed without it.
				throw new Error(
					'Cannot inspect whole-computer backup catalog: ' +
						String(reason) +
						(legacyError ? ' (legacy: ' + legacyError + ')' : ''),
					{ cause: reason }
				);
			}
			const groupData: unknown = JSON.parse(groupJSON);
			if (!Array.isArray(groupData)) throw new Error('Invalid machine-set catalog');
			machineSets = groupData.filter(
				(value): value is MachineSetRecord =>
					value !== null &&
					typeof value === 'object' &&
					typeof value.set_id === 'string' &&
					typeof value.status === 'string' &&
					typeof value.members === 'number'
			);
			machineSets.sort((a, b) => (b.created_utc ?? '').localeCompare(a.created_utc ?? ''));
			selectedSet = machineSets.find((set) => isMachineSetFinished(set.status))?.set_id ?? '';
			machinePlan = '';
			machineResult = '';
		} catch (reason) {
			error = String(reason);
		} finally {
			busy = false;
		}
	}

	async function recoverMachineSet(dryRun: boolean) {
		if (
			!isTauri() ||
			busy ||
			!backupFolder ||
			!staging ||
			!selectedSet ||
			(!dryRun && !machinePlan)
		)
			return;
		if (
			!dryRun &&
			!window.confirm(
				label(
					'Stage every Btrfs volume in this backup set? This does not make the disk bootable.',
					'Restaurer tous les volumes Btrfs dans le dossier de préparation ? Le disque ne sera pas amorçable.',
					'要恢复所有 Btrfs 卷到暂存目录吗？不会使磁盘可启动。'
				)
			)
		)
			return;
		busy = true;
		error = '';
		try {
			const output = await runCheckpointAction({
				action: 'set-restore',
				target: backupFolder,
				allow_local: true,
				set_id: selectedSet,
				staging,
				confirm: !dryRun
			});
			if (dryRun) machinePlan = output;
			else {
				machineResult = label(
					'Btrfs volumes staged. Archived EFI/boot files, if present, are staged separately. Bootloader is not installed: this is not yet a bootable recovery.',
					'Volumes Btrfs restaurés en préparation. Les fichiers EFI/boot archivés, s’ils existent, sont préparés séparément. Chargeur non installé : ce n’est pas encore un système amorçable.',
					'Btrfs 子卷已恢复至暂存目录。EFI/boot 文件（如有）另行暂存；未安装引导加载程序，尚不可直接启动。'
				);
				machinePlan = '';
			}
		} catch (reason) {
			machinePlan = '';
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
		{#if machineSets.some((item) => isMachineSetFinished(item.status))}
			<div class="recovery-section">
				<div class="field">
					<strong>{label('Restore this computer', 'Restaurer cet ordinateur', '恢复此电脑')}</strong
					>
					<p>
						{label('Latest complete backup', 'Dernière sauvegarde complète', '最近一次完整备份')}
						· {machineSets.find((item) => item.set_id === selectedSet)?.created_utc
							? new Date(
									machineSets.find((item) => item.set_id === selectedSet)?.created_utc ?? ''
								).toLocaleString(locale)
							: selectedSet.slice(0, 8)}
						· {machineBackupOrigin(
							machineSets.find((item) => item.set_id === selectedSet) ?? {},
							locale
						)}
						{#if machineSets.find((item) => item.set_id === selectedSet)?.source_snapshot_dates?.length}
							· {label('Snapshot dates', 'Dates des snapshots', '快照日期')}
							{machineSets
								.find((item) => item.set_id === selectedSet)
								?.source_snapshot_dates?.map((d) => new Date(d).toLocaleDateString(locale))
								.join(' / ')}
						{/if}
					</p>
				</div>
				{#if machineSets.filter((item) => isMachineSetFinished(item.status)).length > 1}
					<details class="recovery-advanced">
						<summary
							>{label(
								'Choose an older backup',
								'Choisir une sauvegarde plus ancienne',
								'选择旧备份'
							)}</summary
						>
						<select
							bind:value={selectedSet}
							disabled={busy}
							onchange={() => {
								machinePlan = '';
								machineResult = '';
							}}
						>
							{#each machineSets.filter( (item) => isMachineSetFinished(item.status) ) as item (item.set_id)}
								<option value={item.set_id}>
									{item.created_utc
										? new Date(item.created_utc).toLocaleString(locale)
										: item.set_id.slice(0, 8)} · {item.members} Btrfs · {machineBackupOrigin(
										item,
										locale
									)}
								</option>
							{/each}
						</select>
					</details>
				{/if}
				<p class="warning">
					{label(
						'Btrfs and archived boot/EFI files can be staged; rebuilding the disk bootloader remains manual and is not yet certified.',
						'Restauration des volumes Btrfs et archives EFI possibles dans un dossier temporaire ; le redémarrage sur disque vierge reste non certifié.',
						'可以将 Btrfs 和已存档的 EFI 文件恢复到暂存目录；重新构建启动加载器尚未验证。'
					)}
				</p>
				<button class="secondary" disabled={busy} onclick={() => void browseStaging()}>
					{label(
						'Choose empty Btrfs staging folder…',
						'Choisir un dossier Btrfs vide…',
						'选择空的 Btrfs 暂存目录…'
					)}
				</button>
				{#if staging}<code>{staging}</code>{/if}
				{#if staging}
					<button
						class="secondary"
						disabled={busy || !selectedSet}
						onclick={() => void recoverMachineSet(true)}
					>
						{label('Check all volumes', 'Vérifier tous les volumes', '检查所有卷')}
					</button>
					<button
						class="primary"
						disabled={busy || !machinePlan}
						onclick={() => void recoverMachineSet(false)}
					>
						{label(
							'Stage Btrfs and boot files',
							'Restaurer Btrfs et fichiers de démarrage dans le dossier temporaire',
							'恢复 Btrfs 与启动文件'
						)}
					</button>
				{/if}
				{#if machinePlan}<pre class="plan">{machinePlan}</pre>{/if}
				{#if machineResult}<p role="status">{machineResult}</p>{/if}
			</div>
		{/if}
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
