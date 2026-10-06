<script lang="ts">
	import '../app.css';
	let { children } = $props();
</script>

<svelte:head>
	<title>Btrfs Harbor</title>
	<meta
		name="description"
		content="Native Btrfs backup, recovery and system replication control center"
	/>
	<link rel="icon" href="/logo-mark.svg" />
</svelte:head>

{@render children()}
