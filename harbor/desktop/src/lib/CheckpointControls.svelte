<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { isTauri } from '@tauri-apps/api/core';
	import {
		listHostSnapperSnapshots,
		loadCheckpointTransfers,
		runCheckpointAction,
		type SnapperSnapshotChoice
	} from './agent';
	import { checkpointStateLabel, type CheckpointTransfer } from './checkpoint';
	import { formatBytesBinary } from './status';
	import { checkpointTargetPath, type BackupProfile, type DestinationSpec } from './config';
	import type { Locale } from './i18n';

	export let profile: BackupProfile;
	export let destination: DestinationSpec;
	export let locale: Locale = 'en';

	const translation: Record<Locale, string[]> = {
		en: [
			'Resumable Btrfs backup',
			'This feature is experimental; keep verified backups. Full Btrfs/NFS restore tests are pending.',
			'Send latest',
			'Refresh',
			'Resume',
			'Pause',
			'Stop',
			'Discard partial',
			'Snapshot source',
			'Source mode',
			'Latest stable',
			'Specific number',
			'Create snapshot',
			'Snapshot number',
			'Sending · You can close the window (system tray)',
			'No checkpoint transfers',
			'Confirm discarding this unfinished transfer and releasing its Snapper pin?'
		],
		fr: [
			'Sauvegarde Btrfs reprenable',
			'Conservez vos sauvegardes validées. Les essais Btrfs/NFS complets restent à faire.',
			'Envoyer le dernier',
			'Actualiser',
			'Reprendre',
			'Pause',
			'Arrêter',
			'Abandonner le partiel',
			'Source Snapper',
			'Mode',
			'Dernier stable',
			'Numéro précis',
			'Créer un snapshot',
			'Numéro Snapper',
			'Envoi en cours · Vous pouvez fermer la fenêtre (systray)',
			'Aucun transfert par checkpoints',
			'Abandonner ce transfert inachevé et libérer sa protection Snapper ?'
		],
		'zh-CN': [
			'可续传 Btrfs 备份',
			'请保留已验证的备份。完整 Btrfs/NFS 恢复测试仍待完成。',
			'发送最新快照',
			'刷新',
			'继续',
			'暂停',
			'停止',
			'丢弃部分备份',
			'Snapper 来源',
			'模式',
			'最新稳定',
			'指定编号',
			'创建快照',
			'快照编号',
			'发送中 · 可关闭窗口，程序留在系统托盘',
			'没有检查点传输',
			'丢弃未完成传输并释放 Snapper 保护？'
		]
	};
	$: t = translation[locale];
	$: sources = profile.sources.filter((s) => Boolean(s.snapper_config));
	$: unavailableSources = profile.sources.filter((s) => !s.snapper_config);
	let sourcePath = '';
	$: source = sources.find((s) => s.path === sourcePath) ?? sources[0];
	$: target = source ? (checkpointTargetPath(destination.path, source.target_subdir) ?? '') : '';
	$: allowLocal = destination.kind !== 'nfs' && destination.kind !== 'smb';
	$: ready =
		isTauri() &&
		Boolean(source?.snapper_config) &&
		target.startsWith('/') &&
		destination.kind !== 'ssh';
	let mode: 'latest-snapper' | 'selected-snapper' | 'create-snapper' = 'latest-snapper';
	let number = 0;
	let snapshotChoices: SnapperSnapshotChoice[] = [];
	let snapshotListError = '';
	let snapshotLoading = false;
	async function loadSnapshots() {
		if (!source?.snapper_config) return;
		const requested = source.snapper_config;
		snapshotLoading = true;
		snapshotListError = '';
		try {
			const found = await listHostSnapperSnapshots(requested);
			if (source?.snapper_config !== requested) return;
			snapshotChoices = found;
			if (!snapshotChoices.some((item) => item.number === number)) {
				number = snapshotChoices[0]?.number ?? 0;
			}
		} catch (reason) {
			snapshotChoices = [];
			number = 0;
			snapshotListError = String(reason);
		} finally {
			snapshotLoading = false;
		}
	}
	let performance: 'balanced' | 'fast' = 'balanced';
	let entries: CheckpointTransfer[] = [];
	$: unfinished = entries.some((item) => item.resumable);
	$: visibleEntries = [
		...entries.filter((item) => item.resumable),
		...entries.filter((item) => !item.resumable).slice(0, 1)
	];
	let running = false;
	let busy = false;
	let error = '';
	let info = '';
	function backupName() {
		const name = (source?.snapshot_prefix || 'harbor').replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 45);
		return (
			name +
			'_' +
			new Date()
				.toISOString()
				.replace(/[-:TZ.]/g, '')
				.slice(0, 14)
		);
	}
	async function sourceChanged() {
		entries = [];
		snapshotChoices = [];
		number = 0;
		// Svelte recalculates source/target after updating the binding.
		// Do not query the previous source's destination or show its progress.
		await tick();
		await refresh();
		if (mode === 'selected-snapper') await loadSnapshots();
	}

	async function refresh() {
		if (!ready || !target) {
			entries = [];
			return;
		}
		const requestedTarget = target;
		busy = true;
		try {
			const result = await loadCheckpointTransfers(requestedTarget);
			if (target === requestedTarget) entries = result;
		} catch (e) {
			if (target === requestedTarget) error = String(e);
		} finally {
			busy = false;
		}
	}
	onMount(() => {
		void refresh();
		const interval = setInterval(() => {
			if (!busy) void refresh();
		}, 12000);
		return () => clearInterval(interval);
	});
	async function start() {
		if (!ready || !source?.snapper_config || running || unfinished) return;
		running = true;
		error = '';
		info = t[14];
		try {
			info = await runCheckpointAction({
				action: 'start',
				target,
				name: backupName(),
				profile_id: profile.id,
				source_mode: mode,
				snapper_config: source.snapper_config,
				snapper_number: mode === 'selected-snapper' ? number : null,
				allow_local: allowLocal,
				performance
			});
		} catch (e) {
			error = String(e);
		} finally {
			running = false;
			await refresh();
		}
	}
	async function command(
		action: 'resume' | 'pause' | 'stop' | 'discard',
		item: CheckpointTransfer
	) {
		if (action === 'discard' && !window.confirm(t[16])) return;
		if (action === 'resume') running = true;
		error = '';
		info = '';
		try {
			info = await runCheckpointAction({
				action,
				target,
				name: item.name,
				transfer_id: item.transfer_id,
				allow_local: allowLocal
			});
		} catch (e) {
			error = String(e);
		} finally {
			if (action === 'resume') running = false;
			await refresh();
		}
	}
</script>

{#if sources.length > 0 && destination.kind !== 'ssh'}
	<article class="panel checkpoint-controls">
		<h3>
			{locale === 'fr'
				? 'Envoyer un snapshot'
				: locale === 'zh-CN'
					? '发送快照'
					: 'Send a snapshot'}
		</h3>
		<div class="quick-choices">
			<label>
				{locale === 'fr' ? 'Source' : locale === 'zh-CN' ? '来源' : 'Source'}
				<select bind:value={sourcePath} disabled={running} onchange={() => void sourceChanged()}>
					{#each sources as choice (choice.path)}
						<option value={choice.path}>{choice.path}</option>
					{/each}
				</select>
			</label>
			<label>
				{locale === 'fr' ? 'Snapshot' : locale === 'zh-CN' ? '快照' : 'Snapshot'}
				<select
					bind:value={mode}
					disabled={running}
					onchange={() => {
						if (mode === 'selected-snapper') void loadSnapshots();
					}}
				>
					<option value="latest-snapper">{t[10]}</option>
					<option value="selected-snapper">{t[11]}</option>
					<option value="create-snapper">{t[12]}</option>
				</select>
			</label>
			{#if mode === 'selected-snapper'}
				<label>
					{t[13]}
					{#if snapshotLoading}
						<span
							>{locale === 'fr' ? 'Chargement…' : locale === 'zh-CN' ? '加载中…' : 'Loading…'}</span
						>
					{:else if snapshotChoices.length > 0}
						<select bind:value={number} disabled={running}>
							{#each snapshotChoices as choice (choice.number)}
								<option value={choice.number}
									>#{choice.number} · {new Date(choice.date).toLocaleDateString(locale)}
									{choice.description}</option
								>
							{/each}
						</select>
					{:else}
						<span role="status"
							>{snapshotListError ||
								(locale === 'fr'
									? 'Aucun snapshot disponible'
									: locale === 'zh-CN'
										? '没有可用快照'
										: 'No snapshot available')}</span
						>
					{/if}
				</label>
			{/if}
		</div>
		<div class="destination-summary">
			<strong
				>{locale === 'fr' ? 'Destination' : locale === 'zh-CN' ? '目标目录' : 'Destination'}</strong
			>
			<code>{target || '—'}</code>
		</div>
		{#if source && !target}
			<p class="source-warning" role="alert">
				{locale === 'fr'
					? 'Aucune destination valide. Configurez un dossier avant l’envoi.'
					: locale === 'zh-CN'
						? '目标文件夹无效，请先配置。'
						: 'No valid destination. Configure a folder before sending.'}
			</p>
		{/if}
		{#if unavailableSources.length > 0}
			<p class="source-warning" role="status">
				{locale === 'fr'
					? 'Non inclus dans cet envoi :'
					: locale === 'zh-CN'
						? '此次未包含：'
						: 'Not included in this send:'}
				{unavailableSources.map((s) => s.path).join(', ')}
			</p>
		{/if}
		<details class="advanced">
			<summary
				>{locale === 'fr'
					? 'Réglages avancés'
					: locale === 'zh-CN'
						? '高级设置'
						: 'Advanced settings'}</summary
			>
			<label class="performance-setting">
				CPU / I/O
				<select bind:value={performance} disabled={running}>
					<option value="balanced"
						>{locale === 'fr' ? 'Équilibré' : locale === 'zh-CN' ? '均衡' : 'Balanced'}</option
					>
					<option value="fast"
						>{locale === 'fr' ? 'Rapide' : locale === 'zh-CN' ? '快速' : 'Fast'}</option
					>
				</select>
			</label>
		</details>
		<div class="actions">
			<button
				class="primary compact"
				onclick={start}
				disabled={!ready || running || unfinished || (mode === 'selected-snapper' && number < 1)}
				>{mode === 'latest-snapper'
					? t[2]
					: locale === 'fr'
						? 'Envoyer'
						: locale === 'zh-CN'
							? '发送'
							: 'Send'}</button
			>
		</div>
		{#if info}<p class="operation-status" role="status">{info}</p>{/if}
		{#if error}<p class="error-text" role="alert">{error}</p>{/if}

		{#each visibleEntries as item (item.transfer_id)}
			<div class="entry" aria-live="polite">
				<div class="identity">
					<strong class="transfer-state">{checkpointStateLabel(item.state, locale)}</strong>
					<span class="transfer-bytes">
						{formatBytesBinary(item.committed_raw_bytes)}
						{locale === 'fr' ? 'envoyés' : locale === 'zh-CN' ? '已发送' : 'sent'}
					</span>
					{#if ['preparing', 'uploading', 'replaying', 'pause_requested', 'finalizing'].includes(item.state)}
						<progress
							aria-label={locale === 'fr'
								? 'Transfert en cours, taille totale inconnue'
								: locale === 'zh-CN'
									? '传输进行中，总量未知'
									: 'Transfer in progress, total size unknown'}
						></progress>
					{/if}
				</div>
				<div class="actions">
					{#if item.resumable && item.name}
						<button
							class="primary compact"
							disabled={running}
							onclick={() => command('resume', item)}>{t[4]}</button
						>
					{/if}
					{#if ['preparing', 'uploading', 'replaying', 'pause_requested'].includes(item.state)}
						<button class="secondary compact" onclick={() => command('pause', item)}>{t[5]}</button>
						<button class="secondary compact" onclick={() => command('stop', item)}>{t[6]}</button>
					{/if}
					{#if item.resumable && item.name}
						<details class="advanced">
							<summary
								>{locale === 'fr' ? 'Abandonner' : locale === 'zh-CN' ? '丢弃' : 'Discard'}</summary
							>
							<button
								class="secondary compact"
								disabled={running}
								onclick={() => command('discard', item)}>{t[7]}</button
							>
						</details>
					{/if}
				</div>
			</div>
		{/each}
	</article>
{:else}
	<article class="panel checkpoint-controls">
		<h3>
			{locale === 'fr'
				? 'Configuration Snapper nécessaire'
				: locale === 'zh-CN'
					? '需要配置 Snapper'
					: 'Snapper setup required'}
		</h3>
		<p class="source-warning">
			{locale === 'fr'
				? 'Aucune source de ce profil ne dispose de Snapper. Aucun envoi ne sera lancé avant une configuration explicite.'
				: locale === 'zh-CN'
					? '此配置尚无 Snapper 来源，在设置前不会启动传输。'
					: 'No source in this profile has a Snapper configuration. No send will be started until the source is configured.'}
		</p>
	</article>
{/if}

<style>
	.operation-status {
		font-size: 14px;
		font-weight: 600;
		line-height: 1.5;
		margin: 0;
	}
	.checkpoint-controls {
		display: grid;
		gap: 18px;
		padding: 22px;
	}
	h3 {
		margin: 0;
		font-size: 23px;
		font-weight: 700;
	}
	.source-warning {
		font-size: 13px;
		line-height: 1.5;
		color: var(--warning, #c28a31);
		margin: 0;
	}
	.advanced {
		border-top: 1px solid var(--border);
		padding-top: 12px;
	}
	.advanced summary {
		cursor: pointer;
		font-size: 13px;
		font-weight: 650;
	}
	.quick-choices {
		display: flex;
		flex-wrap: wrap;
		gap: 16px;
	}
	.quick-choices label,
	.performance-setting {
		display: grid;
		gap: 7px;
		font-size: 14px;
		font-weight: 650;
		flex: 1 1 180px;
	}
	.quick-choices select,
	.performance-setting select {
		padding: 11px;
		border: 1px solid var(--border);
		border-radius: 8px;
		color: var(--text);
		background: var(--surface);
		font-size: 15px;
	}
	.destination-summary {
		display: grid;
		gap: 5px;
		font-size: 14px;
	}
	.destination-summary code {
		font-size: 14px;
		overflow-wrap: anywhere;
	}
	.advanced .performance-setting {
		padding-top: 12px;
	}
	.transfer-state {
		font-size: 17px !important;
	}
	.transfer-bytes {
		font-size: 16px;
		font-variant-numeric: tabular-nums;
	}
	progress {
		width: min(440px, 100%);
		height: 13px;
	}
	.actions {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px;
	}
	.entry {
		display: flex;
		align-items: center;
		justify-content: space-between;
		flex-wrap: wrap;
		gap: 10px;
		padding: 10px;
		border: 1px solid var(--border);
		border-radius: 8px;
	}
	.identity {
		display: grid;
		gap: 3px;
	}
	.identity strong {
		font-size: 11px;
	}
</style>
