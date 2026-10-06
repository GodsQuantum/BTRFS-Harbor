<script lang="ts">
	import Box from 'lucide-svelte/icons/box';
	import CircleAlert from 'lucide-svelte/icons/circle-alert';
	import FolderOpen from 'lucide-svelte/icons/folder-open';
	import Play from 'lucide-svelte/icons/play';
	import RotateCcw from 'lucide-svelte/icons/rotate-ccw';
	import ShieldCheck from 'lucide-svelte/icons/shield-check';
	import { chooseStagingDirectory, replicateLxc, type LxcReplicaRequest } from './agent';
	import { translate, type Locale, type TranslationKey } from './i18n';
	import type { BackupProgressEvent } from './status';

	export let advanced = false;
	export let locale: Locale = 'en';

	let stagingPath = '/mnt';
	let vmid = '';
	let confirmVmid = '';
	let targetName = '';
	let createRollbackSnapshot = true;
	let startAfter = false;
	let regenerateSshHostKeys = true;
	let running = false;
	let progress: BackupProgressEvent | null = null;
	let error = '';

	const t = (key: TranslationKey) => translate(locale, key);

	function phaseLabel(phase: string): string {
		switch (phase) {
			case 'replica_start':
			case 'replica_preflight':
				return t('replicaPreflight');
			case 'replica_snapshot':
				return t('replicaSnapshot');
			case 'replica_mount':
				return t('replicaMount');
			case 'replica_preserve':
				return t('replicaPreserve');
			case 'replica_clear':
				return t('replicaClear');
			case 'replica_copy':
				return t('replicaCopy');
			case 'replica_unmount':
				return t('replicaUnmount');
			case 'replica_start_target':
				return t('replicaStarting');
			case 'replica_rollback':
				return t('replicaRollback');
			case 'replica_complete':
				return t('replicaComplete');
			case 'replica_error':
				return t('replicaFailed');
			default:
				return t('lxcReplicaDeploy');
		}
	}

	async function browseStaging() {
		error = '';
		const selected = await chooseStagingDirectory(stagingPath);
		if (selected) stagingPath = selected;
	}

	function parseVmid(value: string): number | null {
		if (!/^\d+$/.test(value.trim())) return null;
		const parsed = Number(value);
		return Number.isInteger(parsed) && parsed >= 100 && parsed <= 999_999_999 ? parsed : null;
	}

	async function deployReplica() {
		error = '';
		progress = null;

		const parsedVmid = parseVmid(vmid);
		const parsedConfirm = parseVmid(confirmVmid);
		if (
			parsedVmid === null ||
			parsedConfirm === null ||
			parsedVmid !== parsedConfirm ||
			!stagingPath.startsWith('/') ||
			!targetName.trim()
		) {
			error = t('replicaRequiredFields');
			return;
		}

		const request: LxcReplicaRequest = {
			vmid: parsedVmid,
			confirm_vmid: parsedConfirm,
			staging_path: stagingPath.trim(),
			target_name: targetName.trim(),
			create_rollback_snapshot: createRollbackSnapshot,
			start_after: startAfter,
			regenerate_ssh_host_keys: regenerateSshHostKeys
		};

		running = true;
		try {
			await replicateLxc(request, (event) => {
				if (event.event !== 'output' || advanced) progress = event;
			});
		} catch (cause) {
			error = cause instanceof Error ? cause.message : String(cause);
		} finally {
			running = false;
		}
	}
</script>

<div class="replica-workspace">
	<article class="replica-panel">
		<div class="replica-head">
			<div class="replica-icon"><Box size={23} /></div>
			<div>
				<p>{t('lxcReplicaDeploy')}</p>
				<h3>{t('lxcReplicaTitle')}</h3>
				<span>{t('lxcReplicaDesc')}</span>
			</div>
		</div>

		<div class="safety">
			<ShieldCheck size={18} />
			<div>
				<strong>{t('lxcStoppedRequired')}</strong>
				<span>{t('lxcStoppedDesc')}</span>
			</div>
		</div>

		<div class="replica-form">
			<label class="wide">
				<span>{t('replicaStagingPath')}</span>
				<div class="input-action">
					<input
						bind:value={stagingPath}
						placeholder="/mnt/harbor-staging/root-snapshot"
						spellcheck="false"
					/>
					<button type="button" class="secondary" onclick={browseStaging}>
						<FolderOpen size={15} />
						{t('chooseFolder')}
					</button>
				</div>
				<small>{t('replicaStagingHelp')}</small>
			</label>

			<label>
				<span>{t('targetVmid')}</span>
				<input bind:value={vmid} inputmode="numeric" placeholder="420" />
			</label>

			<label>
				<span>{t('confirmTargetVmid')}</span>
				<input bind:value={confirmVmid} inputmode="numeric" placeholder="420" />
			</label>

			<label class="wide">
				<span>{t('replicaTargetName')}</span>
				<input bind:value={targetName} placeholder="Colonial-One" spellcheck="false" />
			</label>
		</div>

		{#if advanced}
			<div class="advanced-grid">
				<label class="checkline">
					<input type="checkbox" bind:checked={createRollbackSnapshot} />
					<span>
						<strong>{t('rollbackSnapshot')}</strong>
						<small>{t('rollbackSnapshotDesc')}</small>
					</span>
				</label>
				<label class="checkline">
					<input type="checkbox" bind:checked={regenerateSshHostKeys} />
					<span>
						<strong>{t('regenerateSshKeys')}</strong>
						<small>{t('regenerateSshKeysDesc')}</small>
					</span>
				</label>
				<label class="checkline">
					<input type="checkbox" bind:checked={startAfter} />
					<span>
						<strong>{t('startAfterReplica')}</strong>
						<small>{t('startAfterReplicaDesc')}</small>
					</span>
				</label>
			</div>
		{/if}

		<div class="warning">
			<CircleAlert size={17} />
			<span>{t('lxcReplicaDestructive')}</span>
		</div>

		<div class="replica-actions">
			<div class="target-summary">
				<Box size={16} />
				<span>
					{stagingPath || '—'} → LXC {vmid || '—'}
				</span>
			</div>
			<button type="button" class="primary" disabled={running} onclick={deployReplica}>
				{#if running}
					<RotateCcw class="spin" size={16} /> {t('replicating')}
				{:else}
					<Play size={16} fill="currentColor" /> {t('deployReplica')}
				{/if}
			</button>
		</div>

		{#if progress}
			<div
				class="replica-progress"
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
			<div class="replica-error"><CircleAlert size={15} /><span>{error}</span></div>
		{/if}
	</article>
</div>

<style>
	.replica-workspace {
		display: grid;
		gap: 14px;
	}

	.replica-panel {
		display: grid;
		gap: 17px;
		padding: 20px;
		border: 1px solid var(--border);
		border-radius: 14px;
		background: var(--surface);
	}

	.replica-head,
	.safety,
	.warning,
	.replica-actions,
	.target-summary,
	.replica-progress,
	.replica-error,
	.input-action,
	.checkline {
		display: flex;
		align-items: center;
	}

	.replica-head {
		gap: 12px;
	}

	.replica-icon {
		display: grid;
		width: 42px;
		height: 42px;
		place-items: center;
		border: 1px solid color-mix(in srgb, var(--accent) 35%, var(--border));
		border-radius: 11px;
		background: color-mix(in srgb, var(--accent) 10%, transparent);
		color: var(--accent);
	}

	.replica-head p {
		margin: 0 0 3px;
		color: var(--muted);
		font-size: 9px;
		font-weight: 700;
		letter-spacing: 0.1em;
		text-transform: uppercase;
	}

	.replica-head h3 {
		margin: 0;
		font-size: 17px;
	}

	.replica-head span {
		display: block;
		margin-top: 4px;
		color: var(--muted);
		font-size: 10px;
		line-height: 1.45;
	}

	.safety,
	.warning {
		align-items: flex-start;
		gap: 9px;
		padding: 10px 12px;
		border: 1px solid var(--border);
		border-radius: 9px;
		background: var(--surface-soft);
		color: var(--muted);
	}

	.safety {
		border-color: color-mix(in srgb, var(--good) 30%, var(--border));
	}

	.safety > :global(svg) {
		color: var(--good);
	}

	.warning {
		border-color: color-mix(in srgb, var(--danger) 34%, var(--border));
	}

	.warning > :global(svg) {
		color: var(--danger);
	}

	.safety div {
		display: grid;
		gap: 2px;
	}

	.safety strong {
		color: var(--text);
		font-size: 9px;
	}

	.safety span,
	.warning span {
		font-size: 8px;
		line-height: 1.45;
	}

	.replica-form {
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

	.wide {
		grid-column: 1 / -1;
	}

	input {
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

	input:focus {
		border-color: color-mix(in srgb, var(--accent) 65%, var(--border));
		box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 10%, transparent);
	}

	label small {
		color: var(--muted);
		font-size: 8px;
		font-weight: 500;
		line-height: 1.4;
	}

	.input-action {
		gap: 8px;
	}

	.input-action input {
		flex: 1;
		min-width: 0;
	}

	.secondary,
	.primary {
		display: inline-flex;
		align-items: center;
		justify-content: center;
		gap: 7px;
		min-height: 38px;
		border-radius: 8px;
		font-size: 9px;
		font-weight: 750;
		cursor: pointer;
	}

	.secondary {
		padding: 0 11px;
		border: 1px solid var(--border);
		background: var(--surface-soft);
		color: var(--text);
		white-space: nowrap;
	}

	.primary {
		padding: 0 14px;
		border: 0;
		background: var(--accent);
		color: var(--accent-contrast);
	}

	.primary:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}

	.advanced-grid {
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		gap: 10px;
	}

	.checkline {
		display: flex;
		align-items: flex-start;
		gap: 8px;
		padding: 10px;
		border: 1px solid var(--border);
		border-radius: 9px;
		background: var(--surface-soft);
	}

	.checkline input {
		width: auto;
		min-height: 0;
		margin-top: 2px;
	}

	.checkline span {
		display: grid;
		gap: 2px;
	}

	.checkline strong {
		color: var(--text);
		font-size: 9px;
	}

	.replica-actions {
		justify-content: space-between;
		gap: 12px;
	}

	.target-summary {
		min-width: 0;
		gap: 7px;
		color: var(--muted);
		font-size: 9px;
	}

	.target-summary span {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.replica-progress,
	.replica-error {
		align-items: flex-start;
		gap: 8px;
		padding: 9px 11px;
		border: 1px solid var(--border);
		border-radius: 9px;
		background: var(--surface-soft);
	}

	.replica-progress .dot {
		width: 7px;
		height: 7px;
		margin-top: 4px;
		border-radius: 50%;
		background: var(--accent);
	}

	.replica-progress.complete .dot {
		background: var(--good);
	}

	.replica-progress.failed .dot {
		background: var(--danger);
	}

	.replica-progress div {
		display: grid;
		gap: 2px;
		min-width: 0;
	}

	.replica-progress strong {
		font-size: 9px;
	}

	.replica-progress small {
		color: var(--muted);
		font-size: 8px;
		line-height: 1.4;
		word-break: break-word;
	}

	.replica-error {
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
		.replica-form,
		.advanced-grid {
			grid-template-columns: 1fr;
		}

		.wide {
			grid-column: auto;
		}

		.replica-actions {
			align-items: stretch;
			flex-direction: column;
		}

		.primary {
			width: 100%;
		}
	}
</style>
