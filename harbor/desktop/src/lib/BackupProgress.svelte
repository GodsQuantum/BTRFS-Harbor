<script lang="ts">
	import TerminalSquare from 'lucide-svelte/icons/terminal-square';
	import { backupProgressPercent, type BackupProgressEvent } from './status';
	import { translate, type Locale, type TranslationKey } from './i18n';

	export let progress: BackupProgressEvent | null = null;
	export let detail = '';
	export let engineLabel = '';
	export let locale: Locale = 'en';

	$: percent = backupProgressPercent(progress);
	$: streaming = progress?.phase === 'backup' && progress?.event !== 'finished';
	const t = (key: TranslationKey) => translate(locale, key);

	function phaseLabel(phase: string): string {
		switch (phase) {
			case 'start':
			case 'prepare':
				return t('progressPreparing');
			case 'engine':
				return t('engine');
			case 'mount_guard':
				return t('progressDestination');
			case 'backup':
				return t('progressBackup');
			case 'verify':
				return t('progressVerify');
			case 'recovery_kit':
				return t('progressRecoveryKit');
			case 'complete':
				return t('progressComplete');
			case 'error':
				return t('progressError');
			default:
				return t('backupProgress');
		}
	}
</script>

{#if progress}
	<div
		class="workflow-progress"
		class:complete={progress.event === 'finished'}
		class:failed={progress.event === 'failed'}
	>
		<div class="workflow-head">
			<div>
				<span>{t('workflowProgress')}</span>
				<strong>{phaseLabel(progress.phase)}</strong>
			</div>
			<b>{percent}%</b>
		</div>
		<div
			class="workflow-track"
			class:streaming
			role="progressbar"
			aria-valuemin="0"
			aria-valuemax="100"
			aria-valuenow={percent}
		>
			<span style={`width: ${percent}%`}></span>
		</div>
		<div class="workflow-detail">
			{#if engineLabel}
				<small class="engine-used"><TerminalSquare size={13} /> {engineLabel}</small>
			{/if}
			<small>{detail || progress.message}</small>
			<small class="stage-note">{t('progressStageEstimate')}</small>
		</div>
	</div>
{/if}

<style>
	.workflow-progress {
		display: grid;
		gap: 7px;
		width: min(100%, 520px);
		padding: 10px 11px;
		border: 1px solid var(--border);
		border-radius: 9px;
		background: var(--surface-soft);
	}
	.workflow-head {
		display: flex;
		align-items: end;
		justify-content: space-between;
		gap: 12px;
	}
	.workflow-head > div,
	.workflow-detail {
		display: grid;
		gap: 2px;
		min-width: 0;
	}
	.workflow-head span,
	.workflow-detail small {
		color: var(--muted);
		font-size: 8px;
	}
	.workflow-head strong {
		color: var(--text);
		font-size: 10px;
	}
	.workflow-head b {
		color: var(--accent);
		font-size: 11px;
		font-variant-numeric: tabular-nums;
	}
	.workflow-track {
		position: relative;
		height: 7px;
		overflow: hidden;
		border-radius: 999px;
		background: color-mix(in srgb, var(--border) 72%, transparent);
	}
	.workflow-track > span {
		position: absolute;
		inset: 0 auto 0 0;
		min-width: 3px;
		border-radius: inherit;
		background: var(--accent);
		transition: width 220ms ease;
	}
	.workflow-track.streaming > span::after {
		position: absolute;
		inset: 0;
		background: linear-gradient(
			90deg,
			transparent 0%,
			color-mix(in srgb, white 35%, transparent) 50%,
			transparent 100%
		);
		content: '';
		animation: sweep 1.25s linear infinite;
	}
	.complete .workflow-track > span {
		background: var(--good);
	}
	.failed .workflow-track > span {
		background: var(--danger);
	}
	.engine-used {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		color: var(--text) !important;
		font-weight: 650;
	}
	.stage-note {
		color: var(--subtle) !important;
	}
	@keyframes sweep {
		from {
			transform: translateX(-100%);
		}
		to {
			transform: translateX(100%);
		}
	}
</style>
