<script lang="ts">
	import { onMount } from 'svelte';
	import RotateCcw from 'lucide-svelte/icons/rotate-ccw';
	import { loadCheckpointTransfers } from './agent';
	import { checkpointStateLabel, type CheckpointTransfer } from './checkpoint';
	import { formatBytesBinary } from './status';
	import type { Locale } from './i18n';

	export let target = '';
	export let locale: Locale = 'en';

	let entries: CheckpointTransfer[] = [];
	let loading = false;
	let error = '';

	const labels: Record<
		Locale,
		{
			title: string;
			help: string;
			refresh: string;
			none: string;
			checkpoints: string;
			verified: string;
			last: string;
			noTarget: string;
		}
	> = {
		en: {
			title: 'Checkpoint transfers · experimental',
			help: 'Read-only inspection of local v2 manifests. Published does not mean restore-tested.',
			refresh: 'Refresh',
			none: 'No v2 checkpoint manifests in this destination.',
			checkpoints: 'checkpoints',
			verified: 'Restore not verified',
			last: 'Last committed',
			noTarget: 'Choose a mounted local/NFS/SMB destination to inspect.'
		},
		fr: {
			title: 'Transferts par checkpoints · expérimental',
			help: 'Consultation sans écriture des manifests v2. Publié ne signifie pas restauration testée.',
			refresh: 'Actualiser',
			none: 'Aucun manifest v2 dans cette destination.',
			checkpoints: 'checkpoints',
			verified: 'Restauration non vérifiée',
			last: 'Dernier commit',
			noTarget: 'Choisir une destination locale/NFS/SMB montée pour l’inspection.'
		},
		'zh-CN': {
			title: '检查点传输 · 实验性',
			help: '只读查看本地 v2 清单。已发布不代表已通过恢复测试。',
			refresh: '刷新',
			none: '此目标中没有 v2 检查点清单。',
			checkpoints: '个检查点',
			verified: '恢复尚未验证',
			last: '最后提交',
			noTarget: '请选择已挂载的本地/NFS/SMB 目标。'
		}
	};
	$: copy = labels[locale];

	async function refresh() {
		if (!target.startsWith('/')) {
			entries = [];
			return;
		}
		loading = true;
		error = '';
		try {
			entries = await loadCheckpointTransfers(target);
		} catch (cause) {
			error = cause instanceof Error ? cause.message : String(cause);
			entries = [];
		} finally {
			loading = false;
		}
	}

	onMount(() => {
		void refresh();
	});
</script>

<article class="panel checkpoint-preview">
	<div class="checkpoint-heading">
		<div>
			<h3>{copy.title}</h3>
			<p>{copy.help}</p>
		</div>
		<button
			type="button"
			class="secondary compact"
			onclick={refresh}
			disabled={loading || !target.startsWith('/')}
		>
			<span class:spin={loading}><RotateCcw size={14} /></span>
			{copy.refresh}
		</button>
	</div>
	{#if !target.startsWith('/')}
		<p class="empty-state">{copy.noTarget}</p>
	{:else if error}
		<p class="error-text" role="alert">{error}</p>
	{:else if entries.length === 0 && !loading}
		<p class="empty-state">{copy.none}</p>
	{:else}
		<div class="checkpoint-list">
			{#each entries as item (item.transfer_id)}
				<div class="checkpoint-entry">
					<div>
						<strong>{checkpointStateLabel(item.state, locale)}</strong>
						<small>{item.source}</small>
						<code>{item.transfer_id}</code>
					</div>
					<div class="checkpoint-numbers">
						<b>{formatBytesBinary(item.committed_raw_bytes)}</b>
						<small>{item.checkpoint_count} {copy.checkpoints}</small>
						<small>{copy.verified}</small>
						{#if item.last_checkpoint_at}<small>{copy.last}: {item.last_checkpoint_at}</small>{/if}
					</div>
				</div>
			{/each}
		</div>
	{/if}
</article>

<style>
	.checkpoint-preview {
		display: grid;
		gap: 12px;
		padding: 14px;
	}
	.checkpoint-heading {
		display: flex;
		align-items: start;
		justify-content: space-between;
		gap: 14px;
	}
	.checkpoint-heading h3 {
		margin: 0;
		font-size: 13px;
	}
	.checkpoint-heading p {
		margin: 5px 0 0;
		font-size: 10px;
		color: var(--muted);
		max-width: 650px;
	}
	.checkpoint-list {
		display: grid;
		gap: 7px;
	}
	.checkpoint-entry {
		display: flex;
		justify-content: space-between;
		gap: 12px;
		border: 1px solid var(--border);
		border-radius: 9px;
		padding: 10px;
	}
	.checkpoint-entry > div {
		display: grid;
		gap: 4px;
		min-width: 0;
	}
	.checkpoint-entry strong {
		font-size: 11px;
	}
	.checkpoint-entry code {
		color: var(--muted);
		font-size: 9px;
		word-break: break-word;
	}
	.checkpoint-entry small {
		font-size: 9px;
		color: var(--muted);
		overflow-wrap: anywhere;
	}
	.checkpoint-numbers {
		text-align: right;
	}
	.checkpoint-numbers b {
		font-size: 11px;
		font-variant-numeric: tabular-nums;
	}
	.spin {
		animation: rotating 1s linear infinite;
	}
	@keyframes rotating {
		to {
			transform: rotate(360deg);
		}
	}
</style>
