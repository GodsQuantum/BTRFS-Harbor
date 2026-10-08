<script lang="ts">
	import RefreshCw from 'lucide-svelte/icons/refresh-cw';
	import Download from 'lucide-svelte/icons/download';
	import TerminalSquare from 'lucide-svelte/icons/terminal-square';
	import type { EnginePolicy, EngineSelectionStatus, EngineUpdateOptions } from './engine';
	import { translate, type Locale, type TranslationKey } from './i18n';

	export let status: EngineSelectionStatus | null = null;
	export let updateOptions: EngineUpdateOptions | null = null;
	export let policy: EnginePolicy = 'auto';
	export let locale: Locale = 'en';
	export let busy = false;
	export let error = '';
	export let onPolicyChange: (policy: EnginePolicy) => void | Promise<void> = () => {};
	export let onUpdate: () => void | Promise<void> = () => {};
	export let onRefresh: () => void | Promise<void> = () => {};

	const t = (key: TranslationKey) => translate(locale, key);

	function policyHelp(value: EnginePolicy): string {
		if (value === 'system') return t('engineSystemHelp');
		if (value === 'bundled') return t('engineBundledHelp');
		return t('engineAutoHelp');
	}

	function policyLabel(value: EnginePolicy): string {
		if (value === 'system') return t('engineSystem');
		if (value === 'bundled') return t('engineBundled');
		return t('engineAuto');
	}

	$: updateLabel =
		status?.active.origin === 'bundled' ? t('engineUpdateHarbor') : t('engineUpdateSystem');
	$: canUseSystem = Boolean(status?.system?.compatible);
</script>

<div class="engine-manager">
	<div class="manager-head">
		<div>
			<span>{t('enginePolicy')}</span>
			<strong>{t('upstreamEngine')}</strong>
		</div>
		<button
			class="icon-button"
			type="button"
			onclick={onRefresh}
			disabled={busy}
			title={t('refreshEngine')}
		>
			<span class:spin={busy}><RefreshCw size={15} /></span>
		</button>
	</div>

	<div class="policy-options" role="radiogroup" aria-label={t('enginePolicy')}>
		{#each ['auto', 'system', 'bundled'] as option (option)}
			{@const value = option as EnginePolicy}
			<button
				type="button"
				class:active={policy === value}
				disabled={busy || (value === 'system' && !canUseSystem)}
				aria-pressed={policy === value}
				title={policyHelp(value)}
				onclick={() => onPolicyChange(value)}
			>
				<strong>{policyLabel(value)}</strong>
				<small>{policyHelp(value)}</small>
			</button>
		{/each}
	</div>

	{#if status}
		<div class="engine-details">
			<div class="active-engine">
				<TerminalSquare size={16} />
				<div>
					<span>{t('activeEngine')}</span>
					<strong>
						{status.active.origin === 'system' ? t('engineSystem') : t('engineBundled')}
						{status.active.version ?? '—'}
					</strong>
					<code>{status.active.executable}</code>
				</div>
			</div>
			<div class="candidate-grid">
				<div>
					<span>{t('systemEngine')}</span>
					<strong>
						{status.system
							? `${status.system.version ?? '—'} · ${status.system.compatible ? 'OK' : 'incompatible'}`
							: '—'}
					</strong>
					{#if status.system}<code>{status.system.executable}</code>{/if}
				</div>
				<div>
					<span>{t('bundledEngine')}</span>
					<strong>{status.bundled?.version ?? '—'}</strong>
					{#if status.bundled}<code>{status.bundled.executable}</code>{/if}
				</div>
				<div>
					<span>{t('minimumEngine')}</span>
					<strong>{status.minimum_version}</strong>
				</div>
			</div>
			{#if status.fallback_reason}
				<p class="fallback"><strong>{t('engineFallback')}:</strong> {status.fallback_reason}</p>
			{/if}
		</div>
	{/if}

	{#if updateOptions}
		<div class="update-row">
			<div>
				<span>{t('updateEngine')}</span>
				<p>{updateOptions.reason}</p>
			</div>
			<button
				class="update-button"
				type="button"
				disabled={busy || !updateOptions.can_update}
				onclick={onUpdate}
			>
				<Download size={15} />
				{busy ? t('updatingEngine') : updateLabel}
			</button>
		</div>
		{#if updateOptions.provenance === 'manual_unknown'}
			<small class="warning">{t('engineUnknownManual')}</small>
		{/if}
	{/if}

	{#if error}<p class="error">{error}</p>{/if}
</div>

<style>
	.engine-manager {
		display: grid;
		gap: 12px;
	}
	.manager-head,
	.update-row,
	.active-engine {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 12px;
	}
	.manager-head > div,
	.active-engine > div,
	.update-row > div {
		display: grid;
		gap: 3px;
		min-width: 0;
	}
	.manager-head span,
	.active-engine span,
	.candidate-grid span,
	.update-row span {
		color: var(--muted);
		font-size: 8px;
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
	.manager-head strong,
	.active-engine strong,
	.candidate-grid strong {
		color: var(--text);
		font-size: 10px;
	}
	.icon-button {
		display: grid;
		place-items: center;
		width: 30px;
		height: 30px;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--surface-soft);
		color: var(--text);
		cursor: pointer;
	}
	.policy-options {
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		gap: 7px;
	}
	.policy-options button {
		display: grid;
		gap: 4px;
		min-height: 62px;
		padding: 9px;
		border: 1px solid var(--border);
		border-radius: 8px;
		background: var(--surface-soft);
		color: var(--text);
		text-align: left;
		cursor: pointer;
	}
	.policy-options button.active {
		border-color: var(--accent);
		box-shadow: inset 0 0 0 1px var(--accent);
	}
	.policy-options button:disabled {
		opacity: 0.45;
		cursor: not-allowed;
	}
	.policy-options small,
	.update-row p,
	.fallback,
	.warning {
		margin: 0;
		color: var(--muted);
		font-size: 8px;
		line-height: 1.45;
	}
	.active-engine {
		justify-content: flex-start;
		padding: 9px;
		border-radius: 8px;
		background: var(--surface-soft);
		color: var(--accent);
	}
	code {
		overflow: hidden;
		color: var(--muted);
		font-size: 8px;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.candidate-grid {
		display: grid;
		grid-template-columns: repeat(3, minmax(0, 1fr));
		gap: 7px;
		margin-top: 7px;
	}
	.candidate-grid > div {
		display: grid;
		gap: 3px;
		min-width: 0;
		padding: 8px;
		border: 1px solid var(--border);
		border-radius: 8px;
	}
	.fallback {
		margin-top: 7px;
	}
	.update-row {
		padding-top: 10px;
		border-top: 1px solid var(--border);
	}
	.update-button {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		flex: 0 0 auto;
		padding: 7px 10px;
		border: 0;
		border-radius: 8px;
		background: var(--accent);
		color: var(--accent-contrast);
		font: inherit;
		font-size: 9px;
		font-weight: 700;
		cursor: pointer;
	}
	.update-button:disabled {
		opacity: 0.5;
		cursor: not-allowed;
	}
	.warning {
		color: var(--warning);
	}
	.error {
		margin: 0;
		color: var(--danger);
		font-size: 9px;
	}
	.spin {
		animation: spin 0.8s linear infinite;
	}
	@keyframes spin {
		to {
			transform: rotate(360deg);
		}
	}
	@media (max-width: 760px) {
		.policy-options,
		.candidate-grid {
			grid-template-columns: 1fr;
		}
		.update-row {
			align-items: stretch;
			flex-direction: column;
		}
		.update-button {
			justify-content: center;
		}
	}
</style>
