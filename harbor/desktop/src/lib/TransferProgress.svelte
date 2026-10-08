<script lang="ts">
	import Activity from 'lucide-svelte/icons/activity';
	import {
		aggregateTransferProgress,
		formatBytesBinary,
		formatDuration,
		formatRateBinary,
		type TransferProgressEvent
	} from './status';
	import { translate, type Locale, type TranslationKey } from './i18n';

	export let events: TransferProgressEvent[] = [];
	export let locale: Locale = 'en';

	const t = (key: TranslationKey) => translate(locale, key);
	$: summary = aggregateTransferProgress(events);
	$: hasComparableTotal = summary.percent !== null;
</script>

{#if events.length > 0}
	<div class="transfer-progress">
		<div class="transfer-head">
			<div>
				<span>{t('transferProgress')}</span>
				<strong>{formatBytesBinary(summary.bytesTarget)} {t('transferCopied')}</strong>
			</div>
			{#if hasComparableTotal}
				<b>~{summary.percent?.toFixed(1)}%</b>
			{:else}
				<b>{t('progressInProgress')}</b>
			{/if}
		</div>

		<div
			class:indeterminate={!hasComparableTotal}
			class="transfer-track"
			role="progressbar"
			aria-valuemin="0"
			aria-valuemax="100"
			aria-valuenow={summary.percent ?? undefined}
			aria-valuetext={!hasComparableTotal ? t('transferUnknownTotal') : undefined}
		>
			<span style={hasComparableTotal ? `width: ${summary.percent}%` : undefined}></span>
		</div>

		<div class="transfer-metrics">
			<span><Activity size={13} /> {formatRateBinary(summary.bytesPerSecond)}</span>
			<span>{t('transferElapsed')} {formatDuration(summary.elapsedSeconds)}</span>
			{#if summary.etaSeconds !== null}
				<span>{t('transferRemaining')} ~{formatDuration(summary.etaSeconds)}</span>
			{:else}
				<span>{t('transferEtaUnavailable')}</span>
			{/if}
		</div>

		{#if !hasComparableTotal}
			<small>{t('transferUnknownTotal')}</small>
		{/if}

		<details>
			<summary>{t('transferDetails')}</summary>
			<div class="transfer-list">
				{#each events as event (`${event.volume}:${event.snapshot}:${event.destination}`)}
					<div>
						<strong>{event.volume}</strong>
						<span>{formatBytesBinary(event.bytes_target ?? 0)}</span>
						<span>{formatRateBinary(event.bytes_per_second ?? 0)}</span>
					</div>
				{/each}
			</div>
		</details>
	</div>
{/if}

<style>
	.transfer-progress {
		display: grid;
		gap: 8px;
		width: min(100%, 520px);
		padding: 10px 11px;
		border: 1px solid var(--border);
		border-radius: 9px;
		background: var(--surface-soft);
	}
	.transfer-head,
	.transfer-metrics,
	.transfer-list > div {
		display: flex;
		align-items: center;
		justify-content: space-between;
		gap: 10px;
	}
	.transfer-head > div {
		display: grid;
		gap: 2px;
	}
	.transfer-head span,
	small,
	.transfer-list span {
		color: var(--muted);
		font-size: 8px;
	}
	.transfer-head strong,
	.transfer-head b {
		font-size: 10px;
	}
	.transfer-head b {
		color: var(--accent);
		font-variant-numeric: tabular-nums;
	}
	.transfer-track {
		position: relative;
		height: 7px;
		overflow: hidden;
		border-radius: 999px;
		background: color-mix(in srgb, var(--border) 72%, transparent);
	}
	.transfer-track > span {
		position: absolute;
		inset: 0 auto 0 0;
		height: 100%;
		min-width: 3px;
		border-radius: inherit;
		background: var(--accent);
		transition: width 220ms ease;
	}
	.transfer-track.indeterminate > span {
		width: 34%;
		animation: sweep 1.2s ease-in-out infinite alternate;
	}
	.transfer-metrics {
		justify-content: flex-start;
		flex-wrap: wrap;
		color: var(--text);
		font-size: 9px;
		font-variant-numeric: tabular-nums;
	}
	.transfer-metrics span {
		display: inline-flex;
		align-items: center;
		gap: 4px;
	}
	details summary {
		cursor: pointer;
		color: var(--muted);
		font-size: 8px;
	}
	.transfer-list {
		display: grid;
		gap: 5px;
		margin-top: 6px;
	}
	.transfer-list > div {
		justify-content: flex-start;
		font-size: 8px;
	}
	.transfer-list strong {
		min-width: 60px;
	}
	@keyframes sweep {
		from {
			transform: translateX(-10%);
		}
		to {
			transform: translateX(205%);
		}
	}
</style>
