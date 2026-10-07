<script lang="ts">
	import { onMount } from 'svelte';
	import CircleAlert from 'lucide-svelte/icons/circle-alert';
	import FolderOpen from 'lucide-svelte/icons/folder-open';
	import HardDrive from 'lucide-svelte/icons/hard-drive';
	import LifeBuoy from 'lucide-svelte/icons/life-buoy';
	import Play from 'lucide-svelte/icons/play';
	import RefreshCw from 'lucide-svelte/icons/refresh-cw';
	import RotateCcw from 'lucide-svelte/icons/rotate-ccw';
	import ShieldCheck from 'lucide-svelte/icons/shield-check';
	import {
		chooseStagingDirectory,
		listRestorePoints,
		stageRestoreProfile,
		type RestorePoint,
		type StagedRestoreRequest
	} from './agent';
	import { resolveProfile, type HarborConfig } from './config';
	import { translate, type Locale, type TranslationKey } from './i18n';
	import type { BackupProgressEvent } from './status';

	export let config: HarborConfig;
	export let profileId: string | undefined = undefined;
	export let locale: Locale = 'en';

	let selectedSourcePath = '';
	let selectedDestinationId = '';
	let selectedSnapshot = '';
	let stagingRoot = '/mnt';
	let restorePoints: RestorePoint[] = [];
	let loadingPoints = false;
	let restoring = false;
	let progress: BackupProgressEvent | null = null;
	let error = '';

	$: profile = resolveProfile(config, profileId);
	$: destinations = config.destinations.filter((destination) =>
		profile.destination_ids.includes(destination.id)
	);
	$: if (!profile.sources.some((source) => source.path === selectedSourcePath)) {
		selectedSourcePath =
			profile.sources.find((source) => source.path !== '/')?.path ?? profile.sources[0]?.path ?? '';
	}
	$: if (!destinations.some((destination) => destination.id === selectedDestinationId)) {
		selectedDestinationId = destinations[0]?.id ?? '';
	}
	$: rootSelected = selectedSourcePath === '/';
	$: selectedPoint = restorePoints.find((point) => point.name === selectedSnapshot) ?? null;

	const t = (key: TranslationKey) => translate(locale, key);

	onMount(() => {
		void refreshRestorePoints();
	});

	function phaseLabel(phase: string): string {
		switch (phase) {
			case 'restore_start':
				return t('restorePreparing');
			case 'restore_plan':
				return t('restoreDryRun');
			case 'restore':
				return t('restoreReceiving');
			case 'restore_verify':
				return t('restoreVerifying');
			case 'restore_complete':
				return t('restoreReady');
			case 'restore_error':
				return t('restoreFailed');
			default:
				return t('stagedRestore');
		}
	}

	function formatPoint(point: RestorePoint): string {
		const date = point.created ? new Date(point.created).toLocaleString(locale) : point.name;
		const size =
			point.size && point.size > 0
				? ` · ${new Intl.NumberFormat(locale, { style: 'unit', unit: 'megabyte', maximumFractionDigits: 0 }).format(point.size / 1_000_000)}`
				: '';
		return `${date}${size}`;
	}

	async function sourceChanged() {
		selectedSnapshot = '';
		await refreshRestorePoints();
	}

	async function destinationChanged() {
		selectedSnapshot = '';
		await refreshRestorePoints();
	}

	async function refreshRestorePoints() {
		error = '';
		if (!profile.id || !selectedSourcePath || !selectedDestinationId) {
			restorePoints = [];
			selectedSnapshot = '';
			return;
		}
		loadingPoints = true;
		try {
			restorePoints = await listRestorePoints(
				config,
				profile.id,
				selectedSourcePath,
				selectedDestinationId
			);
			if (!restorePoints.some((point) => point.name === selectedSnapshot)) {
				selectedSnapshot = restorePoints[0]?.name ?? '';
			}
		} catch (cause) {
			restorePoints = [];
			selectedSnapshot = '';
			error = cause instanceof Error ? cause.message : String(cause);
		} finally {
			loadingPoints = false;
		}
	}

	async function browseStaging() {
		error = '';
		const selected = await chooseStagingDirectory(stagingRoot);
		if (selected) stagingRoot = selected;
	}

	async function runRestore() {
		error = '';
		progress = null;
		if (
			!profile.id ||
			!selectedSourcePath ||
			!selectedDestinationId ||
			!selectedSnapshot ||
			!stagingRoot.trim()
		) {
			error = t('restoreRequiredFields');
			return;
		}
		if (rootSelected) {
			error = t('rootRestoreRequiresRescue');
			return;
		}

		const request: StagedRestoreRequest = {
			destination_id: selectedDestinationId,
			source_path: selectedSourcePath,
			staging_root: stagingRoot.trim(),
			snapshot: selectedSnapshot,
			before: null
		};

		restoring = true;
		try {
			await stageRestoreProfile(config, profile.id, request, (event) => {
				if (event.event !== 'output') progress = event;
			});
		} catch (cause) {
			error = cause instanceof Error ? cause.message : String(cause);
		} finally {
			restoring = false;
		}
	}
</script>

<div class="restore-workspace">
	<article class="restore-panel">
		<div class="restore-head">
			<div class="restore-icon"><LifeBuoy size={23} /></div>
			<div>
				<p>{t('stagedRestore')}</p>
				<h3>{t('restoreWorkspaceTitle')}</h3>
				<span>{t('restoreWorkspaceDesc')}</span>
			</div>
		</div>

		<div class="restore-form">
			<label>
				<span>{t('restoreSource')}</span>
				<select bind:value={selectedSourcePath} onchange={sourceChanged}>
					{#each profile.sources as source (source.path)}
						<option value={source.path}>
							{source.path}{source.snapper_config ? ` · Snapper ${source.snapper_config}` : ''}
						</option>
					{/each}
				</select>
			</label>

			<label>
				<span>{t('backupDestination')}</span>
				<select bind:value={selectedDestinationId} onchange={destinationChanged}>
					{#each destinations as destination (destination.id)}
						<option value={destination.id}>{destination.name}</option>
					{/each}
				</select>
			</label>

			<label class="restore-point-field">
				<span>{t('restorePoint')}</span>
				<div class="input-action">
					<select
						bind:value={selectedSnapshot}
						disabled={loadingPoints || restorePoints.length === 0}
					>
						{#each restorePoints as point (point.name)}
							<option value={point.name}>{formatPoint(point)}</option>
						{/each}
					</select>
					<button
						class="secondary"
						type="button"
						onclick={refreshRestorePoints}
						disabled={loadingPoints}
					>
						<span class:spin={loadingPoints}><RefreshCw size={15} /></span>
						{t('refreshSnapshots')}
					</button>
				</div>
				<small>
					{#if loadingPoints}
						{t('loadingSnapshots')}
					{:else if restorePoints.length === 0}
						{t('noRestorePoints')}
					{:else if selectedPoint?.parent_name}
						{t('snapshotParent')}: {selectedPoint.parent_name}
					{:else}
						{selectedPoint?.name ?? ''}
					{/if}
				</small>
			</label>

			<label class="staging-field">
				<span>{t('stagingRoot')}</span>
				<div class="input-action">
					<input bind:value={stagingRoot} placeholder="/mnt/restore-staging" spellcheck="false" />
					<button type="button" class="secondary" onclick={browseStaging}>
						<FolderOpen size={15} />
						{t('chooseFolder')}
					</button>
				</div>
				<small>{t('stagingBtrfsRequired')}</small>
			</label>
		</div>

		<div class="coverage-card">
			<ShieldCheck size={18} />
			<div>
				<strong>{t('recoveryCoverage')}</strong>
				<span>{t('recoveryCoverageDesc')}</span>
			</div>
		</div>

		{#if rootSelected}
			<div class="restore-warning">
				<CircleAlert size={18} />
				<div>
					<strong>{t('requiresRescue')}</strong>
					<span>{t('rootRestoreRequiresRescue')}</span>
				</div>
			</div>
		{:else}
			<div class="restore-safety">
				<ShieldCheck size={18} />
				<div>
					<strong>{t('stagingSafetyTitle')}</strong>
					<span>{t('stagingSafetyDesc')}</span>
				</div>
			</div>
		{/if}

		<div class="restore-actions">
			<div class="restore-target">
				<HardDrive size={16} />
				<span>{selectedSnapshot || '—'} → {stagingRoot || '—'}</span>
			</div>
			<button
				type="button"
				class="primary"
				disabled={restoring || rootSelected || !selectedSnapshot || !stagingRoot.trim()}
				onclick={runRestore}
			>
				{#if restoring}
					<RotateCcw class="spin" size={16} /> {t('restoring')}
				{:else}
					<Play size={16} fill="currentColor" /> {t('stageRestoreNow')}
				{/if}
			</button>
		</div>

		{#if progress}
			<div
				class="restore-progress"
				class:complete={progress.event === 'finished'}
				class:failed={progress.event === 'failed'}
			>
				<span class="dot"></span>
				<div>
					<strong>{phaseLabel(progress.phase)}</strong>
					<small>{progress.message}</small>
				</div>
			</div>
		{/if}

		{#if error}
			<div class="restore-error"><CircleAlert size={15} /><span>{error}</span></div>
		{/if}
	</article>
</div>

<style>
	.restore-workspace,
	.restore-panel {
		display: grid;
		gap: 12px;
	}
	.restore-panel {
		padding: 16px;
		border: 1px solid var(--border);
		border-radius: 14px;
		background: var(--surface);
	}
	.restore-head,
	.restore-safety,
	.restore-warning,
	.coverage-card,
	.restore-actions,
	.restore-target,
	.restore-progress,
	.restore-error,
	.input-action {
		display: flex;
		align-items: center;
	}
	.restore-head {
		gap: 12px;
	}
	.restore-icon {
		display: grid;
		width: 40px;
		height: 40px;
		place-items: center;
		border: 1px solid color-mix(in srgb, var(--accent) 35%, var(--border));
		border-radius: 11px;
		background: color-mix(in srgb, var(--accent) 10%, transparent);
		color: var(--accent);
	}
	.restore-head p,
	.restore-head h3 {
		margin: 0;
	}
	.restore-head p {
		color: var(--muted);
		font-size: 9px;
		font-weight: 700;
		letter-spacing: 0.1em;
		text-transform: uppercase;
	}
	.restore-head h3 {
		font-size: 17px;
	}
	.restore-head span,
	label small,
	.restore-safety span,
	.restore-warning span,
	.coverage-card span,
	.restore-progress small {
		color: var(--muted);
		font-size: 10px;
		line-height: 1.4;
	}
	.restore-form {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 10px;
	}
	.restore-point-field,
	.staging-field {
		grid-column: span 2;
	}
	label {
		display: grid;
		gap: 5px;
		font-size: 10px;
		font-weight: 650;
	}
	select,
	input {
		width: 100%;
		min-width: 0;
		box-sizing: border-box;
		border: 1px solid var(--border);
		border-radius: 9px;
		background: var(--surface-strong);
		color: var(--text);
		padding: 9px 10px;
		font: inherit;
	}
	.input-action {
		gap: 8px;
	}
	.input-action select,
	.input-action input {
		flex: 1;
	}
	.restore-safety,
	.restore-warning,
	.coverage-card {
		gap: 9px;
		padding: 9px 11px;
		border-radius: 10px;
	}
	.restore-safety,
	.coverage-card {
		border: 1px solid color-mix(in srgb, var(--good) 32%, var(--border));
		background: color-mix(in srgb, var(--good) 7%, var(--surface));
	}
	.restore-warning {
		border: 1px solid color-mix(in srgb, var(--warning) 38%, var(--border));
		background: color-mix(in srgb, var(--warning) 8%, var(--surface));
	}
	.restore-safety div,
	.restore-warning div,
	.coverage-card div,
	.restore-progress div {
		display: grid;
		gap: 2px;
	}
	.restore-actions {
		justify-content: space-between;
		gap: 12px;
	}
	.restore-target {
		min-width: 0;
		gap: 7px;
		color: var(--muted);
		font-size: 10px;
	}
	.restore-target span {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.restore-progress,
	.restore-error {
		gap: 8px;
		padding: 9px 11px;
		border: 1px solid var(--border);
		border-radius: 9px;
	}
	.restore-error {
		border-color: color-mix(in srgb, var(--danger) 40%, var(--border));
		color: var(--danger);
		font-size: 10px;
	}
	.dot {
		width: 7px;
		height: 7px;
		border-radius: 50%;
		background: var(--accent);
	}
	.complete .dot {
		background: var(--good);
	}
	.failed .dot {
		background: var(--danger);
	}
	.spin {
		animation: spin 1s linear infinite;
	}
	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}
	@media (max-width: 760px) {
		.restore-form {
			grid-template-columns: 1fr;
		}
		.restore-point-field,
		.staging-field {
			grid-column: auto;
		}
		.restore-actions {
			align-items: stretch;
			flex-direction: column;
		}
	}
</style>
