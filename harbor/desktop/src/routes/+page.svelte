<script lang="ts">
	import { onMount } from 'svelte';
	import Activity from 'lucide-svelte/icons/activity';
	import Anchor from 'lucide-svelte/icons/anchor';
	import ArchiveRestore from 'lucide-svelte/icons/archive-restore';
	import ArrowRight from 'lucide-svelte/icons/arrow-right';
	import Box from 'lucide-svelte/icons/box';
	import Check from 'lucide-svelte/icons/check';
	import ChevronRight from 'lucide-svelte/icons/chevron-right';
	import CircleAlert from 'lucide-svelte/icons/circle-alert';
	import Clock3 from 'lucide-svelte/icons/clock-3';
	import Copy from 'lucide-svelte/icons/copy';
	import DatabaseBackup from 'lucide-svelte/icons/database-backup';
	import Download from 'lucide-svelte/icons/download';
	import Gauge from 'lucide-svelte/icons/gauge';
	import HardDrive from 'lucide-svelte/icons/hard-drive';
	import History from 'lucide-svelte/icons/history';
	import Languages from 'lucide-svelte/icons/languages';
	import Laptop from 'lucide-svelte/icons/laptop';
	import Layers3 from 'lucide-svelte/icons/layers-3';
	import LifeBuoy from 'lucide-svelte/icons/life-buoy';
	import MonitorCog from 'lucide-svelte/icons/monitor-cog';
	import Moon from 'lucide-svelte/icons/moon';
	import Network from 'lucide-svelte/icons/network';
	import Play from 'lucide-svelte/icons/play';
	import RotateCcw from 'lucide-svelte/icons/rotate-ccw';
	import Server from 'lucide-svelte/icons/server';
	import Settings from 'lucide-svelte/icons/settings';
	import ShieldCheck from 'lucide-svelte/icons/shield-check';
	import ShieldEllipsis from 'lucide-svelte/icons/shield-ellipsis';
	import Sun from 'lucide-svelte/icons/sun';
	import TerminalSquare from 'lucide-svelte/icons/terminal-square';
	import Wifi from 'lucide-svelte/icons/wifi';
	import WifiOff from 'lucide-svelte/icons/wifi-off';
	import {
		installFullHarbor,
		loadDashboardStatus,
		loadHarborConfiguration,
		loadInstallationState,
		loadSystemIdentity,
		sendProfileNow,
		type InstallationState,
		type SystemIdentity
	} from '#lib/agent.ts';
	import { type HarborConfig } from '#lib/config.ts';
	import {
		dictionaries,
		localeLabels,
		translate,
		type Locale,
		type TranslationKey
	} from '#lib/i18n.ts';
	import ProtectionEditor from '#lib/ProtectionEditor.svelte';
	import RecoveryEditor from '#lib/RecoveryEditor.svelte';
	import ReplicaEditor from '#lib/ReplicaEditor.svelte';
	import {
		demoStatus,
		protectionState,
		type BackupProgressEvent,
		type DashboardStatus
	} from '#lib/status.ts';

	type Page =
		| 'overview'
		| 'protection'
		| 'timeline'
		| 'recover'
		| 'replicate'
		| 'destinations'
		| 'activity'
		| 'settings';

	let locale: Locale = 'en';
	let active: Page = 'overview';
	let advanced = false;
	let dark = false;
	let loading = true;
	let sending = false;
	let backupProgress: BackupProgressEvent | null = null;
	let dashboard: DashboardStatus = demoStatus;
	let harborConfig: HarborConfig | null = null;
	let configWarning = '';
	let installationState: InstallationState | null = null;
	let systemIdentity: SystemIdentity | null = null;
	let installingFull = false;
	let setupMessage = '';
	let setupError = '';
	$: protection = protectionState(dashboard.engine);

	const t = (key: TranslationKey) => translate(locale, key);

	onMount(async () => {
		const savedLocale = localStorage.getItem('btrfs-harbor-locale') as Locale | null;
		if (savedLocale && savedLocale in dictionaries) locale = savedLocale;
		const savedTheme = localStorage.getItem('btrfs-harbor-theme');
		dark = savedTheme
			? savedTheme === 'dark'
			: window.matchMedia('(prefers-color-scheme: dark)').matches;

		const [dashboardResult, installState, identity] = await Promise.all([
			loadDashboardStatus(),
			loadInstallationState().catch(() => null),
			loadSystemIdentity().catch(() => null)
		]);
		dashboard = dashboardResult;
		installationState = installState;
		systemIdentity = identity;
		try {
			harborConfig = await loadHarborConfiguration();
		} catch (error) {
			configWarning = error instanceof Error ? error.message : String(error);
		}
		loading = false;
	});

	function setLocale(next: Locale) {
		locale = next;
		localStorage.setItem('btrfs-harbor-locale', next);
	}

	function toggleTheme() {
		dark = !dark;
		localStorage.setItem('btrfs-harbor-theme', dark ? 'dark' : 'light');
	}

	async function installFullPackage() {
		installingFull = true;
		setupMessage = '';
		setupError = '';
		try {
			await installFullHarbor();
			installationState = await loadInstallationState();
			dashboard = await loadDashboardStatus();
			setupMessage = t('installationComplete');
		} catch (error) {
			setupError = error instanceof Error ? error.message : String(error);
		} finally {
			installingFull = false;
		}
	}

	function progressLabel(phase: string): string {
		switch (phase) {
			case 'start':
			case 'prepare':
				return t('progressPreparing');
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

	async function sendSnapshot() {
		if (!dashboard.profileId) {
			dashboard.warning = t('noLiveProfile');
			return;
		}
		sending = true;
		backupProgress = null;
		try {
			await sendProfileNow(dashboard.profileId, (event) => {
				if (event.event !== 'output' || advanced) backupProgress = event;
			});
			dashboard = await loadDashboardStatus();
		} catch (error) {
			dashboard.warning = error instanceof Error ? error.message : String(error);
		} finally {
			sending = false;
		}
	}

	function pageTitle(page: Page): string {
		return t(page as TranslationKey);
	}

	const timelineRows = [
		{
			time: '02:13',
			name: 'srv-20261005T021300',
			source: '/srv',
			tags: ['local', 'backedUp', 'verified', 'incremental']
		},
		{
			time: '02:12',
			name: 'root-home-20261005T021200',
			source: '/root',
			tags: ['local', 'backedUp', 'verified', 'incremental']
		},
		{
			time: '02:11',
			name: 'home-20261005T021100',
			source: '/home',
			tags: ['local', 'backedUp', 'verified', 'incremental']
		},
		{
			time: '02:10',
			name: 'root-20261005T021000',
			source: '/',
			tags: ['local', 'backedUp', 'verified', 'incremental', 'restoreTested']
		}
	] as const;
</script>

<div class:dark class="app-shell">
	<aside class="sidebar">
		<button class="brand" onclick={() => (active = 'overview')} aria-label={t('overview')}>
			<img src="/logo-mark.svg" alt="" />
			<div>
				<strong>{t('appName')}</strong>
				<span>{t('tagline')}</span>
			</div>
		</button>

		<nav aria-label="Primary">
			<button class:active={active === 'overview'} onclick={() => (active = 'overview')}>
				<Gauge size={18} strokeWidth={1.8} /><span>{t('overview')}</span>
			</button>
			<button class:active={active === 'protection'} onclick={() => (active = 'protection')}>
				<ShieldCheck size={18} strokeWidth={1.8} /><span>{t('protection')}</span>
			</button>
			{#if dashboard.source === 'live' || dashboard.source === 'demo'}
				<button class:active={active === 'timeline'} onclick={() => (active = 'timeline')}>
					<History size={18} strokeWidth={1.8} /><span>{t('timeline')}</span>
				</button>
				<button class:active={active === 'recover'} onclick={() => (active = 'recover')}>
					<LifeBuoy size={18} strokeWidth={1.8} /><span>{t('recover')}</span>
				</button>
				<button class:active={active === 'destinations'} onclick={() => (active = 'destinations')}>
					<HardDrive size={18} strokeWidth={1.8} /><span>{t('destinations')}</span>
				</button>
				{#if advanced}
					<button class:active={active === 'replicate'} onclick={() => (active = 'replicate')}>
						<Copy size={18} strokeWidth={1.8} /><span>{t('replicate')}</span>
					</button>
					<button class:active={active === 'activity'} onclick={() => (active = 'activity')}>
						<Activity size={18} strokeWidth={1.8} /><span>{t('activity')}</span>
					</button>
				{/if}
			{/if}
			<button class:active={active === 'settings'} onclick={() => (active = 'settings')}>
				<Settings size={18} strokeWidth={1.8} /><span>{t('settings')}</span>
			</button>
		</nav>

		<div class="sidebar-foot">
			<div class="agent-state">
				<span
					class:live={dashboard.source === 'live'}
					class:demo={dashboard.source === 'demo' || dashboard.source === 'setup'}
					class:offline={dashboard.source === 'offline'}
				></span>
				<div>
					<strong>
						{dashboard.source === 'live'
							? t('protectionActive')
							: dashboard.source === 'demo'
								? t('demoData')
								: dashboard.source === 'setup'
									? installationState?.portable_appimage
										? t('portableMode')
										: t('setupRequired')
									: t('systemProblem')}
					</strong>
					<small>
						{dashboard.source === 'live'
							? t('backgroundProtection')
							: dashboard.source === 'setup'
								? t('setupThisComputer')
								: t('systemStatus')}
					</small>
				</div>
			</div>
			{#if dashboard.source === 'live'}<p>{t('closeSafe')}</p>{/if}
		</div>
	</aside>

	<main>
		<header class="topbar">
			<div>
				<p class="eyebrow">{t('comingFoundation')}</p>
				<h1>{pageTitle(active)}</h1>
			</div>
			<div class="top-actions">
				<div class="segmented" aria-label="Complexity">
					<button class:active={!advanced} onclick={() => (advanced = false)}>{t('simple')}</button>
					<button class:active={advanced} onclick={() => (advanced = true)}>{t('advanced')}</button>
				</div>
				<div class="locale">
					<Languages size={16} />
					<select
						aria-label={t('language')}
						value={locale}
						onchange={(event) =>
							setLocale((event.currentTarget as HTMLSelectElement).value as Locale)}
					>
						{#each Object.entries(localeLabels) as [code, label] (code)}
							<option value={code}>{label}</option>
						{/each}
					</select>
				</div>
				<button class="icon-button" onclick={toggleTheme} aria-label={t('theme')}>
					{#if dark}<Sun size={17} />{:else}<Moon size={17} />{/if}
				</button>
			</div>
		</header>

		{#if dashboard.source === 'demo'}
			<div class="demo-banner">
				<DatabaseBackup size={15} />
				<div>
					<strong>{t('demoData')}</strong>
					<span>{t('demoExplain')}</span>
				</div>
			</div>
		{:else if dashboard.source === 'offline'}
			<div class="demo-banner danger">
				<CircleAlert size={16} />
				<div>
					<strong>{t('systemProblem')}</strong>
					<span>{dashboard.warning ?? t('noLiveProfile')}</span>
				</div>
			</div>
		{/if}

		{#if active === 'overview' && dashboard.source === 'setup'}
			<section class="content-stack setup-stack">
				<div class="page-intro">
					<div class="intro-icon"><Laptop size={24} /></div>
					<div>
						<h2>{t('setupTitle')}</h2>
						<p>{t('setupIntro')}</p>
						{#if systemIdentity}
							<div class="system-facts">
								<span><strong>{systemIdentity.hostname}</strong></span>
								<span>{systemIdentity.pretty_name}</span>
								<span>{systemIdentity.architecture}</span>
								<span class:good={systemIdentity.root_fs === 'btrfs'}>{systemIdentity.root_fs}</span
								>
							</div>
						{/if}
					</div>
				</div>
				{#if installationState?.portable_appimage && !installationState.helper_installed}
					<article class="callout install-callout">
						<CircleAlert size={20} />
						<div>
							<strong>{t('portableMode')}</strong>
							<p>{t('portableModeDesc')}</p>
							<button
								class="primary compact"
								onclick={installFullPackage}
								disabled={installingFull}
							>
								{#if installingFull}
									<RotateCcw class="spin" size={15} /> {t('installingHarbor')}
								{:else}
									<Download size={15} /> {t('installHarbor')}
								{/if}
							</button>
							{#if setupMessage}<small class="good-status">{setupMessage}</small>{/if}
							{#if setupError}<small class="error-text">{setupError}</small>{/if}
						</div>
					</article>
				{/if}
				{#if harborConfig}
					<ProtectionEditor
						bind:config={harborConfig}
						{advanced}
						{locale}
						runtime={null}
						onApplied={async () => {
							dashboard = await loadDashboardStatus();
							installationState = await loadInstallationState().catch(() => installationState);
							harborConfig = await loadHarborConfiguration();
						}}
					/>
				{:else}
					<article class="callout danger">
						<CircleAlert size={21} />
						<div>
							<strong>{t('systemProblem')}</strong>
							<p>{configWarning || t('setupIntro')}</p>
						</div>
					</article>
				{/if}
			</section>
		{:else if active === 'overview'}
			<section class="overview-grid">
				<article class="protection-hero {protection}">
					<div class="hero-copy">
						<div class="status-icon">
							{#if protection === 'protected'}
								<ShieldCheck size={28} />
							{:else if protection === 'risk'}
								<ShieldEllipsis size={28} />
							{:else}
								<CircleAlert size={28} />
							{/if}
						</div>
						<div>
							<p class="eyebrow">{t('protection')}</p>
							<h2>
								{protection === 'protected'
									? t('systemProtected')
									: protection === 'risk'
										? t('systemAtRisk')
										: t('systemUnprotected')}
							</h2>
							<p>
								{protection === 'protected'
									? t('protectedDescription')
									: protection === 'risk'
										? t('atRiskDescription')
										: t('unprotectedDescription')}
							</p>
						</div>
					</div>
					<div class="hero-action-stack">
						<button class="primary" onclick={sendSnapshot} disabled={sending || loading}>
							{#if sending}
								<RotateCcw class="spin" size={17} /> {t('sending')}
							{:else}
								<Play size={17} fill="currentColor" /> {t('sendNow')}
							{/if}
						</button>
						{#if backupProgress}
							<div
								class="backup-progress"
								class:complete={backupProgress.event === 'finished'}
								class:failed={backupProgress.event === 'failed'}
							>
								<span></span>
								<div>
									<strong>{progressLabel(backupProgress.phase)}</strong>
									{#if advanced}<small>{backupProgress.message}</small>{/if}
								</div>
							</div>
						{/if}
					</div>
				</article>

				<div class="metric-grid">
					<article class="metric">
						<div class="metric-icon"><DatabaseBackup size={19} /></div>
						<div>
							<span>{t('lastOffHostBackup')}</span>
							<strong>{dashboard.lastBackup}</strong>
							<small><Check size={13} /> {t('verified')}</small>
						</div>
					</article>
					<article class="metric">
						<div class="metric-icon"><ShieldCheck size={19} /></div>
						<div>
							<span>{t('lastVerification')}</span>
							<strong>{dashboard.lastVerify}</strong>
							<small><Check size={13} /> SHA-256</small>
						</div>
					</article>
					<article class="metric">
						<div class="metric-icon"><Clock3 size={19} /></div>
						<div>
							<span>{t('nextRun')}</span>
							<strong>{dashboard.nextRun}</strong>
							<small>systemd · Persistent</small>
						</div>
					</article>
					<article class="metric">
						<div class="metric-icon"><Network size={19} /></div>
						<div>
							<span>{t('destination')}</span>
							<strong>{dashboard.destinationName}</strong>
							<small class:warn={!dashboard.destinationOnline}>
								{#if dashboard.destinationOnline}<Wifi size={13} />{:else}<WifiOff size={13} />{/if}
								{dashboard.destinationOnline ? t('online') : t('offline')}
							</small>
						</div>
					</article>
				</div>

				<article class="panel volumes-panel">
					<div class="panel-head">
						<div>
							<p class="eyebrow">{t('volumesProtected')}</p>
							<h3>{dashboard.engine.volumes.length}</h3>
						</div>
						<div class="chain-pill">
							<Layers3 size={15} />
							<span>{t('chainHealth')}</span>
							<strong>{dashboard.chainHealthy ? t('healthy') : t('attention')}</strong>
						</div>
					</div>
					<div class="volume-table">
						<div class="table-header">
							<span>{t('source')}</span>
							<span>{t('latestSnapshot')}</span>
							<span>{t('sourceSnapshots')}</span>
							<span>{t('remoteBackups')}</span>
							<span>{t('status')}</span>
						</div>
						{#each dashboard.engine.volumes as volume (volume.path)}
							<div class="table-row">
								<strong>{volume.path}</strong>
								<code>{volume.source.latest_snapshot ?? '—'}</code>
								<span>{volume.source.snapshot_count}</span>
								<span>{volume.targets[0]?.backup_count ?? 0}</span>
								<span class="good-status"><Check size={14} /> {t('backedUp')}</span>
							</div>
						{/each}
					</div>
				</article>

				<article class="panel activity-panel">
					<div class="panel-head">
						<div>
							<p class="eyebrow">{t('recentActivity')}</p>
							<h3>{t('activity')}</h3>
						</div>
						<button class="text-button" onclick={() => (active = 'activity')}>
							{t('viewAll')}
							<ChevronRight size={15} />
						</button>
					</div>
					<div class="activity-list">
						{#if dashboard.source === 'demo'}
							<div>
								<span class="event-dot ok"><Check size={12} /></span>
								<p>
									<strong>{t('backupCompleted')}</strong><small
										>02:13 · {dashboard.transferred} · {dashboard.duration}</small
									>
								</p>
								<span>{t('incremental')}</span>
							</div>
							<div>
								<span class="event-dot ok"><ShieldCheck size={12} /></span>
								<p>
									<strong>{t('verificationCompleted')}</strong><small
										>02:29 · 4 streams · SHA-256</small
									>
								</p>
								<span>{t('verified')}</span>
							</div>
							<div>
								<span class="event-dot neutral"><Clock3 size={12} /></span>
								<p>
									<strong>{t('scheduleCreated')}</strong><small>Yesterday · systemd timer</small>
								</p>
								<span>{t('everyDay')}</span>
							</div>
						{:else}
							<p class="empty-state">{t('noActivityYet')}</p>
						{/if}
					</div>
				</article>
			</section>
		{:else if active === 'protection'}
			<section class="content-stack">
				<div class="page-intro">
					<div class="intro-icon"><ShieldCheck size={24} /></div>
					<div>
						<h2>{t('protectionTitle')}</h2>
						<p>{t('protectionIntro')}</p>
					</div>
				</div>

				{#if harborConfig}
					<ProtectionEditor
						bind:config={harborConfig}
						{advanced}
						{locale}
						onApplied={async () => {
							dashboard = await loadDashboardStatus();
							harborConfig = await loadHarborConfiguration();
						}}
					/>
				{:else}
					<article class="callout danger">
						<CircleAlert size={21} />
						<div>
							<strong>{t('systemProblem')}</strong>
							<p>{configWarning || t('noLiveProfile')}</p>
						</div>
					</article>
				{/if}

				<div class="two-column">
					<article class="callout">
						<Network size={21} />
						<div>
							<strong>{t('mountGuard')}</strong>
							<p>{t('mountGuardDesc')}</p>
						</div>
					</article>
					<article class="callout">
						<LifeBuoy size={21} />
						<div>
							<strong>{t('recoveryKit')}</strong>
							<p>{t('recoveryKitDesc')}</p>
						</div>
					</article>
				</div>
			</section>
		{:else if active === 'timeline'}
			<section class="content-stack">
				<div class="page-intro">
					<div class="intro-icon"><History size={24} /></div>
					<div>
						<h2>{t('timelineTitle')}</h2>
						<p>{t('timelineIntro')}</p>
					</div>
				</div>
				<article class="panel timeline">
					{#if dashboard.source === 'demo'}
						{#each timelineRows as row, index (row.name)}
							<div class="timeline-row">
								<div class="timeline-time">{row.time}<span></span></div>
								<div class="timeline-main">
									<div><strong>{row.source}</strong><code>{row.name}</code></div>
									<div class="badges">
										{#each row.tags as tag (tag)}
											<span
												class:good={tag === 'backedUp' ||
													tag === 'verified' ||
													tag === 'restoreTested'}>{t(tag)}</span
											>
										{/each}
									</div>
								</div>
								<div class="timeline-chain">
									{index === timelineRows.length - 1 ? t('full') : t('incremental')}
								</div>
							</div>
						{/each}
					{:else}
						<p class="empty-state">{t('noActivityYet')}</p>
					{/if}
				</article>
			</section>
		{:else if active === 'recover'}
			<section class="content-stack">
				<div class="page-intro">
					<div class="intro-icon"><LifeBuoy size={24} /></div>
					<div>
						<h2>{t('recoveryTitle')}</h2>
						<p>{t('recoveryIntro')}</p>
					</div>
				</div>
				<div class="choice-grid">
					<article class="choice">
						<ArchiveRestore size={25} />
						<h3>{t('recoverFile')}</h3>
						<p>{t('stagedRestoreDesc')}</p>
						<ArrowRight size={18} />
					</article>
					<article class="choice">
						<Layers3 size={25} />
						<h3>{t('recoverSubvolume')}</h3>
						<p>{t('stagedRestoreDesc')}</p>
						<ArrowRight size={18} />
					</article>
					<article class="choice emphasis">
						<MonitorCog size={25} />
						<h3>{t('recoverSystem')}</h3>
						<p>{t('requiresRescue')}</p>
						<ArrowRight size={18} />
					</article>
				</div>
				{#if harborConfig}
					<RecoveryEditor
						config={harborConfig}
						profileId={dashboard.profileId}
						{advanced}
						{locale}
					/>
				{:else}
					<article class="callout large">
						<CircleAlert size={22} />
						<div>
							<strong>{t('systemProblem')}</strong>
							<p>{configWarning || t('noLiveProfile')}</p>
						</div>
					</article>
				{/if}
			</section>
		{:else if active === 'replicate'}
			<section class="content-stack">
				<div class="page-intro">
					<div class="intro-icon"><Copy size={24} /></div>
					<div>
						<h2>{t('replicaTitle')}</h2>
						<p>{t('replicaIntro')}</p>
					</div>
				</div>
				<div class="choice-grid">
					<article class="choice">
						<Laptop size={25} />
						<h3>{t('bareMetal')}</h3>
						<p>Userspace + boot reconciliation</p>
						<ArrowRight size={18} />
					</article>
					<article class="choice">
						<Server size={25} />
						<h3>{t('virtualMachine')}</h3>
						<p>Userspace + virtual hardware reconciliation</p>
						<ArrowRight size={18} />
					</article>
					<article class="choice emphasis">
						<Box size={25} />
						<h3>{t('lxcContainer')}</h3>
						<p>{t('lxcNote')}</p>
						<ArrowRight size={18} />
					</article>
				</div>
				<article class="callout large">
					<Layers3 size={22} />
					<div>
						<strong>{t('btrfsStaging')}</strong>
						<p>{t('btrfsStagingDesc')}</p>
					</div>
				</article>
				<ReplicaEditor {advanced} {locale} />
			</section>
		{:else if active === 'destinations'}
			<section class="content-stack">
				<div class="page-intro">
					<div class="intro-icon"><HardDrive size={24} /></div>
					<div>
						<h2>{t('destinationTitle')}</h2>
						<p>{t('anyFilesystem')}</p>
					</div>
					<button class="primary compact" onclick={() => (active = 'protection')}
						>+ {t('addDestination')}</button
					>
				</div>
				{#if harborConfig?.destinations.some((destination) => destination.path.trim())}
					{#each harborConfig.destinations.filter( (destination) => destination.path.trim() ) as destination (destination.id)}
						<article class="panel destination-card">
							<div class="destination-symbol">
								{#if destination.kind === 'nfs' || destination.kind === 'smb'}
									<Network size={22} />
								{:else}
									<HardDrive size={22} />
								{/if}
							</div>
							<div class="destination-copy">
								<strong>{destination.name}</strong>
								<span>
									{destination.kind.toUpperCase()}
									{#if destination.expected_mount_source}
										· {destination.expected_mount_source}
									{/if}
								</span>
								<code>{destination.path}</code>
							</div>
							<div class="destination-meta">
								<strong>{destination.compression}</strong>
							</div>
						</article>
					{/each}
				{:else}
					<article class="panel empty-panel">
						<HardDrive size={22} />
						<p>{t('noDestinations')}</p>
						<button class="secondary" onclick={() => (active = 'protection')}>
							{t('configureProtection')}
						</button>
					</article>
				{/if}
				<div class="transport-grid">
					{#each [[t('nfs'), 'raw://'], [t('smb'), 'raw://'], [t('localDisk'), 'raw://'], [t('ssh'), 'ssh://']] as transport (transport[0])}
						<div>
							<HardDrive size={18} /><strong>{transport[0]}</strong><code>{transport[1]}</code>
						</div>
					{/each}
				</div>
				<article class="callout">
					<CircleAlert size={20} />
					<div>
						<strong>{t('noFallback')}</strong>
						<p>{t('mountGuardDesc')}</p>
					</div>
				</article>
			</section>
		{:else if active === 'activity'}
			<section class="content-stack">
				<div class="page-intro">
					<div class="intro-icon"><Activity size={24} /></div>
					<div>
						<h2>{t('activityTitle')}</h2>
						<p>{t('eventLog')} · JSONL</p>
					</div>
				</div>
				<article class="panel log-panel">
					{#if dashboard.source === 'demo'}
						{#each [['02:29:18', 'verify', 'completed', '4 streams · SHA-256'], ['02:13:44', 'backup', 'completed', '1.8 GiB · 3m 42s'], ['02:10:02', 'snapshot', 'completed', '@ · @home · @root · @srv'], ['Yesterday', 'schedule', 'created', 'OnCalendar=*-*-* 02:00']] as event (event[0] + event[1])}
							<div class="log-row">
								<code>{event[0]}</code><strong>{event[1]}</strong><span class="badge good"
									>{event[2]}</span
								><span>{event[3]}</span>
							</div>
						{/each}
					{:else}
						<p class="empty-state">{t('noActivityYet')}</p>
					{/if}
				</article>
			</section>
		{:else}
			<section class="content-stack">
				<div class="page-intro">
					<div class="intro-icon"><Settings size={24} /></div>
					<div>
						<h2>{t('settingsTitle')}</h2>
						<p>{t('footerNative')}</p>
					</div>
				</div>
				<div class="two-column">
					<article class="panel">
						<div class="panel-head">
							<div>
								<p class="eyebrow">{t('interface')}</p>
								<h3>{t('appName')}</h3>
							</div>
						</div>
						<div class="setting-row">
							<span>{t('language')}</span><strong>{localeLabels[locale]}</strong>
						</div>
						<div class="setting-row">
							<span>{t('theme')}</span><strong>{dark ? 'Dark' : 'Light'}</strong>
						</div>
						<div class="setting-row">
							<span>Mode</span><strong>{advanced ? t('advanced') : t('simple')}</strong>
						</div>
					</article>
					<article class="panel">
						<div class="panel-head">
							<div>
								<p class="eyebrow">{t('engine')}</p>
								<h3>{t('upstreamEngine')}</h3>
							</div>
							<TerminalSquare size={19} />
						</div>
						<p class="body-copy">{t('inheritedEngine')}</p>
						<div class="setting-row">
							<span>{t('backgroundAgent')}</span><strong>{t('systemdManaged')}</strong>
						</div>
						<div class="setting-row"><span>Protocol</span><strong>{t('footerEngine')}</strong></div>
					</article>
				</div>
				<article class="callout large">
					<ShieldCheck size={22} />
					<div>
						<strong>{t('safety')}</strong>
						<p>{t('safetyText')}</p>
					</div>
				</article>
			</section>
		{/if}

		<footer>
			<span><Anchor size={13} /> {t('footerNative')}</span>
			<span>{t('footerEngine')}</span>
		</footer>
	</main>
</div>
