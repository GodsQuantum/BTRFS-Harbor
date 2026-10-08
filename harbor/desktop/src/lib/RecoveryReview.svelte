<script lang="ts">
	import CircleAlert from 'lucide-svelte/icons/circle-alert';
	import type { MachineRecoveryPlan, RecoveryActionGroup } from './recovery';
	import { groupRecoveryActions } from './recovery';
	import { translate, type Locale, type TranslationKey } from './i18n';

	export let plan: MachineRecoveryPlan;
	export let locale: Locale = 'en';
	const t = (key: TranslationKey) => translate(locale, key);
	$: groups = groupRecoveryActions(plan);
	const sections: Array<[RecoveryActionGroup, TranslationKey]> = [
		['restored', 'recoveryRestored'],
		['adapted', 'recoveryAdapted'],
		['regenerated', 'recoveryRegenerated'],
		['attention', 'recoveryNeedsAttention']
	];
</script>

<section class="review">
	<header><strong>{t('recoveryReview')}</strong><code>{plan.hostname}</code></header>
	{#if plan.compatibility === 'data_migration_only'}
		<div class="compat"><CircleAlert size={16} /><span>{t('recoveryDataOnly')}</span></div>
	{/if}
	<div class="groups">
		{#each sections as [group, label] (group)}
			{@const items = groups[group]}
			{#if items.length}
				<section class:attention={group === 'attention'}>
					<strong>{t(label)}</strong>
					<ul>
						{#each items as item (item.id)}<li>{item.description}</li>{/each}
					</ul>
				</section>
			{/if}
		{/each}
	</div>
</section>

<style>
	.review {
		display: grid;
		gap: 9px;
	}
	header {
		display: flex;
		justify-content: space-between;
		align-items: center;
		gap: 10px;
	}
	header strong {
		font-size: 12px;
	}
	code {
		color: var(--muted);
		font-size: 9px;
	}
	.groups {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 8px;
	}
	.groups section {
		padding: 9px 10px;
		border: 1px solid var(--border);
		border-radius: 9px;
		background: var(--surface-soft);
	}
	.groups section.attention {
		border-color: color-mix(in srgb, var(--warning) 40%, var(--border));
	}
	.groups strong {
		font-size: 9px;
		text-transform: uppercase;
		letter-spacing: 0.05em;
	}
	ul {
		margin: 6px 0 0;
		padding-left: 16px;
		color: var(--muted);
		font-size: 9px;
		line-height: 1.45;
	}
	.compat {
		display: flex;
		gap: 8px;
		align-items: flex-start;
		padding: 9px 10px;
		border: 1px solid color-mix(in srgb, var(--warning) 40%, var(--border));
		border-radius: 9px;
		color: var(--warning);
		font-size: 9px;
		line-height: 1.4;
	}
	@media (max-width: 760px) {
		.groups {
			grid-template-columns: 1fr;
		}
	}
</style>
