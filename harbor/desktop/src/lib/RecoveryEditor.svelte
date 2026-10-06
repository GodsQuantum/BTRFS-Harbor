<script lang="ts">
	import CircleAlert from 'lucide-svelte/icons/circle-alert';
	import FolderOpen from 'lucide-svelte/icons/folder-open';
	import HardDrive from 'lucide-svelte/icons/hard-drive';
	import LifeBuoy from 'lucide-svelte/icons/life-buoy';
	import Play from 'lucide-svelte/icons/play';
	import RotateCcw from 'lucide-svelte/icons/rotate-ccw';
	import ShieldCheck from 'lucide-svelte/icons/shield-check';
	import { chooseStagingDirectory, stageRestoreProfile, type StagedRestoreRequest } from './agent';
	import { resolveProfile, type HarborConfig } from './config';
	import { translate, type Locale, type TranslationKey } from './i18n';
	import type { BackupProgressEvent } from './status';

	export let config: HarborConfig;
	export let profileId: string | undefined = undefined;
	export let advanced = false;
	export let locale: Locale = 'en';

	let selectedSourcePath = '';
	let selectedDestinationId = '';
	let stagingRoot = '/mnt';
	let before = '';
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

	const t = (key: TranslationKey) => translate(locale, key);

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

	async function browseStaging() {
		error = '';
		const selected = await chooseStagingDirectory(stagingRoot);
		if (selected) stagingRoot = selected;
	}

	async function runRestore() {
		error = '';
		progress = null;
		if (!profile.id || !selectedSourcePath || !selectedDestinationId || !stagingRoot.trim()) {
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
			before: before.trim() || null
		};

		restoring = true;
		try {
			await stageRestoreProfile(profile.id, request, (event) => {
				if (event.event !== 'output' || advanced) progress = event;
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
				<select bind:value={selectedSourcePath}>
					{#each profile.sources as source (source.path)}
						<option value={source.path}>
							{source.path}{source.path === '/' ? ` · ${t('requiresRescue')}` : ''}
						</option>
					{/each}
				</select>
			</label>

			<label>
				<span>{t('backupDestination')}</span>
				<select bind:value={selectedDestinationId}>
					{#each destinations as destination (destination.id)}
						<option value={destination.id}
							>{destination.name} · {destination.kind.toUpperCase()}</option
						>
					{/each}
				</select>
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

			{#if advanced}
				<label>
					<span>{t('restoreBefore')}</span>
					<input bind:value={before} placeholder="2026-10-05 02:00:00" spellcheck="false" />
					<small>{t('restoreBeforeHelp')}</small>
				</label>
			{/if}
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
				<span>{selectedSourcePath || '—'} → {stagingRoot || '—'}</span>
			</div>
			<button
				type="button"
				class="primary"
				disabled={restoring || rootSelected || !selectedDestinationId || !stagingRoot.trim()}
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
	.restore-workspace {
		display: grid;
		gap: 14px;
	}

	.restore-panel {
		display: grid;
		gap: 18px;
		padding: 20px;
		border: 1px solid var(--border);
		border-radius: 14px;
		background: var(--surface);
	}

	.restore-head,
	.restore-safety,
	.restore-warning,
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
		width: 42px;
		height: 42px;
		place-items: center;
		border: 1px solid color-mix(in srgb, var(--accent) 35%, var(--border));
		border-radius: 11px;
		background: color-mix(in srgb, var(--accent) 10%, transparent);
		color: var(--accent);
	}

	.restore-head p {
		margin: 0 0 3px;
		color: var(--muted);
		font-size: 9px;
		font-weight: 700;
		letter-spacing: 0.1em;
		text-transform: uppercase;
	}

	.restore-head h3 {
		margin: 0;
		font-size: 17px;
	}

	.restore-head span {
		display: block;
		margin-top: 4px;
		color: var(--muted);
		font-size: 10px;
		line-height: 1.45;
	}

	.restore-form {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 12px;
	}

	label {
		display: grid;
		gap: 6px;
		color: var(--muted);
		font-size: 9px;
		font-weight: 650;
	}

	label > span {
		letter-spacing: 0.02em;
	}

	input,
	select {
		min-height: 38px;
		width: 100%;
		padding: 0 10px;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--surface-soft);
		color: var(--text);
		font: inherit;
		font-size: 10px;
		outline: none;
	}

	input:focus,
	select:focus {
		border-color: color-mix(in srgb, var(--accent) 65%, var(--border));
		box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 10%, transparent);
	}

	label small {
		color: var(--muted);
		font-size: 8px;
		font-weight: 500;
		line-height: 1.4;
	}

	.staging-field {
		grid-column: 1 / -1;
	}

	.input-action {
		gap: 8px;
	}

	.input-action input {
		flex: 1;
		min-width: 0;
	}

	.secondary {
		display: inline-flex;
		align-items: center;
		gap: 6px;
		min-height: 38px;
		padding: 0 11px;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--surface-soft);
		color: var(--text);
		font-size: 9px;
		font-weight: 700;
		white-space: nowrap;
		cursor: pointer;
	}

	.restore-safety,
	.restore-warning {
		align-items: flex-start;
		gap: 9px;
		padding: 10px 12px;
		border: 1px solid var(--border);
		border-radius: 9px;
		background: var(--surface-soft);
		color: var(--muted);
	}

	.restore-safety {
		border-color: color-mix(in srgb, var(--good) 30%, var(--border));
	}

	.restore-safety > :global(svg) {
		color: var(--good);
	}

	.restore-warning {
		border-color: color-mix(in srgb, var(--danger) 35%, var(--border));
	}

	.restore-warning > :global(svg) {
		color: var(--danger);
	}

	.restore-safety div,
	.restore-warning div {
		display: grid;
		gap: 2px;
	}

	.restore-safety strong,
	.restore-warning strong {
		color: var(--text);
		font-size: 9px;
	}

	.restore-safety span,
	.restore-warning span {
		font-size: 8px;
		line-height: 1.45;
	}

	.restore-actions {
		justify-content: space-between;
		gap: 12px;
		padding-top: 2px;
	}

	.restore-target {
		min-width: 0;
		gap: 7px;
		color: var(--muted);
		font-size: 9px;
	}

	.restore-target span {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.primary {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 7px;
		min-height: 38px;
		padding: 0 14px;
		border: 0;
		border-radius: 8px;
		background: var(--accent);
		color: var(--accent-contrast);
		font-size: 9px;
		font-weight: 750;
		cursor: pointer;
	}

	.primary:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}

	.restore-progress,
	.restore-error {
		align-items: flex-start;
		gap: 8px;
		padding: 9px 11px;
		border: 1px solid var(--border);
		border-radius: 9px;
		background: var(--surface-soft);
	}

	.restore-progress .dot {
		width: 7px;
		height: 7px;
		margin-top: 4px;
		border-radius: 50%;
		background: var(--accent);
	}

	.restore-progress.complete .dot {
		background: var(--good);
	}

	.restore-progress.failed .dot {
		background: var(--danger);
	}

	.restore-progress div {
		display: grid;
		gap: 2px;
		min-width: 0;
	}

	.restore-progress strong {
		font-size: 9px;
	}

	.restore-progress small {
		color: var(--muted);
		font-size: 8px;
		line-height: 1.4;
		word-break: break-word;
	}

	.restore-error {
		color: var(--danger);
		font-size: 9px;
	}

	:global(.spin) {
		animation: spin 0.9s linear infinite;
	}

	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}

	@media (max-width: 780px) {
		.restore-form {
			grid-template-columns: 1fr;
		}

		.staging-field {
			grid-column: auto;
		}

		.restore-actions {
			align-items: stretch;
			flex-direction: column;
		}

		.primary {
			width: 100%;
		}
	}
</style>
