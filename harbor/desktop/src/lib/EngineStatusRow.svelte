<script lang="ts">
	import TerminalSquare from 'lucide-svelte/icons/terminal-square';
	import ChevronRight from 'lucide-svelte/icons/chevron-right';
	import { engineHeadline, type EngineSelectionStatus } from './engine';
	import { translate, type Locale, type TranslationKey } from './i18n';

	export let status: EngineSelectionStatus | null = null;
	export let locale: Locale = 'en';
	export let loading = false;
	export let onManage: () => void = () => {};

	const t = (key: TranslationKey) => translate(locale, key);
</script>

<div class="engine-row" aria-live="polite">
	<div class="engine-icon"><TerminalSquare size={16} /></div>
	<div class="engine-copy">
		<span>{t('engine')}</span>
		{#if status}
			<strong>{engineHeadline(status)}</strong>
			{#if status.fallback_reason}<small>{status.fallback_reason}</small>{/if}
		{:else}
			<strong>{loading ? '…' : '—'}</strong>
		{/if}
	</div>
	<button class="engine-manage" type="button" onclick={onManage} disabled={loading}>
		{t('manageEngine')}
		<ChevronRight size={14} />
	</button>
</div>

<style>
	.engine-row {
		display: grid;
		grid-template-columns: auto minmax(0, 1fr) auto;
		align-items: center;
		gap: 10px;
		padding: 9px 11px;
		border: 1px solid var(--border);
		border-radius: 9px;
		background: var(--surface);
	}
	.engine-icon {
		display: grid;
		place-items: center;
		width: 30px;
		height: 30px;
		border-radius: 8px;
		color: var(--accent);
		background: var(--surface-soft);
	}
	.engine-copy {
		display: grid;
		gap: 2px;
		min-width: 0;
	}
	.engine-copy span,
	.engine-copy small {
		color: var(--muted);
		font-size: 8px;
	}
	.engine-copy strong {
		overflow: hidden;
		color: var(--text);
		font-size: 10px;
		font-weight: 650;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.engine-copy small {
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.engine-manage {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		padding: 5px 8px;
		border: 0;
		background: transparent;
		color: var(--accent);
		font: inherit;
		font-size: 9px;
		font-weight: 650;
		cursor: pointer;
	}
	.engine-manage:disabled {
		opacity: 0.5;
		cursor: default;
	}
	@media (max-width: 760px) {
		.engine-row {
			grid-template-columns: auto minmax(0, 1fr);
		}
		.engine-manage {
			grid-column: 2;
			justify-self: start;
			padding-left: 0;
		}
	}
</style>
