<script lang="ts">
	import HardDrive from 'lucide-svelte/icons/hard-drive';
	import Laptop from 'lucide-svelte/icons/laptop';
	import type { RecoveryIntent } from './recovery';
	import { translate, type Locale, type TranslationKey } from './i18n';

	export let intent: RecoveryIntent = 'replace_machine';
	export let sourceHostname = '';
	export let requestedHostname = '';
	export let locale: Locale = 'en';
	export let onIntent: (value: RecoveryIntent) => void = () => {};
	export let onHostname: (value: string) => void = () => {};

	const t = (key: TranslationKey) => translate(locale, key);
</script>

<section class="intent-block">
	<div class="intent-heading">
		<span>1</span>
		<div>
			<strong>{t('recoveryIntentQuestion')}</strong>
			<small>{sourceHostname || '-'}</small>
		</div>
	</div>
	<div class="intent-options">
		<button
			type="button"
			class:active={intent === 'replace_machine'}
			onclick={() => onIntent('replace_machine')}
		>
			<HardDrive size={19} />
			<div>
				<strong>{t('recoveryReplaceMachine')}</strong><small
					>{t('recoveryReplaceMachineDesc')}</small
				>
			</div>
		</button>
		<button
			type="button"
			class:active={intent === 'migrate_machine'}
			onclick={() => onIntent('migrate_machine')}
		>
			<Laptop size={19} />
			<div>
				<strong>{t('recoveryMigrateMachine')}</strong><small
					>{t('recoveryMigrateMachineDesc')}</small
				>
			</div>
		</button>
	</div>
	{#if intent === 'migrate_machine'}
		<label>
			<span>{t('recoveryNewHostname')}</span>
			<input
				value={requestedHostname}
				oninput={(event) => onHostname(event.currentTarget.value)}
				placeholder="ASUS-N55SF"
			/>
		</label>
	{/if}
</section>

<style>
	.intent-block {
		display: grid;
		gap: 10px;
	}
	.intent-heading {
		display: flex;
		align-items: center;
		gap: 9px;
	}
	.intent-heading > span {
		display: grid;
		place-items: center;
		width: 24px;
		height: 24px;
		border-radius: 50%;
		background: var(--accent);
		color: var(--accent-contrast);
		font-weight: 800;
		font-size: 10px;
	}
	.intent-heading div {
		display: grid;
		gap: 2px;
	}
	.intent-heading strong {
		font-size: 12px;
	}
	.intent-heading small,
	.intent-options small {
		color: var(--muted);
		font-size: 9px;
		line-height: 1.4;
	}
	.intent-options {
		display: grid;
		grid-template-columns: repeat(2, minmax(0, 1fr));
		gap: 8px;
	}
	.intent-options button {
		display: flex;
		gap: 9px;
		align-items: flex-start;
		text-align: left;
		padding: 11px;
		border: 1px solid var(--border);
		border-radius: 10px;
		background: var(--surface-soft);
		color: var(--text);
		cursor: pointer;
	}
	.intent-options button.active {
		border-color: var(--accent);
		box-shadow: inset 0 0 0 1px var(--accent);
	}
	.intent-options button > div {
		display: grid;
		gap: 3px;
	}
	label {
		display: grid;
		gap: 5px;
		font-size: 10px;
		font-weight: 650;
	}
	input {
		width: 100%;
		box-sizing: border-box;
		border: 1px solid var(--border);
		border-radius: 9px;
		background: var(--surface-strong);
		color: var(--text);
		padding: 9px 10px;
		font: inherit;
	}
	@media (max-width: 760px) {
		.intent-options {
			grid-template-columns: 1fr;
		}
	}
</style>
