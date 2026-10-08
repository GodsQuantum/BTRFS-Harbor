<script lang="ts">
	import { onMount } from 'svelte';
	import { isTauri } from '@tauri-apps/api/core';
	import { loadCheckpointTransfers, runCheckpointAction } from './agent';
	import { checkpointStateLabel, type CheckpointTransfer } from './checkpoint';
	import { formatBytesBinary } from './status';
	import type { BackupProfile, DestinationSpec } from './config';
	import type { Locale } from './i18n';

	export let profile: BackupProfile;
	export let destination: DestinationSpec;
	export let locale: Locale = 'en';

	const translation: Record<Locale, string[]> = {
		en: [
			'Resumable Btrfs send · experimental',
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
			'Transfer in progress (ETA unknown)',
			'No checkpoint transfers',
			'Confirm discarding this unfinished transfer and releasing its Snapper pin?'
		],
		fr: [
			'Envoi Btrfs reprenable · expérimental',
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
			'Envoi en cours (durée inconnue)',
			'Aucun transfert par checkpoints',
			'Abandonner ce transfert inachevé et libérer sa protection Snapper ?'
		],
		'zh-CN': [
			'可续传 Btrfs 发送 · 实验功能',
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
			'传输中（时间未知）',
			'没有检查点传输',
			'丢弃未完成传输并释放 Snapper 保护？'
		]
	};
	$: t = translation[locale];
	$: sources = profile.sources.filter((s) => Boolean(s.snapper_config));
	let sourcePath = '';
	$: source = sources.find((s) => s.path === sourcePath) ?? sources[0];
	$: target = source
		? destination.path.replace(/\/+$/, '') +
			(source.target_subdir ? '/' + source.target_subdir.replace(/^\/+|\/+$/g, '') : '')
		: '';
	$: allowLocal = destination.kind !== 'nfs' && destination.kind !== 'smb';
	$: ready =
		isTauri() &&
		Boolean(source?.snapper_config) &&
		target.startsWith('/') &&
		destination.kind !== 'ssh';
	let mode: 'latest-snapper' | 'selected-snapper' | 'create-snapper' = 'latest-snapper';
	let number = 0;
	let performance: 'balanced' | 'fast' = 'balanced';
	let entries: CheckpointTransfer[] = [];
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
	async function refresh() {
		if (!ready || !target) {
			entries = [];
			return;
		}
		busy = true;
		try {
			entries = await loadCheckpointTransfers(target);
			error = '';
		} catch (e) {
			error = String(e);
		} finally {
			busy = false;
		}
	}
	onMount(() => {
		void refresh();
		const interval = setInterval(() => {
			if (running) void refresh();
		}, 5000);
		return () => clearInterval(interval);
	});
	async function start() {
		if (!ready || !source?.snapper_config || running) return;
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
		<h3>{t[0]}</h3>
		<p class="quiet">{t[1]}</p>
		<div class="fields">
			<label
				>{t[8]}
				<select bind:value={sourcePath} disabled={running}>
					{#each sources as choice (choice.path)}<option value={choice.path}
							>{choice.path} / {choice.snapper_config}</option
						>{/each}
				</select>
			</label>
			<label
				>{t[9]}
				<select bind:value={mode} disabled={running}>
					<option value="latest-snapper">{t[10]}</option>
					<option value="selected-snapper">{t[11]}</option>
					<option value="create-snapper">{t[12]}</option>
				</select>
			</label>
			{#if mode === 'selected-snapper'}
				<label>{t[13]}<input type="number" min="1" bind:value={number} /></label>
			{/if}
			<label
				>CPU / I/O
				<select bind:value={performance}
					><option value="balanced">Balanced</option><option value="fast">Fast</option></select
				>
			</label>
		</div>
		<small class="quiet">{target}</small>
		<div class="actions">
			<button
				class="primary compact"
				onclick={start}
				disabled={!ready || running || (mode === 'selected-snapper' && number < 1)}>{t[2]}</button
			>
			<button class="secondary compact" onclick={refresh} disabled={busy}>{t[3]}</button>
		</div>
		{#if info}<p class="quiet">{info}</p>{/if}
		{#if error}<p class="error-text" role="alert">{error}</p>{/if}
		{#if entries.length === 0 && !busy}<p class="quiet">{t[15]}</p>{/if}
		{#each entries as item (item.transfer_id)}
			<div class="entry">
				<div class="identity">
					<strong>{checkpointStateLabel(item.state, locale)}</strong>
					<small
						>{item.name ?? item.transfer_id} · {formatBytesBinary(item.committed_raw_bytes)} · {item.checkpoint_count}
						checkpoints</small
					>
				</div>
				<div class="actions">
					{#if item.resumable && item.name}
						<button
							class="secondary compact"
							disabled={running}
							onclick={() => command('resume', item)}>{t[4]}</button
						>
					{/if}
					{#if ['preparing', 'uploading', 'replaying', 'pause_requested'].includes(item.state)}
						<button class="secondary compact" onclick={() => command('pause', item)}>{t[5]}</button>
						<button class="secondary compact" onclick={() => command('stop', item)}>{t[6]}</button>
					{/if}
					{#if item.resumable && item.name}
						<button
							class="secondary compact"
							disabled={running}
							onclick={() => command('discard', item)}>{t[7]}</button
						>
					{/if}
				</div>
			</div>
		{/each}
	</article>
{/if}

<style>
	.checkpoint-controls {
		display: grid;
		gap: 12px;
		padding: 14px;
	}
	h3 {
		margin: 0;
		font-size: 13px;
	}
	.quiet {
		font-size: 10px;
		color: var(--muted);
		margin: 0;
		overflow-wrap: anywhere;
	}
	.fields {
		display: flex;
		flex-wrap: wrap;
		gap: 10px;
	}
	.fields label {
		display: grid;
		gap: 4px;
		color: var(--muted);
		font-size: 10px;
		min-width: 125px;
	}
	.fields input,
	.fields select {
		color: var(--text);
		background: var(--surface);
		border: 1px solid var(--border);
		padding: 7px;
		border-radius: 6px;
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
	.identity small {
		font-size: 9px;
		color: var(--muted);
	}
</style>
